<p align="center">
  <img src="docs/media/banner.svg" alt="SRT Embedder — Lossless Subtitle Muxing & Studio GUI" width="100%" />
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-1.2.0-F59E0B?style=flat-square&labelColor=0B0D10" alt="Version 1.2.0" />
  <img src="https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&labelColor=0B0D10" alt="Python 3.10+" />
  <img src="https://img.shields.io/badge/engines-mkvmerge%20%2B%20ffmpeg-22C55E?style=flat-square&labelColor=0B0D10" alt="mkvmerge + ffmpeg" />
  <img src="https://img.shields.io/badge/platforms-Win%20%7C%20macOS%20%7C%20Linux-38BDF8?style=flat-square&labelColor=0B0D10" alt="Windows, macOS, Linux" />
  <img src="https://img.shields.io/badge/UI-Feelcraft%20Studio-CBD5E1?style=flat-square&labelColor=0B0D10" alt="Feelcraft Studio" />
  <img src="https://img.shields.io/badge/license-MIT-94A3B8?style=flat-square&labelColor=0B0D10" alt="MIT License" />
</p>

<p align="center">
  A cross-platform <strong>Desktop Studio GUI &amp; CLI</strong> for embedding <code>.srt</code>, <code>.vtt</code>, <code>.ass</code>/<code>.ssa</code>, <code>.ttml</code>/<code>.dfxp</code>, MicroDVD <code>.sub</code>, and <code>.zip</code> subtitles into videos.<br/>
  Built with lossless container muxing, GPU-accelerated burn-in, CJK/Arabic encoding &amp; BiDi repair, FPS-drift/VAD sync, MKV font attachments, Watch Daemon, and instant atomic undo.
</p>

<p align="center">
  <img src="docs/media/divider.svg" alt="" width="100%" />
</p>

## Live Studio Walkthrough

<p align="center">
  <img src="docs/media/demo.gif" alt="SRT Embedder Live Studio Demo" width="100%" />
</p>

<p align="center">
  <sub>Recorded directly from the live interface · <a href="docs/media/demo.mp4">Watch full 1080p MP4 demo</a></sub>
</p>

<p align="center">
  <img src="docs/media/divider-timeline.svg" alt="" width="100%" />
</p>

## Interface Gallery

<table>
  <tr>
    <td width="50%" align="center">
      <strong>Obsidian Dark Studio</strong><br/><br/>
      <img src="docs/media/showcase-dark.png" alt="Obsidian Dark Theme" width="100%" />
    </td>
    <td width="50%" align="center">
      <strong>Daylight Studio + Verified Completion</strong><br/><br/>
      <img src="docs/media/showcase-light.png" alt="Daylight Light Theme" width="100%" />
    </td>
  </tr>
  <tr>
    <td colspan="2" align="center">
      <strong>GPU Hardsub &amp; Live 16:9 Style Preview</strong><br/><br/>
      <img src="docs/media/showcase-hardsub.png" alt="GPU Hardsub Style Studio" width="78%" />
    </td>
  </tr>
</table>

<p align="center">
  <img src="docs/media/divider-wave.svg" alt="" width="100%" />
</p>

## Architecture & Core Capabilities

<p align="center">
  <img src="docs/media/features-bento.svg" alt="Core Capabilities Bento" width="100%" />
</p>

<table>
  <tr>
    <td width="50%" valign="top">
      <strong>Smart Fallback Chain</strong><br/>
      <code>mkvmerge</code> → <code>ffmpeg</code> → safe stream map (drops broken data/timecode tracks) → tag strip → lossless <code>.mkv</code> remux fallback.
    </td>
    <td width="50%" valign="top">
      <strong>Global Formats, CJK &amp; RTL</strong><br/>
      Ingests <code>.srt</code>, <code>.vtt</code>, <code>.ass</code>/<code>.ssa</code>, <code>.ttml</code>/<code>.dfxp</code>, <code>.sub</code> &amp; <code>.zip</code>; auto-decodes UTF-8/16, Arabic, CJK, Hebrew, Greek &amp; Cyrillic + <code>--fix-rtl</code> &amp; MKV font attach.
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <strong>Sync Check, FPS Drift &amp; VAD</strong><br/>
      Aligns against embedded text or FFmpeg speech VAD (<code>silencedetect</code>), fitting linear <code>23.976 ↔ 24 ↔ 25 fps</code> speed ratios &amp; offsets (<code>--auto-sync</code>, <code>--speed</code>).
    </td>
    <td width="50%" valign="top">
      <strong>Companion Pairing &amp; Dual Merge</strong><br/>
      Auto-discovers <code>Subs/</code> &amp; <code>.zip</code> companions, pairs <code>S01E01</code> episodes across release names, and merges two tracks into bilingual <code>.ass</code>.
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <strong>Profiles, Naming &amp; Smart Rules</strong><br/>
      Shared GUI/CLI/Watch presets with <code>default</code> / <code>plex</code> / <code>clean</code> output templates (<code>--naming</code>) and foreign-audio default rules (<code>--prefer-sub</code>).
    </td>
    <td width="50%" valign="top">
      <strong>GPU Hardsub &amp; SDH Cleaners</strong><br/>
      NVENC, QSV, AMF &amp; <code>libx264</code> burn-in with live 16:9 BT.709 preview; strips <code>[SDH]</code> cues/promo ads and audits folders via <code>audit</code>.
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <strong>Watch Daemon &amp; System Tray</strong><br/>
      Persistent folder watcher, silent <code>--tray</code> mode with desktop toasts, consumed-sub archive/trash cleanup, and OS sign-in startup.
    </td>
    <td width="50%" valign="top">
      <strong>Atomic Undo &amp; Engine Setup</strong><br/>
      <code>.tmp</code> staging, <code>ffprobe</code> + disk checks, instant backup restore (auto-pruned <code>20</code> max / <code>14d</code> / <code>10 GB</code>), SHA-256 FFmpeg install &amp; release check.
    </td>
  </tr>
</table>

<p align="center">
  <img src="docs/media/divider.svg" alt="" width="100%" />
</p>

## Quick Start

### Installation & Platform Support

Install directly from GitHub via `pip` / `pipx`, or clone and run locally:

```bash
# Lean CLI-only install (ideal for headless Linux/macOS/NAS)
pip install "git+https://github.com/BMQ-1/srt-embedder.git"

# Full Desktop Studio GUI + Tray + Recycle Bin support
pip install "srt-embedder[all] @ git+https://github.com/BMQ-1/srt-embedder.git"

# Or from a local clone (editable)
git clone https://github.com/BMQ-1/srt-embedder.git
cd srt-embedder
pip install -e ".[all]"
```

| Surface | Windows | macOS | Linux |
| :--- | :---: | :---: | :---: |
| **Core Muxer, CJK/RTL Repair, FPS/VAD Sync, Profiles, CLI** | Full | Full | Full |
| **Desktop Studio GUI (`[gui]`), Notifications, Sign-in Startup** | Full (Tray + Toast) | Full (LaunchAgent + Notification) | Full (Autostart + `notify-send`) |
| **File Manager Integration (`Settings` / `shell install`)** | Explorer (`HKCU`) | Finder Quick Action | `.desktop` + Nautilus |
| **Engine Installer & Release Binaries** | SHA-256 FFmpeg + `winget` + `.exe` | Homebrew (`brew install`) | `apt` / `dnf` / `pacman` |

Package manager manifest templates for **WinGet**, **Scoop**, and **Homebrew** live under [`packaging/`](packaging/).

> **Windows SmartScreen Note:** Standalone Windows executables (`SRT-Embedder.exe` and Setup `.exe`) are currently unsigned, so Windows Defender SmartScreen may prompt on first launch. Verify downloads against `SHA256SUMS.txt` on each GitHub Release, or install via `pip` / `pipx`.

### Desktop Studio GUI

Run `srt-embedder`, `srt-embedder-gui`, `python -m srt_embedder`, or launch **`SRT-Embedder.exe`** on Windows (from source on Windows you can also double-click **`srt-embedder.cmd`**).

You can preload a file or folder directly (`srt-embedder gui "movie.mkv"`), switch Studio UI language (`EN`, `AR` with RTL layout, `ES`, `FR`) inside **Settings ⚙**, and enable **File Manager Integration** (adds both Studio GUI and headless **Quick-Embed Subtitles (Auto Profile)** on right-click with zero admin prompt).

### Keyboard Shortcuts

| Shortcut | Action | Shortcut | Action |
| :--- | :--- | :--- | :--- |
| <kbd>Ctrl</kbd>+<kbd>K</kbd> | Open Command Palette | <kbd>Ctrl</kbd>+<kbd>Enter</kbd> | Start Muxing |
| <kbd>Ctrl</kbd>+<kbd>O</kbd> | Browse Video File | <kbd>Ctrl</kbd>+<kbd>,</kbd> | Settings &amp; Engine Doctor |
| <kbd>Esc</kbd> | Close Active Modal | `Drag & Drop` | Video / Subtitle / Folder / Reorder |

<p align="center">
  <img src="docs/media/divider-timeline.svg" alt="" width="100%" />
</p>

## CLI Reference

> When running directly from a Windows checkout without `pip install`, substitute `.\srt-embedder.cmd` for `srt-embedder`.

```bash
# Verify mkvmerge, mkvextract, ffmpeg, ffprobe, GPU encoders, or install engines / check releases
srt-embedder doctor
srt-embedder doctor --install
srt-embedder doctor --check-update

# Inspect video streams or health-check/preview any supported subtitle file
srt-embedder inspect "movie.mkv"
srt-embedder inspect "subs.srt"

# Check timing sync against embedded text track or audio speech VAD
srt-embedder sync-check "movie.mkv" "subs.srt"

# Embed one or multiple subtitles, or omit subtitles to auto-discover nearby Subs/
srt-embedder embed "movie.mkv" "arabic.srt" "english.srt"
srt-embedder embed "movie.mkv" --auto-sync --naming plex --prefer-sub ara --fix-rtl

# Headless 1-step auto-pair & embed using your active profile (with desktop notification)
srt-embedder quick "movie.mkv" --notify
srt-embedder quick "./Downloads/Season 1" -r --profile arabic-rtl

# Clean SDH + translator ads, stretch FPS speed or shift +200ms, and replace original (keeps undo backup)
srt-embedder embed "movie.mkv" "subs.srt" --offset +200ms --speed 25/23.976 --clean-sdh --clean-ads --replace

# Audit a media library for ready / pairable / missing subtitles (and embed pairable in 1 shot)
srt-embedder audit "./Movies" --lang ara
srt-embedder audit "./Movies" --lang ara --embed-pairable

# Manage persistent automation profiles
srt-embedder profile list
srt-embedder profile use arabic-rtl

# Merge two subtitles into a bilingual styled .ass file & extract an embedded track
srt-embedder merge "arabic.srt" "english.srt" -o "bilingual.ass"
srt-embedder extract "movie.mkv" 2

# Batch-pair a season folder recursively or run the Watch Daemon (headless or system tray)
srt-embedder batch "./Movies/Season 1" -r --auto-sync --archive-subs
srt-embedder watch "./Downloads/Movies" -r --tray --archive-subs --auto-sync

# Manage undo backups & per-user file manager context menus
srt-embedder undo --list
srt-embedder undo --purge
srt-embedder shell install
```

<p align="center">
  <img src="docs/media/divider-wave.svg" alt="" width="100%" />
</p>

## Project Layout & Standalone Build

```text
SRT Embedder/
├── srt_embedder/
│   ├── engine.py          # Fallback muxing chain, audio-VAD sync, ASS font attach, pairing & audit
│   ├── subtitle.py        # Multi-encoding (CJK/RTL) decoder, VTT/ASS/TTML/SUB/ZIP, SDH/ad cleaner
│   ├── profiles.py        # Persistent JSON profiles, naming templates & smart language rules
│   ├── watcher.py         # Watchdog folder daemon with persistent manifest & _archived_subs cleanup
│   ├── tray.py            # Cross-platform system tray / desktop notifications & OS sign-in startup
│   ├── bootstrap.py       # SHA-256 verified FFmpeg, winget/brew engines & GitHub release update check
│   ├── undo.py            # Same-filesystem atomic backup, restore & 14d/10 GB auto-pruning store
│   ├── shell_ext.py       # Per-user file manager integration (Windows HKCU, macOS Finder, Linux)
│   ├── cli.py             # Rich terminal CLI with quick, audit, profile, --dry-run, --json
│   ├── gui.py             # pywebview desktop shell & native drag-and-drop bridge
│   └── ui/                # Feelcraft Studio UI (feelcraft.css, index.html, app.js, assets/)
├── docs/media/            # Animated SVG banners/dividers, live demo video/GIF & screenshots
├── packaging/             # WinGet, Scoop & Homebrew manifest templates
├── scripts/               # PyInstaller release builder, headless UI smoke test & showcase recorder
├── installer/             # Inno Setup (.iss) Windows installer definition
├── .github/workflows/     # CI (Ubuntu + macOS + Windows) and tagged release automation
└── tests/                 # pytest unit, CLI, BridgeApi & real-mux integration suite
```

Build the standalone Windows executables (`SRT-Embedder.exe` + `srt-embedder-cli.exe` + portable `.zip` + `SHA256SUMS.txt`):

```powershell
.\scripts\build_release.ps1
```

Tagged releases (`v*.*.*`) automatically run GitHub Actions to publish Windows portable ZIP, Inno Setup `.exe`, PyPI wheel/sdist, and `SHA256SUMS.txt` artifacts.

<p align="center">
  <img src="docs/media/divider.svg" alt="" width="100%" />
</p>

<p align="center">
  <sub>MIT License · Built with Feelcraft precision</sub>
</p>
