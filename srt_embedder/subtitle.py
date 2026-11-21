"""Subtitle decoding, repair, normalization, language detection, and sync checking."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import statistics
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .models import SrtReport, SyncReport, SyncSample

SUBTITLE_EXTENSIONS = {".srt", ".vtt", ".ass", ".ssa", ".ttml", ".dfxp", ".sub"}

# Map common language tokens / ISO codes -> (ISO 639-2/B for mkvmerge/ffmpeg, Human Name)
LANG_MAP: dict[str, tuple[str, str]] = {
    "ar": ("ara", "Arabic"),
    "ara": ("ara", "Arabic"),
    "arb": ("ara", "Arabic"),
    "arabic": ("ara", "Arabic"),
    "en": ("eng", "English"),
    "eng": ("eng", "English"),
    "english": ("eng", "English"),
    "es": ("spa", "Spanish"),
    "spa": ("spa", "Spanish"),
    "spanish": ("spa", "Spanish"),
    "lat": ("spa", "Spanish"),
    "fr": ("fre", "French"),
    "fre": ("fre", "French"),
    "fra": ("fre", "French"),
    "french": ("fre", "French"),
    "de": ("ger", "German"),
    "ger": ("ger", "German"),
    "deu": ("ger", "German"),
    "german": ("ger", "German"),
    "it": ("ita", "Italian"),
    "ita": ("ita", "Italian"),
    "italian": ("ita", "Italian"),
    "pt": ("por", "Portuguese"),
    "por": ("por", "Portuguese"),
    "portuguese": ("por", "Portuguese"),
    "ru": ("rus", "Russian"),
    "rus": ("rus", "Russian"),
    "russian": ("rus", "Russian"),
    "ja": ("jpn", "Japanese"),
    "jpn": ("jpn", "Japanese"),
    "japanese": ("jpn", "Japanese"),
    "ko": ("kor", "Korean"),
    "kor": ("kor", "Korean"),
    "korean": ("kor", "Korean"),
    "zh": ("chi", "Chinese"),
    "chi": ("chi", "Chinese"),
    "zho": ("chi", "Chinese"),
    "chinese": ("chi", "Chinese"),
    "tr": ("tur", "Turkish"),
    "tur": ("tur", "Turkish"),
    "turkish": ("tur", "Turkish"),
    "hi": ("hin", "Hindi"),
    "hin": ("hin", "Hindi"),
    "hindi": ("hin", "Hindi"),
    "nl": ("dut", "Dutch"),
    "dut": ("dut", "Dutch"),
    "nld": ("dut", "Dutch"),
    "pl": ("pol", "Polish"),
    "pol": ("pol", "Polish"),
    "sv": ("swe", "Swedish"),
    "swe": ("swe", "Swedish"),
    "he": ("heb", "Hebrew"),
    "heb": ("heb", "Hebrew"),
    "el": ("gre", "Greek"),
    "gre": ("gre", "Greek"),
    "ell": ("gre", "Greek"),
    "fa": ("per", "Persian"),
    "per": ("per", "Persian"),
    "fas": ("per", "Persian"),
    "ur": ("urd", "Urdu"),
    "urd": ("urd", "Urdu"),
    "vi": ("vie", "Vietnamese"),
    "vie": ("vie", "Vietnamese"),
    "th": ("tha", "Thai"),
    "tha": ("tha", "Thai"),
    "id": ("ind", "Indonesian"),
    "ind": ("ind", "Indonesian"),
    "ms": ("may", "Malay"),
    "may": ("may", "Malay"),
    "und": ("und", "Undetermined"),
}

_TS_FLEX_RE = re.compile(
    r"^\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{1,2})(?:[,.](\d{1,3}))?\s*--?>\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{1,2})(?:[,.](\d{1,3}))?(?:\s+.*)?$"
)
_TAG_RE = re.compile(r"<[^>]+>|\{[^}]+\}")
_CTRL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")


@dataclass
class SrtCue:
    index: int
    start_ms: int
    end_ms: int
    lines: list[str]


def ms_to_srt_time(ms: int) -> str:
    ms = max(0, int(ms))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def parse_offset_ms(raw: str | int | float) -> int:
    """Postel's Law parser for user offsets like '+500ms', '-1.5s', '250', '1,500'."""
    if isinstance(raw, (int, float)):
        return int(round(raw))
    text = str(raw).strip().lower().replace(",", "").replace(" ", "")
    if not text:
        return 0
    if text.endswith("ms"):
        return int(round(float(text[:-2])))
    if text.endswith("s"):
        return int(round(float(text[:-1]) * 1000))
    return int(round(float(text)))


def parse_speed_factor(raw: str | int | float | None) -> float:
    """Parse speed multiplier such as 1.0, '1.0427', '25/23.976', or '23.976->25'."""
    if raw is None or raw == "":
        return 1.0
    if isinstance(raw, (int, float)):
        val = float(raw)
        return val if 0.5 <= val <= 2.0 else 1.0
    s = str(raw).strip().lower().replace("x", "").replace("×", "")
    if "->" in s:
        a, b = s.split("->", 1)
        try:
            fa, fb = float(a), float(b)
            if fa > 0 and fb > 0:
                return round(fb / fa, 6)
        except ValueError:
            return 1.0
    if "/" in s:
        a, b = s.split("/", 1)
        try:
            fa, fb = float(a), float(b)
            if fa > 0 and fb > 0:
                return round(fa / fb, 6)
        except ValueError:
            return 1.0
    try:
        val = float(s)
        return val if 0.5 <= val <= 2.0 else 1.0
    except ValueError:
        return 1.0


def resolve_language(lang_input: str, filename: str = "", sample_text: str = "") -> tuple[str, str]:
    """Resolve user language hint, filename tokens, or Unicode script into (iso639_2, name)."""
    cleaned = (lang_input or "").strip().lower()
    if cleaned and cleaned != "auto":
        if cleaned in LANG_MAP:
            return LANG_MAP[cleaned]
        if len(cleaned) == 3 and cleaned.isalpha():
            return cleaned, cleaned.upper()

    # 1. Check filename tokens (e.g., movie.ar.srt, [Arabic], _eng_)
    tokens = re.split(r"[^a-zA-Z]+", Path(filename).stem.lower())
    for tok in reversed(tokens):
        if tok in LANG_MAP and tok not in ("srt",):
            return LANG_MAP[tok]

    # 2. Unicode script fallback on actual subtitle dialogue
    if sample_text:
        ar_count = len(re.findall(r"[\u0600-\u06FF]", sample_text))
        he_count = len(re.findall(r"[\u0590-\u05FF]", sample_text))
        ru_count = len(re.findall(r"[\u0400-\u04FF]", sample_text))
        el_count = len(re.findall(r"[\u0370-\u03FF]", sample_text))
        th_count = len(re.findall(r"[\u0E00-\u0E7F]", sample_text))
        ja_count = len(re.findall(r"[\u3040-\u30FF]", sample_text))
        ko_count = len(re.findall(r"[\uAC00-\uD7AF]", sample_text))
        zh_count = len(re.findall(r"[\u4E00-\u9FFF]", sample_text))
        lat_count = len(re.findall(r"[A-Za-z]", sample_text))

        best = max(
            [
                (ar_count, ("ara", "Arabic")),
                (he_count, ("heb", "Hebrew")),
                (ru_count, ("rus", "Russian")),
                (el_count, ("gre", "Greek")),
                (th_count, ("tha", "Thai")),
                (ja_count, ("jpn", "Japanese")),
                (ko_count, ("kor", "Korean")),
                (zh_count, ("chi", "Chinese")),
                (lat_count, ("eng", "English")),
            ],
            key=lambda x: x[0],
        )
        if best[0] > 0:
            return best[1]

    return "und", "Undetermined"


def decode_subtitle_bytes(raw: bytes, lang_hint: str = "und") -> tuple[str, str, bool]:
    """Decode bytes with a smart fallback chain: BOM -> UTF-8 -> language-aware -> charset-normalizer -> legacy."""
    has_bom = raw.startswith(b"\xef\xbb\xbf")

    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace"), "utf-16", True

    if has_bom:
        try:
            return raw.decode("utf-8-sig"), "utf-8-sig", True
        except UnicodeDecodeError:
            pass

    try:
        return raw.decode("utf-8"), "utf-8", False
    except UnicodeDecodeError:
        pass

    # If filename/user hints a known legacy-codepage language, try its canonical encodings first
    hint_map: dict[str, tuple[str, ...]] = {
        "ara": ("cp1256",),
        "per": ("cp1256",),
        "heb": ("cp1255",),
        "rus": ("cp1251",),
        "gre": ("cp1253",),
        "tur": ("cp1254",),
        "vie": ("cp1258",),
        "jpn": ("cp932", "shift_jis", "euc_jp"),
        "kor": ("cp949", "euc_kr"),
        "chi": ("gb18030", "big5"),
        "eng": ("cp1252",),
        "spa": ("cp1252",),
        "fre": ("cp1252",),
        "ger": ("cp1252",),
    }
    for enc in hint_map.get(lang_hint, ()):
        try:
            return raw.decode(enc), enc, False
        except UnicodeDecodeError:
            pass

    # Try charset-normalizer if installed (guard against low-coherence CJK false positives on short inputs)
    try:
        from charset_normalizer import from_bytes

        results = from_bytes(raw)
        best = results.best()
        if best and best.encoding:
            if best.encoding in ("cp932", "shift_jis", "euc_jp", "gbk", "gb18030", "big5", "euc_kr") and best.coherence < 0.72:
                pass
            else:
                return str(best), best.encoding, False
    except Exception:
        pass

    # Legacy subtitle fallback chain
    for enc in ("cp1256", "cp1252", "cp1251", "cp1254", "cp932", "gb18030", "cp949", "iso-8859-1"):
        try:
            return raw.decode(enc), enc, False
        except UnicodeDecodeError:
            continue

    return raw.decode("utf-8", errors="replace"), "utf-8-lossy", False


def _parse_ts_groups(h: Optional[str], m: str, s: str, ms: Optional[str]) -> int:
    hours = int(h) if h is not None else 0
    mins = int(m)
    secs = int(s)
    millis = int((ms or "0").ljust(3, "0")[:3])
    return ((hours * 3600) + (mins * 60) + secs) * 1000 + millis


_SDH_BRACKET_RE = re.compile(r"\[[^\]\n]{1,60}\]|\([^)\n]{1,60}\)|♪[^♪\n]*♪")
_SDH_SPEAKER_RE = re.compile(r"^(?:[A-Z][A-Z0-9 .'-]{1,22}):\s+")
_AD_KEYWORDS_RE = re.compile(
    r"(?i)(opensubtitles|subscene|subsource|yify|yts\.|addic7ed|podnapisi|allsubs|titlovi|synced\s+by|encoded\s+by|ripped\s+by|downloaded\s+from|support\s+us\s+and\s+become\s+vip|advertise\s+your\s+product|ترجمة\s*(?:و)?\s*(?:تعديل|مزامنة|إعداد)?\s*:?)"
)


def _clean_sdh_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    for ln in lines:
        cleaned = _SDH_BRACKET_RE.sub("", ln)
        cleaned = _SDH_SPEAKER_RE.sub("", cleaned).strip(" -–—\t")
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        if cleaned:
            out.append(cleaned)
    return out


def _is_ad_cue(lines: list[str]) -> bool:
    joined = " ".join(lines)
    if _AD_KEYWORDS_RE.search(joined):
        return True
    if re.search(r"(?i)(https?://|www\.)", joined):
        return True
    return False


_RTL_CHAR_RE = re.compile(r"[\u0590-\u06FF\u0750-\u077F\u08A0-\u08FF]")


def _fix_rtl_bidi_lines(lines: list[str]) -> list[str]:
    """Fix misplaced leading ASCII punctuation and enforce RTL embedding for Arabic/Hebrew cues."""
    out: list[str] = []
    for ln in lines:
        s = ln.strip().lstrip("\u202a\u202b\u202c\u200e\u200f")
        if _RTL_CHAR_RE.search(s):
            # Move legacy misplaced leading punctuation (e.g. '.مرحبا') to logical end ('مرحبا.')
            m = re.match(r"^([.!?,؛؟]+)\s*([^\s.!?,؛؟].*)$", s)
            if m and not s.endswith((".", "!", "?", "؟", "؛")):
                s = f"{m.group(2)}{m.group(1)}"
            s = f"\u202b{s}\u202c"
        out.append(s)
    return out


def parse_and_repair_srt(
    text: str,
    offset_ms: int = 0,
    strip_tags: bool = False,
    clean_sdh: bool = False,
    clean_ads: bool = False,
    speed_factor: float = 1.0,
    fix_rtl_bidi: bool = False,
) -> tuple[list[SrtCue], int, int, list[str]]:
    """Parse SRT text liberally (Postel's Law), repair malformed cues, apply speed/offset & cleaners."""
    warnings: list[str] = []
    cleaned = _CTRL_RE.sub("", text.lstrip("\ufeff"))
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")

    blocks = re.split(r"\n{2,}", cleaned.strip())
    cues: list[SrtCue] = []
    repaired = 0
    stripped_tags = 0
    removed_sdh_ads = 0

    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip() != ""]
        if not lines:
            continue

        # Locate timestamp line (could be line 0 if index was omitted, or line 1)
        ts_idx = -1
        ts_match = None
        for idx in range(min(2, len(lines))):
            m = _TS_FLEX_RE.match(lines[idx])
            if m:
                ts_idx = idx
                ts_match = m
                break

        if ts_match is None:
            repaired += 1
            continue

        if ts_idx == 0:
            repaired += 1  # Missing numeric index line

        raw_ts = lines[ts_idx]
        if "." in raw_ts or len(raw_ts.split(":")) < 5:
            repaired += 1

        spd = speed_factor if 0.5 <= speed_factor <= 2.0 else 1.0
        start_ms = int(round(_parse_ts_groups(*ts_match.group(1, 2, 3, 4)) * spd)) + offset_ms
        end_ms = int(round(_parse_ts_groups(*ts_match.group(5, 6, 7, 8)) * spd)) + offset_ms

        if start_ms < 0:
            start_ms = 0
            repaired += 1
        if end_ms <= start_ms:
            end_ms = start_ms + 1000
            repaired += 1

        body_lines = lines[ts_idx + 1 :]
        if not body_lines:
            repaired += 1
            continue

        if clean_ads and _is_ad_cue(body_lines):
            removed_sdh_ads += 1
            continue

        if clean_sdh:
            body_lines = _clean_sdh_lines(body_lines)
            if not body_lines:
                removed_sdh_ads += 1
                continue

        if strip_tags:
            new_body: list[str] = []
            for ln in body_lines:
                matches = _TAG_RE.findall(ln)
                if matches:
                    stripped_tags += len(matches)
                    ln = _TAG_RE.sub("", ln).strip()
                if ln:
                    new_body.append(ln)
            body_lines = new_body
            if not body_lines:
                continue

        if fix_rtl_bidi:
            body_lines = _fix_rtl_bidi_lines(body_lines)

        cues.append(
            SrtCue(
                index=len(cues) + 1,
                start_ms=start_ms,
                end_ms=end_ms,
                lines=body_lines,
            )
        )

    if not cues:
        warnings.append("No valid subtitle cues found in file.")
    elif repaired > 0:
        warnings.append(f"Auto-repaired {repaired} cue formatting or timing issue(s).")
    if removed_sdh_ads > 0:
        warnings.append(f"Cleaned {removed_sdh_ads} SDH/promo cue(s).")

    return cues, repaired, stripped_tags, warnings


def format_srt(cues: list[SrtCue]) -> str:
    """Serialize cues to canonical UTF-8 SRT format with sequential indices."""
    out_blocks: list[str] = []
    for i, cue in enumerate(cues, start=1):
        ts = f"{ms_to_srt_time(cue.start_ms)} --> {ms_to_srt_time(cue.end_ms)}"
        out_blocks.append(f"{i}\n{ts}\n" + "\n".join(cue.lines))
    return "\n\n".join(out_blocks) + ("\n" if out_blocks else "")


def _convert_vtt_to_srt_text(text: str) -> str:
    """Convert WebVTT text into SRT-like blocks for unified parsing."""
    lines = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out_lines: list[str] = []
    skip_block = False
    for ln in lines:
        s = ln.strip()
        if s.startswith("WEBVTT") or s.startswith("NOTE") or s.startswith("STYLE") or s.startswith("REGION"):
            skip_block = s.startswith(("NOTE", "STYLE", "REGION"))
            continue
        if skip_block:
            if s == "":
                skip_block = False
            continue
        # Strip WebVTT cue position settings after timestamp
        m = _TS_FLEX_RE.match(ln)
        if m:
            ln = re.sub(r"(\d{1,2}:\d{2}(?::\d{2})?[,.]\d{1,3}\s*-->\s*\d{1,2}:\d{2}(?::\d{2})?[,.]\d{1,3}).*$", r"\1", ln)
        out_lines.append(ln)
    return "\n".join(out_lines)


def _ass_time_to_ms(ts: str) -> int:
    m = re.match(r"(\d+):(\d{2}):(\d{2})[.:](\d{2,3})", ts.strip())
    if not m:
        return 0
    h, mn, s, frac = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
    ms = int(frac) * 10 if len(frac) == 2 else int(frac)
    return ((h * 3600) + (mn * 60) + s) * 1000 + ms


def _convert_ass_to_srt_text(text: str) -> str:
    """Convert ASS/SSA Dialogue lines into SRT blocks."""
    lines = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[str] = []
    idx = 1
    for ln in lines:
        if not ln.startswith("Dialogue:"):
            continue
        parts = ln.split(",", 9)
        if len(parts) < 10:
            continue
        start_ms = _ass_time_to_ms(parts[1])
        end_ms = _ass_time_to_ms(parts[2])
        body = parts[9].replace(r"\N", "\n").replace(r"\n", "\n").replace(r"\h", " ")
        body = re.sub(r"\{[^}]*\}", "", body).strip()
        if body:
            blocks.append(f"{idx}\n{ms_to_srt_time(start_ms)} --> {ms_to_srt_time(end_ms)}\n{body}")
            idx += 1
    return "\n\n".join(blocks)


def extract_subtitles_from_zip(zip_path: str) -> list[str]:
    """Safely extract subtitle files (.srt/.vtt/.ass/.ssa) into an mtime-keyed cache folder."""
    zp = Path(zip_path).resolve()
    out_paths: list[str] = []
    try:
        mtime = zp.stat().st_mtime_ns
    except OSError:
        mtime = 0
    tag = hashlib.sha1(f"{zp}:{mtime}".encode("utf-8", errors="replace")).hexdigest()[:10]
    cache_root = Path(tempfile.gettempdir()) / "srt_embedder_zip_cache" / f"{zp.stem}_{tag}"
    if cache_root.exists():
        shutil.rmtree(cache_root, ignore_errors=True)
    cache_root.mkdir(parents=True, exist_ok=True)
    max_total_bytes = 25 * 1024 * 1024  # 25 MB safety limit
    accumulated = 0
    used_names: set[str] = set()

    with zipfile.ZipFile(zp, "r") as zf:
        for idx, info in enumerate(zf.infolist()):
            if info.is_dir():
                continue
            member_name = Path(info.filename).name
            if not member_name or Path(member_name).suffix.lower() not in SUBTITLE_EXTENSIONS:
                continue
            if info.file_size > max_total_bytes or accumulated + info.file_size > max_total_bytes:
                continue
            accumulated += info.file_size

            # Prevent collisions when nested folders inside the zip share the same subtitle filename
            out_name = member_name
            if out_name.lower() in used_names:
                out_name = f"{idx + 1}_{member_name}"
            used_names.add(out_name.lower())

            target = (cache_root / out_name).resolve()
            if not str(target).startswith(str(cache_root.resolve())):
                continue  # Zip-slip guard
            with zf.open(info, "r") as src:
                target.write_bytes(src.read())
            out_paths.append(str(target))
    return out_paths


def _parse_ttml_time(raw: str, tick_rate: float = 10_000_000.0) -> Optional[int]:
    s = (raw or "").strip()
    if not s:
        return None
    if s.endswith("t") and s[:-1].replace(".", "", 1).isdigit():
        return int(round((float(s[:-1]) / max(1.0, tick_rate)) * 1000.0))
    if s.endswith("ms") and s[:-2].replace(".", "", 1).isdigit():
        return int(round(float(s[:-2])))
    if s.endswith("s") and s[:-1].replace(".", "", 1).isdigit():
        return int(round(float(s[:-1]) * 1000.0))
    m = re.match(r"^(\d+):(\d{2}):(\d{2})(?:[.,](\d+))?$", s)
    if m:
        return _parse_ts_groups(m.group(1), m.group(2), m.group(3), m.group(4))
    return None


def _convert_ttml_to_srt_text(text: str) -> str:
    """Convert W3C TTML / Netflix DFXP XML captions to SRT blocks."""
    cleaned = text.lstrip("\ufeff").strip()
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError:
        return text

    tick_rate = 10_000_000.0
    for k, v in root.attrib.items():
        if k.endswith("tickRate") and str(v).isdigit():
            tick_rate = float(v)
            break

    blocks: list[str] = []
    idx = 1
    for elem in root.iter():
        tag = elem.tag.split("}")[-1].lower()
        if tag != "p":
            continue
        begin = _parse_ttml_time(elem.attrib.get("begin", ""), tick_rate)
        end = _parse_ttml_time(elem.attrib.get("end", ""), tick_rate)
        if begin is None:
            continue
        if end is None:
            dur = _parse_ttml_time(elem.attrib.get("dur", ""), tick_rate)
            end = begin + (dur if dur is not None else 2000)

        parts: list[str] = []
        if elem.text:
            parts.append(elem.text)
        for child in elem:
            ctag = child.tag.split("}")[-1].lower()
            if ctag == "br":
                parts.append("\n")
            elif child.text:
                parts.append(child.text)
            if child.tail:
                parts.append(child.tail)
        body = "\n".join(ln.strip() for ln in "".join(parts).splitlines() if ln.strip())
        if body:
            blocks.append(f"{idx}\n{ms_to_srt_time(begin)} --> {ms_to_srt_time(end)}\n{body}")
            idx += 1
    return "\n\n".join(blocks)


def _convert_microdvd_to_srt_text(text: str, fps: float = 23.976) -> str:
    """Convert MicroDVD {start_frame}{end_frame} text.sub files using video fps."""
    rate = fps if fps and fps > 1.0 else 23.976
    blocks: list[str] = []
    idx = 1
    for ln in text.lstrip("\ufeff").splitlines():
        m = re.match(r"^\s*\{(\d+)\}\{(\d+)\}(.+)$", ln)
        if not m:
            continue
        start_f, end_f, body = int(m.group(1)), int(m.group(2)), m.group(3)
        # Skip MicroDVD FPS header cue {1}{1}23.976
        if start_f <= 1 and end_f <= 1 and re.match(r"^\d+(?:\.\d+)?$", body.strip()):
            rate = float(body.strip())
            continue
        start_ms = int(round((start_f / rate) * 1000.0))
        end_ms = max(start_ms + 500, int(round((end_f / rate) * 1000.0)))
        clean_body = re.sub(r"\{[^}]+\}", "", body.replace("|", "\n")).strip()
        if clean_body:
            blocks.append(f"{idx}\n{ms_to_srt_time(start_ms)} --> {ms_to_srt_time(end_ms)}\n{clean_body}")
            idx += 1
    return "\n\n".join(blocks)


def inspect_srt(
    path: str,
    lang_override: str = "auto",
    offset_ms: int = 0,
    strip_tags: bool = False,
    clean_sdh: bool = False,
    clean_ads: bool = False,
    speed_factor: float = 1.0,
    fix_rtl_bidi: bool = False,
    video_fps: float = 23.976,
) -> SrtReport:
    """Inspect, decode, convert (.vtt/.ass/.ttml/.dfxp/.sub), repair, and normalize to SRT."""
    p = Path(path)
    ext = p.suffix.lower().lstrip(".") or "srt"
    raw = p.read_bytes()
    hint_code, _ = resolve_language(lang_override, p.name, "")
    text, encoding, has_bom = decode_subtitle_bytes(raw, lang_hint=hint_code)

    conversion_note = ""
    if ext == "vtt":
        text = _convert_vtt_to_srt_text(text)
        conversion_note = "Converted WebVTT (.vtt) to SRT."
    elif ext in ("ass", "ssa"):
        text = _convert_ass_to_srt_text(text)
        conversion_note = f"Converted {ext.upper()} subtitle to SRT."
    elif ext in ("ttml", "dfxp"):
        text = _convert_ttml_to_srt_text(text)
        conversion_note = f"Converted {ext.upper()} XML captions to SRT."
    elif ext == "sub":
        converted = _convert_microdvd_to_srt_text(text, fps=video_fps)
        if converted:
            text = converted
            conversion_note = f"Converted MicroDVD (.sub @ {video_fps:.3f} fps) to SRT."

    cues, repaired, stripped_tags, warnings = parse_and_repair_srt(
        text,
        offset_ms=offset_ms,
        strip_tags=strip_tags,
        clean_sdh=clean_sdh,
        clean_ads=clean_ads,
        speed_factor=speed_factor,
        fix_rtl_bidi=fix_rtl_bidi,
    )
    if conversion_note:
        warnings.insert(0, conversion_note)

    sample_text = " ".join(" ".join(c.lines) for c in cues[:40])
    lang_code, lang_name = resolve_language(lang_override, p.name, sample_text)

    if encoding not in ("utf-8", "utf-8-sig"):
        warnings.append(f"Converted legacy encoding '{encoding}' to clean UTF-8.")

    normalized = format_srt(cues)
    first_ms = cues[0].start_ms if cues else 0
    last_ms = cues[-1].end_ms if cues else 0

    preview_list = cues[:3] + (cues[-2:] if len(cues) > 5 else cues[3:])
    sample_cues = [
        {
            "index": c.index,
            "start": ms_to_srt_time(c.start_ms),
            "end": ms_to_srt_time(c.end_ms),
            "text": " ".join(c.lines),
        }
        for c in preview_list
    ]

    return SrtReport(
        path=str(p),
        filename=p.name,
        size_bytes=len(raw),
        encoding_detected=encoding,
        has_bom=has_bom,
        cue_count=len(cues),
        repaired_cues=repaired,
        stripped_tags_count=stripped_tags,
        detected_lang_code=lang_code,
        detected_lang_name=lang_name,
        first_timestamp_ms=first_ms,
        last_timestamp_ms=last_ms,
        source_format=ext,
        sample_cues=sample_cues,
        warnings=warnings,
        normalized_content=normalized,
    )


_COMMON_FPS_RATIOS = (
    25.0 / 23.976,
    23.976 / 25.0,
    24.0 / 23.976,
    23.976 / 24.0,
    25.0 / 24.0,
    24.0 / 25.0,
)


def _fit_drift_correction(pairs_ms: list[tuple[int, int]]) -> tuple[float, int]:
    """Fit speed and offset_ms (ref_ms ≈ tgt_ms * speed + offset_ms) for FPS drift correction."""
    if len(pairs_ms) < 3:
        return 1.0, 0
    xs = [float(tgt) for _, tgt in pairs_ms]
    ys = [float(ref) for ref, _ in pairs_ms]
    mx = statistics.mean(xs)
    my = statistics.mean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom <= 1e-6:
        return 1.0, 0
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    if not (0.9 <= slope <= 1.12):
        return 1.0, 0

    for ratio in _COMMON_FPS_RATIOS:
        if abs(slope - ratio) <= 0.0025:
            slope = ratio
            break

    slope = round(slope, 6)
    offset = int(round(my - slope * mx))
    return slope, offset


def get_preview_cues(
    path: str,
    offset_ms: int = 0,
    speed_factor: float = 1.0,
    strip_tags: bool = False,
    clean_sdh: bool = False,
    clean_ads: bool = False,
    limit: int = 2000,
) -> list[dict]:
    """Return parsed cues (up to limit) for full UI preview and search."""
    rep = inspect_srt(
        path,
        offset_ms=offset_ms,
        speed_factor=speed_factor,
        strip_tags=strip_tags,
        clean_sdh=clean_sdh,
        clean_ads=clean_ads,
    )
    cues, _, _, _ = parse_and_repair_srt(rep.normalized_content)
    return [
        {
            "index": c.index,
            "start": ms_to_srt_time(c.start_ms),
            "end": ms_to_srt_time(c.end_ms),
            "text": " / ".join(c.lines),
        }
        for c in cues[:limit]
    ]


def compare_srt_with_reference(
    ref_srt_text: str,
    target_srt_text: str,
    ref_stream_index: int = -1,
    ref_language: str = "",
) -> SyncReport:
    """Compare an external SRT against an embedded reference subtitle track."""
    ref_cues, _, _, _ = parse_and_repair_srt(ref_srt_text)
    tgt_cues, _, _, _ = parse_and_repair_srt(target_srt_text)

    if len(ref_cues) < 5 or len(tgt_cues) < 5:
        return SyncReport(
            comparable=False,
            reason="Not enough subtitle cues to compare timing reliably.",
        )

    duration_min = max(1, int(ref_cues[-1].start_ms / 60_000))
    step = max(1, duration_min // 9)
    checkpoints = [m for m in range(1, duration_min + 1, step)][:9]
    if not checkpoints:
        checkpoints = [1]

    samples: list[SyncSample] = []
    matched_ms_pairs: list[tuple[int, int]] = []
    for minute in checkpoints:
        target_ms = minute * 60_000
        ref_cue = next((c for c in ref_cues if c.start_ms >= target_ms), None)
        if not ref_cue:
            continue
        nearest = min(tgt_cues, key=lambda c: abs(c.start_ms - ref_cue.start_ms))
        diff = nearest.start_ms - ref_cue.start_ms
        # Ignore huge outliers caused by unmatched intro/outro credit cues
        if abs(diff) > 120_000:
            continue
        matched_ms_pairs.append((ref_cue.start_ms, nearest.start_ms))
        samples.append(
            SyncSample(
                minute=minute,
                ref_time=ms_to_srt_time(ref_cue.start_ms),
                ref_text=" ".join(ref_cue.lines)[:44],
                srt_time=ms_to_srt_time(nearest.start_ms),
                srt_text=" ".join(nearest.lines)[:44],
                diff_ms=diff,
            )
        )

    if not samples:
        return SyncReport(
            comparable=False,
            reason="Could not align cues within a 2-minute tolerance window.",
        )

    diffs = [s.diff_ms for s in samples]
    median_diff = int(round(statistics.median(diffs)))
    min_diff = min(diffs)
    max_diff = max(diffs)
    drift = max_diff - min_diff

    rec_speed = 1.0
    rec_offset = 0

    if drift > 800 and len(samples) >= 3:
        verdict = "drifting"
        fit_speed, fit_offset = _fit_drift_correction(matched_ms_pairs)
        if abs(fit_speed - 1.0) >= 0.001:
            fitted_errors = [abs((tgt * fit_speed + fit_offset) - ref) for ref, tgt in matched_ms_pairs]
            if max(fitted_errors) < drift * 0.5:
                rec_speed = fit_speed
                rec_offset = fit_offset
    elif abs(median_diff) <= 150:
        verdict = "in-sync"
    elif median_diff > 150:
        verdict = "late"
        rec_offset = -median_diff
    else:
        verdict = "early"
        rec_offset = -median_diff

    return SyncReport(
        comparable=True,
        ref_stream_index=ref_stream_index,
        ref_language=ref_language,
        median_diff_ms=median_diff,
        min_diff_ms=min_diff,
        max_diff_ms=max_diff,
        drift_ms=drift,
        verdict=verdict,
        recommended_offset_ms=rec_offset,
        recommended_speed=rec_speed,
        samples=samples,
    )


def _ms_to_ass_time(ms: int) -> str:
    ms = max(0, int(ms))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    centis = millis // 10
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centis:02d}"


def merge_dual_subtitles(
    bottom_path: str,
    top_path: str,
    output_path: Optional[str] = None,
    bottom_offset_ms: int = 0,
    top_offset_ms: int = 0,
) -> tuple[str, int]:
    """Merge two subtitle files (bottom primary + top secondary) into a styled bilingual .ass file."""
    rep_bot = inspect_srt(bottom_path, offset_ms=bottom_offset_ms, strip_tags=True)
    rep_top = inspect_srt(top_path, offset_ms=top_offset_ms, strip_tags=True)

    cues_bot, _, _, _ = parse_and_repair_srt(rep_bot.normalized_content)
    cues_top, _, _, _ = parse_and_repair_srt(rep_top.normalized_content)

    bp = Path(bottom_path)
    out = Path(output_path) if output_path else bp.with_name(f"{bp.stem}.bilingual.ass")

    if out.suffix.lower() == ".srt":
        combined = sorted(
            [(c.start_ms, c.end_ms, c.lines) for c in cues_bot]
            + [(c.start_ms, c.end_ms, c.lines) for c in cues_top],
            key=lambda x: (x[0], x[1]),
        )
        out_cues = [SrtCue(index=i, start_ms=s, end_ms=e, lines=lns) for i, (s, e, lns) in enumerate(combined, 1)]
        out.write_text(format_srt(out_cues), encoding="utf-8")
        return str(out), len(out_cues)

    ass_header = "\n".join([
        "[Script Info]",
        "Title: Bilingual Merged Subtitle",
        "ScriptType: v4.00+",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "WrapStyle: 0",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Bottom,Arial,58,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,40,40,48,1",
        "Style: Top,Arial,44,&H0060D5FF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2.5,1,8,40,40,36,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ])

    events: list[tuple[int, str]] = []
    for c in cues_bot:
        txt = r"\N".join(c.lines)
        events.append((c.start_ms, f"Dialogue: 0,{_ms_to_ass_time(c.start_ms)},{_ms_to_ass_time(c.end_ms)},Bottom,,0,0,0,,{txt}"))
    for c in cues_top:
        txt = r"\N".join(c.lines)
        events.append((c.start_ms, f"Dialogue: 0,{_ms_to_ass_time(c.start_ms)},{_ms_to_ass_time(c.end_ms)},Top,,0,0,0,,{txt}"))

    events.sort(key=lambda x: x[0])
    content = ass_header + "\n" + "\n".join(line for _, line in events) + "\n"
    out.write_text(content, encoding="utf-8")
    return str(out), len(events)
