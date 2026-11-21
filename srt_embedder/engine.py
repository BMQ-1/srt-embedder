"""Core muxing engine with smart fallbacks, atomic commit, progress streaming, and batch pairing."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .models import (
    EmbedOptions,
    EmbedResult,
    HardsubStyle,
    LibraryAuditItem,
    LibraryAuditReport,
    StreamInfo,
    SubtitleInput,
    SyncReport,
    SyncSample,
    VideoInfo,
)
from .subtitle import (
    SUBTITLE_EXTENSIONS,
    compare_srt_with_reference,
    decode_subtitle_bytes,
    extract_subtitles_from_zip,
    inspect_srt,
    ms_to_srt_time,
    parse_and_repair_srt,
    resolve_language,
)
from .tools import detect_hw_encoders, discover_tools
from .undo import register_backup

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".ts", ".flv", ".wmv"}
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}

ProgressCallback = Callable[[float, str, str], None]  # (percent 0..100, phase, detail)


def _no_window_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def probe_video(video_path: str) -> VideoInfo:
    """Inspect a video file using ffprobe."""
    p = Path(video_path)
    if not p.is_file():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    tools = discover_tools()
    ffprobe = tools["ffprobe"].path
    if not ffprobe:
        # Minimal fallback without ffprobe
        return VideoInfo(
            path=str(p),
            filename=p.name,
            container=p.suffix.lstrip(".").lower(),
            size_bytes=p.stat().st_size,
            duration_sec=0.0,
        )

    cmd = [
        ffprobe,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(p),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        creationflags=_no_window_flags(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {p.name}: {proc.stderr.strip()}")

    data = json.loads(proc.stdout or "{}")
    fmt = data.get("format", {})
    duration = float(fmt.get("duration") or 0.0)
    size_bytes = int(fmt.get("size") or p.stat().st_size)

    streams: list[StreamInfo] = []
    width = 0
    height = 0
    fps = ""
    color_space = ""
    color_primaries = ""
    color_transfer = ""
    color_range = ""

    for s in data.get("streams", []):
        tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        disp = s.get("disposition") or {}
        ctype = s.get("codec_type", "data")
        cname = s.get("codec_name", "unknown")

        extra = ""
        if ctype == "video":
            width = int(s.get("width") or 0)
            height = int(s.get("height") or 0)
            if not color_space:
                color_space = s.get("color_space", "") or ""
                color_primaries = s.get("color_primaries", "") or ""
                color_transfer = s.get("color_transfer", "") or ""
                color_range = s.get("color_range", "") or ""
            r_fps = s.get("r_frame_rate", "")
            if "/" in r_fps:
                num, den = r_fps.split("/", 1)
                if den.isdigit() and int(den) > 0:
                    fps = f"{int(num) / int(den):.3f}".rstrip("0").rstrip(".")
            extra = f"{width}x{height}"
        elif ctype == "audio":
            ch = s.get("channels")
            extra = f"{ch}ch" if ch else ""

        streams.append(
            StreamInfo(
                index=int(s.get("index", 0)),
                codec_type=ctype,
                codec_name=cname,
                language=tags.get("language", "und"),
                title=tags.get("title", ""),
                is_default=bool(disp.get("default", 0)),
                is_forced=bool(disp.get("forced", 0)),
                extra=extra,
            )
        )

    return VideoInfo(
        path=str(p),
        filename=p.name,
        container=p.suffix.lstrip(".").lower(),
        size_bytes=size_bytes,
        duration_sec=duration,
        width=width,
        height=height,
        fps=fps,
        color_space=color_space,
        color_primaries=color_primaries,
        color_transfer=color_transfer,
        color_range=color_range,
        streams=streams,
    )


def _check_sync_via_audio_vad(
    video_path: str,
    srt_path: str,
    ffmpeg: str,
    info: VideoInfo,
) -> SyncReport:
    """Estimate subtitle timing against audio speech-activity windows via FFmpeg silencedetect."""
    audio_streams = info.audio_streams
    if not audio_streams:
        return SyncReport(
            comparable=False,
            reason="Video has neither an embedded text subtitle nor an audio stream to align against.",
        )

    tgt_raw = Path(srt_path).read_bytes()
    tgt_text, _, _ = decode_subtitle_bytes(tgt_raw)
    cues, _, _, _ = parse_and_repair_srt(tgt_text)
    if len(cues) < 3:
        return SyncReport(
            comparable=False,
            reason="Not enough subtitle cues to align against audio speech activity.",
        )

    scan_sec = min(600.0, info.duration_sec) if info.duration_sec > 0 else 600.0
    scan_ms = int(scan_sec * 1000)

    primary_audio = audio_streams[0]
    cmd = [
        ffmpeg,
        "-v",
        "info",
        "-t",
        f"{scan_sec:.2f}",
        "-i",
        video_path,
        "-vn",
        "-sn",
        "-af",
        "silencedetect=noise=-32dB:d=0.45",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        creationflags=_no_window_flags(),
    )
    log = (proc.stderr or "") + "\n" + (proc.stdout or "")

    # Parse silence intervals -> invert into active speech intervals (start_ms, end_ms)
    events: list[tuple[str, int]] = []
    for m in re.finditer(r"silence_(start|end):\s*([0-9]+(?:\.[0-9]+)?)", log):
        events.append((m.group(1), int(round(float(m.group(2)) * 1000))))

    speech_segs: list[tuple[int, int]] = []
    cur_speech_start: Optional[int] = 0
    for kind, ts_ms in events:
        if kind == "start":
            if cur_speech_start is not None and ts_ms - cur_speech_start >= 250:
                speech_segs.append((cur_speech_start, ts_ms))
            cur_speech_start = None
        elif kind == "end":
            cur_speech_start = ts_ms
    if cur_speech_start is not None and scan_ms - cur_speech_start >= 250:
        speech_segs.append((cur_speech_start, scan_ms))

    # If the entire clip is silent (e.g. blank synthetic audio) or continuous noise with 0 breaks
    if len(speech_segs) < 2 and not events:
        return SyncReport(
            comparable=False,
            reason="Audio track had no detectable speech pauses to align subtitle timing.",
        )
    if not speech_segs:
        return SyncReport(
            comparable=False,
            reason="Audio track appears silent within the scanned window.",
        )

    window_cues = [c for c in cues if c.start_ms < scan_ms]
    if len(window_cues) < 3:
        window_cues = cues[:30]

    def _score_shift(shift_ms: int) -> float:
        # shift_ms = offset to APPLY to cues so cue + shift aligns with speech
        total_cue_ms = 0
        overlap_ms = 0
        for c in window_cues:
            cs = max(0, c.start_ms + shift_ms)
            ce = max(cs + 100, c.end_ms + shift_ms)
            total_cue_ms += ce - cs
            for ss, se in speech_segs:
                if se <= cs:
                    continue
                if ss >= ce:
                    break
                overlap_ms += min(ce, se) - max(cs, ss)
        return overlap_ms / max(1, total_cue_ms)

    # Coarse grid (-10s..+10s step 200ms) -> fine grid (+/- 300ms step 25ms)
    best_shift = 0
    best_score = _score_shift(0)
    base_score = best_score
    for candidate in range(-10_000, 10_001, 200):
        sc = _score_shift(candidate)
        if sc > best_score:
            best_score = sc
            best_shift = candidate

    fine_center = best_shift
    for candidate in range(fine_center - 250, fine_center + 251, 25):
        sc = _score_shift(candidate)
        if sc > best_score:
            best_score = sc
            best_shift = candidate

    if best_score < 0.25:
        return SyncReport(
            comparable=False,
            reason="Audio speech activity did not match subtitle cadence confidently.",
        )

    # Report delta using same sign convention as embedded-sub:
    # diff_ms = srt_time - actual_speech_time => recommended_offset_ms = -median_diff_ms = best_shift
    median_diff = -best_shift
    if abs(median_diff) <= 150:
        verdict = "in-sync"
    elif median_diff > 150:
        verdict = "late"
    else:
        verdict = "early"

    samples: list[SyncSample] = []
    step = max(1, len(window_cues) // 6)
    for c in window_cues[::step][:6]:
        shifted = max(0, c.start_ms + best_shift)
        nearest_seg = min(speech_segs, key=lambda seg: abs(seg[0] - shifted))
        delta = c.start_ms - nearest_seg[0]
        samples.append(
            SyncSample(
                minute=max(0, int(c.start_ms / 60_000)),
                ref_time=ms_to_srt_time(nearest_seg[0]),
                ref_text=f"Audio speech onset ({primary_audio.language})",
                srt_time=ms_to_srt_time(c.start_ms),
                srt_text=" ".join(c.lines)[:44],
                diff_ms=delta if abs(delta) < 15_000 else median_diff,
            )
        )

    confidence = round(min(1.0, max(0.0, (best_score - 0.15) / 0.7)), 2)
    return SyncReport(
        comparable=True,
        method="audio-vad",
        confidence=confidence,
        ref_stream_index=primary_audio.index,
        ref_language=primary_audio.language,
        median_diff_ms=median_diff,
        min_diff_ms=median_diff,
        max_diff_ms=median_diff,
        drift_ms=0,
        verdict=verdict,
        recommended_offset_ms=best_shift if abs(best_shift) > 150 and best_score > base_score + 0.04 else 0,
        samples=samples,
    )


def check_sync(video_path: str, srt_path: str) -> SyncReport:
    """Compare an external SRT against an embedded text track, falling back to audio speech VAD."""
    tools = discover_tools()
    ffmpeg = tools["ffmpeg"].path
    if not ffmpeg:
        return SyncReport(comparable=False, reason="ffmpeg is required to inspect subtitle sync.")

    info = probe_video(video_path)
    text_subs = [s for s in info.subtitle_streams if s.codec_name.lower() in TEXT_SUB_CODECS]
    if not text_subs:
        return _check_sync_via_audio_vad(video_path, srt_path, ffmpeg, info)

    # Prefer non-SDH English track, otherwise first text subtitle
    ref = sorted(
        text_subs,
        key=lambda s: (
            0 if s.language in ("eng", "en") else 1,
            1 if "sdh" in s.title.lower() else 0,
            s.index,
        ),
    )[0]

    with tempfile.TemporaryDirectory(prefix="srt_sync_") as tmpdir:
        ref_out = Path(tmpdir) / "ref.srt"
        cmd = [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-i",
            video_path,
            "-map",
            f"0:{ref.index}",
            "-f",
            "srt",
            str(ref_out),
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=_no_window_flags(),
        )
        if proc.returncode != 0 or not ref_out.exists():
            return _check_sync_via_audio_vad(video_path, srt_path, ffmpeg, info)

        ref_text = ref_out.read_text(encoding="utf-8", errors="replace")
        tgt_raw = Path(srt_path).read_bytes()
        tgt_text, _, _ = decode_subtitle_bytes(tgt_raw)
        rep = compare_srt_with_reference(
            ref_text,
            tgt_text,
            ref_stream_index=ref.index,
            ref_language=ref.language,
        )
        rep.method = "embedded-sub"
        return rep


def extract_subtitle_stream(
    video_path: str,
    stream_index: int,
    output_path: Optional[str] = None,
) -> tuple[bool, str, str]:
    """Extract an embedded text subtitle stream to .srt or .ass."""
    tools = discover_tools()
    ffmpeg = tools["ffmpeg"].path
    if not ffmpeg:
        return False, "ffmpeg is required to extract embedded subtitles.", ""

    vp = Path(video_path).resolve()
    info = probe_video(str(vp))
    target_stream = next((s for s in info.subtitle_streams if s.index == stream_index), None)
    if not target_stream:
        return False, f"Subtitle stream #{stream_index} not found in {vp.name}.", ""

    if target_stream.codec_name.lower() not in TEXT_SUB_CODECS:
        return False, f"Stream #{stream_index} is image-based ({target_stream.codec_name}); only text subtitles can be extracted as SRT/ASS.", ""

    is_ass = target_stream.codec_name.lower() in ("ass", "ssa")
    ext = ".ass" if is_ass else ".srt"
    if output_path:
        out = Path(output_path).resolve()
    else:
        lang = target_stream.language or "und"
        out = vp.with_name(f"{vp.stem}.stream{stream_index}.{lang}{ext}")

    fmt = "ass" if is_ass else "srt"
    cmd = [ffmpeg, "-y", "-v", "error", "-i", str(vp), "-map", f"0:{stream_index}", "-f", fmt, str(out)]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        creationflags=_no_window_flags(),
    )
    if proc.returncode != 0 or not out.exists():
        return False, proc.stderr.strip() or "Extraction failed.", ""
    return True, f"Extracted stream #{stream_index} to {out.name}", str(out)


@dataclass
class BatchPair:
    video_path: str
    srt_paths: list[str]
    confidence: str  # exact | episode | fuzzy | single-pair


_EPISODE_RE = re.compile(r"(?:^|[.\-_\s\[(])(?:s(\d{1,2})[.\-_\s]?e(\d{1,3})|(\d{1,2})x(\d{1,3})|ep?(\d{1,3}))(?:[.\-_\s\])]|$)", re.IGNORECASE)
_COMPANION_SUBDIRS = ("subs", "subtitles", "sub")


def _extract_episode_key(name: str) -> Optional[tuple[Optional[int], int]]:
    """Extract (season, episode) from filenames like Show.S01E04, Show.1x04, or Ep04."""
    stem = Path(name).stem
    m = _EPISODE_RE.search(stem)
    if not m:
        return None
    if m.group(1) and m.group(2):
        return (int(m.group(1)), int(m.group(2)))
    if m.group(3) and m.group(4):
        return (int(m.group(3)), int(m.group(4)))
    if m.group(5):
        return (None, int(m.group(5)))
    return None


def _episodes_compatible(ep_a: Optional[tuple[Optional[int], int]], ep_b: Optional[tuple[Optional[int], int]]) -> bool:
    if ep_a is None or ep_b is None:
        return True
    if ep_a[1] != ep_b[1]:
        return False
    if ep_a[0] is not None and ep_b[0] is not None and ep_a[0] != ep_b[0]:
        return False
    return True


def _normalize_stem(name: str) -> set[str]:
    stem = Path(name).stem.lower()
    # Strip trailing language codes like .ar, .ara, .en, .eng
    stem = re.sub(r"[._\-\s](ar|ara|arabic|en|eng|english|es|spa|fr|fre)$", "", stem)
    tokens = set(re.findall(r"[a-z0-9]+", stem))
    noise = {"1080p", "720p", "2160p", "4k", "web", "dl", "webrip", "bluray", "x264", "x265", "h264", "h265", "hevc", "aac", "aac5", "5", "1", "yts", "bz", "bone", "rarbg"}
    cleaned = tokens - noise
    return cleaned or tokens


def find_batch_pairs(directory: str, recursive: bool = False) -> list[BatchPair]:
    """Auto-pair video files and subtitle files (.srt/.vtt/.ass/.ssa/.zip) inside a directory."""
    pairs, _, _ = scan_batch_directory(directory, recursive=recursive)
    return pairs


def scan_batch_directory(
    directory: str,
    recursive: bool = False,
) -> tuple[list[BatchPair], list[str], list[str]]:
    """Scan folder and return (pairs, unmatched_videos, unmatched_subs)."""
    root = Path(directory)
    if not root.is_dir():
        raise NotADirectoryError(f"Directory not found: {directory}")

    iterator = root.rglob("*") if recursive else root.iterdir()
    files = [
        f for f in iterator
        if f.is_file()
        and ".tmp-" not in f.name
        and ".bak-" not in f.name
        and "_archived_subs" not in f.parts
    ]

    videos = sorted([f for f in files if f.suffix.lower() in VIDEO_EXTENSIONS])
    raw_subs = sorted([f for f in files if f.suffix.lower() in SUBTITLE_EXTENSIONS])

    # Expand any .zip subtitle archives safely
    expanded_zips: list[Path] = []
    for z in sorted([f for f in files if f.suffix.lower() == ".zip"]):
        try:
            for ext_sub in extract_subtitles_from_zip(str(z)):
                expanded_zips.append(Path(ext_sub))
        except Exception:
            continue

    srts = raw_subs + expanded_zips

    if not videos or not srts:
        return [], [str(v) for v in videos], [str(s) for s in srts]

    if len(videos) == 1:
        return [BatchPair(video_path=str(videos[0]), srt_paths=[str(s) for s in srts], confidence="single-pair")], [], []

    pairs: list[BatchPair] = []
    used_srts: set[Path] = set()
    unmatched_videos: list[str] = []

    for vid in videos:
        v_stem = vid.stem.lower()
        v_ep = _extract_episode_key(vid.name)
        matched: list[str] = []
        conf = "exact"

        # Prefer subtitles in the same folder first when recursive
        same_dir_first = sorted(srts, key=lambda s: (0 if s.parent == vid.parent else 1, s.name.lower()))

        # 1. Exact stem or prefix match (e.g. Movie.mkv + Movie.ar.srt)
        for s in same_dir_first:
            if s in used_srts:
                continue
            s_stem = s.stem.lower()
            if s_stem == v_stem or s_stem.startswith(v_stem + ".") or s_stem.startswith(v_stem + "_") or s_stem.startswith(v_stem + "-"):
                matched.append(str(s))
                used_srts.add(s)

        # 2. Season/Episode key match (e.g. Show.S01E03.mkv + Show - 1x03.ar.srt)
        if not matched and v_ep is not None:
            for s in same_dir_first:
                if s in used_srts:
                    continue
                s_ep = _extract_episode_key(s.name)
                if s_ep is not None and _episodes_compatible(v_ep, s_ep) and s_ep[1] == v_ep[1]:
                    if v_ep[0] is None or s_ep[0] is None or v_ep[0] == s_ep[0]:
                        matched.append(str(s))
                        used_srts.add(s)
                        conf = "episode"

        # 3. Fuzzy token similarity fallback (refusing conflicting episode tags)
        if not matched:
            v_toks = _normalize_stem(vid.name)
            best_score = 0.0
            best_srt: Optional[Path] = None
            for s in same_dir_first:
                if s in used_srts:
                    continue
                s_ep = _extract_episode_key(s.name)
                if not _episodes_compatible(v_ep, s_ep):
                    continue
                s_toks = _normalize_stem(s.name)
                if not v_toks or not s_toks:
                    continue
                inter = len(v_toks & s_toks)
                score = inter / max(1, min(len(v_toks), len(s_toks)))
                if score > best_score and score >= 0.6:
                    best_score = score
                    best_srt = s
            if best_srt:
                matched.append(str(best_srt))
                used_srts.add(best_srt)
                conf = "fuzzy"

        if matched:
            pairs.append(BatchPair(video_path=str(vid), srt_paths=matched, confidence=conf))
        else:
            unmatched_videos.append(str(vid))

    unmatched_subs = [str(s) for s in srts if s not in used_srts]
    return pairs, unmatched_videos, unmatched_subs


def find_companion_subtitles(video_path: str) -> list[str]:
    """Auto-discover matching subtitles for a video in its folder and Subs/ subfolders."""
    vp = Path(video_path).resolve()
    if not vp.is_file():
        return []
    parent = vp.parent

    # 1. Gather candidate subtitle files in parent + common Subs subfolders
    candidate_dirs: list[Path] = [parent]
    for child in parent.iterdir():
        if child.is_dir() and (child.name.lower() in _COMPANION_SUBDIRS or child.name.lower() == vp.stem.lower()):
            candidate_dirs.append(child)
            # Also handle Subs/<VideoStem>/ common scene release layout
            nested = child / vp.stem
            if nested.is_dir():
                candidate_dirs.append(nested)

    candidates: list[Path] = []
    seen_paths: set[str] = set()
    for d in candidate_dirs:
        iter_items = d.rglob("*") if d != parent else d.iterdir()
        for f in sorted(iter_items):
            if not f.is_file():
                continue
            low = f.suffix.lower()
            if low in SUBTITLE_EXTENSIONS:
                key = str(f.resolve()).lower()
                if key not in seen_paths:
                    seen_paths.add(key)
                    candidates.append(f)
            elif low == ".zip":
                try:
                    for ext_sub in extract_subtitles_from_zip(str(f)):
                        ep = Path(ext_sub)
                        key = str(ep.resolve()).lower()
                        if key not in seen_paths:
                            seen_paths.add(key)
                            candidates.append(ep)
                except Exception:
                    continue

    if not candidates:
        return []

    sibling_videos = [f for f in parent.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS and ".tmp-" not in f.name and ".bak-" not in f.name]
    v_stem = vp.stem.lower()
    v_ep = _extract_episode_key(vp.name)
    v_toks = _normalize_stem(vp.name)

    matched: list[str] = []
    for s in candidates:
        s_stem = s.stem.lower()
        s_ep = _extract_episode_key(s.name)
        in_named_subdir = s.parent.name.lower() == v_stem or (
            s.parent.parent != parent and s.parent.name.lower() in _COMPANION_SUBDIRS
        )

        # Exact stem/prefix match or inside <VideoStem>/ folder
        if (
            s_stem == v_stem
            or s_stem.startswith(v_stem + ".")
            or s_stem.startswith(v_stem + "_")
            or s_stem.startswith(v_stem + "-")
            or s.parent.name.lower() == v_stem
        ):
            matched.append(str(s))
            continue

        # Episode key match
        if v_ep is not None and s_ep is not None and _episodes_compatible(v_ep, s_ep):
            matched.append(str(s))
            continue

        # Single-video folder heuristic (e.g. Movie/Movie.mkv + Movie/Subs/Arabic.srt)
        if len(sibling_videos) <= 1 and _episodes_compatible(v_ep, s_ep):
            matched.append(str(s))
            continue

        # Fuzzy token match across multi-video folders
        if _episodes_compatible(v_ep, s_ep):
            s_toks = _normalize_stem(s.name)
            if v_toks and s_toks:
                score = len(v_toks & s_toks) / max(1, min(len(v_toks), len(s_toks)))
                if score >= 0.6 or in_named_subdir:
                    matched.append(str(s))

    return matched


def audit_library(
    directory: str,
    target_lang: str = "ara",
    recursive: bool = True,
) -> LibraryAuditReport:
    """Audit a media folder for videos that already have, can pair, or lack target subtitles."""
    root = Path(directory)
    if not root.is_dir():
        raise NotADirectoryError(f"Directory not found: {directory}")

    lang_code, _ = resolve_language(target_lang)
    iterator = root.rglob("*") if recursive else root.iterdir()
    videos = sorted(
        f for f in iterator
        if f.is_file()
        and f.suffix.lower() in VIDEO_EXTENSIONS
        and ".tmp-" not in f.name
        and ".bak-" not in f.name
        and " [subtitled" not in f.stem
    )

    items: list[LibraryAuditItem] = []
    ready_count = 0
    pairable_count = 0
    missing_count = 0

    for vid in videos:
        try:
            info = probe_video(str(vid))
            embedded_langs = sorted({(s.language or "und").lower() for s in info.subtitle_streams})
        except Exception:
            embedded_langs = []

        has_target = (
            bool(embedded_langs)
            if lang_code in ("auto", "und")
            else lang_code.lower() in embedded_langs
        )
        if has_target:
            status = "ready"
            companions: list[str] = []
            ready_count += 1
        else:
            companions = find_companion_subtitles(str(vid))
            if companions:
                status = "pairable"
                pairable_count += 1
            else:
                status = "missing"
                missing_count += 1

        items.append(
            LibraryAuditItem(
                video_path=str(vid),
                filename=vid.name,
                status=status,
                embedded_langs=embedded_langs,
                companion_subs=companions,
            )
        )

    return LibraryAuditReport(
        directory=str(root.resolve()),
        target_lang=lang_code,
        total_videos=len(videos),
        ready_count=ready_count,
        pairable_count=pairable_count,
        missing_count=missing_count,
        items=items,
    )


_FONT_EXTS = {".ttf", ".otf", ".ttc"}


def _discover_ass_fonts(sub_paths: list[str]) -> list[str]:
    """Find .ttf/.otf/.ttc font files alongside .ass/.ssa subtitles or in Fonts/ subfolders."""
    found: list[str] = []
    seen: set[str] = set()
    for raw in sub_paths:
        sp = Path(raw)
        if sp.suffix.lower() not in (".ass", ".ssa"):
            continue
        dirs = [sp.parent, sp.parent / "Fonts", sp.parent / "fonts"]
        for d in dirs:
            if not d.is_dir():
                continue
            for f in sorted(d.iterdir()):
                if f.is_file() and f.suffix.lower() in _FONT_EXTS:
                    key = str(f.resolve()).lower()
                    if key not in seen:
                        seen.add(key)
                        found.append(str(f.resolve()))
    return found


def _default_output_path(
    video_path: str,
    target_ext: str,
    replace_original: bool,
    output_template: str = "default",
    lang_tags: Optional[list[str]] = None,
) -> Path:
    vp = Path(video_path)
    if replace_original and vp.suffix.lower() == target_ext.lower():
        return vp
    langs_str = ",".join(t for t in (lang_tags or []) if t and t != "und") or "sub"
    tmpl = (output_template or "default").strip()
    if tmpl == "clean":
        sub_dir = vp.parent / "subtitled"
        return sub_dir / f"{vp.stem}{target_ext}"
    if tmpl == "plex":
        base_name = f"{vp.stem} ({langs_str})"
    elif tmpl == "default":
        base_name = f"{vp.stem} [subtitled]"
    else:
        base_name = (
            tmpl.replace("{stem}", vp.stem)
            .replace("{langs}", langs_str)
            .replace("{lang}", langs_str.split(",")[0])
            .replace("{ext}", target_ext.lstrip("."))
        )
        # Guard against directory traversal or empty name
        base_name = Path(base_name).name or f"{vp.stem} [subtitled]"

    candidate = vp.with_name(f"{base_name}{target_ext}")
    if not candidate.exists():
        return candidate
    idx = 2
    while True:
        alt = vp.with_name(f"{base_name} {idx}{target_ext}")
        if not alt.exists():
            return alt
        idx += 1


def _hex_to_ass_color(hex_color: str) -> str:
    """Convert #RRGGBB into ASS BGR &H00BBGGRR format."""
    c = (hex_color or "#FFFFFF").strip().lstrip("#")
    if len(c) != 6:
        return "&H00FFFFFF"
    rr, gg, bb = c[0:2], c[2:4], c[4:6]
    return f"&H00{bb}{gg}{rr}".upper()


def detect_installed_fonts() -> list[str]:
    """Return available common subtitle fonts on the current OS."""
    candidates = ("Arial", "Segoe UI", "Tahoma", "Verdana", "DejaVu Sans", "Liberation Sans", "Noto Sans", "Noto Sans Arabic")
    if os.name == "nt":
        font_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        if font_dir.is_dir():
            files = {f.stem.lower() for f in font_dir.iterdir() if f.is_file()}
            found = []
            checks = {
                "Arial": ("arial",),
                "Segoe UI": ("segoeui",),
                "Tahoma": ("tahoma",),
                "Verdana": ("verdana",),
            }
            for name, stems in checks.items():
                if any(st in files for st in stems):
                    found.append(name)
            return found or ["Arial"]
    elif shutil.which("fc-list"):
        try:
            proc = subprocess.run(
                ["fc-list", ":", "family"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            )
            if proc.returncode == 0:
                installed_low = proc.stdout.lower()
                found = [c for c in candidates if c.lower() in installed_low]
                if found:
                    return found
        except Exception:
            pass
    return ["Arial", "Tahoma", "DejaVu Sans"]


def _build_hardsub_filter(sub_path: str, style: Optional[HardsubStyle]) -> str:
    escaped = _escape_ffmpeg_sub_path(sub_path)
    base = f"subtitles='{escaped}':charenc=UTF-8"
    if os.name == "nt":
        win_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        if win_fonts.is_dir():
            base = f"{base}:fontsdir='{_escape_ffmpeg_sub_path(str(win_fonts))}'"
    if not style or Path(sub_path).suffix.lower() in (".ass", ".ssa"):
        return base
    style_parts = [
        f"FontName={style.font_name}",
        f"FontSize={int(style.font_size)}",
        f"PrimaryColour={_hex_to_ass_color(style.primary_color)}",
        f"OutlineColour={_hex_to_ass_color(style.outline_color)}",
        f"Outline={int(style.outline_width)}",
        f"MarginV={int(style.margin_v)}",
    ]
    return f"{base}:force_style='{','.join(style_parts)}'"


def _color_preservation_args(info: VideoInfo) -> list[str]:
    """Preserve source BT.709 / color tags so hardsubbed videos never turn washed out."""
    args: list[str] = []
    cs = info.color_space if info.color_space and info.color_space != "unknown" else ("bt709" if info.width >= 1280 else "")
    cp = info.color_primaries if info.color_primaries and info.color_primaries != "unknown" else ("bt709" if info.width >= 1280 else "")
    ct = info.color_transfer if info.color_transfer and info.color_transfer != "unknown" else ("bt709" if info.width >= 1280 else "")
    cr = info.color_range if info.color_range and info.color_range != "unknown" else "tv"
    if cs:
        args.extend(["-colorspace", cs])
    if cp:
        args.extend(["-color_primaries", cp])
    if ct:
        args.extend(["-color_trc", ct])
    if cr:
        args.extend(["-color_range", cr])
    return args


def _video_codec_args(encoder: str, crf: int, preset: str, color_args: Optional[list[str]] = None) -> list[str]:
    extra = list(color_args or [])
    if encoder == "h264_nvenc":
        return [
            "-c:v", "h264_nvenc",
            "-rc", "vbr", "-cq", str(crf), "-b:v", "0",
            "-preset", "p5", "-tune", "hq", "-profile:v", "high", "-pix_fmt", "yuv420p",
            *extra,
        ]
    if encoder == "hevc_nvenc":
        return [
            "-c:v", "hevc_nvenc",
            "-rc", "vbr", "-cq", str(crf), "-b:v", "0",
            "-preset", "p5", "-tune", "hq",
            *extra,
        ]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-global_quality", str(crf), "-look_ahead", "1", "-preset", "medium", *extra]
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-rc", "cqp", "-qp_i", str(crf), "-qp_p", str(crf), "-quality", "quality", *extra]
    return ["-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p", *extra]


def _escape_ffmpeg_sub_path(path: str) -> str:
    """Escape a Windows/POSIX path for ffmpeg's subtitles= filter."""
    p = str(Path(path).resolve()).replace("\\", "/")
    p = p.replace(":", r"\:")
    p = p.replace("'", r"\'")
    p = p.replace(",", r"\,")
    p = p.replace("[", r"\[").replace("]", r"\]")
    return p


def _run_monitored_process(
    cmd: list[str],
    duration_sec: float,
    engine_label: str,
    progress_cb: Optional[ProgressCallback],
    cancel_event: Optional[threading.Event],
) -> tuple[int, str]:
    """Run mkvmerge or ffmpeg with live progress parsing and clean cancellation."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=_no_window_flags(),
    )

    output_lines: list[str] = []

    def _kill_proc() -> None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    if proc.stdout:
        for line in proc.stdout:
            if cancel_event and cancel_event.is_set():
                _kill_proc()
                raise InterruptedError("Operation cancelled by user.")

            stripped = line.strip()
            if stripped:
                output_lines.append(stripped)
                if len(output_lines) > 250:
                    output_lines = output_lines[-150:]

            if progress_cb:
                # mkvmerge progress: "Progress: 42%"
                m_mkv = re.search(r"Progress:\s*(\d+)%", stripped)
                if m_mkv:
                    pct = float(m_mkv.group(1))
                    progress_cb(min(98.0, pct), f"Muxing ({engine_label})", f"{int(pct)}%")
                    continue

                # ffmpeg progress: "time=00:12:34.56 ... speed=12.4x"
                m_time = re.search(r"time=(\d{2}):(\d{2}):(\d{2})\.(\d{2})", stripped)
                if m_time and duration_sec > 0:
                    cur = (
                        int(m_time.group(1)) * 3600
                        + int(m_time.group(2)) * 60
                        + int(m_time.group(3))
                        + int(m_time.group(4)) / 100.0
                    )
                    pct = min(98.0, max(1.0, (cur / duration_sec) * 100.0))
                    m_spd = re.search(r"speed=\s*([^\s]+)", stripped)
                    spd = f" · {m_spd.group(1)}" if m_spd else ""
                    progress_cb(pct, f"Muxing ({engine_label})", f"{pct:.0f}%{spd}")

    ret = proc.wait()
    if cancel_event and cancel_event.is_set():
        raise InterruptedError("Operation cancelled by user.")

    return ret, "\n".join(output_lines[-25:])


def _get_mkvmerge_drop_ids(mkvmerge_exe: str, video_path: str, target_langs: set[str], fallback_info: VideoInfo) -> list[str]:
    """Resolve exact mkvmerge track IDs for subtitle tracks matching target_langs."""
    try:
        proc = subprocess.run(
            [mkvmerge_exe, "-J", video_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=_no_window_flags(),
        )
        if proc.returncode == 0:
            data = json.loads(proc.stdout or "{}")
            drop_ids: list[str] = []
            for tr in data.get("tracks", []):
                if tr.get("type") == "subtitles":
                    lang = ((tr.get("properties") or {}).get("language") or "und").lower()
                    if lang in target_langs:
                        drop_ids.append(str(tr["id"]))
            return drop_ids
    except Exception:
        pass
    return [str(s.index) for s in fallback_info.subtitle_streams if s.language.lower() in target_langs]


def _build_mkvmerge_cmd(
    mkvmerge_exe: str,
    video_path: str,
    prepared_subs: list[tuple[str, str, str, bool, bool]],  # (tmp_srt, lang, title, default, forced)
    out_path: str,
    existing_subs: str,
    video_info: VideoInfo,
    font_attachments: Optional[list[str]] = None,
) -> list[str]:
    cmd = [mkvmerge_exe, "--ui-language", "en", "-o", out_path]

    if existing_subs == "replace-all":
        cmd.append("--no-subtitles")
    elif existing_subs == "replace-same-lang":
        target_langs = {lang for _, lang, _, _, _ in prepared_subs if lang != "und"}
        if target_langs:
            drop_ids = _get_mkvmerge_drop_ids(mkvmerge_exe, video_path, target_langs, video_info)
            if drop_ids:
                cmd.extend(["--subtitle-tracks", "!" + ",".join(drop_ids)])

    cmd.append(video_path)

    for srt_file, lang, title, is_default, is_forced in prepared_subs:
        cmd.extend(["--language", f"0:{lang}"])
        if title:
            cmd.extend(["--track-name", f"0:{title}"])
        cmd.extend(["--default-track-flag", f"0:{'yes' if is_default else 'no'}"])
        cmd.extend(["--forced-display-flag", f"0:{'yes' if is_forced else 'no'}"])
        cmd.append(srt_file)

    for font_path in font_attachments or []:
        mime = "application/vnd.ms-opentype" if font_path.lower().endswith(".otf") else "application/x-truetype-font"
        cmd.extend(["--attachment-mime-type", mime, "--attach-file", font_path])

    return cmd


def _build_ffmpeg_soft_cmd(
    ffmpeg_exe: str,
    video_path: str,
    prepared_subs: list[tuple[str, str, str, bool, bool]],
    out_path: str,
    target_ext: str,
    existing_subs: str,
    video_info: VideoInfo,
    safe_stream_map: bool = False,
    font_attachments: Optional[list[str]] = None,
) -> list[str]:
    cmd = [ffmpeg_exe, "-y", "-hide_banner", "-stats", "-i", video_path]
    for srt_file, _, _, _, _ in prepared_subs:
        cmd.extend(["-i", srt_file])

    # Determine which existing streams to map
    target_langs = {lang for _, lang, _, _, _ in prepared_subs if lang != "und"}
    kept_existing_sub_count = 0

    if safe_stream_map:
        cmd.extend(["-map", "0:v", "-map", "0:a?"])
    else:
        cmd.extend(["-map", "0:v", "-map", "0:a?"])
        if existing_subs == "keep" or existing_subs == "replace-same-lang":
            for s in video_info.subtitle_streams:
                if existing_subs == "replace-same-lang" and s.language.lower() in target_langs:
                    continue
                # Skip image-based subs when muxing into MP4 mov_text
                if target_ext in (".mp4", ".m4v", ".mov") and s.codec_name.lower() not in TEXT_SUB_CODECS:
                    continue
                cmd.extend(["-map", f"0:{s.index}"])
                kept_existing_sub_count += 1
        if target_ext == ".mkv":
            # Preserve chapters/attachments if not in safe minimal mode
            cmd.extend(["-map", "0:t?"])

    for i in range(len(prepared_subs)):
        cmd.extend(["-map", f"{i + 1}:0"])

    sub_codec = "mov_text" if target_ext in (".mp4", ".m4v", ".mov") else "srt"
    cmd.extend(["-c:v", "copy", "-c:a", "copy", "-c:s", sub_codec])

    for idx, (_, lang, title, is_default, is_forced) in enumerate(prepared_subs):
        out_sub_idx = kept_existing_sub_count + idx
        cmd.extend([f"-metadata:s:s:{out_sub_idx}", f"language={lang}"])
        if title:
            cmd.extend([f"-metadata:s:s:{out_sub_idx}", f"title={title}"])
        disps = []
        if is_default:
            disps.append("default")
        if is_forced:
            disps.append("forced")
        disp_val = "+".join(disps) if disps else "0"
        cmd.extend([f"-disposition:s:{out_sub_idx}", disp_val])

    if target_ext in (".mp4", ".m4v", ".mov"):
        cmd.extend(["-movflags", "+faststart"])
    elif target_ext == ".mkv" and font_attachments and not safe_stream_map:
        base_att = sum(1 for s in video_info.streams if s.codec_type == "attachment")
        for a_idx, font_path in enumerate(font_attachments):
            mime = "application/vnd.ms-opentype" if font_path.lower().endswith(".otf") else "application/x-truetype-font"
            cmd.extend(["-attach", font_path, f"-metadata:s:t:{base_att + a_idx}", f"mimetype={mime}"])

    cmd.append(out_path)
    return cmd


def embed_subtitles(
    options: EmbedOptions,
    progress_cb: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> EmbedResult:
    """Embed one or more SRT files into a video with smart encoding & container fallbacks."""
    t0 = time.perf_counter()
    tools = discover_tools()
    mkvmerge_exe = tools["mkvmerge"].path
    ffmpeg_exe = tools["ffmpeg"].path

    video_path = str(Path(options.video_path).resolve())
    if not Path(video_path).is_file():
        return EmbedResult(
            success=False,
            video_path=video_path,
            output_path="",
            engine_used="none",
            error=f"Video file not found: {video_path}",
        )

    if not options.subtitles:
        return EmbedResult(
            success=False,
            video_path=video_path,
            output_path="",
            engine_used="none",
            error="No subtitle files provided.",
        )

    if progress_cb:
        progress_cb(2.0, "Inspecting media", Path(video_path).name)

    video_info = probe_video(video_path)
    src_ext = Path(video_path).suffix.lower()

    fallbacks: list[str] = []
    warnings: list[str] = []

    # Decide target container extension
    if options.mode == "soft-mkv":
        target_ext = ".mkv"
    elif options.mode == "hardsub":
        target_ext = src_ext if src_ext in (".mkv", ".mp4", ".mov", ".m4v") else ".mp4"
    elif src_ext in (".mkv", ".webm"):
        target_ext = ".mkv"
    elif src_ext in (".mp4", ".m4v", ".mov"):
        target_ext = src_ext
    else:
        # AVI / TS / FLV / WMV cannot natively store standard soft SRT cleanly
        if options.fallback_to_mkv:
            target_ext = ".mkv"
            fallbacks.append(f"Container '{src_ext}' does not support standard soft SRT tracks; falling back to lossless .mkv remux.")
        else:
            return EmbedResult(
                success=False,
                video_path=video_path,
                output_path="",
                engine_used="none",
                error=f"Container '{src_ext}' does not support soft SRT embedding. Enable MKV fallback or use --mode soft-mkv / hardsub.",
            )

    if options.output_path:
        final_out = Path(options.output_path).resolve()
        target_ext = final_out.suffix.lower() or target_ext
    else:
        lang_hints = [resolve_language(s.lang_code, s.path)[0] for s in options.subtitles]
        final_out = _default_output_path(
            video_path,
            target_ext,
            options.replace_original,
            output_template=getattr(options, "output_template", "default"),
            lang_tags=lang_hints,
        )
        if options.replace_original and final_out != Path(video_path):
            warnings.append(f"Original preserved because target format ({target_ext}) differs from source ({src_ext}).")

    # Pre-flight disk space guard (skip on dry-run)
    if not options.dry_run:
        final_out.parent.mkdir(parents=True, exist_ok=True)
        free_bytes = shutil.disk_usage(final_out.parent).free
        needed_bytes = int(video_info.size_bytes * 1.05) + 10_000_000
        if free_bytes < needed_bytes:
            need_gb = needed_bytes / (1024 ** 3)
            free_gb = free_bytes / (1024 ** 3)
            return EmbedResult(
                success=False,
                video_path=video_path,
                output_path=str(final_out),
                engine_used="none",
                error=f"Insufficient disk space in {final_out.parent}: need ~{need_gb:.2f} GB, only {free_gb:.2f} GB free.",
            )

    # Prepare normalized UTF-8 SRT files in a temporary directory
    with tempfile.TemporaryDirectory(prefix="srt_embed_") as tmpdir:
        tmp_dir = Path(tmpdir)
        prepared_subs: list[tuple[str, str, str, bool, bool]] = []
        prepared_stripped_subs: list[tuple[str, str, str, bool, bool]] = []

        for idx, sub in enumerate(options.subtitles):
            if not Path(sub.path).is_file():
                return EmbedResult(
                    success=False,
                    video_path=video_path,
                    output_path=str(final_out),
                    engine_used="none",
                    error=f"Subtitle file not found: {sub.path}",
                )

            effective_offset = sub.offset_ms
            effective_speed = sub.speed_factor if 0.5 <= getattr(sub, "speed_factor", 1.0) <= 2.0 else 1.0
            if options.auto_sync and effective_offset == 0 and abs(effective_speed - 1.0) < 1e-4:
                if progress_cb:
                    progress_cb(3.5, "Auto-syncing", Path(sub.path).name)
                try:
                    sync_rep = check_sync(video_path, sub.path)
                    if sync_rep.comparable:
                        if abs(sync_rep.recommended_speed - 1.0) >= 0.001:
                            effective_speed = sync_rep.recommended_speed
                            effective_offset = sync_rep.recommended_offset_ms
                            warnings.append(
                                f"[{Path(sub.path).name}] Auto-sync applied {effective_speed:.4f}x + {effective_offset:+d}ms ({sync_rep.method})."
                            )
                        elif abs(sync_rep.recommended_offset_ms) >= options.auto_sync_threshold_ms:
                            effective_offset = sync_rep.recommended_offset_ms
                            warnings.append(
                                f"[{Path(sub.path).name}] Auto-sync applied {effective_offset:+d}ms ({sync_rep.method})."
                            )
                except Exception:
                    pass

            try:
                vid_fps = float(video_info.fps) if video_info.fps else 23.976
            except ValueError:
                vid_fps = 23.976

            rep = inspect_srt(
                sub.path,
                lang_override=sub.lang_code,
                offset_ms=effective_offset,
                strip_tags=sub.strip_html_tags,
                clean_sdh=sub.clean_sdh,
                clean_ads=sub.clean_ads,
                speed_factor=effective_speed,
                fix_rtl_bidi=getattr(sub, "fix_rtl_bidi", False),
                video_fps=vid_fps,
            )
            warnings.extend([f"[{Path(sub.path).name}] {w}" for w in rep.warnings])
            if rep.cue_count == 0:
                return EmbedResult(
                    success=False,
                    video_path=video_path,
                    output_path=str(final_out),
                    engine_used="none",
                    error=f"Subtitle file has 0 valid cues: {sub.path}",
                )

            title = sub.title.strip() or rep.detected_lang_name
            # Keep native .ass styling for MKV / Hardsub when no offset/clean flags alter the file
            src_sub_ext = Path(sub.path).suffix.lower()
            preserve_ass = (
                (src_sub_ext in (".ass", ".ssa") and target_ext == ".mkv")
                or (options.mode == "hardsub" and src_sub_ext in (".ass", ".ssa"))
            ) and effective_offset == 0 and abs(effective_speed - 1.0) < 1e-4 and not sub.clean_sdh and not sub.clean_ads and not sub.strip_html_tags and not getattr(sub, "fix_rtl_bidi", False)

            if preserve_ass:
                tmp_sub = tmp_dir / f"sub_{idx}_{rep.detected_lang_code}{src_sub_ext}"
                shutil.copyfile(sub.path, tmp_sub)
                prepared_subs.append((str(tmp_sub), rep.detected_lang_code, title, sub.is_default, sub.is_forced))
            else:
                tmp_srt = tmp_dir / f"sub_{idx}_{rep.detected_lang_code}.srt"
                tmp_srt.write_text(rep.normalized_content, encoding="utf-8")
                prepared_subs.append((str(tmp_srt), rep.detected_lang_code, title, sub.is_default, sub.is_forced))

            # Also pre-generate tag-stripped copy for mov_text fallback if needed
            rep_clean = inspect_srt(
                sub.path,
                lang_override=sub.lang_code,
                offset_ms=effective_offset,
                strip_tags=True,
                clean_sdh=sub.clean_sdh,
                clean_ads=sub.clean_ads,
                speed_factor=effective_speed,
                fix_rtl_bidi=getattr(sub, "fix_rtl_bidi", False),
                video_fps=vid_fps,
            )
            tmp_srt_clean = tmp_dir / f"sub_{idx}_{rep.detected_lang_code}_clean.srt"
            tmp_srt_clean.write_text(rep_clean.normalized_content, encoding="utf-8")
            prepared_stripped_subs.append((str(tmp_srt_clean), rep.detected_lang_code, title, sub.is_default, sub.is_forced))

        # Smart Audio+Subtitle Default Rule: promote target lang when primary audio is foreign
        pref_lang = (getattr(options, "prefer_sub_lang_when_foreign_audio", "") or "").strip().lower()
        if pref_lang and video_info.audio_streams:
            pref_iso, _ = resolve_language(pref_lang)
            primary_audio_lang = (video_info.audio_streams[0].language or "und").lower()
            if primary_audio_lang != pref_iso and any(p[1] == pref_iso for p in prepared_subs):
                prepared_subs = [
                    (p[0], p[1], p[2], (p[1] == pref_iso), p[4]) for p in prepared_subs
                ]
                prepared_stripped_subs = [
                    (p[0], p[1], p[2], (p[1] == pref_iso), p[4]) for p in prepared_stripped_subs
                ]
                warnings.append(f"Smart rule: marked '{pref_iso}' as default subtitle (primary audio is '{primary_audio_lang}').")

        font_attachments: list[str] = (
            _discover_ass_fonts([s.path for s in options.subtitles])
            if getattr(options, "attach_fonts", True) and target_ext == ".mkv"
            else []
        )
        if font_attachments:
            warnings.append(f"Attaching {len(font_attachments)} font file(s) for styled ASS playback.")

        # Write to staging file in the SAME directory as final_out for instant atomic swap
        final_out.parent.mkdir(parents=True, exist_ok=True)
        stage_path = final_out.with_name(f".{final_out.stem}.tmp-{uuid.uuid4().hex[:6]}{target_ext}")

        # Build execution attempts in fallback priority order
        # Each tuple: (engine_name, cmd, stage_file_path, fallback_note)
        attempts: list[tuple[str, list[str], Path, str]] = []

        if options.mode == "hardsub":
            if not ffmpeg_exe:
                return EmbedResult(
                    success=False,
                    video_path=video_path,
                    output_path=str(final_out),
                    engine_used="none",
                    error="ffmpeg is required for hardsub (burn-in) mode.",
                )
            if len(prepared_subs) > 1:
                warnings.append(
                    f"Hardsub burns only the first queued subtitle ({Path(options.subtitles[0].path).name}); "
                    f"{len(prepared_subs) - 1} additional track(s) were ignored. Merge Bilingual (.ass) first to burn two tracks."
                )
            sub_filter = _build_hardsub_filter(prepared_subs[0][0], options.hardsub_style)
            req_enc = options.hardsub_style.encoder if options.hardsub_style else "auto"
            encoders_to_try = detect_hw_encoders(ffmpeg_exe) if req_enc == "auto" else [req_enc, "libx264"]
            # Deduplicate preserving order
            seen_enc: set[str] = set()
            unique_encs = [e for e in encoders_to_try if not (e in seen_enc or seen_enc.add(e))]

            c_args = _color_preservation_args(video_info)
            for e_idx, enc in enumerate(unique_encs):
                v_args = _video_codec_args(enc, options.crf, options.preset, c_args)
                note = "" if e_idx == 0 else f"Encoder '{unique_encs[e_idx - 1]}' failed; fell back to '{enc}'."
                attempts.append((
                    f"ffmpeg-hardsub-{enc}",
                    [
                        ffmpeg_exe, "-y", "-hide_banner", "-stats",
                        "-i", video_path, "-vf", sub_filter,
                        *v_args, "-c:a", "copy", str(stage_path),
                    ],
                    stage_path,
                    note,
                ))
                attempts.append((
                    f"ffmpeg-hardsub-{enc}-aac",
                    [
                        ffmpeg_exe, "-y", "-hide_banner", "-stats",
                        "-i", video_path, "-vf", sub_filter,
                        *v_args, "-c:a", "aac", "-b:a", "192k", str(stage_path),
                    ],
                    stage_path,
                    "Audio stream copy incompatible with output container; fell back to AAC audio re-encode.",
                ))
        else:
            # Soft-mux attempts
            if target_ext == ".mkv":
                if options.mode != "ffmpeg" and mkvmerge_exe:
                    attempts.append((
                        "mkvmerge",
                        _build_mkvmerge_cmd(mkvmerge_exe, video_path, prepared_subs, str(stage_path), options.existing_subs, video_info, font_attachments=font_attachments),
                        stage_path,
                        "",
                    ))
                elif options.mode == "mkvmerge" and not mkvmerge_exe:
                    fallbacks.append("mkvmerge not found; falling back to ffmpeg.")

                if ffmpeg_exe:
                    note = "mkvmerge unavailable or failed; fell back to ffmpeg Matroska muxer." if mkvmerge_exe and options.mode != "ffmpeg" else ""
                    attempts.append((
                        "ffmpeg-mkv",
                        _build_ffmpeg_soft_cmd(ffmpeg_exe, video_path, prepared_subs, str(stage_path), ".mkv", options.existing_subs, video_info, font_attachments=font_attachments),
                        stage_path,
                        note,
                    ))
                    attempts.append((
                        "ffmpeg-mkv-safe-map",
                        _build_ffmpeg_soft_cmd(ffmpeg_exe, video_path, prepared_subs, str(stage_path), ".mkv", options.existing_subs, video_info, safe_stream_map=True),
                        stage_path,
                        "Dropped auxiliary data/attachment tracks that caused mux failure.",
                    ))
            else:
                # MP4 / M4V / MOV
                if ffmpeg_exe:
                    attempts.append((
                        "ffmpeg-mov_text",
                        _build_ffmpeg_soft_cmd(ffmpeg_exe, video_path, prepared_subs, str(stage_path), target_ext, options.existing_subs, video_info),
                        stage_path,
                        "",
                    ))
                    attempts.append((
                        "ffmpeg-mov_text-safe-map",
                        _build_ffmpeg_soft_cmd(ffmpeg_exe, video_path, prepared_subs, str(stage_path), target_ext, options.existing_subs, video_info, safe_stream_map=True),
                        stage_path,
                        "Excluded incompatible data/timecode/image-sub streams for MP4 muxing.",
                    ))
                    if options.strip_tags_on_fallback:
                        attempts.append((
                            "ffmpeg-mov_text-stripped",
                            _build_ffmpeg_soft_cmd(ffmpeg_exe, video_path, prepared_stripped_subs, str(stage_path), target_ext, options.existing_subs, video_info, safe_stream_map=True),
                            stage_path,
                            "Stripped HTML/font tags from SRT for strict MP4 mov_text compatibility.",
                        ))

                if options.fallback_to_mkv and not options.output_path:
                    mkv_out = _default_output_path(video_path, ".mkv", replace_original=False)
                    mkv_stage = mkv_out.with_name(f".{mkv_out.stem}.tmp-{uuid.uuid4().hex[:6]}.mkv")
                    if mkvmerge_exe:
                        attempts.append((
                            "mkvmerge-container-fallback",
                            _build_mkvmerge_cmd(mkvmerge_exe, video_path, prepared_subs, str(mkv_stage), options.existing_subs, video_info),
                            mkv_stage,
                            f"MP4 container rejected subtitle stream; fell back to lossless MKV ({mkv_out.name}).",
                        ))
                    elif ffmpeg_exe:
                        attempts.append((
                            "ffmpeg-mkv-container-fallback",
                            _build_ffmpeg_soft_cmd(ffmpeg_exe, video_path, prepared_subs, str(mkv_stage), ".mkv", options.existing_subs, video_info, safe_stream_map=True),
                            mkv_stage,
                            f"MP4 container rejected subtitle stream; fell back to lossless MKV ({mkv_out.name}).",
                        ))

        if not attempts:
            return EmbedResult(
                success=False,
                video_path=video_path,
                output_path=str(final_out),
                engine_used="none",
                error="Neither mkvmerge nor ffmpeg was found on this system.",
            )

        if options.dry_run:
            eng, cmd, _stg, _note = attempts[0]
            return EmbedResult(
                success=True,
                video_path=video_path,
                output_path=str(final_out),
                engine_used=eng,
                fallbacks_triggered=fallbacks,
                warnings=warnings,
                dry_run=True,
                command_preview=cmd,
            )

        last_err = ""
        used_engine = ""
        succeeded_stage: Optional[Path] = None
        succeeded_final: Path = final_out

        for attempt_idx, (eng_name, cmd, cur_stage, fb_note) in enumerate(attempts):
            cur_stage.unlink(missing_ok=True)
            if attempt_idx > 0 and fb_note:
                fallbacks.append(fb_note)
                if progress_cb:
                    progress_cb(5.0, "Switching fallback", fb_note)

            try:
                ret, tail_log = _run_monitored_process(
                    cmd,
                    video_info.duration_sec,
                    eng_name,
                    progress_cb,
                    cancel_event,
                )
            except InterruptedError:
                cur_stage.unlink(missing_ok=True)
                return EmbedResult(
                    success=False,
                    video_path=video_path,
                    output_path=str(final_out),
                    engine_used=eng_name,
                    error="Cancelled by user.",
                )

            # mkvmerge returns 0 (clean) or 1 (warnings, valid output)
            valid_exit = (ret in (0, 1)) if "mkvmerge" in eng_name else (ret == 0)
            if valid_exit and cur_stage.exists() and cur_stage.stat().st_size > 0:
                if ret == 1 and "mkvmerge" in eng_name:
                    warnings.append(f"mkvmerge completed with warnings:\n{tail_log}")
                used_engine = eng_name
                succeeded_stage = cur_stage
                if "container-fallback" in eng_name:
                    succeeded_final = _default_output_path(video_path, ".mkv", replace_original=False)
                break

            cur_stage.unlink(missing_ok=True)
            last_err = tail_log or f"{eng_name} exited with code {ret}"

        if not succeeded_stage or not succeeded_stage.exists():
            return EmbedResult(
                success=False,
                video_path=video_path,
                output_path=str(final_out),
                engine_used="none",
                fallbacks_triggered=fallbacks,
                warnings=warnings,
                error=f"All muxing attempts failed. Last error:\n{last_err}",
            )

        if progress_cb:
            progress_cb(99.0, "Verifying output", succeeded_final.name)

        # Verify muxed output with ffprobe before replacing original
        try:
            out_info = probe_video(str(succeeded_stage))
            sub_count = len(out_info.subtitle_streams)
            if options.mode != "hardsub" and sub_count == 0:
                raise RuntimeError("Output file has 0 subtitle streams after muxing.")
        except Exception as exc:
            succeeded_stage.unlink(missing_ok=True)
            return EmbedResult(
                success=False,
                video_path=video_path,
                output_path=str(succeeded_final),
                engine_used=used_engine,
                fallbacks_triggered=fallbacks,
                warnings=warnings,
                error=f"Verification failed: {exc}",
            )

        undo_tx_id: Optional[str] = None
        if options.replace_original and succeeded_final == Path(video_path):
            if options.create_backup:
                tx_tag = uuid.uuid4().hex[:6]
                bak_path = Path(video_path).with_name(f".{Path(video_path).stem}.bak-{tx_tag}{src_ext}")
                os.replace(video_path, str(bak_path))
                os.replace(str(succeeded_stage), str(succeeded_final))
                rec = register_backup(video_path, str(bak_path), str(succeeded_final), used_engine)
                undo_tx_id = rec.tx_id
            else:
                os.replace(str(succeeded_stage), str(succeeded_final))
        else:
            os.replace(str(succeeded_stage), str(succeeded_final))

        if progress_cb:
            progress_cb(100.0, "Complete", succeeded_final.name)

        return EmbedResult(
            success=True,
            video_path=video_path,
            output_path=str(succeeded_final),
            engine_used=used_engine,
            fallbacks_triggered=fallbacks,
            warnings=warnings,
            elapsed_sec=round(time.perf_counter() - t0, 2),
            output_size_bytes=succeeded_final.stat().st_size,
            subtitle_streams_after=sub_count,
            undo_tx_id=undo_tx_id,
        )
