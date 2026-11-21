"""Automated headless Playwright UI smoke check against srt_embedder/ui/index.html."""

from __future__ import annotations

import http.server
import socketserver
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

UI_DIR = Path(__file__).resolve().parent.parent / "srt_embedder" / "ui"


class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(UI_DIR), **kwargs)

    def log_message(self, *args):
        pass


def main() -> int:
    with socketserver.TCPServer(("127.0.0.1", 0), _Handler) as srv:
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        errors: list[str] = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1140, "height": 770})
                page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
                page.on(
                    "console",
                    lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None,
                )
                page.add_init_script(
                    """
                    window.pywebview = { api: {
                      get_environment: async () => ({
                        appName: 'SRT Embedder', version: '1.1.0', platform: 'nt',
                        shellInstalled: false, watchStartup: false, hwEncoders: ['h264_nvenc', 'libx264'],
                        watching: false,
                        tools: { mkvmerge: {available:true}, ffmpeg: {available:true}, mkvextract:{available:true}, ffprobe:{available:true} },
                        undos: [],
                        config: {
                          active_profile: 'cinema-default', express_drop: false, auto_discover_companions: true,
                          watch: { directory: 'C:/Movies', recursive: false, auto_sync: true, clean_sdh: true, clean_ads: true, sub_cleanup: 'archive', notify_toast: true },
                          profiles: {
                            'cinema-default': { label: 'Cinema Default', mode: 'auto', existing_subs: 'keep', auto_sync: false },
                            'arabic-rtl': { label: 'Arabic RTL Clean', mode: 'auto', existing_subs: 'replace-same-lang', auto_sync: true }
                          }
                        }
                      }),
                      set_profile: async (name) => ({ success: true, config: { active_profile: name, profiles: { 'arabic-rtl': { label: 'Arabic RTL Clean', mode: 'auto', existing_subs: 'replace-same-lang', auto_sync: true } } } }),
                      save_profile: async (name, prof) => ({ success: true, slug: 'saved-test', config: { active_profile: 'saved-test', profiles: { 'cinema-default': { label: 'Cinema Default' }, 'saved-test': { label: name, ...prof } } } }),
                      audit_folder: async (dir, lang, rec) => ({ ok: true, directory: dir, target_lang: lang, total_videos: 2, ready_count: 1, pairable_count: 1, missing_count: 0, items: [{video_path:'a.mkv', filename:'a.mkv', status:'ready', embedded_langs:['ara'], companion_subs:[]}, {video_path:'b.mkv', filename:'b.mkv', status:'pairable', embedded_langs:[], companion_subs:['b.ar.srt']}] }),
                      scan_folder: async (dir, rec) => ({ ok: true, folder: dir, pairs: [], unmatched_videos: [], unmatched_subs: [] }),
                      update_automation_prefs: async (p) => ({ success: true, config: {} })
                    }};
                    """
                )
                page.goto(f"http://127.0.0.1:{port}/index.html", wait_until="networkidle")
                page.wait_for_timeout(150)

                opts = page.locator("#opt-profile option").all_text_contents()
                assert len(opts) == 2, f"Expected 2 profiles, got {opts}"

                page.select_option("#opt-profile", "arabic-rtl")
                page.wait_for_timeout(100)
                assert page.is_checked("#opt-auto-sync"), "Profile switch did not toggle #opt-auto-sync"

                # Test Feelcraft Profile Manager modal (no window.prompt)
                page.click("#btn-save-profile")
                page.wait_for_timeout(100)
                assert page.locator("#profile-modal.open").count() == 1
                page.fill("#profile-name-input", "Smoke Preset")
                page.click("#btn-profile-confirm-save")
                page.wait_for_timeout(100)
                page.keyboard.press("Escape")
                page.wait_for_timeout(100)
                assert page.locator("#profile-modal.open").count() == 0

                page.click("#tab-batch")
                page.wait_for_timeout(100)
                page.click("#tab-watch")
                page.wait_for_timeout(100)
                assert page.text_content("#watch-folder-label") == "C:/Movies"

                page.click("#btn-settings")
                page.wait_for_timeout(100)
                assert page.locator("#settings-modal.open").count() == 1
                page.select_option("#opt-ui-lang", "ar")
                page.wait_for_timeout(80)
                assert page.evaluate("document.documentElement.dir") == "rtl"
                page.select_option("#opt-ui-lang", "en")
                page.wait_for_timeout(80)
                assert page.evaluate("document.documentElement.dir") == "ltr"
                page.keyboard.press("Escape")
                page.wait_for_timeout(100)
                assert page.locator("#settings-modal.open").count() == 0

                assert not errors, f"UI console/page errors: {errors}"
                browser.close()
        finally:
            srv.shutdown()
    print("UI smoke test PASSED (0 errors).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
