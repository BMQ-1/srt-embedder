"""Folder Watch Daemon with persistent manifest, auto-sync, and post-embed cleanup."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

from .engine import embed_subtitles, scan_batch_directory
from .models import EmbedOptions, ExistingSubsPolicy, MuxMode, SubCleanupPolicy, SubtitleInput

_SKIP_CLEANUP_PARTS = {"srt_zip_cache", "_archived_subs"}


@dataclass
class WatchEvent:
    timestamp: str
    kind: str  # info | paired | success | error
    message: str
    video_path: str = ""
    output_path: str = ""
    engine: str = ""


EventCallback = Callable[[dict], None]


def default_manifest_path() -> Path:
    base = Path(os.environ.get("APPDATA") or (Path.home() / ".config"))
    root = base / "SRT Embedder"
    root.mkdir(parents=True, exist_ok=True)
    return root / "watch_manifest.json"


def _file_is_stable(path: Path, wait_sec: float = 0.8) -> bool:
    """Confirm file size is unchanged across wait_sec and file isn't locked."""
    try:
        if not path.exists():
            return False
        s1 = path.stat().st_size
        time.sleep(wait_sec)
        if not path.exists():
            return False
        s2 = path.stat().st_size
        return s1 == s2 and s2 > 0
    except OSError:
        return False


def _cleanup_consumed_subs(sub_paths: list[str], watch_root: Path, policy: SubCleanupPolicy) -> int:
    """Archive or trash original subtitle files inside watch_root after verified muxing."""
    if policy == "keep":
        return 0
    handled = 0
    archive_dir = watch_root / "_archived_subs"
    for raw in sub_paths:
        sp = Path(raw)
        if not sp.is_file():
            continue
        # Never touch temporary extracted files outside watch_root
        try:
            rel = sp.resolve().relative_to(watch_root.resolve())
        except ValueError:
            continue
        if any(part in _SKIP_CLEANUP_PARTS for part in sp.parts):
            continue

        if policy == "trash":
            try:
                from send2trash import send2trash  # type: ignore

                send2trash(str(sp))
                handled += 1
                continue
            except Exception:
                pass

        # Archive inside watch_root/_archived_subs preserving relative hierarchy
        dest = archive_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest = dest.with_name(f"{dest.stem}_{int(time.time())}{dest.suffix}")
        try:
            shutil.move(str(sp), str(dest))
            handled += 1
        except OSError:
            pass
    return handled


class FolderWatcher:
    """Watches a folder for new video + subtitle pairs and auto-embeds safely."""

    def __init__(
        self,
        directory: str,
        recursive: bool = False,
        mode: MuxMode = "auto",
        existing_subs: ExistingSubsPolicy = "keep",
        replace_original: bool = False,
        default_lang: str = "auto",
        auto_sync: bool = False,
        clean_sdh: bool = False,
        clean_ads: bool = False,
        strip_html_tags: bool = False,
        sub_cleanup: SubCleanupPolicy = "keep",
        poll_interval: float = 2.5,
        manifest_path: Optional[Path | str] = None,
        persist_manifest: bool = True,
        on_event: Optional[EventCallback] = None,
    ) -> None:
        self.directory = str(Path(directory).resolve())
        self.recursive = recursive
        self.mode = mode
        self.existing_subs = existing_subs
        self.replace_original = replace_original
        self.default_lang = default_lang
        self.auto_sync = auto_sync
        self.clean_sdh = clean_sdh
        self.clean_ads = clean_ads
        self.strip_html_tags = strip_html_tags
        self.sub_cleanup = sub_cleanup
        self.poll_interval = poll_interval
        self.persist_manifest = persist_manifest
        self._manifest_path = Path(manifest_path) if manifest_path else default_manifest_path()
        self.on_event = on_event

        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._processed: dict[str, dict] = self._load_manifest() if persist_manifest else {}

    def _load_manifest(self) -> dict[str, dict]:
        if not self._manifest_path.exists():
            return {}
        try:
            data = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _save_manifest(self) -> None:
        if not self.persist_manifest:
            return
        try:
            self._manifest_path.parent.mkdir(parents=True, exist_ok=True)
            # Cap stored history to latest 1000 entries
            if len(self._processed) > 1000:
                keys = list(self._processed.keys())[-1000:]
                self._processed = {k: self._processed[k] for k in keys}
            tmp = self._manifest_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._processed, indent=2), encoding="utf-8")
            os.replace(tmp, self._manifest_path)
        except Exception:
            pass

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop_event.is_set()

    @property
    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    def pause(self) -> None:
        self._pause_event.set()
        self._emit("info", "Watch daemon paused.")

    def resume(self) -> None:
        self._pause_event.clear()
        self._emit("info", "Watch daemon resumed.")

    def _emit(self, kind: str, message: str, video_path: str = "", output_path: str = "", engine: str = "") -> None:
        if self.on_event:
            ev = WatchEvent(
                timestamp=time.strftime("%H:%M:%S"),
                kind=kind,
                message=message,
                video_path=video_path,
                output_path=output_path,
                engine=engine,
            )
            self.on_event(asdict(ev))

    def _record_processed(self, sig: str, video_path: str, output_path: str) -> None:
        self._processed[sig] = {
            "video": video_path,
            "output": output_path,
            "ts": int(time.time()),
        }
        self._save_manifest()

    def scan_once(self) -> None:
        if self._pause_event.is_set():
            return
        try:
            pairs, _, _ = scan_batch_directory(self.directory, recursive=self.recursive)
        except Exception as exc:
            self._emit("error", f"Scan error: {exc}")
            return

        watch_root = Path(self.directory)
        for pair in pairs:
            if self._stop_event.is_set() or self._pause_event.is_set():
                return
            vp = Path(pair.video_path)
            if " [subtitled" in vp.stem:
                continue

            sub_sig = "|".join(sorted(pair.srt_paths))
            try:
                sig = f"{vp.resolve()}::{vp.stat().st_mtime_ns}::{sub_sig}"
            except OSError:
                continue
            if sig in self._processed:
                continue

            if not self.replace_original and vp.with_name(f"{vp.stem} [subtitled]{vp.suffix}").exists():
                self._record_processed(sig, str(vp), str(vp.with_name(f"{vp.stem} [subtitled]{vp.suffix}")))
                continue

            if not _file_is_stable(vp):
                continue

            self._emit("paired", f"Detected pair: {vp.name} ({len(pair.srt_paths)} sub)", video_path=str(vp))
            subs = [
                SubtitleInput(
                    path=s,
                    lang_code=self.default_lang,
                    is_default=(i == 0),
                    strip_html_tags=self.strip_html_tags,
                    clean_sdh=self.clean_sdh,
                    clean_ads=self.clean_ads,
                )
                for i, s in enumerate(pair.srt_paths)
            ]
            res = embed_subtitles(
                EmbedOptions(
                    video_path=str(vp),
                    subtitles=subs,
                    mode=self.mode,
                    existing_subs=self.existing_subs,
                    replace_original=self.replace_original,
                    create_backup=True,
                    auto_sync=self.auto_sync,
                ),
                cancel_event=self._stop_event,
            )
            if res.success:
                # Recompute video mtime if replace_original mutated the file
                try:
                    new_sig = f"{vp.resolve()}::{vp.stat().st_mtime_ns}::{sub_sig}"
                except OSError:
                    new_sig = sig
                self._record_processed(sig, str(vp), res.output_path)
                if new_sig != sig:
                    self._record_processed(new_sig, str(vp), res.output_path)

                archived = _cleanup_consumed_subs(pair.srt_paths, watch_root, self.sub_cleanup)
                extra = f" · archived {archived} sub(s)" if archived else ""
                self._emit(
                    "success",
                    f"Embedded {Path(res.output_path).name} ({res.engine_used}, {res.elapsed_sec}s{extra})",
                    video_path=str(vp),
                    output_path=res.output_path,
                    engine=res.engine_used,
                )
            else:
                self._record_processed(sig, str(vp), "")
                self._emit("error", f"Failed {vp.name}: {res.error}", video_path=str(vp))

    def _run_loop(self) -> None:
        self._emit("info", f"Watching {self.directory} (recursive={self.recursive}, cleanup={self.sub_cleanup})")
        observer = None
        wake_event = threading.Event()

        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            class _Handler(FileSystemEventHandler):
                def on_any_event(self, _event):
                    wake_event.set()

            observer = Observer()
            observer.schedule(_Handler(), self.directory, recursive=self.recursive)
            observer.start()
        except Exception:
            observer = None

        try:
            while not self._stop_event.is_set():
                self.scan_once()
                wake_event.wait(timeout=self.poll_interval)
                wake_event.clear()
        finally:
            if observer:
                try:
                    observer.stop()
                    observer.join(timeout=2)
                except Exception:
                    pass
            self._emit("info", "Watch daemon stopped.")

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        self._pause_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
