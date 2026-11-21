"""Unit and integration tests for CLI, BridgeApi, shell_ext, tray, and bootstrap."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import types
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from srt_embedder import bootstrap, cli, gui, shell_ext, tray, undo
from srt_embedder.tools import discover_tools


def _make_tiny_mkv(out_path: Path) -> None:
    ffmpeg = discover_tools()["ffmpeg"].path
    assert ffmpeg, "ffmpeg required"
    subprocess.run(
        [
            ffmpeg, "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=black:s=160x90:d=1",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(out_path),
        ],
        check=True,
    )


def test_cli_subcommands_json_dry_run_and_typos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("SRT_EMBEDDER_CONFIG", str(tmp_path / "cfg.json"))

    # 1. doctor --json
    rc = cli.main(["doctor", "--json"])
    assert rc in (0, 1)
    doc = json.loads(capsys.readouterr().out)
    assert "tools" in doc and "hw_encoders" in doc

    # 2. profile list / save / use / delete --json (both flag positions)
    assert cli.main(["profile", "list", "--json"]) == 0
    prof_list = json.loads(capsys.readouterr().out)
    assert prof_list["active"] == "cinema-default"

    assert cli.main(["--json", "profile", "save", "Fast Preset", "--mode", "soft-mkv", "--auto-sync"]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["saved"] == "fast-preset"

    assert cli.main(["profile", "use", "arabic-rtl", "--json"]) == 0
    used = json.loads(capsys.readouterr().out)
    assert used["active"] == "arabic-rtl"

    assert cli.main(["profile", "delete", "fast-preset", "--json"]) == 0
    deleted = json.loads(capsys.readouterr().out)
    assert deleted["success"] is True

    # 3. Typo suggestion returns exit code 2
    assert cli.main(["embd"]) == 2
    err = capsys.readouterr().err
    assert "Did you mean 'embed'?" in err

    # 4. embed (auto-discovery) + quick --dry-run --json + audit --json
    vid = tmp_path / "Clip.mkv"
    _make_tiny_mkv(vid)
    srt = tmp_path / "Clip.ar.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:01,500\nمرحبا\n", encoding="utf-8")

    assert cli.main(["embed", str(vid), "--dry-run", "--json"]) == 0
    emb = json.loads(capsys.readouterr().out)
    assert emb["dry_run"] is True and emb["success"] is True

    assert cli.main(["quick", str(vid), "--dry-run", "--json"]) == 0
    qck = json.loads(capsys.readouterr().out)
    assert qck["dry_run"] is True and qck["success"] is True

    assert cli.main(["audit", str(tmp_path), "--lang", "ara", "--json"]) == 0
    aud = json.loads(capsys.readouterr().out)
    assert aud["total_videos"] == 1
    assert aud["pairable_count"] == 1


def test_bridge_api_methods_and_serialization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SRT_EMBEDDER_CONFIG", str(tmp_path / "cfg.json"))
    api = gui.BridgeApi()

    env = api.get_environment()
    assert json.dumps(env)  # strictly JSON-serializable
    assert "config" in env and "tools" in env

    res = api.save_profile("Custom TV", {"mode": "hardsub", "auto_sync": True})
    assert res["success"] is True
    assert res["slug"] == "custom-tv"

    switched = api.set_profile("cinema-default")
    assert switched["config"]["active_profile"] == "cinema-default"

    prefs = api.update_automation_prefs({"express_drop": True, "auto_discover_companions": True})
    assert prefs["config"]["express_drop"] is True

    # Companion discovery + zip inspection via BridgeApi
    subs_dir = tmp_path / "Subs"
    subs_dir.mkdir()
    vid = tmp_path / "Show.S01E01.mkv"
    _make_tiny_mkv(vid)
    sub = subs_dir / "Show.S01E01.en.srt"
    sub.write_text("1\n00:00:00,500 --> 00:00:01,500\nHi\n", encoding="utf-8")

    drop = api.inspect_dropped_paths([str(vid)])
    assert json.dumps(drop)
    assert drop["auto_discovered"] == 1
    assert len(drop["subtitles"]) == 1

    scan = api.scan_folder(str(tmp_path), recursive=True)
    assert scan["ok"] is True and len(scan["pairs"]) == 1

    audit = api.audit_folder(str(tmp_path), target_lang="ara", recursive=True)
    assert audit["ok"] is True and audit["pairable_count"] == 1

    cues_res = api.get_cues(str(sub))
    assert cues_res["success"] is True and len(cues_res["cues"]) == 1
    assert "installedFonts" in env and "totalBackupBytes" in env

    deleted = api.delete_profile("custom-tv")
    assert deleted["success"] is True


def test_shell_ext_mocked_winreg(monkeypatch: pytest.MonkeyPatch) -> None:
    store: dict[str, dict[str, str]] = {}

    class _FakeKey:
        def __init__(self, path: str):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    fake_winreg = types.SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        REG_SZ=1,
        CreateKey=lambda _root, sub: store.setdefault(sub, {}) and _FakeKey(sub) or _FakeKey(sub),
        SetValueEx=lambda k, name, _res, _typ, val: store[k.path].__setitem__(name, val),
        OpenKey=lambda _root, sub: _FakeKey(sub) if sub in store else (_ for _ in ()).throw(FileNotFoundError(sub)),
        DeleteKey=lambda _root, sub: store.pop(sub) if sub in store else (_ for _ in ()).throw(FileNotFoundError(sub)),
    )
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake_winreg)
    monkeypatch.setattr(shell_ext.sys, "platform", "win32")

    assert shell_ext.is_shell_installed() is False
    ok, _msg = shell_ext.install_shell_menu()
    assert ok is True
    assert shell_ext.is_shell_installed() is True
    assert r"Software\Classes\Directory\shell\SRTEmbedderQuick\command" in store

    ok_u, _ = shell_ext.uninstall_shell_menu()
    assert ok_u is True
    assert shell_ext.is_shell_installed() is False
    assert store == {}


def test_tray_startup_and_safe_notify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tray.sys, "platform", "win32")

    cmd = tray._startup_command(str(tmp_path), recursive=True, profile="arabic-rtl")
    assert "watch" in cmd and "--tray" in cmd and "-r" in cmd and '--profile "arabic-rtl"' in cmd

    captured = {}

    def _fake_popen(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env", {})
        return MagicMock()

    monkeypatch.setattr(tray.subprocess, "Popen", _fake_popen)

    evil_name = "Movie $(Calc) `whoami` 'test'.mkv"
    tray.notify_desktop("SRT Embedder", evil_name)
    assert "args" in captured
    # Title and message must NEVER be interpolated into the PowerShell -Command string
    ps_cmd = captured["args"][-1]
    assert evil_name not in ps_cmd
    assert "$env:SRT_NOTIFY_MSG" in ps_cmd
    assert captured["env"]["SRT_NOTIFY_MSG"] == evil_name

    # Mock winreg for startup toggle
    run_vals: dict[str, str] = {}

    class _FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    fake_winreg = types.SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_SET_VALUE=2,
        REG_SZ=1,
        CreateKey=lambda _r, _p: _FakeKey(),
        OpenKey=lambda _r, _p, *_a: _FakeKey(),
        SetValueEx=lambda _k, name, _res, _t, val: run_vals.__setitem__(name, val),
        QueryValueEx=lambda _k, name: (run_vals[name], 1) if name in run_vals else (_ for _ in ()).throw(FileNotFoundError()),
        DeleteValue=lambda _k, name: run_vals.pop(name) if name in run_vals else (_ for _ in ()).throw(FileNotFoundError()),
    )
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake_winreg)

    assert tray.is_watch_startup_installed() is False
    ok_i, _ = tray.install_watch_startup(str(tmp_path), recursive=True, profile="cinema-default")
    assert ok_i and tray.is_watch_startup_installed() is True
    ok_u, _ = tray.uninstall_watch_startup()
    assert ok_u and tray.is_watch_startup_installed() is False


def test_bootstrap_sha256_verification_and_rejection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "managed_bin_dir", lambda: tmp_path)
    monkeypatch.setattr(bootstrap.sys, "platform", "win32")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ffmpeg-test/bin/ffmpeg.exe", b"fake-ffmpeg-binary")
        zf.writestr("ffmpeg-test/bin/ffprobe.exe", b"fake-ffprobe-binary")
    zip_bytes = buf.getvalue()
    valid_sha = hashlib.sha256(zip_bytes).hexdigest()

    class _FakeResp:
        def __init__(self, data: bytes):
            self._bio = io.BytesIO(data)
            self.headers = {"Content-Length": str(len(data))}

        def read(self, n: int = -1) -> bytes:
            return self._bio.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    # 1. Valid SHA-256 succeeds and extracts both binaries
    def _urlopen_ok(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith(".sha256"):
            return _FakeResp(f"{valid_sha}  ffmpeg-release-essentials.zip\n".encode("utf-8"))
        return _FakeResp(zip_bytes)

    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", _urlopen_ok)
    ok, msg = bootstrap.install_ffmpeg_portable()
    assert ok, msg
    assert (tmp_path / "ffmpeg.exe").read_bytes() == b"fake-ffmpeg-binary"
    assert (tmp_path / "ffprobe.exe").read_bytes() == b"fake-ffprobe-binary"

    # 2. Tampered SHA-256 must fail before extraction
    tampered_sha = "0" * 64

    def _urlopen_bad(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith(".sha256"):
            return _FakeResp(tampered_sha.encode("utf-8"))
        return _FakeResp(zip_bytes)

    (tmp_path / "ffmpeg.exe").unlink()
    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", _urlopen_bad)
    ok_bad, msg_bad = bootstrap.install_ffmpeg_portable()
    assert ok_bad is False
    assert "mismatch" in msg_bad.lower()
    assert not (tmp_path / "ffmpeg.exe").exists()


def test_cross_platform_shell_tray_updates_and_undo_pruning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Cross-platform shell_ext on Linux and macOS (mocked HOME & sys.platform)
    monkeypatch.setattr(shell_ext.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(shell_ext.sys, "platform", "linux")
    ok_l, _ = shell_ext.install_shell_menu()
    assert ok_l and shell_ext.is_shell_installed() is True
    assert (tmp_path / ".local/share/applications/srt-embedder.desktop").exists()
    shell_ext.uninstall_shell_menu()
    assert shell_ext.is_shell_installed() is False

    monkeypatch.setattr(shell_ext.sys, "platform", "darwin")
    ok_m, _ = shell_ext.install_shell_menu()
    assert ok_m and shell_ext.is_shell_installed() is True
    shell_ext.uninstall_shell_menu()
    assert shell_ext.is_shell_installed() is False

    # 2. Cross-platform tray startup + notifications on Linux & macOS
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    monkeypatch.setattr(tray.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(tray.sys, "platform", "darwin")
    ok_td, _ = tray.install_watch_startup(str(watch_dir), recursive=True)
    assert ok_td and tray.is_watch_startup_installed() is True
    tray.uninstall_watch_startup()
    assert tray.is_watch_startup_installed() is False

    # 3. GitHub Release update checker (mocked HTTP)
    class _FakeRel:
        def read(self, _n=-1):
            return json.dumps({"tag_name": "v9.9.0", "html_url": "https://example.com/rel"}).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", lambda _req, timeout=6: _FakeRel())
    upd = bootstrap.check_for_updates("owner/srt-embedder")
    assert upd["checked"] is True and upd["update_available"] is True and upd["latest_version"] == "9.9.0"

    # 4. Undo age/size pruning
    b1 = tmp_path / "old.bak"
    b2 = tmp_path / "new.bak"
    b1.write_bytes(b"1" * 100)
    b2.write_bytes(b"2" * 100)
    recs = [
        {"tx_id": "old", "created_at": "2020-01-01T00:00:00+00:00", "backup_path": str(b1), "backup_size_bytes": 100},
        {"tx_id": "new", "created_at": "2026-11-21T00:00:00+00:00", "backup_path": str(b2), "backup_size_bytes": 100},
    ]
    pruned = undo._prune_records(recs, max_keep=20, max_age_days=14, max_total_gb=10.0)
    assert len(pruned) == 1 and pruned[0]["tx_id"] == "new"
    assert not b1.exists() and b2.exists()
