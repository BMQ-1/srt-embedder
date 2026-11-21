"""Cross-platform system tray daemon, safe desktop notifications, and sign-in startup integration."""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

STARTUP_KEY_NAME = "SRTEmbedderWatch"
RUN_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _tray_icon_path() -> Optional[Path]:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cand = Path(meipass) / "srt_embedder" / "assets" / "icon.png"
        if cand.exists():
            return cand
    cand = Path(__file__).resolve().parent / "assets" / "icon.png"
    return cand if cand.exists() else None


def _startup_command(watch_dir: str, recursive: bool = False, profile: str = "") -> str:
    prof_arg = f' --profile "{profile}"' if profile else ""
    rec_arg = " -r" if recursive else ""
    if sys.platform != "win32":
        parts = [sys.executable, "-m", "srt_embedder", "watch", watch_dir, "--tray"]
        if recursive:
            parts.append("-r")
        if profile:
            parts.extend(["--profile", profile])
        return " ".join(shlex.quote(p) for p in parts)

    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        cli_exe = exe.with_name("srt-embedder-cli.exe")
        target = cli_exe if cli_exe.exists() else exe
        return f'"{target}" watch "{watch_dir}" --tray{rec_arg}{prof_arg}'

    py_dir = Path(sys.executable).resolve().parent
    pythonw = py_dir / "pythonw.exe"
    py_exe = pythonw if pythonw.exists() else Path(sys.executable).resolve()
    launcher = Path(__file__).resolve().parent / "__main__.py"
    return f'"{py_exe}" "{launcher}" watch "{watch_dir}" --tray{rec_arg}{prof_arg}'


def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "io.github.srt-embedder.watch.plist"


def _linux_autostart_path() -> Path:
    return Path.home() / ".config" / "autostart" / "srt-embedder-watch.desktop"


def is_watch_startup_installed() -> bool:
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_PATH) as k:
                winreg.QueryValueEx(k, STARTUP_KEY_NAME)
                return True
        except FileNotFoundError:
            return False
        except Exception:
            return False
    if sys.platform == "darwin":
        return _macos_plist_path().exists()
    return _linux_autostart_path().exists()


def install_watch_startup(watch_dir: str, recursive: bool = False, profile: str = "") -> tuple[bool, str]:
    """Register Watch Daemon tray runner on OS user sign-in (no admin required)."""
    if not watch_dir or not Path(watch_dir).is_dir():
        return False, "Select a valid watch folder before enabling startup."

    resolved_dir = str(Path(watch_dir).resolve())
    if sys.platform == "win32":
        import winreg

        cmd = _startup_command(resolved_dir, recursive=recursive, profile=profile)
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_REG_PATH) as k:
                winreg.SetValueEx(k, STARTUP_KEY_NAME, 0, winreg.REG_SZ, cmd)
            return True, f"Registered Watch Daemon to start on Windows sign-in ({Path(resolved_dir).name})."
        except Exception as exc:
            return False, f"Failed to register Windows startup: {exc}"

    if sys.platform == "darwin":
        args = [sys.executable, "-m", "srt_embedder", "watch", resolved_dir, "--tray"]
        if recursive:
            args.append("-r")
        if profile:
            args.extend(["--profile", profile])
        plist_p = _macos_plist_path()
        plist_p.parent.mkdir(parents=True, exist_ok=True)
        plist_p.write_bytes(
            plistlib.dumps({
                "Label": "io.github.srt-embedder.watch",
                "ProgramArguments": args,
                "RunAtLoad": True,
                "KeepAlive": False,
            })
        )
        return True, f"Registered macOS LaunchAgent at {plist_p}"

    cmd = _startup_command(resolved_dir, recursive=recursive, profile=profile)
    desktop_p = _linux_autostart_path()
    desktop_p.parent.mkdir(parents=True, exist_ok=True)
    desktop_p.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=SRT Embedder Watch Daemon\n"
        f"Exec={cmd}\n"
        "X-GNOME-Autostart-enabled=true\n",
        encoding="utf-8",
    )
    return True, f"Registered Linux autostart entry at {desktop_p}"


def uninstall_watch_startup() -> tuple[bool, str]:
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_PATH, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, STARTUP_KEY_NAME)
            return True, "Removed Watch Daemon from Windows startup."
        except FileNotFoundError:
            return True, "Watch Daemon startup entry was not set."
        except Exception as exc:
            return False, f"Failed to remove startup entry: {exc}"

    if sys.platform == "darwin":
        _macos_plist_path().unlink(missing_ok=True)
        return True, "Removed macOS Watch Daemon LaunchAgent."

    _linux_autostart_path().unlink(missing_ok=True)
    return True, "Removed Linux Watch Daemon autostart entry."


_NOTIFY_PS_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "$n = New-Object System.Windows.Forms.NotifyIcon;"
    "$n.Icon = [System.Drawing.SystemIcons]::Information;"
    "$n.Visible = $true;"
    "$n.ShowBalloonTip(4500, $env:SRT_NOTIFY_TITLE, $env:SRT_NOTIFY_MSG, [System.Windows.Forms.ToolTipIcon]::Info);"
    "Start-Sleep -Milliseconds 1200; $n.Dispose()"
)


def notify_desktop(title: str, message: str) -> None:
    """Best-effort native desktop notification without shell string interpolation."""
    clean_title = str(title)[:80]
    clean_msg = str(message)[:220]
    try:
        if sys.platform == "win32":
            env = os.environ.copy()
            env["SRT_NOTIFY_TITLE"] = clean_title
            env["SRT_NOTIFY_MSG"] = clean_msg
            subprocess.Popen(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", _NOTIFY_PS_SCRIPT],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        elif sys.platform == "darwin":
            env = os.environ.copy()
            env["SRT_NOTIFY_TITLE"] = clean_title
            env["SRT_NOTIFY_MSG"] = clean_msg
            subprocess.Popen(
                ["osascript", "-e", 'display notification (system attribute "SRT_NOTIFY_MSG") with title (system attribute "SRT_NOTIFY_TITLE")'],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif shutil.which("notify-send"):
            subprocess.Popen(
                ["notify-send", clean_title, clean_msg],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception:
        pass


def run_watch_tray(
    watcher,
    on_open_gui: Optional[Callable[[], None]] = None,
    on_icon_ready: Optional[Callable[[object], None]] = None,
    on_exit: Optional[Callable[[], None]] = None,
) -> None:
    """Run a system tray icon bound to a FolderWatcher until restored or exited."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise RuntimeError(
            "Tray mode requires optional GUI dependencies. Install with: pip install \"srt-embedder[gui]\""
        ) from exc

    icon_file = _tray_icon_path()
    if icon_file:
        img = Image.open(icon_file).convert("RGBA")
    else:
        img = Image.new("RGBA", (64, 64), (15, 18, 25, 255))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle((6, 6, 58, 58), radius=12, outline=(245, 158, 11, 255), width=4)

    tray_ref: dict[str, pystray.Icon] = {}
    orig_cb = watcher.on_event

    def _on_ev(ev: dict) -> None:
        if orig_cb:
            orig_cb(ev)
        ic = tray_ref.get("icon")
        if ic and ev.get("kind") in ("success", "error"):
            try:
                ic.notify(ev["message"][:220], "SRT Embedder")
            except Exception:
                pass

    watcher.on_event = _on_ev
    if not watcher.is_running:
        watcher.start()

    def _toggle_pause(icon, _item) -> None:
        if watcher.is_paused:
            watcher.resume()
        else:
            watcher.pause()
        icon.update_menu()

    def _open_folder(_icon, _item) -> None:
        if os.name == "nt":
            subprocess.Popen(["explorer", watcher.directory])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", watcher.directory])
        else:
            subprocess.Popen(["xdg-open", watcher.directory])

    def _open_gui_item(icon, _item) -> None:
        if on_open_gui:
            try:
                icon.stop()
            except Exception:
                pass
            on_open_gui()
            return
        if getattr(sys, "frozen", False):
            exe = Path(sys.executable).resolve()
            gui_exe = exe.with_name("SRT-Embedder.exe")
            subprocess.Popen([str(gui_exe if gui_exe.exists() else exe)])
        else:
            subprocess.Popen([sys.executable, "-m", "srt_embedder", "gui"])

    def _exit(icon, _item) -> None:
        watcher.stop()
        try:
            icon.stop()
        except Exception:
            pass
        if on_exit:
            on_exit()

    menu = pystray.Menu(
        pystray.MenuItem(
            lambda _item: f"Watching: {Path(watcher.directory).name}",
            None,
            enabled=False,
        ),
        pystray.MenuItem(
            lambda _item: "Resume Watching" if watcher.is_paused else "Pause Watching",
            _toggle_pause,
        ),
        pystray.MenuItem("Open Watch Folder", _open_folder),
        pystray.MenuItem("Open SRT Embedder Studio", _open_gui_item),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Stop & Exit", _exit),
    )

    icon = pystray.Icon("srt_embedder_watch", img, f"SRT Embedder · Watching {Path(watcher.directory).name}", menu)
    tray_ref["icon"] = icon
    if on_icon_ready:
        on_icon_ready(icon)
    icon.run()
