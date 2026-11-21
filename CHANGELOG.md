# Changelog

All notable changes to **SRT Embedder** will be documented in this file.

## [1.2.0] - 2026-11-21

### Added
- **Lean Global Packaging & Split Extras:** Split GUI/tray packages (`pywebview`, `pystray`, `pillow`) into `[gui]` extra so headless/NAS/server users can install the CLI cleanly (`pip install srt-embedder` or `pip install "srt-embedder[gui]"`). Added WinGet, Scoop, and Homebrew manifest templates under `packaging/`, plus opt-in GitHub Release update checking (`doctor --check-update` & Settings button).
- **macOS & Linux Native Parity:** Added macOS Finder Quick Action (`~/Library/Services`) + LaunchAgents + Homebrew discovery/bootstrapper + `osascript` notifications, and Linux `.desktop` + Nautilus script + `~/.config/autostart` + `notify-send` support.
- **Global Formats, CJK/RTL & ASS Font Attachments:** Added `.ttml`/`.dfxp` XML and MicroDVD `.sub` frame-based conversion, CJK (`cp932`, `cp949`, `gb18030`, `big5`) + Hebrew/Greek/Vietnamese decoding, `--fix-rtl` BiDi punctuation fix, automatic `.ttf`/`.otf` font attachment muxing into `.mkv` for `.ass` tracks, and UI localization (`en`, `ar` RTL, `es`, `fr`).
- **Library & Backup Automation Rules:** Added Plex/Clean output naming templates (`--naming`), foreign-audio smart default subtitle rule (`--prefer-sub`), and automatic age (`14d`) / size (`10 GB`) Undo backup pruning.

## [1.1.0] - 2026-11-21

### Added
- **Drop-Only Companion Auto-Discovery & Episode Pairing:** Automatically discovers matching `.srt`/`.vtt`/`.ass`/`.zip` subtitles in the video's folder and `Subs/` / `Subtitles/` subfolders, plus explicit `S01E02` / `1x02` episode-aware batch matching.
- **Audio Speech-Onset VAD Sync & Auto-Sync:** Falls back to FFmpeg `silencedetect` speech-activity alignment when a video has no embedded text subtitle track, and supports `--auto-sync` / GUI **Auto-sync timing before embed** for shifts `>= 250ms`.
- **Persistent Automation Profiles & Express Drop-to-Go:** Built-in presets (`cinema-default`, `arabic-rtl`, `tv-compat`, `archive-inplace`) and custom user profiles shared across GUI, CLI (`profile`), Watch Daemon, and Explorer actions, plus 1-step **Express Drop-to-Go** mode.
- **Background Watch Daemon, Tray & Startup:** Persistent `watch_manifest.json` across restarts, post-embed subtitle cleanup (`_archived_subs/` or Recycle Bin), `--tray` system tray daemon with desktop notifications, and optional `HKCU` Windows sign-in startup registration.
- **Headless Quick-Embed & Library Audit:** New `quick` (`q` / `auto`) command and Explorer right-click **"Quick-Embed Subtitles (Auto Profile)"** verb on files & folders, plus `audit` library scanner (`ready` / `pairable` / `missing` + `--embed-pairable`).
- **Expanded Verification:** Added `scripts/ui_smoke_test.py` and unit/CLI coverage across `cli.py`, `gui.py` (`BridgeApi`), `shell_ext.py`, `tray.py`, and `bootstrap.py`.
- **Linear FPS Drift Correction:** `sync-check` and `--auto-sync` now fit `23.976 ↔ 24 ↔ 25 fps` speed ratios + offsets (`--speed` in CLI, 1-click fix in Sync modal).
- **Polish & Lifecycle Fixes:** Replaced `window.prompt()` with an inline Profile Manager modal (save + delete custom profiles), added full-file searchable Cue Preview, live Audit row progress + per-job Batch language/Studio controls, Hardsub multi-track/font advisories, collision-safe `.zip` extraction caching, single-instance tray lifecycle on restore/close, cross-platform `open`/`xdg-open` reveal & playback, and stale backup pruning in `undo.py`.

### Security
- Verified `gyan.dev` FFmpeg downloads against `.sha256` digests prior to extraction in `bootstrap.py`.
- Eliminated PowerShell command string interpolation in `tray.notify_desktop()` by passing notification text via process environment variables.

## [1.0.0] - 2026-11-20

### Added
- **Unified Desktop Studio GUI (`pywebview`) & Fast CLI:** Launch GUI by default or run `embed`, `batch`, `inspect`, `sync-check`, `undo`, `shell`, and `doctor` from the terminal.
- **Smart Container & Engine Fallback Chain:** `mkvmerge` -> `ffmpeg` -> safe stream map -> HTML tag stripping -> lossless `.mkv` container fallback, plus UTF-8 hardsub burn-in with configurable CRF and preset.
- **Subtitle Encoding & Repair:** Automatic detection and conversion of `UTF-8`, `UTF-8 BOM`, `UTF-16`, `Windows-1256` (Arabic), `Windows-1252`, `Windows-1251`, and `ISO-8859-1`; automatic `.vtt`, `.ass`/`.ssa`, and `.zip` subtitle archive ingestion; cue preview and malformed cue repair.
- **Timing Sync Checker:** Compares external subtitles against embedded reference tracks to detect offset and framerate drift with 1-click offset repair.
- **Atomic Staging & Instant Undo:** Same-filesystem `.bak` transactions with `ffprobe` post-mux verification, Undo Toast, Backup Manager modal, and `undo --purge`.
- **Feelcraft Motion & Ergonomics:** ~280–290ms `cubic-bezier(0.22, 1, 0.36, 1)` transitions, sliding segmented mode indicator, `Ctrl+K` Command Palette, Dark/Light studio themes, and `HKCU` Windows Explorer right-click integration.
- **Packaging & CI/CD:** Multi-res `.ico`/`.png`/`.svg` assets, PyInstaller spec + release build script, Inno Setup `.iss` installer script, and GitHub Actions CI/Release workflows.
