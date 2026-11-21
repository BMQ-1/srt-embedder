"""Core data models for SRT Embedder."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


MuxMode = Literal["auto", "mkvmerge", "ffmpeg", "soft-mkv", "hardsub"]
ExistingSubsPolicy = Literal["keep", "replace-same-lang", "replace-all"]
HwEncoder = Literal["auto", "libx264", "h264_nvenc", "hevc_nvenc", "h264_qsv", "h264_amf"]
SubCleanupPolicy = Literal["keep", "archive", "trash"]


@dataclass
class HardsubStyle:
    font_name: str = "Arial"
    font_size: int = 24
    primary_color: str = "#FFFFFF"
    outline_color: str = "#000000"
    outline_width: int = 2
    margin_v: int = 28
    encoder: HwEncoder = "auto"


@dataclass
class StreamInfo:
    index: int
    codec_type: str  # video | audio | subtitle | data
    codec_name: str
    language: str = "und"
    title: str = ""
    is_default: bool = False
    is_forced: bool = False
    extra: str = ""


@dataclass
class VideoInfo:
    path: str
    filename: str
    container: str
    size_bytes: int
    duration_sec: float
    width: int = 0
    height: int = 0
    fps: str = ""
    color_space: str = ""
    color_primaries: str = ""
    color_transfer: str = ""
    color_range: str = ""
    streams: list[StreamInfo] = field(default_factory=list)

    @property
    def video_streams(self) -> list[StreamInfo]:
        return [s for s in self.streams if s.codec_type == "video"]

    @property
    def audio_streams(self) -> list[StreamInfo]:
        return [s for s in self.streams if s.codec_type == "audio"]

    @property
    def subtitle_streams(self) -> list[StreamInfo]:
        return [s for s in self.streams if s.codec_type == "subtitle"]


@dataclass
class SrtReport:
    path: str
    filename: str
    size_bytes: int
    encoding_detected: str
    has_bom: bool
    cue_count: int
    repaired_cues: int
    stripped_tags_count: int
    detected_lang_code: str
    detected_lang_name: str
    first_timestamp_ms: int
    last_timestamp_ms: int
    source_format: str = "srt"
    sample_cues: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    normalized_content: str = ""


@dataclass
class SubtitleInput:
    path: str
    lang_code: str = "auto"
    title: str = ""
    is_default: bool = True
    is_forced: bool = False
    offset_ms: int = 0
    speed_factor: float = 1.0
    strip_html_tags: bool = False
    clean_sdh: bool = False
    clean_ads: bool = False
    fix_rtl_bidi: bool = False


@dataclass
class EmbedOptions:
    video_path: str
    subtitles: list[SubtitleInput]
    output_path: Optional[str] = None
    mode: MuxMode = "auto"
    existing_subs: ExistingSubsPolicy = "keep"
    fallback_to_mkv: bool = True
    replace_original: bool = False
    create_backup: bool = True
    dry_run: bool = False
    strip_tags_on_fallback: bool = True
    auto_sync: bool = False
    auto_sync_threshold_ms: int = 250
    output_template: str = "default"  # default | plex | clean | custom string with {stem},{lang}
    attach_fonts: bool = True
    prefer_sub_lang_when_foreign_audio: str = ""
    crf: int = 18
    preset: str = "medium"
    hardsub_style: Optional[HardsubStyle] = None


@dataclass
class EmbedResult:
    success: bool
    video_path: str
    output_path: str
    engine_used: str
    fallbacks_triggered: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    elapsed_sec: float = 0.0
    output_size_bytes: int = 0
    subtitle_streams_after: int = 0
    undo_tx_id: Optional[str] = None
    dry_run: bool = False
    command_preview: list[str] = field(default_factory=list)


@dataclass
class SyncSample:
    minute: int
    ref_time: str
    ref_text: str
    srt_time: str
    srt_text: str
    diff_ms: int


@dataclass
class SyncReport:
    comparable: bool
    reason: str = ""
    method: str = "embedded-sub"  # embedded-sub | audio-vad
    confidence: float = 1.0
    ref_stream_index: int = -1
    ref_language: str = ""
    median_diff_ms: int = 0
    min_diff_ms: int = 0
    max_diff_ms: int = 0
    drift_ms: int = 0
    verdict: str = ""  # in-sync | early | late | drifting
    recommended_offset_ms: int = 0
    recommended_speed: float = 1.0
    samples: list[SyncSample] = field(default_factory=list)


@dataclass
class LibraryAuditItem:
    video_path: str
    filename: str
    status: str  # ready | pairable | missing
    embedded_langs: list[str] = field(default_factory=list)
    companion_subs: list[str] = field(default_factory=list)


@dataclass
class LibraryAuditReport:
    directory: str
    target_lang: str
    total_videos: int
    ready_count: int
    pairable_count: int
    missing_count: int
    items: list[LibraryAuditItem] = field(default_factory=list)
