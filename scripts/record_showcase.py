"""Locked 60.000 FPS Virtual-Time Recordly Studio Showcase Recorder (Zero Stutter, Zero Dropped Frames)."""

from __future__ import annotations

import base64
import http.server
import json
import shutil
import socketserver
import subprocess
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI_DIR = ROOT / "srt_embedder" / "ui"
MEDIA_DIR = ROOT / "docs" / "media"

FPS = 60
FRAME_MS = 1000.0 / FPS

VIRTUAL_CLOCK_JS = r"""
(() => {
  if (window.__vcInstalled) return;
  window.__vcInstalled = true;
  let now = 0;
  let rafId = 1, timerId = 1;
  let rafs = [];
  const timers = new Map();
  const realDateNow = Date.now.bind(Date);

  performance.now = () => now;
  Date.now = () => Math.floor(1700000000000 + now);
  window.requestAnimationFrame = (cb) => {
    const id = rafId++;
    rafs.push({ id, cb });
    return id;
  };
  window.cancelAnimationFrame = (id) => {
    rafs = rafs.filter(r => r.id !== id);
  };
  window.setTimeout = (fn, delay = 0, ...args) => {
    const id = timerId++;
    timers.set(id, { at: now + Math.max(0, Number(delay) || 0), fn, args, interval: null });
    return id;
  };
  window.clearTimeout = (id) => { timers.delete(id); };
  window.setInterval = (fn, delay = 0, ...args) => {
    const id = timerId++;
    const step = Math.max(1, Number(delay) || 16);
    timers.set(id, { at: now + step, fn, args, interval: step });
    return id;
  };
  window.clearInterval = (id) => { timers.delete(id); };

  const handledAnims = new WeakSet();
  window.__stepClock = (dt) => {
    now += dt;
    let guard = 0;
    while (guard++ < 1000) {
      const due = [...timers.entries()]
        .filter(([, t]) => t.at <= now)
        .sort((a, b) => a[1].at - b[1].at);
      if (!due.length) break;
      const [id, t] = due[0];
      if (t.interval !== null) {
        t.at += t.interval;
      } else {
        timers.delete(id);
      }
      if (typeof t.fn === 'function') {
        try { t.fn(...t.args); } catch (e) {}
      }
    }
    const batch = rafs.splice(0, rafs.length);
    for (const r of batch) {
      try { r.cb(now); } catch (e) {}
    }
    // Flush CSS style/layout so transitions started in this tick appear in getAnimations()
    if (document.documentElement) void document.documentElement.offsetHeight;
    for (const anim of document.getAnimations({ subtree: true })) {
      if (anim.playState === 'idle' || anim.playState === 'finished') continue;
      const timing = anim.effect && anim.effect.getComputedTiming ? anim.effect.getComputedTiming() : null;
      const endTime = (timing && typeof timing.endTime === 'number') ? timing.endTime : Infinity;
      if (!handledAnims.has(anim)) {
        handledAnims.add(anim);
        anim.pause();
        anim.currentTime = Math.min(endTime, dt);
      } else {
        const cur = typeof anim.currentTime === 'number' ? anim.currentTime : 0;
        if (cur < endTime) {
          anim.currentTime = Math.min(endTime, cur + dt);
        }
      }
    }
    return now;
  };
})();
"""

STAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body {
    width: 1920px;
    height: 1200px;
    overflow: hidden;
    background: #07090d;
    font-family: "Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
  }

  .studio-bg {
    position: fixed;
    inset: 0;
    background:
      radial-gradient(ellipse 68% 55% at 22% 18%, rgba(245, 158, 11, 0.24), transparent 62%),
      radial-gradient(ellipse 62% 52% at 82% 82%, rgba(56, 189, 248, 0.16), transparent 60%),
      radial-gradient(ellipse 50% 45% at 55% 45%, rgba(217, 119, 6, 0.08), transparent 65%),
      linear-gradient(145deg, #0b0e14 0%, #07090d 55%, #0c1018 100%);
  }

  .studio-grid {
    position: fixed;
    inset: 0;
    background-image: radial-gradient(rgba(255, 255, 255, 0.06) 1px, transparent 1px);
    background-size: 28px 28px;
    opacity: 0.55;
  }

  .camera-viewport {
    position: fixed;
    inset: 0;
    display: grid;
    place-items: center;
    overflow: hidden;
  }

  #camera-rig {
    width: 1520px;
    height: 980px;
    transform-origin: center center;
    will-change: transform;
    display: grid;
    place-items: center;
  }

  .window-frame {
    position: relative;
    width: 1240px;
    height: 800px;
    border-radius: 18px;
    overflow: hidden;
    background: #0a0c10;
    border: 1px solid rgba(255, 255, 255, 0.14);
    box-shadow:
      0 42px 110px rgba(0, 0, 0, 0.78),
      0 14px 36px rgba(0, 0, 0, 0.52),
      inset 0 1px 0 rgba(255, 255, 255, 0.12);
    transform: translateZ(0);
  }

  iframe#app-frame {
    width: 1240px;
    height: 800px;
    border: 0;
    display: block;
    background: #0a0c10;
  }

  #rec-cursor {
    position: absolute;
    left: 620px;
    top: 400px;
    width: 26px;
    height: 26px;
    z-index: 500;
    pointer-events: none;
    filter: drop-shadow(0 4px 10px rgba(0, 0, 0, 0.55));
    transform-origin: 4px 2px;
  }

  .click-ripple {
    position: absolute;
    width: 36px;
    height: 36px;
    margin-left: -18px;
    margin-top: -18px;
    border-radius: 50%;
    border: 2.5px solid #f59e0b;
    background: rgba(245, 158, 11, 0.18);
    pointer-events: none;
    z-index: 490;
    animation: ripple-out 420ms cubic-bezier(0.22, 1, 0.36, 1) forwards;
  }

  @keyframes ripple-out {
    from { opacity: 0.95; transform: scale(0.35); }
    to   { opacity: 0; transform: scale(1.9); }
  }

  #hud-pill {
    position: fixed;
    bottom: 40px;
    left: 50%;
    transform: translateX(-50%) translateY(16px) scale(0.96);
    opacity: 0;
    z-index: 600;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 18px;
    border-radius: 999px;
    background: rgba(17, 20, 26, 0.9);
    border: 1px solid rgba(255, 255, 255, 0.16);
    box-shadow: 0 18px 42px rgba(0, 0, 0, 0.55), inset 0 1px 0 rgba(255, 255, 255, 0.1);
    backdrop-filter: blur(12px);
    color: #f1f4f9;
    font-size: 14px;
    font-weight: 600;
    letter-spacing: -0.01em;
    transition: opacity 240ms cubic-bezier(0.22, 1, 0.36, 1), transform 240ms cubic-bezier(0.22, 1, 0.36, 1);
  }
  #hud-pill.show {
    opacity: 1;
    transform: translateX(-50%) translateY(0) scale(1);
  }
  .hud-kbd {
    font-family: "Cascadia Code", monospace;
    font-size: 12px;
    padding: 3px 8px;
    border-radius: 6px;
    background: rgba(245, 158, 11, 0.18);
    border: 1px solid rgba(245, 158, 11, 0.45);
    color: #fcd34d;
  }

  #title-card {
    position: fixed;
    inset: 0;
    z-index: 700;
    display: grid;
    place-items: center;
    background: rgba(7, 9, 13, 0.84);
    backdrop-filter: blur(14px);
    opacity: 1;
    transition: opacity 380ms cubic-bezier(0.22, 1, 0.36, 1);
    pointer-events: none;
  }
  #title-card.hidden {
    opacity: 0;
  }
  .title-inner {
    text-align: center;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 14px;
    transform: scale(1);
    transition: transform 380ms cubic-bezier(0.22, 1, 0.36, 1);
  }
  #title-card.hidden .title-inner {
    transform: scale(0.96);
  }
  .title-badge {
    width: 68px;
    height: 68px;
    border-radius: 18px;
    background: rgba(245, 158, 11, 0.14);
    border: 1.5px solid rgba(245, 158, 11, 0.45);
    display: grid;
    place-items: center;
    color: #f59e0b;
    box-shadow: 0 20px 50px rgba(245, 158, 11, 0.18);
  }
</style>
</head>
<body>
  <div class="studio-bg"></div>
  <div class="studio-grid"></div>

  <div class="camera-viewport">
    <div id="camera-rig">
      <div class="window-frame" id="win-frame">
        <iframe id="app-frame" src="/index.html"></iframe>
        <div id="rec-cursor">
          <svg width="26" height="26" viewBox="0 0 24 24" fill="#ffffff" stroke="#0b0d10" stroke-width="1.5">
            <path d="M4 2l15 10.5-6.6 1.5-3.2 6z"/>
          </svg>
        </div>
      </div>
    </div>
  </div>

  <div id="hud-pill"><span id="hud-text"></span></div>

  <div id="title-card">
    <div class="title-inner">
      <div class="title-badge">
        <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">
          <rect x="2" y="4" width="20" height="16" rx="3" />
          <path d="M7 15h4M15 15h2M7 11h2M13 11h4" />
        </svg>
      </div>
      <div style="font-size:40px;font-weight:700;color:#f1f4f9;letter-spacing:-0.025em;">SRT Embedder</div>
      <div style="font-size:17px;color:#98a2b3;">Lossless Muxing · Smart Fallbacks · GPU Hardsub Studio</div>
    </div>
  </div>

<script>
  const cam = { x: 0, y: 0, z: 1.28, tx: 0, ty: 0, tz: 1.28, vx: 0, vy: 0, vz: 0 };
  const rig = document.getElementById('camera-rig');
  let lastT = null;

  function tickCam(now) {
    if (lastT === null) lastT = now;
    const dt = Math.min(0.035, Math.max(0.001, (now - lastT) / 1000));
    lastT = now;
    const k = 95, d = 19.5;
    for (const axis of [['x','vx','tx'], ['y','vy','ty'], ['z','vz','tz']]) {
      const [p, v, tgt] = axis;
      const force = (cam[tgt] - cam[p]) * k;
      cam[v] = (cam[v] + force * dt) * Math.exp(-d * dt);
      cam[p] += cam[v] * dt;
    }
    rig.style.transform = `translate3d(${cam.x.toFixed(2)}px, ${cam.y.toFixed(2)}px, 0) scale(${cam.z.toFixed(4)})`;
    requestAnimationFrame(tickCam);
  }
  requestAnimationFrame(tickCam);

  window.__setCamera = (zoom, focusX = 620, focusY = 400) => {
    cam.tz = zoom;
    const dx = 620 - focusX;
    const dy = 400 - focusY;
    const rawX = dx * Math.max(0, zoom - 1.15) * 0.9;
    const rawY = dy * Math.max(0, zoom - 1.15) * 0.9;
    cam.tx = Math.max(-320, Math.min(320, rawX));
    cam.ty = Math.max(-210, Math.min(210, rawY));
  };

  window.__setCursor = (x, y, pressed = false) => {
    const c = document.getElementById('rec-cursor');
    c.style.left = x + 'px';
    c.style.top = y + 'px';
    c.style.transform = pressed ? 'scale(0.82)' : 'scale(1)';
  };

  window.__rippleAt = (x, y) => {
    const frame = document.getElementById('win-frame');
    const r = document.createElement('div');
    r.className = 'click-ripple';
    r.style.left = x + 'px';
    r.style.top = y + 'px';
    frame.appendChild(r);
    setTimeout(() => r.remove(), 450);
  };

  let hudTimer = null;
  window.__showHud = (html, ms = 1400) => {
    const pill = document.getElementById('hud-pill');
    document.getElementById('hud-text').innerHTML = html;
    pill.classList.add('show');
    clearTimeout(hudTimer);
    hudTimer = setTimeout(() => pill.classList.remove('show'), ms);
  };

  window.__hideTitle = () => {
    document.getElementById('title-card').classList.add('hidden');
  };
  window.__showOutro = () => {
    const tc = document.getElementById('title-card');
    tc.querySelector('div[style*="font-size:17px"]').textContent = 'Ready for Daily Use · Windows GUI & CLI';
    tc.classList.remove('hidden');
  };
</script>
</body>
</html>
"""

MOCK_BRIDGE_JS = """
window.pywebview = {
  api: {
    get_environment: async () => ({
      appName: "SRT Embedder",
      version: "1.0.0",
      platform: "nt",
      frozen: false,
      shellInstalled: true,
      hwEncoders: ["h264_nvenc", "hevc_nvenc", "h264_qsv", "libx264"],
      watching: false,
      tools: {
        mkvmerge: { name: "mkvmerge", available: true, path: "C:\\\\Program Files\\\\MKVToolNix\\\\mkvmerge.exe", version: "mkvmerge v103.0" },
        mkvextract: { name: "mkvextract", available: true, path: "C:\\\\Program Files\\\\MKVToolNix\\\\mkvextract.exe", version: "mkvextract v103.0" },
        ffmpeg: { name: "ffmpeg", available: true, path: "C:\\\\ffmpeg\\\\bin\\\\ffmpeg.exe", version: "ffmpeg 9.0.1" },
        ffprobe: { name: "ffprobe", available: true, path: "C:\\\\ffmpeg\\\\bin\\\\ffprobe.exe", version: "ffprobe 9.0.1" }
      },
      undos: [{
        tx_id: "a84f21c0",
        created_at: "2026-11-20T22:45:00Z",
        original_path: "D:\\\\Movies\\\\Primate.2025.1080p.WEB-DL.mkv",
        backup_path: "D:\\\\Movies\\\\.Primate.bak",
        output_path: "D:\\\\Movies\\\\Primate.2025.1080p.WEB-DL.mkv",
        engine_used: "mkvmerge",
        backup_size_bytes: 1374576442
      }]
    }),
    inspect_dropped_paths: async (paths) => ({
      videos: paths.some(p => p.endsWith(".mkv")) ? [{
        path: "D:\\\\Movies\\\\Primate.2025.1080p.WEB-DL.HEVC.mkv",
        filename: "Primate.2025.1080p.WEB-DL.HEVC.mkv",
        container: "mkv",
        size_bytes: 1374576442,
        duration_sec: 5528,
        width: 1920,
        height: 800,
        fps: "23.976",
        streams: [
          { index: 0, codec_type: "video", codec_name: "hevc", language: "und", title: "", is_default: true, is_forced: false },
          { index: 1, codec_type: "audio", codec_name: "aac", language: "eng", title: "5.1 Surround", is_default: true, is_forced: false },
          { index: 2, codec_type: "subtitle", codec_name: "subrip", language: "eng", title: "English", is_default: false, is_forced: false },
          { index: 3, codec_type: "subtitle", codec_name: "subrip", language: "spa", title: "Spanish", is_default: false, is_forced: false }
        ]
      }] : [],
      subtitles: paths.filter(p => p.endsWith(".srt")).map((p) => p.includes("arabic") ? ({
        path: p,
        filename: "Primate.2025.1080p.WEBRip-[SubSource].ar.srt",
        size_bytes: 57339,
        encoding_detected: "utf-8-sig",
        has_bom: true,
        cue_count: 754,
        repaired_cues: 2,
        stripped_tags_count: 0,
        detected_lang_code: "ara",
        detected_lang_name: "Arabic",
        source_format: "srt",
        sample_cues: [
          { index: 1, start: "00:00:30,450", end: "00:00:34,950", text: "سُجلت أول حالة لمرض رهاب الماء عام 2300 قبل الميلاد." },
          { index: 2, start: "00:01:00,769", end: "00:01:03,063", text: "مرحباً يا بن. الدكتور لامبرت هنا." },
          { index: 3, start: "01:22:39,852", end: "01:22:41,208", text: "نحن أيضاً نحبكِ يا أختاه." }
        ]
      }) : ({
        path: p,
        filename: "Primate.2025.Commentary.en.srt",
        size_bytes: 42110,
        encoding_detected: "cp1252",
        has_bom: false,
        cue_count: 618,
        repaired_cues: 0,
        stripped_tags_count: 0,
        detected_lang_code: "eng",
        detected_lang_name: "English",
        source_format: "vtt",
        sample_cues: [
          { index: 1, start: "00:01:00,799", end: "00:01:03,063", text: "Hey, Ben. Doc Lambert here." }
        ]
      })),
      folders: [],
      errors: []
    }),
    run_sync_check: async () => ({
      comparable: true,
      ref_stream_index: 2,
      ref_language: "eng",
      median_diff_ms: -42,
      min_diff_ms: -68,
      max_diff_ms: 0,
      drift_ms: 68,
      verdict: "in-sync",
      recommended_offset_ms: 0,
      samples: [
        { minute: 1, ref_time: "00:01:00,799", srt_time: "00:01:00,769", diff_ms: -30 },
        { minute: 20, ref_time: "00:20:01,896", srt_time: "00:20:01,896", diff_ms: 0 },
        { minute: 40, ref_time: "00:40:29,644", srt_time: "00:40:29,593", diff_ms: -51 },
        { minute: 70, ref_time: "01:10:00,413", srt_time: "01:10:00,362", diff_ms: -51 }
      ]
    }),
    start_embed: async (payload) => {
      setTimeout(() => window.__onMuxProgress({ percent: 26, phase: "Muxing (mkvmerge)", detail: "26%" }), 170);
      setTimeout(() => window.__onMuxProgress({ percent: 66, phase: "Muxing (mkvmerge)", detail: "66%" }), 420);
      setTimeout(() => window.__onMuxProgress({ percent: 95, phase: "Verifying (ffprobe)", detail: "95%" }), 680);
      setTimeout(() => window.__onMuxComplete({
        success: true,
        video_path: payload.video_path,
        output_path: "D:\\\\Movies\\\\Primate.2025.1080p.WEB-DL.HEVC [subtitled].mkv",
        engine_used: "mkvmerge",
        fallbacks_triggered: [],
        elapsed_sec: 0.92,
        output_size_bytes: 1374634120,
        subtitle_streams_after: 3,
        undo_tx_id: null,
        undos: []
      }), 950);
      return { accepted: true };
    },
    scan_folder: async (folder) => ({
      ok: true,
      folder,
      unmatched_videos: [],
      unmatched_subs: [],
      pairs: [
        { video_name: "Dune.Part.Two.2024.2160p.mkv", confidence: "exact", subtitles: [{ filename: "Dune.Part.Two.2024.ar.srt", lang_code: "ara" }] },
        { video_name: "Severance.S02E01.1080p.mkv", confidence: "fuzzy", subtitles: [{ filename: "Severance.S02E01.eng.srt", lang_code: "eng" }] }
      ]
    }),
    start_watch: async () => {
      setTimeout(() => window.__onWatchEvent({ timestamp: "23:14:02", kind: "info", message: "Watching D:\\\\Downloads\\\\Movies (recursive=true)" }), 120);
      setTimeout(() => window.__onWatchEvent({ timestamp: "23:14:04", kind: "paired", message: "Detected pair: Blade.Runner.2049.mkv (1 sub)" }), 450);
      setTimeout(() => window.__onWatchEvent({ timestamp: "23:14:06", kind: "success", message: "Embedded Blade.Runner.2049 [subtitled].mkv (mkvmerge, 1.1s)", output_path: "D:\\\\Downloads\\\\Movies\\\\Blade.Runner.2049 [subtitled].mkv" }), 820);
      return { success: true, watching: true };
    },
    stop_watch: async () => ({ success: true, watching: false }),
    reveal_in_explorer: async () => true,
    open_media: async () => true,
    toggle_shell_menu: async (en) => ({ success: true, message: "Updated Explorer menu.", shellInstalled: en }),
    pick_videos: async () => [],
    pick_subtitles: async () => [],
    pick_folder: async () => "D:\\\\Movies\\\\Season 2"
  }
};
"""


def start_ui_server():
    class StageHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(UI_DIR), **kwargs)

        def do_GET(self):
            if self.path == "/__stage__.html":
                body = STAGE_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

        def log_message(self, format, *args):
            pass

    httpd = socketserver.TCPServer(("127.0.0.1", 0), StageHandler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port


class DeterministicRecorder:
    """Steps synchronous virtual time by exact 16.6667ms ticks and captures 1 PNG per frame."""

    def __init__(self, page, app_frame, cdp, frames_dir: Path) -> None:
        self.page = page
        self.app_frame = app_frame
        self.cdp = cdp
        self.frames_dir = frames_dir
        self.frame_idx = 0

    def advance_ms(self, ms: float) -> None:
        self.page.evaluate("dt => window.__stepClock && window.__stepClock(dt)", ms)
        self.app_frame.evaluate("dt => window.__stepClock && window.__stepClock(dt)", ms)

    def capture_frame(self) -> None:
        res = self.cdp.send("Page.captureScreenshot", {"format": "png", "fromSurface": True})
        out = self.frames_dir / f"frame_{self.frame_idx:05d}.png"
        out.write_bytes(base64.b64decode(res["data"]))
        self.frame_idx += 1

    def hold_frames(self, seconds: float) -> None:
        n = max(1, int(round(seconds * FPS)))
        for _ in range(n):
            self.advance_ms(FRAME_MS)
            self.capture_frame()


def move_and_click(rec: DeterministicRecorder, outer_page, app_frame, selector: str, zoom: float = 1.48, hud: str = "", pause_sec: float = 0.35):
    loc = app_frame.locator(selector).first
    coords = loc.evaluate("""el => {
      const r = el.getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    }""")
    if not coords:
        return
    tx = float(coords["x"])
    ty = float(coords["y"])

    if not hasattr(move_and_click, "_pos"):
        move_and_click._pos = (620.0, 400.0)
    sx, sy = move_and_click._pos

    outer_page.evaluate(f"window.__setCamera({zoom}, {tx:.1f}, {ty:.1f})")
    if hud:
        outer_page.evaluate(f"window.__showHud({json.dumps(hud)}, 1450)")

    # Exact 28 frames (~0.46s at 60fps) cubic-eased cursor glide
    glide_frames = 28
    for i in range(1, glide_frames + 1):
        t = i / glide_frames
        ease = 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2
        cx = sx + (tx - sx) * ease
        cy = sy + (ty - sy) * ease
        outer_page.evaluate(f"window.__setCursor({cx:.1f}, {cy:.1f}, false)")
        rec.advance_ms(FRAME_MS)
        rec.capture_frame()

    move_and_click._pos = (tx, ty)
    outer_page.evaluate(f"window.__setCursor({tx:.1f}, {ty:.1f}, true); window.__rippleAt({tx:.1f}, {ty:.1f});")
    loc.dispatch_event("click")
    rec.hold_frames(0.10)
    outer_page.evaluate(f"window.__setCursor({tx:.1f}, {ty:.1f}, false)")
    rec.hold_frames(pause_sec)


def main():
    from playwright.sync_api import sync_playwright

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    frames_dir = ROOT / "build" / "locked60_frames"
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)

    httpd, port = start_ui_server()
    stage_url = f"http://127.0.0.1:{port}/__stage__.html"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1920, "height": 1200}, device_scale_factor=1)
        context.add_init_script(VIRTUAL_CLOCK_JS)
        context.add_init_script(MOCK_BRIDGE_JS)
        page = context.new_page()
        cdp = context.new_cdp_session(page)

        page.goto(stage_url, wait_until="networkidle")
        app_frame = page.frames[1]

        rec = DeterministicRecorder(page, app_frame, cdp, frames_dir)
        for _ in range(4):
            rec.advance_ms(FRAME_MS)  # settle initial rAF + environment init

        # 1. Intro Title Card -> Smooth Reveal
        rec.hold_frames(0.55)
        page.evaluate("window.__hideTitle(); window.__setCamera(1.28, 620, 400);")
        rec.hold_frames(0.55)

        # 2. Drop Video + Arabic & English Subtitles
        page.evaluate("window.__showHud('Drag &amp; Drop Video + Subtitles', 1400)")
        app_frame.evaluate("""window.__onNativeDrop([
            'D:\\\\Movies\\\\Primate.2025.1080p.WEB-DL.HEVC.mkv',
            'D:\\\\Movies\\\\Primate.2025.arabic.srt',
            'D:\\\\Movies\\\\Primate.2025.english.srt'
        ])""")
        rec.hold_frames(0.75)

        # 3. Recordly Zoom -> Nudge +100ms, Clean SDH, FLIP Reorder, Sync Check
        move_and_click(rec, page, app_frame, ".nudge-btn:nth-of-type(2)", zoom=1.52, hud="Nudge Timing <span class='hud-kbd'>+100ms</span>", pause_sec=0.30)
        move_and_click(rec, page, app_frame, "button:has-text('Clean SDH')", zoom=1.52, hud="Strip SDH Descriptions &amp; Speaker Tags", pause_sec=0.30)
        move_and_click(rec, page, app_frame, "button[data-tip='Move track down']", zoom=1.50, hud="Smooth FLIP Track Reordering", pause_sec=0.45)
        move_and_click(rec, page, app_frame, "button[data-tip='Move track up']", zoom=1.50, hud="", pause_sec=0.35)
        move_and_click(rec, page, app_frame, "button:has-text('Sync')", zoom=1.42, hud="Verify Sync Against Embedded Track", pause_sec=0.80)
        move_and_click(rec, page, app_frame, "#btn-close-sync", zoom=1.32, hud="", pause_sec=0.28)

        # 4. Hardsub GPU & Live 16:9 Style Preview
        page.evaluate("window.__setCamera(1.48, 960, 320); window.__showHud('GPU Hardsub &amp; Live Style Studio', 1500);")
        app_frame.select_option("#opt-mode", "hardsub")
        app_frame.dispatch_event("#opt-mode", "change")
        rec.hold_frames(0.45)
        app_frame.fill("#hs-size", "30")
        app_frame.dispatch_event("#hs-size", "input")
        app_frame.evaluate("document.getElementById('hs-color').value='#fbbf24'; document.getElementById('hs-color').dispatchEvent(new Event('input'));")
        rec.hold_frames(0.55)

        # 5. Auto Smart Fallback -> Embed with Peak-End Checkmark
        app_frame.select_option("#opt-mode", "auto")
        app_frame.dispatch_event("#opt-mode", "change")
        rec.hold_frames(0.30)
        move_and_click(rec, page, app_frame, "#btn-embed", zoom=1.48, hud="Embed Subtitles <span class='hud-kbd'>Ctrl + Enter</span>", pause_sec=1.25)

        # 6. Batch Folder + Watch Daemon + Instant Light Theme
        page.evaluate("window.__setCamera(1.28, 620, 400)")
        move_and_click(rec, page, app_frame, "#tab-batch", zoom=1.30, hud="Batch Folder Auto-Pairing", pause_sec=0.30)
        app_frame.evaluate("document.getElementById('batch-folder-label').textContent='D:\\\\Movies\\\\Season 2'")
        rec.hold_frames(0.38)

        move_and_click(rec, page, app_frame, "#tab-watch", zoom=1.30, hud="Watch Daemon · Auto-Mux Incoming Media", pause_sec=0.30)
        app_frame.evaluate("window.pywebview.api.start_watch({directory:'D:\\\\Downloads\\\\Movies'}); document.getElementById('watch-folder-label').textContent='D:\\\\Downloads\\\\Movies'; document.getElementById('watch-dot').classList.add('active');")
        rec.hold_frames(1.10)

        move_and_click(rec, page, app_frame, "#tab-single", zoom=1.28, hud="", pause_sec=0.30)
        move_and_click(rec, page, app_frame, "#btn-theme", zoom=1.28, hud="Instant Daylight Studio Theme", pause_sec=0.65)

        # 7. Outro Card
        page.evaluate("window.__showOutro()")
        rec.hold_frames(0.75)

        context.close()
        browser.close()

    httpd.shutdown()

    ffmpeg = shutil.which("ffmpeg") or r"C:\ffmpeg_folder\ffmpeg-essentials_build\bin\ffmpeg.exe"
    mp4_out = MEDIA_DIR / "demo.mp4"
    gif_out = MEDIA_DIR / "demo.gif"
    pattern = str(frames_dir / "frame_%05d.png")

    # Locked 60.000 FPS CFR H.264 MP4 with exact BT.709 color calibration
    subprocess.run(
        [
            ffmpeg, "-y", "-v", "error",
            "-framerate", "60", "-i", pattern,
            "-vf", "scale=1920:1200:flags=lanczos+accurate_rnd+full_chroma_int:out_color_matrix=bt709:out_range=tv,format=yuv420p",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
            "-c:v", "libx264", "-crf", "12", "-preset", "slow",
            "-profile:v", "high", "-aq-mode", "3",
            "-x264-params", "deblock=-1,-1",
            "-r", "60", "-movflags", "+faststart",
            str(mp4_out),
        ],
        check=True,
    )

    # Crisp 20fps RGB GIF directly from locked 60fps PNG sequence (every 3rd frame)
    subprocess.run(
        [
            ffmpeg, "-y", "-v", "error",
            "-framerate", "60", "-i", pattern,
            "-vf", "fps=20,scale=960:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=256:stats_mode=diff[p];[s1][p]paletteuse=dither=floyd_steinberg",
            str(gif_out),
        ],
        check=True,
    )

    shutil.rmtree(frames_dir, ignore_errors=True)
    print(f"Rendered {rec.frame_idx} locked-60fps frames ({rec.frame_idx / FPS:.2f}s) -> {mp4_out.name} ({mp4_out.stat().st_size} bytes), {gif_out.name} ({gif_out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
