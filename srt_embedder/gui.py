"""pywebview Desktop GUI host and Python-JS bridge for SRT Embedder."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import webbrowser
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from . import __app_name__, __version__
from .bootstrap import bootstrap_tool, check_for_updates
from .engine import (
    VIDEO_EXTENSIONS,
    audit_library,
    check_sync,
    detect_installed_fonts,
    embed_subtitles,
    extract_subtitle_stream,
    find_companion_subtitles,
    probe_video,
    scan_batch_directory,
)
from .models import EmbedOptions, HardsubStyle, SubtitleInput
from .profiles import (
    delete_custom_profile,
    load_config,
    save_config,
    save_custom_profile,
    set_active_profile,
)
from .shell_ext import install_shell_menu, is_shell_installed, uninstall_shell_menu
from .subtitle import (
    SUBTITLE_EXTENSIONS,
    extract_subtitles_from_zip,
    get_preview_cues,
    inspect_srt,
    merge_dual_subtitles,
    parse_offset_ms,
    parse_speed_factor,
)
from .tools import detect_hw_encoders, discover_tools
from .tray import (
    install_watch_startup,
    is_watch_startup_installed,
    notify_desktop,
    run_watch_tray,
    uninstall_watch_startup,
)
from .undo import delete_backup, list_undo_records, perform_undo, purge_all_backups
from .watcher import FolderWatcher, _cleanup_consumed_subs


def _resource_root() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "srt_embedder"
    return Path(__file__).resolve().parent


class BridgeApi:
    """Exposed to JavaScript via window.pywebview.api."""

    def __init__(self) -> None:
        self._window = None
        self._cancel_event: Optional[threading.Event] = None
        self._active_lock = threading.Lock()
        self._running = False
        self._watcher: Optional[FolderWatcher] = None
        self._tray_lock = threading.Lock()
        self._tray_icon = None
        self._force_exit = False

    def attach_window(self, window) -> None:
        self._window = window

    def _emit(self, fn_name: str, payload: Any) -> None:
        if not self._window:
            return
        try:
            serialized = json.dumps(payload, ensure_ascii=False)
            self._window.evaluate_js(f"window.{fn_name} && window.{fn_name}({serialized})")
        except Exception:
            pass

    def get_environment(self) -> dict[str, Any]:
        tools = discover_tools()
        hw = detect_hw_encoders(tools["ffmpeg"].path) if tools["ffmpeg"].available else ["libx264"]
        undos = list_undo_records()
        return {
            "appName": __app_name__,
            "version": __version__,
            "platform": os.name,
            "frozen": bool(getattr(sys, "frozen", False)),
            "shellInstalled": is_shell_installed(),
            "watchStartup": is_watch_startup_installed(),
            "hwEncoders": hw,
            "installedFonts": detect_installed_fonts(),
            "watching": bool(self._watcher and self._watcher.is_running),
            "tools": {k: asdict(v) for k, v in tools.items()},
            "undos": [asdict(u) for u in undos],
            "totalBackupBytes": sum(u.backup_size_bytes for u in undos),
            "config": load_config(),
        }

    def set_profile(self, name: str) -> dict[str, Any]:
        cfg = set_active_profile(name)
        return {"success": True, "config": cfg}

    def save_profile(self, name: str, data: dict[str, Any]) -> dict[str, Any]:
        try:
            slug, cfg = save_custom_profile(name, data, activate=True)
            return {"success": True, "slug": slug, "config": cfg}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def delete_profile(self, name: str) -> dict[str, Any]:
        ok, msg, cfg = delete_custom_profile(name)
        return {"success": ok, "message": msg, "config": cfg}

    def update_automation_prefs(self, patch: dict[str, Any]) -> dict[str, Any]:
        cfg = load_config()
        for key in ("express_drop", "auto_discover_companions"):
            if key in patch:
                cfg[key] = bool(patch[key])
        if "ui_lang" in patch and patch["ui_lang"] in ("en", "ar", "es", "fr"):
            cfg["ui_lang"] = patch["ui_lang"]
        if isinstance(patch.get("watch"), dict):
            cfg["watch"].update(patch["watch"])
        return {"success": True, "config": save_config(cfg)}

    def check_updates(self) -> dict[str, Any]:
        return check_for_updates()

    def toggle_watch_startup(self, enable: bool, directory: str = "", recursive: bool = False, profile: str = "") -> dict[str, Any]:
        ok, msg = (
            install_watch_startup(directory, recursive=recursive, profile=profile)
            if enable
            else uninstall_watch_startup()
        )
        return {"success": ok, "message": msg, "watchStartup": is_watch_startup_installed()}

    def _stop_tray_icon(self) -> None:
        with self._tray_lock:
            ic = self._tray_icon
            self._tray_icon = None
        if ic is not None:
            try:
                ic.stop()
            except Exception:
                pass

    def send_watch_to_tray(self) -> dict[str, Any]:
        if not self._watcher or not self._watcher.is_running:
            return {"success": False, "error": "Start Watch Daemon first."}
        with self._tray_lock:
            if self._tray_icon is not None:
                if self._window:
                    try:
                        self._window.hide()
                    except Exception:
                        pass
                return {"success": True}

        if self._window:
            try:
                self._window.hide()
            except Exception:
                pass

        def _on_ready(icon) -> None:
            with self._tray_lock:
                self._tray_icon = icon

        def _restore() -> None:
            with self._tray_lock:
                self._tray_icon = None
            if self._window:
                try:
                    self._window.show()
                except Exception:
                    pass

        def _exit() -> None:
            self._force_exit = True
            with self._tray_lock:
                self._tray_icon = None
            if self._window:
                try:
                    self._window.destroy()
                except Exception:
                    pass

        threading.Thread(
            target=lambda: run_watch_tray(
                self._watcher,
                on_open_gui=_restore,
                on_icon_ready=_on_ready,
                on_exit=_exit,
            ),
            daemon=True,
        ).start()
        return {"success": True}

    def get_cues(
        self,
        path: str,
        offset_ms: int = 0,
        speed_factor: float = 1.0,
        strip_html_tags: bool = False,
        clean_sdh: bool = False,
        clean_ads: bool = False,
    ) -> dict[str, Any]:
        try:
            cues = get_preview_cues(
                path,
                offset_ms=parse_offset_ms(offset_ms),
                speed_factor=parse_speed_factor(speed_factor),
                strip_tags=bool(strip_html_tags),
                clean_sdh=bool(clean_sdh),
                clean_ads=bool(clean_ads),
            )
            return {"success": True, "cues": cues}
        except Exception as exc:
            return {"success": False, "error": str(exc), "cues": []}

    def install_tool(self, tool_name: str) -> dict[str, Any]:
        def _run() -> None:
            ok, msg = bootstrap_tool(
                tool_name,
                progress_cb=lambda pct, status: self._emit("__onBootstrapProgress", {"tool": tool_name, "percent": round(pct, 1), "status": status}),
            )
            env = self.get_environment()
            self._emit("__onBootstrapDone", {"tool": tool_name, "success": ok, "message": msg, "env": env})

        threading.Thread(target=_run, daemon=True).start()
        return {"started": True}

    def extract_stream(self, video_path: str, stream_index: int) -> dict[str, Any]:
        ok, msg, out_path = extract_subtitle_stream(video_path, int(stream_index))
        return {"success": ok, "message": msg, "output_path": out_path}

    def merge_bilingual(
        self,
        bottom_path: str,
        top_path: str,
        bottom_offset_ms: int = 0,
        top_offset_ms: int = 0,
    ) -> dict[str, Any]:
        try:
            out_path, count = merge_dual_subtitles(
                bottom_path,
                top_path,
                bottom_offset_ms=int(bottom_offset_ms),
                top_offset_ms=int(top_offset_ms),
            )
            rep = inspect_srt(out_path)
            d = asdict(rep)
            d.pop("normalized_content", None)
            return {"success": True, "output_path": out_path, "events": count, "subtitle": d}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def start_watch(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._watcher and self._watcher.is_running:
            self._watcher.stop()
        folder = payload.get("directory", "")
        if not folder or not Path(folder).is_dir():
            return {"success": False, "error": "Select a valid watch folder."}

        notify_toast = bool(payload.get("notify_toast", True))
        cfg = load_config()
        cfg["watch"] = {
            "directory": folder,
            "recursive": bool(payload.get("recursive", False)),
            "auto_sync": bool(payload.get("auto_sync", False)),
            "clean_sdh": bool(payload.get("clean_sdh", False)),
            "clean_ads": bool(payload.get("clean_ads", False)),
            "sub_cleanup": payload.get("sub_cleanup", "keep"),
            "notify_toast": notify_toast,
        }
        save_config(cfg)

        def _on_ev(ev: dict[str, Any]) -> None:
            self._emit("__onWatchEvent", ev)
            if notify_toast and ev.get("kind") in ("success", "error"):
                notify_desktop("SRT Embedder Watch", ev.get("message", ""))

        self._watcher = FolderWatcher(
            directory=folder,
            recursive=bool(payload.get("recursive", False)),
            mode=payload.get("mode", "auto"),
            existing_subs=payload.get("existing_subs", "keep"),
            replace_original=bool(payload.get("replace_original", False)),
            default_lang=payload.get("default_lang", "auto"),
            auto_sync=bool(payload.get("auto_sync", False)),
            clean_sdh=bool(payload.get("clean_sdh", False)),
            clean_ads=bool(payload.get("clean_ads", False)),
            sub_cleanup=payload.get("sub_cleanup", "keep"),
            on_event=_on_ev,
        )
        self._watcher.start()
        return {"success": True, "watching": True}

    def stop_watch(self) -> dict[str, Any]:
        self._stop_tray_icon()
        if self._watcher:
            self._watcher.stop()
        return {"success": True, "watching": False}

    def toggle_shell_menu(self, enable: bool) -> dict[str, Any]:
        ok, msg = install_shell_menu() if enable else uninstall_shell_menu()
        return {
            "success": ok,
            "message": msg,
            "shellInstalled": is_shell_installed(),
        }

    def open_url(self, url: str) -> bool:
        if url.startswith(("https://", "http://")):
            webbrowser.open(url)
            return True
        return False

    def pick_videos(self) -> list[str]:
        import webview

        if not self._window:
            return []
        res = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("Video Files (*.mkv;*.mp4;*.m4v;*.mov;*.avi;*.webm;*.ts;*.flv)", "All Files (*.*)"),
        )
        return list(res) if res else []

    def pick_subtitles(self) -> list[str]:
        import webview

        if not self._window:
            return []
        res = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=True,
            file_types=("Subtitle Files (*.srt;*.vtt;*.ass;*.ssa;*.ttml;*.dfxp;*.sub;*.zip)", "All Files (*.*)"),
        )
        return list(res) if res else []

    def pick_folder(self) -> Optional[str]:
        import webview

        if not self._window:
            return None
        res = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        return res[0] if res else None

    def pick_save_path(self, current_video: str, target_ext: str = ".mkv") -> Optional[str]:
        import webview

        if not self._window:
            return None
        vp = Path(current_video) if current_video else Path.home()
        suggested = f"{vp.stem} [subtitled]{target_ext}" if current_video else f"output{target_ext}"
        res = self._window.create_file_dialog(
            webview.SAVE_DIALOG,
            directory=str(vp.parent) if vp.parent.exists() else "",
            save_filename=suggested,
        )
        if isinstance(res, (list, tuple)):
            return res[0] if res else None
        return res

    def inspect_dropped_paths(self, paths: list[str]) -> dict[str, Any]:
        videos = []
        subtitles = []
        folders = []
        errors = []

        expanded_paths: list[str] = []
        for raw in paths:
            p = Path(raw)
            if p.is_file() and p.suffix.lower() == ".zip":
                try:
                    extracted = extract_subtitles_from_zip(str(p))
                    if not extracted:
                        errors.append(f"{p.name}: No subtitle files (.srt/.vtt/.ass) inside zip.")
                    expanded_paths.extend(extracted)
                except Exception as exc:
                    errors.append(f"{p.name}: {exc}")
            else:
                expanded_paths.append(raw)

        for raw in expanded_paths:
            p = Path(raw)
            if p.is_dir():
                folders.append(str(p))
            elif p.is_file():
                ext = p.suffix.lower()
                if ext in SUBTITLE_EXTENSIONS:
                    try:
                        rep = inspect_srt(str(p))
                        d = asdict(rep)
                        d.pop("normalized_content", None)
                        subtitles.append(d)
                    except Exception as exc:
                        errors.append(f"{p.name}: {exc}")
                elif ext in VIDEO_EXTENSIONS:
                    try:
                        info = probe_video(str(p))
                        videos.append(asdict(info))
                    except Exception as exc:
                        errors.append(f"{p.name}: {exc}")
                else:
                    errors.append(f"Unsupported file type: {p.name}")

        auto_discovered = 0
        cfg = load_config()
        if videos and not subtitles and cfg.get("auto_discover_companions", True):
            seen_sub_paths: set[str] = set()
            for v in videos:
                for comp in find_companion_subtitles(v["path"]):
                    norm = str(Path(comp).resolve()).lower()
                    if norm in seen_sub_paths:
                        continue
                    seen_sub_paths.add(norm)
                    try:
                        rep = inspect_srt(comp)
                        d = asdict(rep)
                        d.pop("normalized_content", None)
                        d["auto_discovered"] = True
                        subtitles.append(d)
                        auto_discovered += 1
                    except Exception:
                        pass

        return {
            "videos": videos,
            "subtitles": subtitles,
            "folders": folders,
            "errors": errors,
            "auto_discovered": auto_discovered,
        }

    def audit_folder(self, folder_path: str, target_lang: str = "ara", recursive: bool = True) -> dict[str, Any]:
        try:
            rep = audit_library(folder_path, target_lang=target_lang, recursive=recursive)
            return {"ok": True, **asdict(rep)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def scan_folder(self, folder_path: str, recursive: bool = False) -> dict[str, Any]:
        try:
            pairs, unmatched_videos, unmatched_subs = scan_batch_directory(folder_path, recursive=recursive)
            enriched = []
            for pair in pairs:
                subs_info = []
                for s in pair.srt_paths:
                    rep = inspect_srt(s)
                    subs_info.append({
                        "path": s,
                        "filename": rep.filename,
                        "lang_code": rep.detected_lang_code,
                        "lang_name": rep.detected_lang_name,
                        "cue_count": rep.cue_count,
                        "encoding": rep.encoding_detected,
                    })
                enriched.append({
                    "video_path": pair.video_path,
                    "video_name": Path(pair.video_path).name,
                    "confidence": pair.confidence,
                    "subtitles": subs_info,
                })
            return {
                "ok": True,
                "folder": folder_path,
                "pairs": enriched,
                "unmatched_videos": [Path(v).name for v in unmatched_videos],
                "unmatched_subs": [Path(s).name for s in unmatched_subs],
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "pairs": [], "unmatched_videos": [], "unmatched_subs": []}

    def run_sync_check(self, video_path: str, srt_path: str) -> dict[str, Any]:
        try:
            report = check_sync(video_path, srt_path)
            return asdict(report)
        except Exception as exc:
            return {"comparable": False, "reason": str(exc)}

    def start_embed(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._active_lock:
            if self._running:
                return {"accepted": False, "error": "A job is already in progress."}
            self._running = True
            self._cancel_event = threading.Event()

        def _worker() -> None:
            try:
                subs = [
                    SubtitleInput(
                        path=s["path"],
                        lang_code=s.get("lang_code", "auto"),
                        title=s.get("title", ""),
                        is_default=bool(s.get("is_default", False)),
                        is_forced=bool(s.get("is_forced", False)),
                        offset_ms=parse_offset_ms(s.get("offset_ms", 0)),
                        speed_factor=parse_speed_factor(s.get("speed_factor", 1.0)),
                        strip_html_tags=bool(s.get("strip_html_tags", False)),
                        clean_sdh=bool(s.get("clean_sdh", False)),
                        clean_ads=bool(s.get("clean_ads", False)),
                        fix_rtl_bidi=bool(s.get("fix_rtl_bidi", False)),
                    )
                    for s in payload.get("subtitles", [])
                ]

                hs = payload.get("hardsub_style") or {}
                style_obj = HardsubStyle(
                    font_name=str(hs.get("font_name", "Arial")),
                    font_size=int(hs.get("font_size", 24)),
                    primary_color=str(hs.get("primary_color", "#FFFFFF")),
                    outline_color=str(hs.get("outline_color", "#000000")),
                    outline_width=int(hs.get("outline_width", 2)),
                    margin_v=int(hs.get("margin_v", 28)),
                    encoder=str(hs.get("encoder", "auto")),  # type: ignore[arg-type]
                )

                opts = EmbedOptions(
                    video_path=payload["video_path"],
                    subtitles=subs,
                    output_path=payload.get("output_path") or None,
                    mode=payload.get("mode", "auto"),
                    existing_subs=payload.get("existing_subs", "keep"),
                    fallback_to_mkv=bool(payload.get("fallback_to_mkv", True)),
                    replace_original=bool(payload.get("replace_original", False)),
                    create_backup=bool(payload.get("create_backup", True)),
                    dry_run=bool(payload.get("dry_run", False)),
                    auto_sync=bool(payload.get("auto_sync", False)),
                    output_template=str(payload.get("output_template", "default")),
                    prefer_sub_lang_when_foreign_audio=str(payload.get("prefer_sub_lang_when_foreign_audio", "")),
                    crf=int(payload.get("crf", 18)),
                    preset=str(payload.get("preset", "medium")),
                    hardsub_style=style_obj,
                )

                def _on_prog(pct: float, phase: str, detail: str) -> None:
                    self._emit("__onMuxProgress", {
                        "percent": round(pct, 1),
                        "phase": phase,
                        "detail": detail,
                    })

                result = embed_subtitles(opts, progress_cb=_on_prog, cancel_event=self._cancel_event)
                res_dict = asdict(result)
                res_dict["undos"] = [asdict(u) for u in list_undo_records()]
                self._emit("__onMuxComplete", res_dict)
            except Exception as exc:
                self._emit("__onMuxComplete", {
                    "success": False,
                    "error": str(exc),
                    "video_path": payload.get("video_path", ""),
                    "output_path": "",
                    "engine_used": "none",
                })
            finally:
                with self._active_lock:
                    self._running = False
                    self._cancel_event = None

        threading.Thread(target=_worker, daemon=True).start()
        return {"accepted": True}

    def start_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._active_lock:
            if self._running:
                return {"accepted": False, "error": "A job is already in progress."}
            self._running = True
            self._cancel_event = threading.Event()

        jobs = payload.get("jobs", [])
        mode = payload.get("mode", "auto")
        existing_subs = payload.get("existing_subs", "keep")
        replace_original = bool(payload.get("replace_original", False))
        fallback_to_mkv = bool(payload.get("fallback_to_mkv", True))
        auto_sync = bool(payload.get("auto_sync", False))
        clean_sdh = bool(payload.get("clean_sdh", False))
        clean_ads = bool(payload.get("clean_ads", False))
        strip_tags = bool(payload.get("strip_html_tags", False))
        sub_cleanup = str(payload.get("sub_cleanup", "keep"))
        batch_dir = payload.get("directory") or ""

        def _worker() -> None:
            results = []
            try:
                total = max(1, len(jobs))
                for idx, job in enumerate(jobs):
                    if self._cancel_event and self._cancel_event.is_set():
                        break
                    vpath = job["video_path"]
                    raw_subs = job.get("subtitles") or []
                    subs = [
                        SubtitleInput(
                            path=s["path"] if isinstance(s, dict) else str(s),
                            lang_code=s.get("lang_code", "auto") if isinstance(s, dict) else "auto",
                            title=s.get("title", "") if isinstance(s, dict) else "",
                            is_default=(s_idx == 0),
                            strip_html_tags=strip_tags,
                            clean_sdh=clean_sdh,
                            clean_ads=clean_ads,
                        )
                        for s_idx, s in enumerate(raw_subs)
                    ]

                    def _on_prog(pct: float, phase: str, detail: str, j_idx=idx) -> None:
                        overall = ((j_idx + pct / 100.0) / total) * 100.0
                        self._emit("__onBatchProgress", {
                            "jobIndex": j_idx,
                            "jobPercent": round(pct, 1),
                            "overallPercent": round(overall, 1),
                            "phase": phase,
                            "detail": detail,
                        })

                    res = embed_subtitles(
                        EmbedOptions(
                            video_path=vpath,
                            subtitles=subs,
                            mode=mode,
                            existing_subs=existing_subs,
                            fallback_to_mkv=fallback_to_mkv,
                            replace_original=replace_original,
                            create_backup=True,
                            auto_sync=auto_sync,
                        ),
                        progress_cb=_on_prog,
                        cancel_event=self._cancel_event,
                    )
                    if res.success and sub_cleanup != "keep" and batch_dir:
                        _cleanup_consumed_subs([s.path for s in subs], Path(batch_dir), sub_cleanup)  # type: ignore[arg-type]
                    res_dict = asdict(res)
                    res_dict["jobIndex"] = idx
                    results.append(res_dict)
                    self._emit("__onBatchItemDone", res_dict)
            finally:
                self._emit("__onBatchComplete", {
                    "results": results,
                    "undos": [asdict(u) for u in list_undo_records()],
                })
                with self._active_lock:
                    self._running = False
                    self._cancel_event = None

        threading.Thread(target=_worker, daemon=True).start()
        return {"accepted": True}

    def cancel_embed(self) -> bool:
        if self._cancel_event:
            self._cancel_event.set()
            return True
        return False

    def undo_replace(self, tx_id: Optional[str] = None) -> dict[str, Any]:
        ok, msg, rec = perform_undo(tx_id)
        return {
            "success": ok,
            "message": msg,
            "record": asdict(rec) if rec else None,
            "undos": [asdict(u) for u in list_undo_records()],
        }

    def delete_undo_backup(self, tx_id: str) -> dict[str, Any]:
        ok, msg = delete_backup(tx_id)
        return {
            "success": ok,
            "message": msg,
            "undos": [asdict(u) for u in list_undo_records()],
        }

    def purge_undo_backups(self) -> dict[str, Any]:
        removed, freed = purge_all_backups()
        return {
            "success": True,
            "removed": removed,
            "freed_bytes": freed,
            "undos": [],
        }

    def reveal_in_explorer(self, path: str) -> bool:
        p = Path(path)
        target = p if p.exists() else p.parent
        if not target.exists():
            return False
        if os.name == "nt":
            if p.is_file():
                subprocess.Popen(["explorer", "/select,", str(p)])
            else:
                subprocess.Popen(["explorer", str(target)])
            return True
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(p)] if p.is_file() else ["open", str(target)])
            return True
        subprocess.Popen(["xdg-open", str(p.parent if p.is_file() else target)])
        return True

    def open_media(self, path: str) -> bool:
        p = Path(path)
        if not p.is_file():
            return False
        if os.name == "nt":
            os.startfile(str(p))  # type: ignore[attr-defined]
            return True
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, str(p)])
        return True


def launch_gui(initial_paths: Optional[list[str]] = None) -> None:
    try:
        import webview
        from webview.dom import DOMEventHandler
    except ImportError as exc:
        print(
            'Desktop GUI requires optional dependencies. Install with: pip install "srt-embedder[gui]"',
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    root = _resource_root()
    ui_index = root / "ui" / "index.html"
    icon_file = root / "assets" / "icon.ico"
    api = BridgeApi()

    window = webview.create_window(
        title=f"{__app_name__} · Studio Muxer",
        url=str(ui_index),
        js_api=api,
        width=1140,
        height=770,
        min_size=(880, 620),
        background_color="#0B0D10",
        text_select=False,
    )
    api.attach_window(window)

    def _on_loaded() -> None:
        def _on_dragover(_e):
            pass

        def _on_drop(e):
            files = (e.get("dataTransfer") or {}).get("files") or []
            full_paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
            if full_paths:
                api._emit("__onNativeDrop", full_paths)

        try:
            window.dom.document.events.dragover += DOMEventHandler(_on_dragover, True, True)
            window.dom.document.events.drop += DOMEventHandler(_on_drop, True, True)
        except Exception:
            pass

        if initial_paths:
            valid = [str(Path(p).resolve()) for p in initial_paths if Path(p).exists()]
            if valid:
                threading.Timer(0.25, lambda: api._emit("__onNativeDrop", valid)).start()

    def _on_closing() -> bool:
        if not api._force_exit and api._watcher and api._watcher.is_running:
            api.send_watch_to_tray()
            return False
        api._stop_tray_icon()
        if api._watcher and api._watcher.is_running:
            api._watcher.stop()
        return True

    window.events.closing += _on_closing
    window.events.loaded += _on_loaded
    start_kwargs: dict[str, Any] = {"debug": False}
    if icon_file.exists():
        start_kwargs["icon"] = str(icon_file)
    webview.start(**start_kwargs)
