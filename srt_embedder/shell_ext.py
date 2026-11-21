"""Cross-platform per-user file manager integration (Windows HKCU, macOS Finder Services, Linux .desktop/Nautilus)."""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import sys
from pathlib import Path

VIDEO_SHELL_EXTENSIONS = [".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".ts"]
SUB_SHELL_EXTENSIONS = [".srt", ".vtt", ".ass", ".ssa", ".ttml", ".dfxp", ".sub"]
SHELL_EXTENSIONS = VIDEO_SHELL_EXTENSIONS + SUB_SHELL_EXTENSIONS

VERB_KEY = "SRTEmbedder"
QUICK_VERB_KEY = "SRTEmbedderQuick"
MENU_LABEL = "Embed Subtitles with SRT Embedder"
QUICK_MENU_LABEL = "Quick-Embed Subtitles (Auto Profile)"


def _launch_command() -> str:
    """Build the GUI command string used by Explorer when right-clicking a file or folder."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        return f'"{exe}" "%1"'

    py_dir = Path(sys.executable).resolve().parent
    pythonw = py_dir / "pythonw.exe"
    py_exe = pythonw if pythonw.exists() else Path(sys.executable).resolve()
    app_root = Path(__file__).resolve().parent.parent
    launcher = app_root / "srt_embedder" / "__main__.py"
    return f'"{py_exe}" "{launcher}" gui "%1"'


def _quick_command() -> str:
    """Build the headless Quick-Embed command string used by Explorer right-click."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        cli_exe = exe.with_name("srt-embedder-cli.exe")
        target = cli_exe if cli_exe.exists() else exe
        return f'"{target}" quick "%1" --notify'

    py_dir = Path(sys.executable).resolve().parent
    pythonw = py_dir / "pythonw.exe"
    py_exe = pythonw if pythonw.exists() else Path(sys.executable).resolve()
    app_root = Path(__file__).resolve().parent.parent
    launcher = app_root / "srt_embedder" / "__main__.py"
    return f'"{py_exe}" "{launcher}" quick "%1" --notify'


def _icon_path() -> str:
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve())
    ico = Path(__file__).resolve().parent / "assets" / ("icon.ico" if sys.platform == "win32" else "icon.png")
    return str(ico) if ico.exists() else ""


def _linux_paths() -> tuple[Path, Path]:
    apps = Path.home() / ".local" / "share" / "applications" / "srt-embedder.desktop"
    nautilus = Path.home() / ".local" / "share" / "nautilus" / "scripts" / "Quick-Embed Subtitles"
    return apps, nautilus


def _macos_workflow_dir() -> Path:
    return Path.home() / "Library" / "Services" / "Quick-Embed Subtitles.workflow"


def is_shell_installed() -> bool:
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                rf"Software\Classes\SystemFileAssociations\.mkv\shell\{VERB_KEY}",
            ):
                return True
        except FileNotFoundError:
            return False
        except Exception:
            return False

    if sys.platform == "darwin":
        return _macos_workflow_dir().exists()

    apps, _ = _linux_paths()
    return apps.exists()


def _write_verb(winreg, base: str, label: str, cmd_str: str, icon_str: str) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, label)
        if icon_str:
            winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, icon_str)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\command") as ck:
        winreg.SetValueEx(ck, "", 0, winreg.REG_SZ, cmd_str)


def _delete_verb(winreg, base: str) -> bool:
    deleted = False
    for sub in (base + r"\command", base):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
            if sub == base:
                deleted = True
        except FileNotFoundError:
            pass
    return deleted


def install_shell_menu() -> tuple[bool, str]:
    """Register Studio GUI + Headless Quick-Embed per-user file manager actions (no admin needed)."""
    if sys.platform == "win32":
        import winreg

        gui_cmd = _launch_command()
        quick_cmd = _quick_command()
        icon_str = _icon_path()

        try:
            for ext in SHELL_EXTENSIONS:
                _write_verb(
                    winreg,
                    rf"Software\Classes\SystemFileAssociations\{ext}\shell\{VERB_KEY}",
                    MENU_LABEL,
                    gui_cmd,
                    icon_str,
                )
                if ext in VIDEO_SHELL_EXTENSIONS:
                    _write_verb(
                        winreg,
                        rf"Software\Classes\SystemFileAssociations\{ext}\shell\{QUICK_VERB_KEY}",
                        QUICK_MENU_LABEL,
                        quick_cmd,
                        icon_str,
                    )

            for dir_cls in ("Directory", r"Directory\Background"):
                suffix = '"%1"' if dir_cls == "Directory" else '"%V"'
                _write_verb(
                    winreg,
                    rf"Software\Classes\{dir_cls}\shell\{VERB_KEY}",
                    MENU_LABEL,
                    gui_cmd.replace('"%1"', suffix),
                    icon_str,
                )
                _write_verb(
                    winreg,
                    rf"Software\Classes\{dir_cls}\shell\{QUICK_VERB_KEY}",
                    QUICK_MENU_LABEL,
                    quick_cmd.replace('"%1"', suffix),
                    icon_str,
                )

            return True, f"Registered Studio + Quick-Embed Explorer menu for {len(SHELL_EXTENSIONS)} file types and folders."
        except Exception as exc:
            return False, f"Failed to register Explorer menu: {exc}"

    py_quoted = shlex.quote(sys.executable)
    if sys.platform == "darwin":
        wf = _macos_workflow_dir() / "Contents"
        wf.mkdir(parents=True, exist_ok=True)
        info_plist = {
            "NSServices": [{
                "NSMenuItem": {"default": QUICK_MENU_LABEL},
                "NSMessage": "runWorkflowAsService",
                "NSSendFileTypes": ["public.movie", "public.folder"],
            }]
        }
        (wf / "Info.plist").write_bytes(plistlib.dumps(info_plist))
        script_text = f'for f in "$@"; do {py_quoted} -m srt_embedder quick "$f" --notify; done\n'
        (wf / "document.wflow").write_bytes(
            plistlib.dumps({
                "AMApplicationBuild": "520",
                "workflowMetaData": {"serviceInputTypeIdentifier": "com.apple.Automator.fileSystemObject", "workflowTypeIdentifier": "com.apple.Automator.servicesMenu"},
                "actions": [{"action": {"ActionParameters": {"COMMAND_STRING": script_text, "CheckedForUserDefaultShell": True, "inputMethod": 1}}}],
            })
        )
        return True, f"Installed macOS Finder Quick Action at {_macos_workflow_dir()}"

    # Linux Freedesktop .desktop + Nautilus script
    apps, nautilus = _linux_paths()
    apps.parent.mkdir(parents=True, exist_ok=True)
    nautilus.parent.mkdir(parents=True, exist_ok=True)
    icon_str = _icon_path()
    apps.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=SRT Embedder\n"
        "Comment=Embed subtitles into videos with smart fallbacks\n"
        f"Exec={py_quoted} -m srt_embedder gui %F\n"
        f"Icon={icon_str}\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Video;\n"
        "MimeType=video/x-matroska;video/mp4;video/x-msvideo;video/webm;video/quicktime;\n",
        encoding="utf-8",
    )
    nautilus.write_text(
        f'#!/usr/bin/env bash\nfor p in "$@"; do\n  {py_quoted} -m srt_embedder quick "$p" --notify\ndone\n',
        encoding="utf-8",
    )
    os.chmod(nautilus, 0o755)
    return True, "Installed Linux .desktop launcher and Nautilus Quick-Embed script."


def uninstall_shell_menu() -> tuple[bool, str]:
    """Remove per-user file manager integration entries cleanly."""
    if sys.platform == "win32":
        import winreg

        removed = 0
        try:
            for ext in SHELL_EXTENSIONS:
                for verb in (VERB_KEY, QUICK_VERB_KEY):
                    if _delete_verb(winreg, rf"Software\Classes\SystemFileAssociations\{ext}\shell\{verb}"):
                        removed += 1
            for dir_cls in ("Directory", r"Directory\Background"):
                for verb in (VERB_KEY, QUICK_VERB_KEY):
                    if _delete_verb(winreg, rf"Software\Classes\{dir_cls}\shell\{verb}"):
                        removed += 1
            return True, f"Removed Explorer right-click menu ({removed} entries cleaned)."
        except Exception as exc:
            return False, f"Failed to remove Explorer menu: {exc}"

    if sys.platform == "darwin":
        wf = _macos_workflow_dir()
        if wf.exists():
            shutil.rmtree(wf, ignore_errors=True)
        return True, "Removed macOS Finder Quick Action."

    apps, nautilus = _linux_paths()
    apps.unlink(missing_ok=True)
    nautilus.unlink(missing_ok=True)
    return True, "Removed Linux .desktop and Nautilus entries."
