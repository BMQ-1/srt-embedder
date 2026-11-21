"""Tool binary discovery, GPU hardware encoder probing, and environment diagnostics."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ToolStatus:
    name: str
    path: Optional[str]
    version: str = ""
    available: bool = False


_WINDOWS_CANDIDATES: dict[str, list[str]] = {
    "mkvmerge": [
        r"C:\Program Files\MKVToolNix\mkvmerge.exe",
        r"C:\Program Files (x86)\MKVToolNix\mkvmerge.exe",
    ],
    "mkvextract": [
        r"C:\Program Files\MKVToolNix\mkvextract.exe",
        r"C:\Program Files (x86)\MKVToolNix\mkvextract.exe",
    ],
    "ffmpeg": [
        r"C:\ffmpeg_folder\ffmpeg-essentials_build\bin\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    ],
    "ffprobe": [
        r"C:\ffmpeg_folder\ffmpeg-essentials_build\bin\ffprobe.exe",
        r"C:\ffmpeg\bin\ffprobe.exe",
        r"C:\Program Files\ffmpeg\bin\ffprobe.exe",
    ],
}


def managed_bin_dir() -> Path:
    """Per-user writable bin directory used by the 1-click engine bootstrapper."""
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SRTEmbedder" / "bin"
    base.mkdir(parents=True, exist_ok=True)
    return base


def find_binary(name: str) -> Optional[str]:
    """Locate a binary via env override, bundled PyInstaller dir, managed bin, PATH, or Windows defaults."""
    env_override = os.environ.get(f"SRT_EMBEDDER_{name.upper()}_PATH")
    if env_override and Path(env_override).is_file():
        return str(Path(env_override))

    ext = ".exe" if os.name == "nt" else ""

    # Check alongside frozen executable / _MEIPASS / bin subfolder / managed bin dir
    bundle_dirs: list[Path] = [managed_bin_dir()]
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        bundle_dirs.extend([exe_dir, exe_dir / "bin"])
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bundle_dirs.extend([Path(meipass), Path(meipass) / "bin"])
    for bdir in bundle_dirs:
        cand = bdir / f"{name}{ext}"
        if cand.is_file():
            return str(cand)

    if sys.platform == "darwin" and name in ("ffmpeg", "ffprobe"):
        for brew_full in (
            Path("/opt/homebrew/opt/ffmpeg-full/bin") / name,
            Path("/usr/local/opt/ffmpeg-full/bin") / name,
        ):
            if brew_full.is_file():
                return str(brew_full)

    found = shutil.which(name)
    if found:
        return found

    for candidate in _WINDOWS_CANDIDATES.get(name, []):
        if Path(candidate).is_file():
            return candidate

    local_app = os.environ.get("LOCALAPPDATA", "")
    user_profile = os.environ.get("USERPROFILE", "")
    extra_roots = [
        Path(user_profile) / "scoop" / "shims" / f"{name}{ext}",
        Path(local_app) / "Microsoft" / "WinGet" / "Links" / f"{name}{ext}",
        Path(r"C:\ProgramData\chocolatey\bin") / f"{name}{ext}",
    ] if os.name == "nt" else [
        Path("/opt/homebrew/bin") / name,
        Path("/usr/local/bin") / name,
        Path.home() / ".local" / "bin" / name,
        Path("/usr/bin") / name,
    ]
    for p in extra_roots:
        if p.is_file():
            return str(p)
    return None


def _read_version(exe_path: str, arg: str = "-version") -> str:
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.run(
            [exe_path, arg],
            capture_output=True,
            text=True,
            timeout=3,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        line = (proc.stdout or proc.stderr or "").strip().splitlines()[0]
        return line[:120]
    except Exception:
        return ""


def discover_tools() -> dict[str, ToolStatus]:
    """Check availability and version of mkvmerge, mkvextract, ffmpeg, and ffprobe."""
    specs = [
        ("mkvmerge", "--version"),
        ("mkvextract", "--version"),
        ("ffmpeg", "-version"),
        ("ffprobe", "-version"),
    ]
    result: dict[str, ToolStatus] = {}
    for name, ver_arg in specs:
        exe = find_binary(name)
        if exe:
            result[name] = ToolStatus(
                name=name,
                path=exe,
                version=_read_version(exe, ver_arg),
                available=True,
            )
        else:
            result[name] = ToolStatus(name=name, path=None, available=False)
    return result


def detect_hw_encoders(ffmpeg_exe: Optional[str] = None) -> list[str]:
    """Probe which GPU H.264/HEVC encoders actually initialize on this machine."""
    exe = ffmpeg_exe or find_binary("ffmpeg")
    if not exe:
        return ["libx264"]

    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        enc_out = subprocess.run(
            [exe, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=5,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        ).stdout
    except Exception:
        return ["libx264"]

    candidates = ["h264_nvenc", "hevc_nvenc", "h264_qsv", "h264_amf"]
    usable: list[str] = []
    for enc in candidates:
        if enc not in enc_out:
            continue
        # Quick 1-frame null probe to verify driver/GPU support
        try:
            probe = subprocess.run(
                [
                    exe, "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1",
                    "-frames:v", "1", "-c:v", enc, "-f", "null", "-",
                ],
                capture_output=True,
                timeout=5,
                creationflags=flags,
            )
            if probe.returncode == 0:
                usable.append(enc)
        except Exception:
            continue

    usable.append("libx264")
    return usable
