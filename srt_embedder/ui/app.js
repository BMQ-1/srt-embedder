(() => {
  "use strict";

  const PREFS_KEY = "srt_embedder_studio_prefs_v2";
  const RECENTS_KEY = "srt_embedder_recents_v1";
  const TAB_ORDER = ["single", "batch", "watch"];

  const LANG_CHOICES = [
    ["auto", "Auto-detect"],
    ["ara", "Arabic (ara)"],
    ["eng", "English (eng)"],
    ["spa", "Spanish (spa)"],
    ["fre", "French (fre)"],
    ["ger", "German (ger)"],
    ["ita", "Italian (ita)"],
    ["por", "Portuguese (por)"],
    ["rus", "Russian (rus)"],
    ["jpn", "Japanese (jpn)"],
    ["kor", "Korean (kor)"],
    ["chi", "Chinese (chi)"],
    ["tur", "Turkish (tur)"],
    ["hin", "Hindi (hin)"],
    ["und", "Undetermined"],
  ];

  const state = {
    tab: "single",
    theme: "dark",
    env: null,
    config: null,
    video: null,
    subtitles: [],
    dragTrackIdx: null,
    batchFolder: null,
    batchPairs: [],
    unmatchedVideos: [],
    unmatchedSubs: [],
    auditReport: null,
    activeBatchKind: "batch", // batch | audit
    previewCues: [],
    watchFolder: null,
    watching: false,
    undos: [],
    running: false,
    lastResult: null,
    cmdIndex: 0,
  };

  const $ = (id) => document.getElementById(id);

  function escHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function fmtBytes(bytes) {
    if (!bytes) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let val = bytes;
    let i = 0;
    while (val >= 1024 && i < units.length - 1) {
      val /= 1024;
      i++;
    }
    return `${val.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
  }

  function fmtDur(sec) {
    const t = Math.max(0, Math.round(sec || 0));
    const h = String(Math.floor(t / 3600)).padStart(2, "0");
    const m = String(Math.floor((t % 3600) / 60)).padStart(2, "0");
    const s = String(t % 60).padStart(2, "0");
    return `${h}:${m}:${s}`;
  }

  function parseOffsetInput(raw) {
    const s = String(raw || "").trim().toLowerCase().replace(/,/g, "").replace(/\s+/g, "");
    if (!s) return 0;
    if (s.endsWith("ms")) return Math.round(parseFloat(s.slice(0, -2)) || 0);
    if (s.endsWith("s")) return Math.round((parseFloat(s.slice(0, -1)) || 0) * 1000);
    return Math.round(parseFloat(s) || 0);
  }

  function formatOffsetLabel(ms) {
    if (!ms) return "0ms";
    return `${ms > 0 ? "+" : ""}${ms}ms`;
  }

  /* Feelcraft Instant Theme Switch (zero transition lag across cards/text) */
  function applyTheme(theme, instant = false) {
    const next = theme === "light" ? "light" : "dark";
    if (instant) {
      document.documentElement.classList.add("theme-switching");
    }
    state.theme = next;
    document.documentElement.setAttribute("data-theme", next);
    if (instant) {
      void document.documentElement.offsetHeight;
      document.documentElement.classList.remove("theme-switching");
    }
  }

  function toggleTheme() {
    applyTheme(state.theme === "dark" ? "light" : "dark", true);
    savePrefs();
  }

  const I18N = {
    en: { tabSingle: "Mux Studio", tabBatch: "Batch Folder", tabWatch: "Watch Daemon" },
    ar: { tabSingle: "استوديو الدمج", tabBatch: "مجلد دفعة", tabWatch: "مراقب المجلد" },
    es: { tabSingle: "Estudio Mux", tabBatch: "Carpeta Lote", tabWatch: "Monitor Carpeta" },
    fr: { tabSingle: "Studio Mux", tabBatch: "Dossier Lot", tabWatch: "Veille Dossier" },
  };

  function applyUiLanguage(lang) {
    const code = I18N[lang] ? lang : "en";
    document.documentElement.lang = code;
    document.documentElement.dir = code === "ar" ? "rtl" : "ltr";
    const dict = I18N[code];
    document.querySelectorAll("[data-i18n]").forEach((el) => {
      const key = el.getAttribute("data-i18n");
      if (dict[key]) el.textContent = dict[key];
    });
  }

  function activeProfileDefaults() {
    const cfg = state.config || {};
    const key = cfg.active_profile || "cinema-default";
    return (cfg.profiles && cfg.profiles[key]) || {};
  }

  function renderProfiles(cfg) {
    if (!cfg) return;
    state.config = cfg;
    if (cfg.ui_lang) {
      if ($("opt-ui-lang")) $("opt-ui-lang").value = cfg.ui_lang;
      applyUiLanguage(cfg.ui_lang);
    }
    const sel = $("opt-profile");
    if (!sel) return;
    const profiles = cfg.profiles || {};
    sel.innerHTML = "";
    for (const [key, prof] of Object.entries(profiles)) {
      const opt = document.createElement("option");
      opt.value = key;
      opt.textContent = prof.label || key;
      if (key === cfg.active_profile) opt.selected = true;
      sel.appendChild(opt);
    }
    if (typeof cfg.express_drop === "boolean") $("opt-express-drop").checked = cfg.express_drop;
    if (typeof cfg.auto_discover_companions === "boolean") $("opt-autodiscover").checked = cfg.auto_discover_companions;

    const actProf = profiles[cfg.active_profile] || {};
    if (actProf.output_template && $("opt-naming")) $("opt-naming").value = actProf.output_template;
    if (actProf.prefer_sub_lang_when_foreign_audio != null && $("opt-prefer-sub")) {
      $("opt-prefer-sub").value = actProf.prefer_sub_lang_when_foreign_audio;
    }

    const w = cfg.watch || {};
    if (w.directory && !state.watchFolder) {
      state.watchFolder = w.directory;
      $("watch-folder-label").textContent = w.directory;
      $("btn-watch-toggle").disabled = false;
    }
    if (typeof w.recursive === "boolean") $("watch-recursive").checked = w.recursive;
    if (typeof w.auto_sync === "boolean") $("watch-autosync").checked = w.auto_sync;
    if (typeof w.clean_sdh === "boolean" || typeof w.clean_ads === "boolean") {
      $("watch-clean").checked = Boolean(w.clean_sdh || w.clean_ads);
    }
    if (typeof w.notify_toast === "boolean") $("watch-notify").checked = w.notify_toast;
    if (w.sub_cleanup) $("watch-cleanup").value = w.sub_cleanup;
  }

  function applyProfileToControls(prof) {
    if (!prof) return;
    if (prof.mode) $("opt-mode").value = prof.mode;
    if (prof.existing_subs) $("opt-existing").value = prof.existing_subs;
    if (typeof prof.fallback_to_mkv === "boolean") $("opt-mkv-fallback").checked = prof.fallback_to_mkv;
    if (typeof prof.replace_original === "boolean") $("opt-replace").checked = prof.replace_original;
    if (typeof prof.auto_sync === "boolean") $("opt-auto-sync").checked = prof.auto_sync;
    if (prof.output_template && $("opt-naming")) $("opt-naming").value = prof.output_template;
    if (prof.prefer_sub_lang_when_foreign_audio != null && $("opt-prefer-sub")) {
      $("opt-prefer-sub").value = prof.prefer_sub_lang_when_foreign_audio;
    }
    if (prof.crf) $("opt-crf").value = String(prof.crf);
    if (prof.preset) $("opt-preset").value = prof.preset;
    const hs = prof.hardsub_style || {};
    if (hs.font_name) $("hs-font").value = hs.font_name;
    if (hs.font_size) $("hs-size").value = String(hs.font_size);
    if (hs.outline_width != null) $("hs-outline").value = String(hs.outline_width);
    if (hs.margin_v != null) $("hs-margin").value = String(hs.margin_v);
    if (hs.primary_color) $("hs-color").value = hs.primary_color;
    if (hs.encoder) $("hs-encoder").value = hs.encoder;
    syncHardsubDrawer();
    checkLanguageAdvisory();
    savePrefs();
  }

  function loadPrefs() {
    try {
      const raw = localStorage.getItem(PREFS_KEY);
      if (!raw) return;
      const p = JSON.parse(raw);
      if (p.theme) applyTheme(p.theme, true);
      if (p.mode) $("opt-mode").value = p.mode;
      if (p.existing_subs) $("opt-existing").value = p.existing_subs;
      if (typeof p.fallback_to_mkv === "boolean") $("opt-mkv-fallback").checked = p.fallback_to_mkv;
      if (typeof p.replace_original === "boolean") $("opt-replace").checked = p.replace_original;
      if (typeof p.auto_sync === "boolean") $("opt-auto-sync").checked = p.auto_sync;
      if (typeof p.batch_recursive === "boolean") $("batch-recursive").checked = p.batch_recursive;
      if (typeof p.watch_recursive === "boolean") $("watch-recursive").checked = p.watch_recursive;
      if (p.crf) $("opt-crf").value = String(p.crf);
      if (p.preset) $("opt-preset").value = p.preset;
      if (p.hs_font) $("hs-font").value = p.hs_font;
      if (p.hs_size) $("hs-size").value = String(p.hs_size);
      if (p.hs_outline != null) $("hs-outline").value = String(p.hs_outline);
      if (p.hs_margin != null) $("hs-margin").value = String(p.hs_margin);
      if (p.hs_color) $("hs-color").value = p.hs_color;
      if (p.hs_encoder) $("hs-encoder").value = p.hs_encoder;
    } catch (_) {}
  }

  function savePrefs() {
    try {
      localStorage.setItem(
        PREFS_KEY,
        JSON.stringify({
          theme: state.theme,
          mode: $("opt-mode").value,
          existing_subs: $("opt-existing").value,
          fallback_to_mkv: $("opt-mkv-fallback").checked,
          replace_original: $("opt-replace").checked,
          auto_sync: $("opt-auto-sync").checked,
          batch_recursive: $("batch-recursive").checked,
          watch_recursive: $("watch-recursive").checked,
          crf: Number($("opt-crf").value || 18),
          preset: $("opt-preset").value || "medium",
          hs_font: $("hs-font").value,
          hs_size: Number($("hs-size").value || 24),
          hs_outline: Number($("hs-outline").value || 2),
          hs_margin: Number($("hs-margin").value || 28),
          hs_color: $("hs-color").value,
          hs_encoder: $("hs-encoder").value,
        })
      );
    } catch (_) {}
  }

  /* Feelcraft Skip-Delay Tooltips (400ms first dwell, 0ms across siblings) */
  function initTooltips() {
    const tip = $("fc-tooltip");
    let openTimer = null;
    let warmTimer = null;
    let isWarm = false;
    let activeTarget = null;

    function positionTip(el) {
      const rect = el.getBoundingClientRect();
      const tipRect = tip.getBoundingClientRect();
      const left = Math.max(8, Math.min(window.innerWidth - tipRect.width - 8, rect.left + rect.width / 2 - tipRect.width / 2));
      const top = rect.bottom + 8 + tipRect.height > window.innerHeight ? rect.top - tipRect.height - 8 : rect.bottom + 8;
      tip.style.left = `${Math.round(left)}px`;
      tip.style.top = `${Math.round(top)}px`;
    }

    document.addEventListener("pointerover", (e) => {
      const el = e.target.closest("[data-tip]");
      if (!el || el === activeTarget) return;
      activeTarget = el;
      clearTimeout(openTimer);
      clearTimeout(warmTimer);
      const text = el.getAttribute("data-tip");
      if (!text) return;

      const show = () => {
        tip.textContent = text;
        tip.classList.add("open");
        positionTip(el);
        isWarm = true;
      };

      if (isWarm) {
        show();
      } else {
        openTimer = setTimeout(show, 400);
      }
    });

    document.addEventListener("pointerout", (e) => {
      if (!activeTarget) return;
      const to = e.relatedTarget?.closest?.("[data-tip]");
      if (to === activeTarget) return;
      clearTimeout(openTimer);
      activeTarget = null;
      tip.classList.remove("open");
      clearTimeout(warmTimer);
      warmTimer = setTimeout(() => {
        isWarm = false;
      }, 360);
    });
  }

  function getRecents() {
    try {
      const raw = localStorage.getItem(RECENTS_KEY);
      return raw ? JSON.parse(raw) : [];
    } catch (_) {
      return [];
    }
  }

  function pushRecent(videoPath, filename) {
    if (!videoPath || !filename) return;
    try {
      const list = getRecents().filter((r) => r.path.toLowerCase() !== videoPath.toLowerCase());
      list.unshift({ path: videoPath, name: filename });
      localStorage.setItem(RECENTS_KEY, JSON.stringify(list.slice(0, 4)));
      renderRecents();
    } catch (_) {}
  }

  function renderRecents() {
    const strip = $("recent-strip");
    if (!strip) return;
    const recents = getRecents();
    if (state.video || recents.length === 0) {
      strip.classList.add("hidden");
      strip.innerHTML = "";
      return;
    }
    strip.classList.remove("hidden");
    strip.innerHTML = `<span style="font-size:11px;color:var(--text-muted);align-self:center;margin-right:2px;">Recent:</span>`;
    recents.forEach((r) => {
      const btn = document.createElement("button");
      btn.className = "recent-pill";
      btn.setAttribute("data-tip", r.path);
      btn.innerHTML = `<span style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escHtml(r.name)}</span>`;
      btn.addEventListener("click", () => handlePaths([r.path]));
      strip.appendChild(btn);
    });
  }

  function showToast(message, options = {}) {
    const rack = $("toast-rack");
    const toast = document.createElement("div");
    toast.className = "toast";

    const textSpan = document.createElement("span");
    textSpan.textContent = message;
    toast.appendChild(textSpan);

    if (options.actionLabel && typeof options.onAction === "function") {
      const actionBtn = document.createElement("button");
      actionBtn.className = "btn";
      actionBtn.style.minHeight = "28px";
      actionBtn.style.padding = "0 10px";
      actionBtn.textContent = options.actionLabel;
      actionBtn.addEventListener("click", () => {
        options.onAction();
        toast.remove();
      });
      toast.appendChild(actionBtn);
    }

    rack.appendChild(toast);
    const ttl = options.durationMs || 4200;
    setTimeout(() => {
      toast.style.transition = "opacity 170ms cubic-bezier(0.4, 0, 1, 1), transform 170ms cubic-bezier(0.4, 0, 1, 1)";
      toast.style.opacity = "0";
      toast.style.transform = "translateY(8px) scale(0.97)";
      setTimeout(() => toast.remove(), 180);
    }, ttl);
  }

  function renderToolPills(tools) {
    const box = $("tool-pills");
    box.innerHTML = "";
    for (const key of ["mkvmerge", "ffmpeg"]) {
      const t = tools[key];
      const pill = document.createElement("span");
      pill.className = "status-pill";
      const dot = document.createElement("span");
      dot.className = `dot ${t?.available ? "ok" : "warn"}`;
      pill.appendChild(dot);
      pill.appendChild(document.createTextNode(key));
      box.appendChild(pill);
    }
  }

  function renderHwEncoders(hwList) {
    const sel = $("hs-encoder");
    const prev = sel.value;
    sel.innerHTML = `<option value="auto">Auto GPU (Best Available)</option>`;
    const labels = {
      h264_nvenc: "NVIDIA NVENC (H.264 GPU)",
      hevc_nvenc: "NVIDIA NVENC (HEVC GPU)",
      h264_qsv: "Intel QuickSync (QSV)",
      h264_amf: "AMD AMF (H.264 GPU)",
      libx264: "CPU libx264 (Universal)",
    };
    for (const enc of hwList || ["libx264"]) {
      const opt = document.createElement("option");
      opt.value = enc;
      opt.textContent = labels[enc] || enc;
      sel.appendChild(opt);
    }
    if ([...sel.options].some((o) => o.value === prev)) {
      sel.value = prev;
    }
  }

  function syncHardsubDrawer() {
    const isHard = $("opt-mode").value === "hardsub";
    $("hardsub-drawer").classList.toggle("collapsed", !isHard);
    updateStylePreview();
  }

  function updateStylePreview() {
    const el = $("hs-preview-text");
    if (!el) return;
    const font = $("hs-font").value || "Arial";
    const size = Math.max(12, Math.min(34, Math.round(Number($("hs-size").value || 24) * 0.68)));
    const outline = Math.max(0, Math.min(4, Number($("hs-outline").value || 2)));
    const margin = Math.max(6, Math.min(44, Math.round(Number($("hs-margin").value || 28) * 0.55)));
    const color = $("hs-color").value || "#ffffff";

    el.style.fontFamily = `${font}, sans-serif`;
    el.style.fontSize = `${size}px`;
    el.style.color = color;
    el.style.marginBottom = `${margin}px`;
    el.style.textShadow = outline
      ? `0 0 ${outline * 2}px #000, ${outline}px ${outline}px 0 #000, -${outline}px -${outline}px 0 #000, ${outline}px -${outline}px 0 #000, -${outline}px ${outline}px 0 #000`
      : "none";
  }

  function renderUndoHeader() {
    const btn = $("btn-undo-drawer");
    const cnt = $("undo-count");
    cnt.textContent = String(state.undos.length);
    btn.classList.toggle("hidden", state.undos.length === 0);
  }

  function checkLanguageAdvisory() {
    const adv = $("video-advisory");
    if (state.subtitles.length === 0) {
      adv.classList.add("hidden");
      adv.innerHTML = "";
      return;
    }

    if ($("opt-mode").value === "hardsub" && state.subtitles.length > 1) {
      adv.classList.remove("hidden");
      adv.innerHTML = `
        <span>Hardsub burns only Track 01 (<strong>${escHtml(state.subtitles[0].filename)}</strong>). Merge top 2 tracks to burn both together.</span>
        <button id="btn-adv-merge" class="btn" style="min-height:28px;padding:0 10px;font-size:11.5px;">Merge Bilingual (.ass)</button>
      `;
      $("btn-adv-merge").addEventListener("click", mergeTopTwoBilingual);
      return;
    }

    if ($("opt-mode").value === "hardsub" && state.env && Array.isArray(state.env.installedFonts)) {
      const selFont = $("hs-font").value;
      const fonts = state.env.installedFonts.map((f) => f.toLowerCase());
      if (fonts.length > 0 && !fonts.includes(selFont.toLowerCase())) {
        adv.classList.remove("hidden");
        adv.innerHTML = `
          <span>Font <strong>${escHtml(selFont)}</strong> was not detected on this system; FFmpeg may substitute another font.</span>
          <button id="btn-adv-font" class="btn" style="min-height:28px;padding:0 10px;font-size:11.5px;">Use ${escHtml(state.env.installedFonts[0])}</button>
        `;
        $("btn-adv-font").addEventListener("click", () => {
          const fallbackFont = state.env.installedFonts[0];
          if ([...$("hs-font").options].some((o) => o.value === fallbackFont)) {
            $("hs-font").value = fallbackFont;
            savePrefs();
            updateStylePreview();
            checkLanguageAdvisory();
          }
        });
        return;
      }
    }

    if (!state.video) {
      adv.classList.add("hidden");
      adv.innerHTML = "";
      return;
    }

    const existingSubs = (state.video.streams || []).filter((s) => s.codec_type === "subtitle");
    const existingLangs = new Set(existingSubs.map((s) => (s.language || "und").toLowerCase()));
    const dupLangs = state.subtitles
      .map((s) => (s.lang_code || "und").toLowerCase())
      .filter((l) => l !== "und" && l !== "auto" && existingLangs.has(l));

    if (dupLangs.length > 0 && $("opt-existing").value === "keep") {
      const unique = [...new Set(dupLangs)].join(", ");
      adv.classList.remove("hidden");
      adv.innerHTML = `
        <span>Video already contains <strong>${escHtml(unique)}</strong> subtitle stream(s).</span>
        <button id="btn-adv-replace" class="btn" style="min-height:28px;padding:0 10px;font-size:11.5px;">Replace Matching</button>
      `;
      $("btn-adv-replace").addEventListener("click", () => {
        $("opt-existing").value = "replace-same-lang";
        savePrefs();
        checkLanguageAdvisory();
        showToast("Updated policy: replace matching subtitle language.");
      });
    } else {
      adv.classList.add("hidden");
      adv.innerHTML = "";
    }
  }

  function renderVideoPanel() {
    const v = state.video;
    const title = $("video-title");
    const sub = $("video-sub");
    const clearBtn = $("btn-clear-video");
    const strip = $("video-streams");

    if (!v) {
      title.textContent = "Drop a video file anywhere or click to browse";
      sub.textContent = "Supports MKV, MP4, M4V, MOV, AVI, WEBM, TS · Click embedded sub chips to extract";
      clearBtn.classList.add("hidden");
      strip.classList.add("hidden");
      strip.innerHTML = "";
      renderRecents();
      checkLanguageAdvisory();
      updateEmbedButton();
      return;
    }

    renderRecents();
    title.textContent = v.filename;
    sub.textContent = `${v.container.toUpperCase()} · ${fmtDur(v.duration_sec)} · ${fmtBytes(v.size_bytes)} · ${v.width}×${v.height}${v.fps ? ` @ ${v.fps} fps` : ""}`;
    clearBtn.classList.remove("hidden");

    strip.innerHTML = "";
    (v.streams || []).forEach((s, i) => {
      const chip = document.createElement("span");
      chip.className = `stream-chip${s.codec_type === "subtitle" ? " sub-chip" : ""}`;
      chip.style.animationDelay = `${Math.min(i, 7) * 24}ms`;
      chip.innerHTML = `<span class="tag">#${s.index} ${escHtml(s.codec_type)}</span><span class="tabular">${escHtml(s.codec_name)}</span>${s.language && s.language !== "und" ? `<span>${escHtml(s.language)}</span>` : ""}${s.is_default ? `<span class="tag" style="color:var(--accent);">DEF</span>` : ""}`;

      if (s.codec_type === "subtitle") {
        const extBtn = document.createElement("button");
        extBtn.className = "chip-extract-btn";
        extBtn.textContent = "Extract";
        extBtn.setAttribute("data-tip", `Extract stream #${s.index} to .srt/.ass`);
        extBtn.addEventListener("click", async (ev) => {
          ev.stopPropagation();
          extBtn.disabled = true;
          extBtn.textContent = "...";
          const res = await window.pywebview.api.extract_stream(v.path, s.index);
          extBtn.disabled = false;
          extBtn.textContent = "Extract";
          if (res.success) {
            showToast(res.message, {
              actionLabel: "Load into Queue",
              durationMs: 5500,
              onAction: () => handlePaths([res.output_path]),
            });
          } else {
            showToast(res.message || "Extraction failed.", { durationMs: 4500 });
          }
        });
        chip.appendChild(extBtn);
      }
      strip.appendChild(chip);
    });
    strip.classList.toggle("hidden", !v.streams || v.streams.length === 0);
    checkLanguageAdvisory();
    updateEmbedButton();
  }

  function renderPreviewCueRows(query = "") {
    const body = $("preview-body");
    const q = query.trim().toLowerCase();
    const all = state.previewCues || [];
    const filtered = q
      ? all.filter((c) => c.text.toLowerCase().includes(q) || c.start.toLowerCase().includes(q) || String(c.index).includes(q))
      : all;
    $("preview-count").textContent = q ? `${filtered.length} / ${all.length} cues` : `${all.length} cues`;
    if (filtered.length === 0) {
      body.innerHTML = `<div class="panel-inner" style="color:var(--text-secondary);">${q ? "No matching cues found." : "No preview cues available."}</div>`;
      return;
    }
    body.innerHTML = filtered
      .slice(0, 300)
      .map(
        (c) => `
        <div class="cue-line">
          <div class="tabular mono" style="font-size:11px;color:var(--text-muted);">#${c.index} · ${escHtml(c.start)} → ${escHtml(c.end)}</div>
          <div class="cue-text-rtl">${escHtml(c.text)}</div>
        </div>`
      )
      .join("");
  }

  async function openCuePreview(sub) {
    const modal = $("preview-modal");
    $("preview-title").textContent = `Cue Preview · ${sub.filename}`;
    $("preview-search").value = "";
    state.previewCues = sub.sample_cues || [];
    renderPreviewCueRows("");
    modal.classList.add("open");
    if (window.pywebview?.api?.get_cues) {
      const res = await window.pywebview.api.get_cues(
        sub.path,
        sub.offset_ms || 0,
        sub.speed_factor || 1.0,
        Boolean(sub.strip_html_tags),
        Boolean(sub.clean_sdh),
        Boolean(sub.clean_ads)
      );
      if (res && res.success && Array.isArray(res.cues)) {
        state.previewCues = res.cues;
        renderPreviewCueRows($("preview-search").value || "");
      }
    }
  }

  function moveTrack(fromIdx, toIdx) {
    if (toIdx < 0 || toIdx >= state.subtitles.length || fromIdx === toIdx) return;
    // FLIP animation: capture old vertical positions keyed by track path
    const oldRects = new Map();
    document.querySelectorAll("#track-list .track-card").forEach((el, i) => {
      const sub = state.subtitles[i];
      if (sub) oldRects.set(sub.path, el.getBoundingClientRect().top);
    });

    const [item] = state.subtitles.splice(fromIdx, 1);
    state.subtitles.splice(toIdx, 0, item);
    renderSubtitles();

    // Invert & Play smooth 240ms cubic-bezier glide
    document.querySelectorAll("#track-list .track-card").forEach((el, i) => {
      const sub = state.subtitles[i];
      if (!sub || !oldRects.has(sub.path)) return;
      const dy = oldRects.get(sub.path) - el.getBoundingClientRect().top;
      if (Math.abs(dy) > 1) {
        el.style.animation = "none";
        el.style.transition = "none";
        el.style.transform = `translateY(${dy}px)`;
        void el.offsetHeight;
        el.style.transition = "transform 240ms cubic-bezier(0.22, 1, 0.36, 1)";
        el.style.transform = "translateY(0)";
      }
    });
  }

  function renderSubtitles() {
    const list = $("track-list");
    $("sub-count").textContent = String(state.subtitles.length);
    $("btn-merge-bilingual").classList.toggle("hidden", state.subtitles.length < 2);
    list.innerHTML = "";

    if (state.subtitles.length === 0) {
      list.innerHTML = `
        <div class="empty-tracks">
          <img class="empty-tracks-art" src="assets/empty-tracks.svg" alt="" />
          <div style="font-weight:600;color:var(--text-secondary);">No subtitle tracks queued yet</div>
          <div style="font-size:12px;">Drop <code>.srt</code>, <code>.vtt</code>, <code>.ass</code>, or <code>.zip</code> files above — or extract an embedded stream from the video.</div>
        </div>
      `;
      checkLanguageAdvisory();
      updateEmbedButton();
      return;
    }

    state.subtitles.forEach((sub, idx) => {
      const card = document.createElement("div");
      card.className = "track-card";
      card.draggable = true;
      card.style.animationDelay = `${Math.min(idx, 6) * 26}ms`;

      card.addEventListener("dragstart", (e) => {
        state.dragTrackIdx = idx;
        e.dataTransfer.effectAllowed = "move";
        card.style.opacity = "0.55";
      });
      card.addEventListener("dragend", () => {
        state.dragTrackIdx = null;
        card.style.opacity = "1";
        document.querySelectorAll(".track-card.drag-target").forEach((el) => el.classList.remove("drag-target"));
      });
      card.addEventListener("dragover", (e) => {
        if (state.dragTrackIdx === null) return;
        e.preventDefault();
        card.classList.add("drag-target");
      });
      card.addEventListener("dragleave", () => card.classList.remove("drag-target"));
      card.addEventListener("drop", (e) => {
        if (state.dragTrackIdx === null) return;
        e.preventDefault();
        e.stopPropagation();
        card.classList.remove("drag-target");
        moveTrack(state.dragTrackIdx, idx);
      });

      const top = document.createElement("div");
      top.className = "track-top";

      const leftTop = document.createElement("div");
      leftTop.style.display = "flex";
      leftTop.style.alignItems = "center";
      leftTop.style.gap = "8px";
      leftTop.style.minWidth = "0";

      const grip = document.createElement("span");
      grip.className = "drag-handle";
      grip.setAttribute("data-tip", "Drag to reorder track priority");
      grip.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><circle cx="9" cy="6" r="1.6"/><circle cx="15" cy="6" r="1.6"/><circle cx="9" cy="12" r="1.6"/><circle cx="15" cy="12" r="1.6"/><circle cx="9" cy="18" r="1.6"/><circle cx="15" cy="18" r="1.6"/></svg>`;

      const idxPill = document.createElement("span");
      idxPill.className = "track-index-pill tabular mono";
      idxPill.textContent = String(idx + 1).padStart(2, "0");

      const nameEl = document.createElement("div");
      nameEl.className = "track-name";
      nameEl.title = sub.path;
      nameEl.textContent = sub.filename;

      leftTop.appendChild(grip);
      leftTop.appendChild(idxPill);
      leftTop.appendChild(nameEl);

      const badges = document.createElement("div");
      badges.className = "track-badges";
      badges.innerHTML = `
        <span class="badge tabular">${sub.cue_count} cues</span>
        <span class="badge ${sub.encoding_detected.startsWith("utf-8") ? "ok" : "warn"}">${escHtml(sub.encoding_detected)}</span>
      `;
      if (sub.source_format && sub.source_format !== "srt") {
        badges.innerHTML += `<span class="badge warn">${escHtml(sub.source_format.toUpperCase())}</span>`;
      }
      if (sub.repaired_cues > 0) {
        badges.innerHTML += `<span class="badge warn tabular">repaired ${sub.repaired_cues}</span>`;
      }
      if (sub.speed_factor && Math.abs(sub.speed_factor - 1.0) >= 0.001) {
        badges.innerHTML += `<span class="badge warn tabular">${Number(sub.speed_factor).toFixed(4)}×</span>`;
      }

      const rmBtn = document.createElement("button");
      rmBtn.className = "icon-btn";
      rmBtn.setAttribute("data-tip", "Remove track");
      rmBtn.setAttribute("aria-label", "Remove subtitle track");
      rmBtn.textContent = "✕";
      rmBtn.addEventListener("click", () => {
        card.classList.add("removing");
        setTimeout(() => {
          const removed = state.subtitles.splice(idx, 1)[0];
          renderSubtitles();
          showToast(`Removed ${removed.filename}`, {
            actionLabel: "Undo",
            durationMs: 5000,
            onAction: () => {
              state.subtitles.splice(Math.min(idx, state.subtitles.length), 0, removed);
              renderSubtitles();
            },
          });
        }, 155);
      });

      const rightWrap = document.createElement("div");
      rightWrap.style.display = "flex";
      rightWrap.style.alignItems = "center";
      rightWrap.style.gap = "6px";
      rightWrap.appendChild(badges);
      rightWrap.appendChild(rmBtn);

      top.appendChild(leftTop);
      top.appendChild(rightWrap);

      // Row 1: Language, Title, Offset
      const row1 = document.createElement("div");
      row1.className = "track-controls-row1";

      const langSel = document.createElement("select");
      for (const [code, label] of LANG_CHOICES) {
        const opt = document.createElement("option");
        opt.value = code;
        opt.textContent = label;
        if (code === sub.lang_code) opt.selected = true;
        langSel.appendChild(opt);
      }
      langSel.addEventListener("change", (e) => {
        sub.lang_code = e.target.value;
        checkLanguageAdvisory();
      });

      const titleIn = document.createElement("input");
      titleIn.type = "text";
      titleIn.placeholder = "Track title";
      titleIn.value = sub.title || "";
      titleIn.addEventListener("input", (e) => {
        sub.title = e.target.value;
      });

      const stepper = document.createElement("div");
      stepper.className = "offset-stepper";

      const minusBtn = document.createElement("button");
      minusBtn.className = "nudge-btn tabular";
      minusBtn.textContent = "-100";
      minusBtn.setAttribute("data-tip", "Shift earlier by 100ms");

      const offIn = document.createElement("input");
      offIn.type = "text";
      offIn.className = "tabular mono";
      offIn.style.width = "76px";
      offIn.style.textAlign = "center";
      offIn.setAttribute("data-tip", "Timing shift (e.g. +500ms, -1.5s)");
      offIn.value = formatOffsetLabel(sub.offset_ms);
      offIn.addEventListener("blur", (e) => {
        sub.offset_ms = parseOffsetInput(e.target.value);
        e.target.value = formatOffsetLabel(sub.offset_ms);
      });

      const plusBtn = document.createElement("button");
      plusBtn.className = "nudge-btn tabular";
      plusBtn.textContent = "+100";
      plusBtn.setAttribute("data-tip", "Shift later by 100ms");

      const flashOffset = () => {
        offIn.classList.add("nudge-flash");
        setTimeout(() => offIn.classList.remove("nudge-flash"), 200);
      };

      minusBtn.addEventListener("click", () => {
        sub.offset_ms = (sub.offset_ms || 0) - 100;
        offIn.value = formatOffsetLabel(sub.offset_ms);
        flashOffset();
      });
      plusBtn.addEventListener("click", () => {
        sub.offset_ms = (sub.offset_ms || 0) + 100;
        offIn.value = formatOffsetLabel(sub.offset_ms);
        flashOffset();
      });

      stepper.appendChild(minusBtn);
      stepper.appendChild(offIn);
      stepper.appendChild(plusBtn);

      row1.appendChild(langSel);
      row1.appendChild(titleIn);
      row1.appendChild(stepper);

      // Row 2: Flags/Cleaners + Inspection Actions
      const row2 = document.createElement("div");
      row2.className = "track-controls-row2";

      const flagsGroup = document.createElement("div");
      flagsGroup.className = "pill-group";

      const makeToggle = (label, tipText, prop, exclusive = false) => {
        const b = document.createElement("button");
        b.className = "pill-toggle";
        b.textContent = label;
        b.setAttribute("data-tip", tipText);
        b.setAttribute("aria-pressed", String(!!sub[prop]));
        b.addEventListener("click", () => {
          const next = !sub[prop];
          if (exclusive && next) state.subtitles.forEach((s) => (s[prop] = false));
          sub[prop] = next;
          if (exclusive) renderSubtitles();
          else b.setAttribute("aria-pressed", String(next));
        });
        return b;
      };

      flagsGroup.appendChild(makeToggle("Default", "Mark as default playback subtitle", "is_default", true));
      flagsGroup.appendChild(makeToggle("Forced", "Mark as forced track", "is_forced"));
      flagsGroup.appendChild(makeToggle("Strip Tags", "Remove <font>, <i>, <b>, {...} styling tags", "strip_html_tags"));
      flagsGroup.appendChild(makeToggle("Clean SDH", "Strip [MUSIC], (SIGHS), ♪ notes & SPEAKER:", "clean_sdh"));
      flagsGroup.appendChild(makeToggle("Clean Ads", "Remove translator promo & URL cues", "clean_ads"));
      flagsGroup.appendChild(makeToggle("RTL BiDi", "Fix Arabic/Hebrew trailing punctuation & RTL embedding", "fix_rtl_bidi"));

      const actionsGroup = document.createElement("div");
      actionsGroup.className = "pill-group";

      if (state.subtitles.length > 1) {
        const upBtn = document.createElement("button");
        upBtn.className = "pill-toggle";
        upBtn.textContent = "↑";
        upBtn.disabled = idx === 0;
        upBtn.setAttribute("data-tip", "Move track up");
        upBtn.addEventListener("click", () => moveTrack(idx, idx - 1));
        actionsGroup.appendChild(upBtn);

        const dnBtn = document.createElement("button");
        dnBtn.className = "pill-toggle";
        dnBtn.textContent = "↓";
        dnBtn.disabled = idx === state.subtitles.length - 1;
        dnBtn.setAttribute("data-tip", "Move track down");
        dnBtn.addEventListener("click", () => moveTrack(idx, idx + 1));
        actionsGroup.appendChild(dnBtn);
      }

      const prevBtn = document.createElement("button");
      prevBtn.className = "btn";
      prevBtn.style.minHeight = "30px";
      prevBtn.style.padding = "0 9px";
      prevBtn.style.fontSize = "11.5px";
      prevBtn.textContent = "Cues";
      prevBtn.setAttribute("data-tip", "Inspect decoded subtitle cues");
      prevBtn.addEventListener("click", () => openCuePreview(sub));

      const syncBtn = document.createElement("button");
      syncBtn.className = "btn";
      syncBtn.style.minHeight = "30px";
      syncBtn.style.padding = "0 9px";
      syncBtn.style.fontSize = "11.5px";
      syncBtn.textContent = "Sync";
      syncBtn.setAttribute("data-tip", "Check timing against embedded track");
      syncBtn.disabled = !state.video;
      syncBtn.addEventListener("click", () => openSyncCheck(sub));

      actionsGroup.appendChild(prevBtn);
      actionsGroup.appendChild(syncBtn);

      row2.appendChild(flagsGroup);
      row2.appendChild(actionsGroup);

      card.appendChild(top);
      card.appendChild(row1);
      card.appendChild(row2);
      list.appendChild(card);
    });

    checkLanguageAdvisory();
    updateEmbedButton();
  }

  function updateEmbedButton() {
    const canRun = !!state.video && state.subtitles.length > 0 && !state.running;
    $("btn-embed").disabled = !canRun;
  }

  async function handlePaths(paths) {
    if (!paths || paths.length === 0) return;

    const strip = $("video-streams");
    const skelTimer = setTimeout(() => {
      if (paths.some((p) => !/\.(srt|vtt|ass|ssa|ttml|dfxp|sub|zip)$/i.test(p))) {
        strip.classList.remove("hidden");
        strip.innerHTML = `<div class="skeleton" style="width:110px;"></div><div class="skeleton" style="width:90px;"></div><div class="skeleton" style="width:130px;"></div>`;
      }
    }, 120);

    const res = await window.pywebview.api.inspect_dropped_paths(paths);
    clearTimeout(skelTimer);

    if (res.folders && res.folders.length > 0) {
      switchTab("batch");
      await loadBatchFolder(res.folders[0]);
    }

    if (res.videos && res.videos.length > 0) {
      state.video = res.videos[0];
      state.lastResult = null;
      $("result-box").classList.add("hidden");
      pushRecent(state.video.path, state.video.filename);
      renderVideoPanel();
    }

    if (res.subtitles && res.subtitles.length > 0) {
      const prof = activeProfileDefaults();
      const defLang = prof.default_lang && prof.default_lang !== "auto" ? prof.default_lang : null;
      for (const s of res.subtitles) {
        const exists = state.subtitles.some((x) => x.path.toLowerCase() === s.path.toLowerCase());
        if (!exists) {
          state.subtitles.push({
            ...s,
            lang_code: s.detected_lang_code && s.detected_lang_code !== "und" ? s.detected_lang_code : (defLang || "auto"),
            title: s.detected_lang_name !== "Undetermined" ? s.detected_lang_name : "",
            is_default: state.subtitles.length === 0,
            is_forced: false,
            offset_ms: 0,
            strip_html_tags: Boolean(prof.strip_html_tags),
            clean_sdh: Boolean(prof.clean_sdh),
            clean_ads: Boolean(prof.clean_ads),
            fix_rtl_bidi: Boolean(prof.fix_rtl_bidi),
          });
        }
      }
      renderSubtitles();
      if (res.auto_discovered > 0) {
        showToast(`Auto-discovered ${res.auto_discovered} matching companion subtitle(s).`, { durationMs: 3800 });
      }
    } else if (res.videos && res.videos.length > 0) {
      renderSubtitles();
    }

    for (const err of res.errors || []) {
      showToast(err, { durationMs: 5000 });
    }

    if ($("opt-express-drop")?.checked && state.video && state.subtitles.length > 0 && !state.running && state.tab === "single") {
      showToast("Express Drop-to-Go: starting embed automatically...", { durationMs: 2600 });
      startMux();
    }
  }

  async function mergeTopTwoBilingual() {
    if (state.subtitles.length < 2) return;
    const bot = state.subtitles[0];
    const top = state.subtitles[1];
    const res = await window.pywebview.api.merge_bilingual(bot.path, top.path, bot.offset_ms || 0, top.offset_ms || 0);
    if (!res.success) {
      showToast(res.error || "Bilingual merge failed.", { durationMs: 4500 });
      return;
    }
    const prevList = [...state.subtitles];
    const mergedSub = {
      ...res.subtitle,
      lang_code: bot.lang_code || "auto",
      title: `${bot.title || bot.lang_code} + ${top.title || top.lang_code} (Dual)`,
      is_default: true,
      is_forced: false,
      offset_ms: 0,
      strip_html_tags: false,
      clean_sdh: false,
      clean_ads: false,
    };
    state.subtitles = [mergedSub, ...state.subtitles.slice(2)];
    renderSubtitles();
    showToast(`Merged ${res.events} dual cues -> ${mergedSub.filename}`, {
      actionLabel: "Undo",
      durationMs: 5000,
      onAction: () => {
        state.subtitles = prevList;
        renderSubtitles();
      },
    });
  }

  async function openSyncCheck(sub) {
    if (!state.video) return;
    const modal = $("sync-modal");
    const body = $("sync-body");
    modal.classList.add("open");
    body.innerHTML = `<div class="skeleton" style="height:120px;width:100%;"></div>`;

    const rep = await window.pywebview.api.run_sync_check(state.video.path, sub.path);
    if (!rep.comparable) {
      body.innerHTML = `<div class="panel-inner"><div style="color:var(--warn);font-weight:600;">Cannot Compare</div><p class="fc-pretty" style="color:var(--text-secondary);margin:6px 0 0;">${escHtml(rep.reason)}</p></div>`;
      return;
    }

    const color = rep.verdict === "in-sync" ? "var(--ok)" : "var(--warn)";
    const rows = (rep.samples || [])
      .map(
        (s) => `
        <tr>
          <td class="tabular" style="color:var(--text-muted);padding:6px 8px;">${s.minute}m</td>
          <td class="tabular mono" style="padding:6px 8px;">${escHtml(s.ref_time)}</td>
          <td class="tabular mono" style="padding:6px 8px;">${escHtml(s.srt_time)}</td>
          <td class="tabular mono" style="padding:6px 8px;text-align:right;color:${Math.abs(s.diff_ms) <= 150 ? "var(--ok)" : "var(--warn)"};">${s.diff_ms >= 0 ? "+" : ""}${s.diff_ms} ms</td>
        </tr>`
      )
      .join("");

    const methodText = rep.method === "audio-vad"
      ? `Audio Speech VAD (stream #${rep.ref_stream_index} ${rep.ref_language})`
      : `Embedded Ref #${rep.ref_stream_index} (${rep.ref_language})`;
    const hasSpeedFix = rep.recommended_speed && Math.abs(rep.recommended_speed - 1.0) >= 0.001;
    const hasFix = hasSpeedFix || rep.recommended_offset_ms !== 0;
    const fixLabel = hasSpeedFix
      ? `Apply ${Number(rep.recommended_speed).toFixed(4)}× + ${rep.recommended_offset_ms >= 0 ? "+" : ""}${rep.recommended_offset_ms}ms Fix`
      : `Apply ${rep.recommended_offset_ms >= 0 ? "+" : ""}${rep.recommended_offset_ms}ms Fix`;

    body.innerHTML = `
      <div class="panel-inner" style="margin-bottom:10px;display:flex;justify-content:space-between;align-items:center;gap:12px;">
        <div>
          <div style="font-size:14px;font-weight:650;color:${color};text-transform:uppercase;">${escHtml(rep.verdict)}</div>
          <div class="tabular" style="font-size:12px;color:var(--text-secondary);">${escHtml(methodText)} · Median delta ${rep.median_diff_ms >= 0 ? "+" : ""}${rep.median_diff_ms} ms · Drift ${rep.drift_ms} ms</div>
        </div>
        ${hasFix ? `<button id="btn-apply-sync-fix" class="btn btn-primary" style="min-height:34px;">${escHtml(fixLabel)}</button>` : ""}
      </div>
      <div class="panel-inner" style="padding:4px 8px;max-height:260px;overflow:auto;">
        <table style="width:100%;border-collapse:collapse;font-size:12px;">
          <thead>
            <tr style="color:var(--text-muted);text-align:left;border-bottom:1px solid var(--border-hairline);">
              <th style="padding:6px 8px;">Min</th>
              <th style="padding:6px 8px;">Reference Onset</th>
              <th style="padding:6px 8px;">External SRT</th>
              <th style="padding:6px 8px;text-align:right;">Delta</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    `;

    const fixBtn = $("btn-apply-sync-fix");
    if (fixBtn) {
      fixBtn.addEventListener("click", () => {
        sub.offset_ms = rep.recommended_offset_ms || 0;
        sub.speed_factor = hasSpeedFix ? rep.recommended_speed : 1.0;
        renderSubtitles();
        modal.classList.remove("open");
        showToast(
          hasSpeedFix
            ? `Applied ${Number(sub.speed_factor).toFixed(4)}× + ${formatOffsetLabel(sub.offset_ms)} to ${sub.filename}`
            : `Applied ${formatOffsetLabel(sub.offset_ms)} offset to ${sub.filename}`
        );
      });
    }
  }

  function openUndoManager() {
    const modal = $("undo-modal");
    const body = $("undo-body");
    const totalBytes = state.undos.reduce((acc, u) => acc + (u.backup_size_bytes || 0), 0);
    if ($("undo-total-size")) $("undo-total-size").textContent = fmtBytes(totalBytes);
    if (state.undos.length === 0) {
      body.innerHTML = `<div class="panel-inner" style="color:var(--text-secondary);">No active backups stored.</div>`;
    } else {
      body.innerHTML = "";
      state.undos.forEach((u) => {
        const row = document.createElement("div");
        row.className = "cue-line";
        row.style.flexDirection = "row";
        row.style.justifyContent = "space-between";
        row.style.alignItems = "center";
        row.style.gap = "10px";
        row.innerHTML = `
          <div style="min-width:0;flex:1;">
            <div style="font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escHtml(u.original_path)}</div>
            <div class="tabular" style="font-size:11.5px;color:var(--text-muted);">TX ${escHtml(u.tx_id)} · ${fmtBytes(u.backup_size_bytes)} · ${escHtml(u.engine_used)}</div>
          </div>
          <div style="display:flex;gap:6px;flex-shrink:0;">
            <button class="btn" data-restore="${escHtml(u.tx_id)}" style="min-height:30px;padding:0 10px;">Restore</button>
            <button class="btn btn-danger" data-del="${escHtml(u.tx_id)}" style="min-height:30px;padding:0 10px;">Delete</button>
          </div>
        `;
        row.querySelector("[data-restore]").addEventListener("click", async () => {
          await triggerUndo(u.tx_id);
          openUndoManager();
        });
        row.querySelector("[data-del]").addEventListener("click", async () => {
          const res = await window.pywebview.api.delete_undo_backup(u.tx_id);
          state.undos = res.undos || [];
          renderUndoHeader();
          showToast(res.message);
          openUndoManager();
        });
        body.appendChild(row);
      });
    }
    modal.classList.add("open");
  }

  async function startMux() {
    if (state.running || !state.video || state.subtitles.length === 0) return;

    state.running = true;
    updateEmbedButton();
    $("btn-cancel").classList.remove("hidden");
    $("progress-box").classList.remove("hidden");
    $("result-box").classList.add("hidden");
    $("prog-fill").style.transform = "scaleX(0.03)";
    $("prog-phase").textContent = "Preparing streams...";
    $("prog-detail").textContent = "0%";

    const idempotencyKey = window.crypto && window.crypto.randomUUID ? window.crypto.randomUUID() : String(Date.now());
    const payload = {
      idempotency_key: idempotencyKey,
      video_path: state.video.path,
      subtitles: state.subtitles.map((s) => ({
        path: s.path,
        lang_code: s.lang_code,
        title: s.title,
        is_default: s.is_default,
        is_forced: s.is_forced,
        offset_ms: s.offset_ms,
        speed_factor: s.speed_factor || 1.0,
        strip_html_tags: s.strip_html_tags,
        clean_sdh: s.clean_sdh,
        clean_ads: s.clean_ads,
        fix_rtl_bidi: Boolean(s.fix_rtl_bidi),
      })),
      output_path: $("opt-output").value.trim() || null,
      output_template: $("opt-naming")?.value || "default",
      prefer_sub_lang_when_foreign_audio: $("opt-prefer-sub")?.value || "",
      mode: $("opt-mode").value,
      existing_subs: $("opt-existing").value,
      fallback_to_mkv: $("opt-mkv-fallback").checked,
      replace_original: $("opt-replace").checked,
      create_backup: true,
      auto_sync: $("opt-auto-sync").checked,
      crf: Number($("opt-crf").value || 18),
      preset: $("opt-preset").value || "medium",
      hardsub_style: {
        font_name: $("hs-font").value || "Arial",
        font_size: Number($("hs-size").value || 24),
        primary_color: $("hs-color").value || "#FFFFFF",
        outline_color: "#000000",
        outline_width: Number($("hs-outline").value || 2),
        margin_v: Number($("hs-margin").value || 28),
        encoder: $("hs-encoder").value || "auto",
      },
    };

    const ack = await window.pywebview.api.start_embed(payload);
    if (!ack.accepted) {
      state.running = false;
      updateEmbedButton();
      $("btn-cancel").classList.add("hidden");
      showToast(ack.error || "Could not start muxing.");
    }
  }

  window.__onMuxProgress = (data) => {
    const ratio = Math.max(0, Math.min(1, (data.percent || 0) / 100));
    $("prog-fill").style.transform = `scaleX(${ratio})`;
    $("prog-phase").textContent = data.phase || "Muxing";
    $("prog-detail").textContent = data.detail || `${data.percent}%`;
  };

  window.__onMuxComplete = (res) => {
    state.running = false;
    state.lastResult = res;
    if (Array.isArray(res.undos)) {
      state.undos = res.undos;
      renderUndoHeader();
    }
    updateEmbedButton();
    $("btn-cancel").classList.add("hidden");
    $("progress-box").classList.add("hidden");

    const box = $("result-box");
    box.classList.remove("hidden");

    if (!res.success) {
      box.innerHTML = `
        <div class="result-card error">
          <div style="font-weight:650;color:#fca5a5;">Embedding Failed</div>
          <div class="mono" style="font-size:11.5px;color:var(--text-secondary);white-space:pre-wrap;max-height:130px;overflow:auto;">${escHtml(res.error)}</div>
        </div>
      `;
      return;
    }

    const fallbacks = (res.fallbacks_triggered || []).length
      ? `<div style="font-size:11.5px;color:#fcd34d;">Fallback used: ${escHtml(res.fallbacks_triggered.join(" · "))}</div>`
      : "";

    box.innerHTML = `
      <div class="result-card">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <strong style="color:#86efac;display:inline-flex;align-items:center;gap:7px;">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
              <circle class="ring-draw" cx="12" cy="12" r="9.5" opacity="0.55" />
              <path class="check-draw" d="M7.5 12.5l3 3 6-6.5" />
            </svg>
            Embedded Successfully
          </strong>
          <span class="badge ok tabular">${escHtml(res.engine_used)} · ${res.elapsed_sec}s</span>
        </div>
        <div class="mono" style="font-size:11.5px;color:var(--text-secondary);word-break:break-all;">${escHtml(res.output_path)}</div>
        <div class="tabular" style="font-size:12px;color:var(--text-muted);">${fmtBytes(res.output_size_bytes)} · ${res.subtitle_streams_after} subtitle stream(s) verified</div>
        ${fallbacks}
        <div class="result-actions">
          <button id="btn-res-reveal" class="btn">Reveal in Explorer</button>
          <button id="btn-res-play" class="btn">Play Video</button>
          <button id="btn-res-next" class="btn btn-ghost">New Job</button>
          ${res.undo_tx_id ? `<button id="btn-res-undo" class="btn btn-danger">Undo Replace</button>` : ""}
        </div>
      </div>
    `;

    $("btn-res-reveal").addEventListener("click", () => window.pywebview.api.reveal_in_explorer(res.output_path));
    $("btn-res-play").addEventListener("click", () => window.pywebview.api.open_media(res.output_path));
    $("btn-res-next").addEventListener("click", () => {
      state.video = null;
      state.subtitles = [];
      box.classList.add("hidden");
      renderVideoPanel();
      renderSubtitles();
    });
    const undoBtn = $("btn-res-undo");
    if (undoBtn) {
      undoBtn.addEventListener("click", () => triggerUndo(res.undo_tx_id));
    }
  };

  async function triggerUndo(txId = null) {
    const res = await window.pywebview.api.undo_replace(txId);
    state.undos = res.undos || [];
    renderUndoHeader();
    showToast(res.message, { durationMs: 4500 });
    if (res.success && $("btn-res-undo")) {
      $("btn-res-undo").disabled = true;
      $("btn-res-undo").textContent = "Restored";
    }
  }

  // Batch Mode
  async function loadBatchFolder(folderPath) {
    state.batchFolder = folderPath;
    state.auditReport = null;
    $("batch-folder-label").textContent = folderPath;
    $("btn-audit-batch").disabled = false;
    const recursive = $("batch-recursive").checked;
    const res = await window.pywebview.api.scan_folder(folderPath, recursive);
    state.batchPairs = (res.pairs || []).map((p) => ({ ...p, enabled: true, status: "ready" }));
    state.unmatchedVideos = res.unmatched_videos || [];
    state.unmatchedSubs = res.unmatched_subs || [];
    $("batch-folder-sub").textContent =
      `${state.batchPairs.length} paired · ${state.unmatchedVideos.length} unmatched video(s) · ${state.unmatchedSubs.length} unmatched sub(s)`;
    renderBatchList();
  }

  async function runLibraryAudit() {
    if (!state.batchFolder) return;
    $("btn-audit-batch").disabled = true;
    $("btn-audit-batch").textContent = "Auditing...";
    const lang = $("audit-lang").value || "ara";
    const rec = $("batch-recursive").checked;
    const rep = await window.pywebview.api.audit_folder(state.batchFolder, lang, rec);
    $("btn-audit-batch").disabled = false;
    $("btn-audit-batch").textContent = "Audit Library";
    if (!rep.ok) {
      showToast(rep.error || "Audit failed.");
      return;
    }
    state.auditReport = rep;
    $("batch-folder-sub").textContent =
      `Audit (${rep.target_lang}): ${rep.ready_count} ready · ${rep.pairable_count} pairable · ${rep.missing_count} missing`;
    renderAuditList(rep);
  }

  function renderAuditList(rep) {
    const list = $("batch-list");
    list.innerHTML = "";
    const pairable = (rep.items || []).filter((i) => i.status === "pairable");
    const jobIndexByVideo = new Map(pairable.map((it, jIdx) => [it.video_path, jIdx]));
    if (pairable.length > 0) {
      const bar = document.createElement("div");
      bar.className = "advisory-bar";
      bar.innerHTML = `
        <span><strong>${pairable.length}</strong> video(s) lack <code>${escHtml(rep.target_lang)}</code> inside, but have external companion subtitle(s).</span>
        <button id="btn-embed-pairable" class="btn btn-primary" style="min-height:30px;padding:0 12px;font-size:12px;">Embed All Pairable</button>
      `;
      list.appendChild(bar);
      bar.querySelector("#btn-embed-pairable").addEventListener("click", async () => {
        const btn = bar.querySelector("#btn-embed-pairable");
        btn.disabled = true;
        btn.textContent = "Running...";
        state.activeBatchKind = "audit";
        const jobs = pairable.map((it) => ({
          video_path: it.video_path,
          subtitles: it.companion_subs.map((p) => ({ path: p, lang_code: rep.target_lang === "auto" ? "auto" : rep.target_lang })),
        }));
        const prof = activeProfileDefaults();
        await window.pywebview.api.start_batch({
          jobs,
          directory: state.batchFolder,
          mode: $("opt-mode").value,
          existing_subs: $("opt-existing").value,
          fallback_to_mkv: $("opt-mkv-fallback").checked,
          replace_original: $("opt-replace").checked,
          auto_sync: $("opt-auto-sync").checked,
          clean_sdh: Boolean(prof.clean_sdh),
          clean_ads: Boolean(prof.clean_ads),
        });
      });
    }

    (rep.items || []).forEach((it, idx) => {
      const row = document.createElement("div");
      row.className = "track-card";
      row.style.animationDelay = `${Math.min(idx, 7) * 20}ms`;
      const cls = it.status === "ready" ? "ok" : it.status === "pairable" ? "warn" : "";
      const jobIdx = jobIndexByVideo.get(it.video_path);
      const badgeIdAttr = jobIdx !== undefined ? `id="audit-st-${jobIdx}"` : "";
      const detail =
        it.status === "ready"
          ? `Embedded: ${(it.embedded_langs || []).join(", ") || "yes"}`
          : it.companion_subs.length
            ? `Found nearby: ${it.companion_subs.map((p) => p.split(/[\\/]/).pop()).join(", ")}`
            : "No embedded or nearby subtitle found";
      row.innerHTML = `
        <div class="track-top">
          <span class="track-name">${escHtml(it.filename)}</span>
          <span ${badgeIdAttr} class="badge ${cls}">${escHtml(it.status.toUpperCase())}</span>
        </div>
        <div style="font-size:12px;color:var(--text-secondary);">${escHtml(detail)}</div>
      `;
      list.appendChild(row);
    });
  }

  function renderBatchList() {
    const list = $("batch-list");
    list.innerHTML = "";
    $("btn-run-batch").disabled = state.batchPairs.filter((p) => p.enabled).length === 0;

    if (state.batchPairs.length === 0) {
      list.innerHTML = state.batchFolder
        ? `<div class="panel-inner" style="color:var(--text-secondary);">No matching video + subtitle pairs found in this folder.</div>`
        : "";
      return;
    }

    state.batchPairs.forEach((pair, idx) => {
      const row = document.createElement("div");
      row.className = "track-card";
      row.style.animationDelay = `${Math.min(idx, 7) * 24}ms`;
      const subNames = pair.subtitles.map((s) => `${s.filename} (${s.lang_code})`).join(", ");
      const badgeCls = pair.status.startsWith("done") ? "ok" : pair.status === "error" ? "warn" : "";
      const firstLang = pair.subtitles[0]?.lang_code || "auto";
      const langOpts = LANG_CHOICES.map(
        ([code, label]) => `<option value="${escHtml(code)}"${code === firstLang ? " selected" : ""}>${escHtml(label)}</option>`
      ).join("");
      row.innerHTML = `
        <div class="track-top">
          <label style="display:flex;align-items:center;gap:9px;min-width:0;cursor:pointer;">
            <input type="checkbox" ${pair.enabled ? "checked" : ""} data-bidx="${idx}" />
            <span class="track-name">${escHtml(pair.video_name)}</span>
          </label>
          <div class="track-badges">
            <select data-blang="${idx}" style="min-height:26px;padding:2px 6px;font-size:11px;width:auto;">${langOpts}</select>
            <button data-bopen="${idx}" class="btn" style="min-height:26px;padding:0 8px;font-size:11px;" data-tip="Open this pair in Mux Studio">Studio</button>
            <span class="badge">${escHtml(pair.confidence)}</span>
            <span id="batch-st-${idx}" class="badge ${badgeCls}">${escHtml(pair.status)}</span>
          </div>
        </div>
        <div style="font-size:12px;color:var(--text-secondary);padding-left:24px;">↳ ${escHtml(subNames)}</div>
      `;
      row.querySelector("input").addEventListener("change", (e) => {
        pair.enabled = e.target.checked;
        $("btn-run-batch").disabled = state.batchPairs.filter((p) => p.enabled).length === 0;
      });
      row.querySelector("[data-blang]").addEventListener("change", (e) => {
        pair.subtitles.forEach((s) => { s.lang_code = e.target.value; });
      });
      row.querySelector("[data-bopen]").addEventListener("click", async () => {
        state.subtitles = [];
        switchTab("single");
        await handlePaths([pair.video_path, ...pair.subtitles.map((s) => s.path)]);
      });
      list.appendChild(row);
    });
  }

  window.__onBatchProgress = (data) => {
    const prefix = state.activeBatchKind === "audit" ? "audit-st-" : "batch-st-";
    const badge = $(`${prefix}${data.jobIndex}`);
    if (badge) badge.textContent = `${data.jobPercent}%`;
  };

  window.__onBatchItemDone = (res) => {
    const prefix = state.activeBatchKind === "audit" ? "audit-st-" : "batch-st-";
    if (state.activeBatchKind !== "audit") {
      const pair = state.batchPairs[res.jobIndex];
      if (pair) pair.status = res.success ? `done (${res.engine_used})` : "error";
    }
    const badge = $(`${prefix}${res.jobIndex}`);
    if (badge) {
      badge.className = `badge ${res.success ? "ok" : "warn"}`;
      badge.textContent = res.success ? `done · ${res.elapsed_sec}s` : "failed";
    }
  };

  window.__onBatchComplete = (data) => {
    if (Array.isArray(data.undos)) {
      state.undos = data.undos;
      renderUndoHeader();
    }
    $("btn-run-batch").disabled = false;
    $("btn-run-batch").textContent = "Run Batch";
    const okCount = (data.results || []).filter((r) => r.success).length;
    showToast(`Batch finished: ${okCount}/${(data.results || []).length} succeeded`, { durationMs: 5000 });
    if (state.activeBatchKind === "audit" && state.batchFolder) {
      state.activeBatchKind = "batch";
      runLibraryAudit();
    }
  };

  // Watch Daemon Events
  window.__onWatchEvent = (ev) => {
    const log = $("watch-log");
    if (log.children.length === 1 && !log.children[0].classList.contains("watch-entry")) {
      log.innerHTML = "";
    }
    const row = document.createElement("div");
    row.className = "watch-entry";
    const dotCls = ev.kind === "success" ? "ok" : ev.kind === "error" ? "err" : ev.kind === "paired" ? "warn" : "";
    row.innerHTML = `
      <span class="dot ${dotCls}"></span>
      <span class="tabular mono" style="font-size:11px;color:var(--text-muted);">${escHtml(ev.timestamp)}</span>
      <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;">${escHtml(ev.message)}</span>
    `;
    if (ev.output_path) {
      const rev = document.createElement("button");
      rev.className = "btn";
      rev.style.minHeight = "26px";
      rev.style.padding = "0 9px";
      rev.style.fontSize = "11px";
      rev.textContent = "Reveal";
      rev.addEventListener("click", () => window.pywebview.api.reveal_in_explorer(ev.output_path));
      row.appendChild(rev);
    }
    log.prepend(row);
  };

  window.__onBootstrapProgress = (data) => {
    $("bootstrap-progress").classList.remove("hidden");
    $("boot-status").textContent = data.status || `Installing ${data.tool}...`;
    $("boot-pct").textContent = `${data.percent || 0}%`;
    $("boot-fill").style.transform = `scaleX(${Math.max(0, Math.min(1, (data.percent || 0) / 100))})`;
  };

  window.__onBootstrapDone = (data) => {
    $("bootstrap-progress").classList.add("hidden");
    if (data.env) {
      state.env = data.env;
      renderToolPills(data.env.tools || {});
      renderHwEncoders(data.env.hwEncoders || []);
      if ($("settings-modal").classList.contains("open")) {
        openSettingsModal();
      }
    }
    showToast(data.message || (data.success ? "Tool installed!" : "Installation failed."), { durationMs: 5000 });
  };

  /* Directional Tab Switcher (+20px forward, -20px backward) */
  function switchTab(nextTab) {
    if (state.tab === nextTab) return;
    state.tab = nextTab;
    const nextIdx = TAB_ORDER.indexOf(nextTab);

    $("mode-tabs").setAttribute("data-active", nextTab);
    $("tab-single").setAttribute("aria-selected", String(nextTab === "single"));
    $("tab-batch").setAttribute("aria-selected", String(nextTab === "batch"));
    $("tab-watch").setAttribute("aria-selected", String(nextTab === "watch"));

    const map = { single: $("view-single"), batch: $("view-batch"), watch: $("view-watch") };
    TAB_ORDER.forEach((key, idx) => {
      const el = map[key];
      el.classList.remove("is-hidden-left", "is-hidden-right");
      if (idx < nextIdx) el.classList.add("is-hidden-left");
      else if (idx > nextIdx) el.classList.add("is-hidden-right");
    });
  }

  function openSettingsModal() {
    const env = state.env || {};
    const tools = env.tools || {};
    const shellBtn = $("btn-shell-toggle");
    const shellRow = $("shell-row");
    shellRow.classList.remove("hidden");
    shellBtn.textContent = env.shellInstalled ? "Remove Menu" : "Enable Menu";
    shellBtn.className = env.shellInstalled ? "btn btn-danger" : "btn";

    const installHints = {
      mkvmerge: { winget: "winget install MoritzBunkus.MKVToolNix", url: "https://mkvtoolnix.download/" },
      mkvextract: { winget: "winget install MoritzBunkus.MKVToolNix", url: "https://mkvtoolnix.download/" },
      ffmpeg: { winget: "winget install Gyan.FFmpeg", url: "https://www.gyan.dev/ffmpeg/builds/" },
      ffprobe: { winget: "winget install Gyan.FFmpeg", url: "https://www.gyan.dev/ffmpeg/builds/" },
    };

    const docList = $("doctor-list");
    docList.innerHTML = "";
    for (const name of ["mkvmerge", "mkvextract", "ffmpeg", "ffprobe"]) {
      const t = tools[name] || { available: false };
      const hint = installHints[name];
      const row = document.createElement("div");
      row.className = "cue-line";
      row.style.flexDirection = "row";
      row.style.justifyContent = "space-between";
      row.style.alignItems = "center";
      row.style.gap = "10px";
      row.innerHTML = `
        <div style="min-width:0;flex:1;">
          <div style="display:flex;align-items:center;gap:6px;font-weight:600;">
            <span class="dot ${t.available ? "ok" : "warn"}"></span>
            <span>${escHtml(name)}</span>
            <span class="badge ${t.available ? "ok" : "warn"}">${t.available ? "READY" : "MISSING"}</span>
          </div>
          <div class="mono" style="font-size:11px;color:var(--text-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escHtml(t.available ? t.path : hint.winget)}</div>
        </div>
        ${!t.available ? `
          <div style="display:flex;gap:6px;flex-shrink:0;">
            <button class="btn btn-primary" data-install="${escHtml(name)}" style="min-height:28px;padding:0 10px;font-size:11.5px;">1-Click Install</button>
            <button class="btn" data-copy="${escHtml(hint.winget)}" style="min-height:28px;padding:0 9px;font-size:11.5px;">Copy winget</button>
            <button class="btn" data-url="${escHtml(hint.url)}" style="min-height:28px;padding:0 9px;font-size:11.5px;">Site</button>
          </div>` : ""}
      `;
      const instBtn = row.querySelector("[data-install]");
      if (instBtn) {
        instBtn.addEventListener("click", () => {
          instBtn.disabled = true;
          instBtn.textContent = "Installing...";
          window.pywebview.api.install_tool(instBtn.getAttribute("data-install"));
        });
      }
      const copyBtn = row.querySelector("[data-copy]");
      if (copyBtn) {
        copyBtn.addEventListener("click", () => {
          navigator.clipboard?.writeText(copyBtn.getAttribute("data-copy"));
          showToast("Copied winget install command.");
        });
      }
      const urlBtn = row.querySelector("[data-url]");
      if (urlBtn) {
        urlBtn.addEventListener("click", () => window.pywebview.api.open_url(urlBtn.getAttribute("data-url")));
      }
      docList.appendChild(row);
    }

    $("settings-modal").classList.add("open");
  }

  // Command Palette (Ctrl+K)
  function getCommands() {
    return [
      { label: "Browse Video File", kbd: "Ctrl+O", run: async () => handlePaths(await window.pywebview.api.pick_videos()) },
      { label: "Add Subtitle (.srt / .vtt / .ass / .zip)", kbd: "", run: async () => handlePaths(await window.pywebview.api.pick_subtitles()) },
      { label: "Embed Subtitles Now", kbd: "Ctrl+Enter", run: startMux },
      { label: "Merge Top 2 Tracks into Bilingual (.ass)", kbd: "", run: mergeTopTwoBilingual },
      { label: "Switch to Mux Studio", kbd: "", run: () => switchTab("single") },
      { label: "Switch to Batch Folder Mode", kbd: "", run: () => switchTab("batch") },
      { label: "Switch to Watch Daemon", kbd: "", run: () => switchTab("watch") },
      { label: "Choose Batch Folder", kbd: "", run: async () => { const f = await window.pywebview.api.pick_folder(); if (f) { switchTab("batch"); await loadBatchFolder(f); } } },
      { label: "Audit Library for Missing Subtitles", kbd: "", run: async () => { switchTab("batch"); if (!state.batchFolder) { const f = await window.pywebview.api.pick_folder(); if (f) await loadBatchFolder(f); } if (state.batchFolder) await runLibraryAudit(); } },
      { label: "Save Current Settings as Profile", kbd: "", run: () => $("btn-save-profile").click() },
      { label: "Minimize Watch Daemon to System Tray", kbd: "", run: () => $("btn-watch-tray").click() },
      { label: "Studio Settings & Engine Doctor", kbd: "Ctrl+,", run: openSettingsModal },
      { label: "Toggle Dark / Light Theme", kbd: "", run: toggleTheme },
      { label: "Manage Undo Backups", kbd: "", run: openUndoManager },
      { label: "Undo Last In-Place Replace", kbd: "", run: () => triggerUndo() },
      { label: "Clear Video & Tracks", kbd: "", run: () => { state.video = null; state.subtitles = []; renderVideoPanel(); renderSubtitles(); } },
    ];
  }

  function renderCmdList(query = "") {
    const q = query.trim().toLowerCase();
    const items = getCommands().filter((c) => c.label.toLowerCase().includes(q));
    state.cmdIndex = Math.min(state.cmdIndex, Math.max(0, items.length - 1));
    const list = $("cmd-list");
    list.innerHTML = "";
    items.forEach((cmd, idx) => {
      const row = document.createElement("div");
      row.className = `cmd-item${idx === state.cmdIndex ? " active" : ""}`;
      row.innerHTML = `<span>${escHtml(cmd.label)}</span>${cmd.kbd ? `<kbd>${escHtml(cmd.kbd)}</kbd>` : ""}`;
      row.addEventListener("click", () => {
        $("cmd-modal").classList.remove("open");
        cmd.run();
      });
      list.appendChild(row);
    });
    return items;
  }

  function openCmdPalette() {
    $("cmd-modal").classList.add("open");
    $("cmd-input").value = "";
    state.cmdIndex = 0;
    renderCmdList("");
    setTimeout(() => $("cmd-input").focus(), 20);
  }

  function closeAllModals() {
    ["cmd-modal", "sync-modal", "preview-modal", "profile-modal", "undo-modal", "settings-modal"].forEach((id) => {
      $(id).classList.remove("open");
    });
  }

  function openProfileManager() {
    const modal = $("profile-modal");
    const list = $("profile-manage-list");
    const cfg = state.config || {};
    const profiles = cfg.profiles || {};
    const builtins = new Set(["cinema-default", "arabic-rtl", "tv-compat", "archive-inplace"]);
    list.innerHTML = "";

    for (const [key, prof] of Object.entries(profiles)) {
      const isBuiltIn = builtins.has(key);
      const isActive = key === cfg.active_profile;
      const row = document.createElement("div");
      row.className = "cue-line";
      row.style.flexDirection = "row";
      row.style.justifyContent = "space-between";
      row.style.alignItems = "center";
      row.style.gap = "10px";
      row.innerHTML = `
        <div style="min-width:0;flex:1;">
          <div style="font-weight:600;display:flex;align-items:center;gap:6px;">
            <span>${escHtml(prof.label || key)}</span>
            ${isActive ? `<span class="badge ok">ACTIVE</span>` : ""}
            ${isBuiltIn ? `<span class="badge">BUILT-IN</span>` : ""}
          </div>
          <div class="tabular" style="font-size:11.5px;color:var(--text-muted);">${escHtml(prof.mode || "auto")} · existing=${escHtml(prof.existing_subs || "keep")} · auto_sync=${Boolean(prof.auto_sync)}</div>
        </div>
        <div style="display:flex;gap:6px;flex-shrink:0;">
          <button class="btn" data-puse="${escHtml(key)}" style="min-height:28px;padding:0 10px;">Apply</button>
          ${!isBuiltIn ? `<button class="btn btn-danger" data-pdel="${escHtml(key)}" style="min-height:28px;padding:0 10px;">Delete</button>` : ""}
        </div>
      `;
      row.querySelector("[data-puse]").addEventListener("click", async () => {
        const res = await window.pywebview.api.set_profile(key);
        if (res.success && res.config) {
          renderProfiles(res.config);
          applyProfileToControls(res.config.profiles[key]);
          openProfileManager();
          showToast(`Applied profile: ${prof.label || key}`);
        }
      });
      const delBtn = row.querySelector("[data-pdel]");
      if (delBtn) {
        delBtn.addEventListener("click", async () => {
          const res = await window.pywebview.api.delete_profile(key);
          if (res.config) renderProfiles(res.config);
          showToast(res.message || "Deleted profile.");
          openProfileManager();
        });
      }
      list.appendChild(row);
    }

    modal.classList.add("open");
    setTimeout(() => $("profile-name-input").focus(), 20);
  }

  window.__onNativeDrop = (paths) => {
    $("drag-overlay").classList.remove("visible");
    handlePaths(paths);
  };

  function bindEvents() {
    initTooltips();

    $("tab-single").addEventListener("click", () => switchTab("single"));
    $("tab-batch").addEventListener("click", () => switchTab("batch"));
    $("tab-watch").addEventListener("click", () => switchTab("watch"));

    ["opt-mode", "opt-existing", "opt-mkv-fallback", "opt-replace", "opt-auto-sync", "batch-recursive", "watch-recursive", "opt-crf", "opt-preset", "hs-font", "hs-size", "hs-outline", "hs-margin", "hs-color", "hs-encoder"].forEach((id) => {
      $(id).addEventListener("change", () => {
        savePrefs();
        checkLanguageAdvisory();
        syncHardsubDrawer();
        if (id === "batch-recursive" && state.batchFolder) {
          loadBatchFolder(state.batchFolder);
        }
      });
    });

    $("opt-profile").addEventListener("change", async (e) => {
      const res = await window.pywebview.api.set_profile(e.target.value);
      if (res.success && res.config) {
        renderProfiles(res.config);
        applyProfileToControls(res.config.profiles[res.config.active_profile]);
        showToast(`Applied profile: ${res.config.profiles[res.config.active_profile]?.label || e.target.value}`);
      }
    });

    $("btn-save-profile").addEventListener("click", openProfileManager);
    $("btn-close-profile").addEventListener("click", () => $("profile-modal").classList.remove("open"));
    $("btn-profile-confirm-save").addEventListener("click", async () => {
      const name = ($("profile-name-input").value || "").trim();
      if (!name) {
        showToast("Enter a profile name first.");
        return;
      }
      const prof = {
        mode: $("opt-mode").value,
        existing_subs: $("opt-existing").value,
        fallback_to_mkv: $("opt-mkv-fallback").checked,
        replace_original: $("opt-replace").checked,
        auto_sync: $("opt-auto-sync").checked,
        crf: Number($("opt-crf").value || 18),
        preset: $("opt-preset").value || "medium",
        hardsub_style: {
          font_name: $("hs-font").value || "Arial",
          font_size: Number($("hs-size").value || 24),
          primary_color: $("hs-color").value || "#FFFFFF",
          outline_color: "#000000",
          outline_width: Number($("hs-outline").value || 2),
          margin_v: Number($("hs-margin").value || 28),
          encoder: $("hs-encoder").value || "auto",
        },
      };
      const res = await window.pywebview.api.save_profile(name, prof);
      if (res.success) {
        $("profile-name-input").value = "";
        renderProfiles(res.config);
        openProfileManager();
        showToast(`Saved profile "${name}"`);
      } else {
        showToast(res.error || "Could not save profile.");
      }
    });
    $("preview-search").addEventListener("input", (e) => renderPreviewCueRows(e.target.value));

    $("opt-express-drop").addEventListener("change", async (e) => {
      const res = await window.pywebview.api.update_automation_prefs({ express_drop: e.target.checked });
      if (res.config) state.config = res.config;
      showToast(e.target.checked ? "Express Drop-to-Go enabled." : "Express Drop-to-Go disabled.");
    });

    $("opt-autodiscover").addEventListener("change", async (e) => {
      const res = await window.pywebview.api.update_automation_prefs({ auto_discover_companions: e.target.checked });
      if (res.config) state.config = res.config;
    });

    $("opt-ui-lang")?.addEventListener("change", async (e) => {
      applyUiLanguage(e.target.value);
      const res = await window.pywebview.api.update_automation_prefs({ ui_lang: e.target.value });
      if (res.config) state.config = res.config;
    });

    $("btn-check-updates")?.addEventListener("click", async () => {
      const btn = $("btn-check-updates");
      btn.disabled = true;
      btn.textContent = "Checking...";
      const res = await window.pywebview.api.check_updates();
      btn.disabled = false;
      btn.textContent = "Check Updates";
      if ($("update-status-desc")) $("update-status-desc").textContent = res.message || "";
      if (res.update_available && res.url) {
        showToast(res.message, { actionLabel: "Open Release", durationMs: 6000, onAction: () => window.pywebview.api.open_url(res.url) });
      } else {
        showToast(res.message || "Update check finished.");
      }
    });

    ["hs-size", "hs-outline", "hs-margin", "hs-color"].forEach((id) => {
      $(id).addEventListener("input", updateStylePreview);
    });

    $("btn-theme").addEventListener("click", toggleTheme);
    $("btn-settings-theme").addEventListener("click", toggleTheme);
    $("btn-settings").addEventListener("click", openSettingsModal);
    $("tool-pills").addEventListener("click", openSettingsModal);
    $("btn-close-settings").addEventListener("click", () => $("settings-modal").classList.remove("open"));
    $("btn-shell-toggle").addEventListener("click", async () => {
      const next = !(state.env && state.env.shellInstalled);
      const res = await window.pywebview.api.toggle_shell_menu(next);
      if (state.env) state.env.shellInstalled = res.shellInstalled;
      showToast(res.message);
      openSettingsModal();
    });

    $("btn-browse-video").addEventListener("click", async () => handlePaths(await window.pywebview.api.pick_videos()));
    $("video-dropzone").addEventListener("click", async (e) => {
      if (e.target.closest("#btn-clear-video")) return;
      handlePaths(await window.pywebview.api.pick_videos());
    });
    $("btn-clear-video").addEventListener("click", () => {
      state.video = null;
      renderVideoPanel();
      renderSubtitles();
    });

    $("btn-browse-subs").addEventListener("click", async () => handlePaths(await window.pywebview.api.pick_subtitles()));
    $("sub-dropzone").addEventListener("click", async () => handlePaths(await window.pywebview.api.pick_subtitles()));
    $("btn-merge-bilingual").addEventListener("click", mergeTopTwoBilingual);

    $("btn-pick-output").addEventListener("click", async () => {
      const ext = $("opt-mode").value === "soft-mkv" ? ".mkv" : (state.video ? `.${state.video.container}` : ".mkv");
      const out = await window.pywebview.api.pick_save_path(state.video?.path || "", ext);
      if (out) $("opt-output").value = out;
    });

    $("btn-embed").addEventListener("click", startMux);
    $("btn-cancel").addEventListener("click", () => window.pywebview.api.cancel_embed());

    $("btn-undo-drawer").addEventListener("click", openUndoManager);
    $("btn-purge-undos").addEventListener("click", async () => {
      const res = await window.pywebview.api.purge_undo_backups();
      state.undos = [];
      renderUndoHeader();
      openUndoManager();
      showToast(`Freed ${fmtBytes(res.freed_bytes)} across ${res.removed} backup(s).`);
    });

    $("btn-cmd-palette").addEventListener("click", openCmdPalette);
    $("btn-close-sync").addEventListener("click", () => $("sync-modal").classList.remove("open"));
    $("btn-close-preview").addEventListener("click", () => $("preview-modal").classList.remove("open"));
    $("btn-close-undo").addEventListener("click", () => $("undo-modal").classList.remove("open"));

    $("btn-pick-folder").addEventListener("click", async () => {
      const f = await window.pywebview.api.pick_folder();
      if (f) await loadBatchFolder(f);
    });
    $("btn-audit-batch").addEventListener("click", runLibraryAudit);

    $("btn-run-batch").addEventListener("click", async () => {
      const enabled = state.batchPairs.filter((p) => p.enabled);
      if (!enabled.length) return;
      const prof = activeProfileDefaults();
      state.activeBatchKind = "batch";
      $("btn-run-batch").disabled = true;
      $("btn-run-batch").textContent = "Running...";
      await window.pywebview.api.start_batch({
        jobs: enabled,
        directory: state.batchFolder,
        mode: $("opt-mode").value,
        existing_subs: $("opt-existing").value,
        fallback_to_mkv: $("opt-mkv-fallback").checked,
        replace_original: $("opt-replace").checked,
        auto_sync: $("opt-auto-sync").checked,
        clean_sdh: Boolean(prof.clean_sdh),
        clean_ads: Boolean(prof.clean_ads),
        strip_html_tags: Boolean(prof.strip_html_tags),
      });
    });

    // Watch Folder Controls
    $("btn-watch-folder").addEventListener("click", async () => {
      const f = await window.pywebview.api.pick_folder();
      if (f) {
        state.watchFolder = f;
        $("watch-folder-label").textContent = f;
        $("btn-watch-toggle").disabled = false;
      }
    });

    $("btn-watch-toggle").addEventListener("click", async () => {
      if (!state.watchFolder && !state.watching) return;
      if (state.watching) {
        await window.pywebview.api.stop_watch();
        state.watching = false;
        $("btn-watch-toggle").textContent = "Start Watching";
        $("btn-watch-toggle").className = "btn btn-primary";
        $("watch-dot").classList.remove("active");
        $("btn-watch-tray").classList.add("hidden");
      } else {
        const cleanOn = $("watch-clean").checked;
        const prof = activeProfileDefaults();
        const res = await window.pywebview.api.start_watch({
          directory: state.watchFolder,
          recursive: $("watch-recursive").checked,
          mode: $("opt-mode").value,
          existing_subs: $("opt-existing").value,
          replace_original: $("opt-replace").checked,
          default_lang: prof.default_lang || "auto",
          auto_sync: $("watch-autosync").checked,
          clean_sdh: cleanOn,
          clean_ads: cleanOn,
          sub_cleanup: $("watch-cleanup").value,
          notify_toast: $("watch-notify").checked,
        });
        if (res.success) {
          state.watching = true;
          $("btn-watch-toggle").textContent = "Stop Watching";
          $("btn-watch-toggle").className = "btn btn-danger";
          $("watch-dot").classList.add("active");
          $("btn-watch-tray").classList.remove("hidden");
        } else {
          showToast(res.error || "Could not start watch daemon.");
        }
      }
    });

    $("btn-watch-tray").addEventListener("click", async () => {
      const res = await window.pywebview.api.send_watch_to_tray();
      if (!res.success) showToast(res.error || "Could not minimize to tray.");
    });

    $("watch-startup").addEventListener("change", async (e) => {
      const enable = e.target.checked;
      const profKey = $("opt-profile")?.value || "";
      const res = await window.pywebview.api.toggle_watch_startup(
        enable,
        state.watchFolder || "",
        $("watch-recursive").checked,
        profKey
      );
      if (!res.success) {
        e.target.checked = Boolean(res.watchStartup);
      }
      showToast(res.message);
    });

    $("btn-watch-clear").addEventListener("click", () => {
      $("watch-log").innerHTML = `<div class="panel-inner" style="color:var(--text-muted);">Log cleared.</div>`;
    });

    // Global drag visual cue (ignoring internal track reorder drags)
    let dragDepth = 0;
    document.addEventListener("dragenter", (e) => {
      if (state.dragTrackIdx !== null) return;
      e.preventDefault();
      dragDepth++;
      $("drag-overlay").classList.add("visible");
    });
    document.addEventListener("dragover", (e) => {
      if (state.dragTrackIdx === null) e.preventDefault();
    });
    document.addEventListener("dragleave", () => {
      if (state.dragTrackIdx !== null) return;
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) $("drag-overlay").classList.remove("visible");
    });
    document.addEventListener("drop", () => {
      dragDepth = 0;
      $("drag-overlay").classList.remove("visible");
    });

    for (const id of ["cmd-modal", "sync-modal", "preview-modal", "profile-modal", "undo-modal", "settings-modal"]) {
      $(id).addEventListener("click", (e) => {
        if (e.target === $(id)) $(id).classList.remove("open");
      });
    }

    $("cmd-input").addEventListener("input", (e) => {
      state.cmdIndex = 0;
      renderCmdList(e.target.value);
    });
    $("cmd-input").addEventListener("keydown", (e) => {
      const items = getCommands().filter((c) => c.label.toLowerCase().includes($("cmd-input").value.trim().toLowerCase()));
      if (e.key === "ArrowDown") {
        e.preventDefault();
        state.cmdIndex = (state.cmdIndex + 1) % Math.max(1, items.length);
        renderCmdList($("cmd-input").value);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        state.cmdIndex = (state.cmdIndex - 1 + Math.max(1, items.length)) % Math.max(1, items.length);
        renderCmdList($("cmd-input").value);
      } else if (e.key === "Enter" && items[state.cmdIndex]) {
        e.preventDefault();
        $("cmd-modal").classList.remove("open");
        items[state.cmdIndex].run();
      }
    });

    document.addEventListener("keydown", async (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        openCmdPalette();
      } else if ((e.ctrlKey || e.metaKey) && e.key === ",") {
        e.preventDefault();
        openSettingsModal();
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "o") {
        e.preventDefault();
        handlePaths(await window.pywebview.api.pick_videos());
      } else if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        startMux();
      } else if (e.key === "Escape") {
        closeAllModals();
      }
    });
  }

  async function init() {
    loadPrefs();
    bindEvents();
    syncHardsubDrawer();
    renderRecents();
    renderSubtitles();
    const env = await window.pywebview.api.get_environment();
    state.env = env;
    if (env.config) renderProfiles(env.config);
    if ($("watch-startup")) {
      $("watch-startup").checked = Boolean(env.watchStartup);
    }
    renderToolPills(env.tools || {});
    renderHwEncoders(env.hwEncoders || []);
    state.undos = env.undos || [];
    renderUndoHeader();
  }

  if (window.pywebview && window.pywebview.api) {
    init();
  } else {
    window.addEventListener("pywebviewready", init);
  }
})();
