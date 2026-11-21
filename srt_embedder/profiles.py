"""Persistent profiles, rule-based defaults, and watch configuration."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

DEFAULT_PROFILES: dict[str, dict[str, Any]] = {
    "cinema-default": {
        "label": "Cinema Default (Lossless MKV/MP4)",
        "mode": "auto",
        "existing_subs": "keep",
        "fallback_to_mkv": True,
        "replace_original": False,
        "default_lang": "auto",
        "clean_sdh": True,
        "clean_ads": True,
        "strip_html_tags": False,
        "fix_rtl_bidi": False,
        "auto_sync": False,
        "auto_sync_threshold_ms": 250,
        "output_template": "default",
        "prefer_sub_lang_when_foreign_audio": "",
        "sub_cleanup": "keep",
        "crf": 18,
        "preset": "medium",
        "hardsub_style": {
            "font_name": "Arial",
            "font_size": 24,
            "primary_color": "#FFFFFF",
            "outline_color": "#000000",
            "outline_width": 2,
            "margin_v": 28,
            "encoder": "auto",
        },
    },
    "arabic-rtl": {
        "label": "Arabic RTL Clean + Auto-Sync",
        "mode": "auto",
        "existing_subs": "replace-same-lang",
        "fallback_to_mkv": True,
        "replace_original": False,
        "default_lang": "ara",
        "clean_sdh": True,
        "clean_ads": True,
        "strip_html_tags": False,
        "fix_rtl_bidi": True,
        "auto_sync": True,
        "auto_sync_threshold_ms": 250,
        "output_template": "default",
        "prefer_sub_lang_when_foreign_audio": "ara",
        "sub_cleanup": "keep",
        "crf": 18,
        "preset": "medium",
        "hardsub_style": {
            "font_name": "Tahoma",
            "font_size": 26,
            "primary_color": "#FFFFFF",
            "outline_color": "#000000",
            "outline_width": 2,
            "margin_v": 28,
            "encoder": "auto",
        },
    },
    "tv-compat": {
        "label": "TV / Phone Burn-in (GPU Hardsub)",
        "mode": "hardsub",
        "existing_subs": "keep",
        "fallback_to_mkv": True,
        "replace_original": False,
        "default_lang": "auto",
        "clean_sdh": True,
        "clean_ads": True,
        "strip_html_tags": True,
        "auto_sync": True,
        "auto_sync_threshold_ms": 250,
        "sub_cleanup": "keep",
        "crf": 18,
        "preset": "medium",
        "hardsub_style": {
            "font_name": "Tahoma",
            "font_size": 26,
            "primary_color": "#FFFFFF",
            "outline_color": "#000000",
            "outline_width": 2,
            "margin_v": 28,
            "encoder": "auto",
        },
    },
    "archive-inplace": {
        "label": "Archive Lossless MKV In-Place",
        "mode": "soft-mkv",
        "existing_subs": "replace-same-lang",
        "fallback_to_mkv": True,
        "replace_original": True,
        "default_lang": "auto",
        "clean_sdh": False,
        "clean_ads": True,
        "strip_html_tags": False,
        "auto_sync": False,
        "auto_sync_threshold_ms": 250,
        "sub_cleanup": "archive",
        "crf": 18,
        "preset": "medium",
        "hardsub_style": {
            "font_name": "Arial",
            "font_size": 24,
            "primary_color": "#FFFFFF",
            "outline_color": "#000000",
            "outline_width": 2,
            "margin_v": 28,
            "encoder": "auto",
        },
    },
}


def config_path() -> Path:
    """Return path to the user's persistent SRT Embedder configuration JSON."""
    override = os.environ.get("SRT_EMBEDDER_CONFIG", "").strip()
    if override:
        p = Path(override).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    base = Path(os.environ.get("APPDATA") or (Path.home() / ".config"))
    root = base / "SRT Embedder"
    root.mkdir(parents=True, exist_ok=True)
    return root / "config.json"


def _default_config() -> dict[str, Any]:
    return {
        "active_profile": "cinema-default",
        "ui_lang": "en",
        "express_drop": False,
        "auto_discover_companions": True,
        "backup_max_days": 14,
        "backup_max_gb": 10.0,
        "watch": {
            "directory": "",
            "recursive": False,
            "auto_sync": False,
            "clean_sdh": True,
            "clean_ads": True,
            "sub_cleanup": "archive",
            "notify_toast": True,
        },
        "profiles": deepcopy(DEFAULT_PROFILES),
    }


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


def load_config() -> dict[str, Any]:
    """Load persisted configuration, merging in any newly added default profiles."""
    cfg = _default_config()
    p = config_path()
    if p.exists():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg = _deep_merge(cfg, loaded)
        except Exception:
            pass
    # Guarantee built-ins always exist if missing
    for name, prof in DEFAULT_PROFILES.items():
        cfg["profiles"].setdefault(name, deepcopy(prof))
    if cfg.get("active_profile") not in cfg["profiles"]:
        cfg["active_profile"] = "cinema-default"
    return cfg


def save_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Persist configuration atomically to disk and return normalized config."""
    normalized = _deep_merge(_default_config(), cfg)
    p = config_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(normalized, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)
    return normalized


def get_profile(name: Optional[str] = None) -> tuple[str, dict[str, Any]]:
    """Return (profile_key, profile_dict) for requested name or active profile."""
    cfg = load_config()
    key = (name or cfg.get("active_profile") or "cinema-default").strip()
    profiles = cfg["profiles"]
    if key not in profiles:
        # Case-insensitive or label lookup
        low = key.lower()
        for pk, pv in profiles.items():
            if pk.lower() == low or str(pv.get("label", "")).lower() == low:
                return pk, deepcopy(pv)
        raise KeyError(f"Unknown profile '{key}'. Available: {', '.join(profiles)}")
    return key, deepcopy(profiles[key])


def set_active_profile(name: str) -> dict[str, Any]:
    """Switch the persisted active profile."""
    key, _ = get_profile(name)
    cfg = load_config()
    cfg["active_profile"] = key
    return save_config(cfg)


def save_custom_profile(name: str, profile_data: dict[str, Any], activate: bool = True) -> tuple[str, dict[str, Any]]:
    """Create or update a named profile and optionally activate it."""
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name.strip().lower()).strip("-_")
    if not slug:
        raise ValueError("Profile name must contain letters or digits.")
    cfg = load_config()
    base = deepcopy(DEFAULT_PROFILES["cinema-default"])
    merged = _deep_merge(base, profile_data)
    merged["label"] = str(profile_data.get("label") or name.strip())
    cfg["profiles"][slug] = merged
    if activate:
        cfg["active_profile"] = slug
    return slug, save_config(cfg)


def delete_custom_profile(name: str) -> tuple[bool, str, dict[str, Any]]:
    """Delete a non-builtin custom profile."""
    if name in DEFAULT_PROFILES:
        return False, "Built-in profiles cannot be deleted.", load_config()
    cfg = load_config()
    if name not in cfg["profiles"]:
        return False, f"Profile '{name}' not found.", cfg
    del cfg["profiles"][name]
    if cfg.get("active_profile") == name:
        cfg["active_profile"] = "cinema-default"
    return True, f"Deleted profile '{name}'.", save_config(cfg)
