"""1-Click portable binary bootstrapper for FFmpeg/FFprobe and MKVToolNix."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

from . import __version__
from .tools import find_binary, managed_bin_dir

ProgressFn = Callable[[float, str], None]

FFMPEG_ESSENTIALS_ZIP = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_ESSENTIALS_SHA256 = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256"


def _parse_sha256_text(text: str) -> str:
    m = re.search(r"\b([a-fA-F0-9]{64})\b", text or "")
    if not m:
        raise ValueError("Invalid or missing 64-char SHA-256 digest.")
    return m.group(1).lower()


def _fetch_expected_sha256(url: str = FFMPEG_ESSENTIALS_SHA256) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": f"SRTEmbedder/{__version__}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read(4096).decode("utf-8", errors="replace")
    return _parse_sha256_text(raw)


def _compute_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest().lower()


def install_ffmpeg_portable(progress_cb: Optional[ProgressFn] = None) -> tuple[bool, str]:
    """Download, SHA-256 verify, and extract portable ffmpeg.exe & ffprobe.exe into %LOCALAPPDATA%/SRTEmbedder/bin."""
    if sys.platform != "win32":
        return False, "Automatic portable download is only implemented for Windows. Install ffmpeg via your package manager."

    bin_dir = managed_bin_dir()
    if progress_cb:
        progress_cb(5.0, "Fetching SHA-256 checksum & connecting to gyan.dev...")

    try:
        expected_sha256 = _fetch_expected_sha256(FFMPEG_ESSENTIALS_SHA256)
        with tempfile.TemporaryDirectory(prefix="srt_dl_ffmpeg_") as tmpdir:
            zip_path = Path(tmpdir) / "ffmpeg.zip"

            req = urllib.request.Request(FFMPEG_ESSENTIALS_ZIP, headers={"User-Agent": f"SRTEmbedder/{__version__}"})
            with urllib.request.urlopen(req, timeout=60) as resp, zip_path.open("wb") as out:
                total = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb and total > 0:
                        pct = min(75.0, 5.0 + (downloaded / total) * 70.0)
                        progress_cb(pct, f"Downloading FFmpeg ({downloaded / 1048576:.1f} / {total / 1048576:.1f} MB)...")

            if progress_cb:
                progress_cb(78.0, "Verifying SHA-256 checksum...")
            actual_sha256 = _compute_file_sha256(zip_path)
            if actual_sha256 != expected_sha256:
                return False, f"SHA-256 checksum mismatch for FFmpeg archive (expected {expected_sha256[:12]}..., got {actual_sha256[:12]}...)."

            if progress_cb:
                progress_cb(84.0, "Extracting verified ffmpeg.exe and ffprobe.exe...")

            extracted = []
            with zipfile.ZipFile(zip_path, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    fname = Path(info.filename).name.lower()
                    if fname in ("ffmpeg.exe", "ffprobe.exe"):
                        target = (bin_dir / fname).resolve()
                        with zf.open(info, "r") as src, target.open("wb") as dst:
                            shutil.copyfileobj(src, dst)
                        extracted.append(fname)

            if not extracted:
                return False, "Archive did not contain ffmpeg.exe / ffprobe.exe."

            if progress_cb:
                progress_cb(100.0, f"Installed {', '.join(extracted)} to {bin_dir}")
            return True, f"Installed {', '.join(extracted)} to {bin_dir}"
    except Exception as exc:
        return False, f"FFmpeg download failed: {exc}"


def install_via_winget(package_id: str, progress_cb: Optional[ProgressFn] = None) -> tuple[bool, str]:
    """Attempt silent winget install (e.g. MoritzBunkus.MKVToolNix or Gyan.FFmpeg)."""
    if sys.platform != "win32":
        return False, "winget is only available on Windows."
    winget = shutil.which("winget")
    if not winget:
        return False, "winget is not installed on this system."

    if progress_cb:
        progress_cb(15.0, f"Running winget install {package_id}...")

    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        proc = subprocess.run(
            [
                winget, "install", "--id", package_id,
                "--exact", "--silent",
                "--accept-package-agreements", "--accept-source-agreements",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            creationflags=flags,
        )
        if proc.returncode == 0:
            if progress_cb:
                progress_cb(100.0, f"Installed {package_id} via winget.")
            return True, f"Installed {package_id} successfully."
        return False, (proc.stderr or proc.stdout or f"winget exited with code {proc.returncode}").strip()[:300]
    except Exception as exc:
        return False, f"winget failed: {exc}"


def _parse_semver(ver: str) -> tuple[int, ...]:
    nums = [int(x) for x in re.findall(r"\d+", ver or "")]
    return tuple(nums[:3]) if nums else (0, 0, 0)


DEFAULT_REPO_SLUG = "BMQ-1/srt-embedder"


def check_for_updates(repo_slug: Optional[str] = None) -> dict[str, Any]:
    """GitHub Releases check comparing __version__ against latest published tag."""
    slug = (repo_slug or os.environ.get("SRT_EMBEDDER_REPO", DEFAULT_REPO_SLUG)).strip()
    if not slug or "/" not in slug:
        return {
            "checked": False,
            "current_version": __version__,
            "update_available": False,
            "message": "Set SRT_EMBEDDER_REPO=owner/repo or publish to GitHub to enable release update checks.",
        }
    api_url = f"https://api.github.com/repos/{slug}/releases/latest"
    try:
        req = urllib.request.Request(
            api_url,
            headers={"User-Agent": f"SRTEmbedder/{__version__}", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read(65536).decode("utf-8", errors="replace"))
        latest_tag = str(data.get("tag_name") or "").lstrip("vV")
        html_url = str(data.get("html_url") or "")
        is_newer = bool(latest_tag) and _parse_semver(latest_tag) > _parse_semver(__version__)
        return {
            "checked": True,
            "current_version": __version__,
            "latest_version": latest_tag or __version__,
            "update_available": is_newer,
            "url": html_url,
            "message": f"Update v{latest_tag} is available." if is_newer else f"You are running the latest version (v{__version__}).",
        }
    except Exception as exc:
        return {
            "checked": False,
            "current_version": __version__,
            "update_available": False,
            "message": f"Could not check for updates: {exc}",
        }


def _install_via_homebrew(formula: str, progress_cb: Optional[ProgressFn] = None) -> tuple[bool, str]:
    brew = shutil.which("brew")
    if not brew:
        return False, "Homebrew not found. Install Homebrew from https://brew.sh/ then run: brew install " + formula
    if progress_cb:
        progress_cb(20.0, f"Running brew install {formula}...")
    try:
        proc = subprocess.run([brew, "install", formula], capture_output=True, text=True, timeout=300)
        if proc.returncode == 0:
            if progress_cb:
                progress_cb(100.0, f"Installed {formula} via Homebrew.")
            return True, f"Installed {formula} via Homebrew."
        return False, (proc.stderr or proc.stdout or "brew install failed")[:240]
    except Exception as exc:
        return False, f"brew failed: {exc}"


def bootstrap_tool(tool_name: str, progress_cb: Optional[ProgressFn] = None) -> tuple[bool, str]:
    """1-click installer for ffmpeg/ffprobe or mkvmerge/mkvextract across OSes."""
    if tool_name in ("ffmpeg", "ffprobe"):
        if find_binary("ffmpeg") and find_binary("ffprobe"):
            return True, "FFmpeg and FFprobe are already installed."
        if os.name == "nt":
            ok, msg = install_ffmpeg_portable(progress_cb)
            if ok:
                return True, msg
            return install_via_winget("Gyan.FFmpeg", progress_cb)
        if sys.platform == "darwin":
            return _install_via_homebrew("ffmpeg", progress_cb)
        return False, "Install ffmpeg via your package manager (e.g. sudo apt install ffmpeg / sudo dnf install ffmpeg)."

    if tool_name in ("mkvmerge", "mkvextract"):
        if find_binary("mkvmerge") and find_binary("mkvextract"):
            return True, "MKVToolNix is already installed."
        if os.name == "nt":
            return install_via_winget("MoritzBunkus.MKVToolNix", progress_cb)
        if sys.platform == "darwin":
            return _install_via_homebrew("mkvtoolnix", progress_cb)
        return False, "Install mkvtoolnix via your package manager (e.g. sudo apt install mkvtoolnix / sudo dnf install mkvtoolnix)."

    return False, f"Unknown tool '{tool_name}'."
