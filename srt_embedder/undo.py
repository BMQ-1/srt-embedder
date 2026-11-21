"""Instant same-filesystem backup and undo transaction store."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class UndoRecord:
    tx_id: str
    created_at: str
    original_path: str
    backup_path: str
    output_path: str
    engine_used: str
    backup_size_bytes: int = 0


def _store_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SRTEmbedder"
    base.mkdir(parents=True, exist_ok=True)
    return base / "undo_log.json"


def _load_records() -> list[dict]:
    p = _store_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_records(records: list[dict]) -> None:
    p = _store_path()
    p.write_text(json.dumps(records[-50:], indent=2), encoding="utf-8")


def _prune_records(
    records: list[dict],
    max_keep: int = 20,
    max_age_days: int = 14,
    max_total_gb: float = 10.0,
) -> list[dict]:
    """Prune missing, expired, or over-quota backups oldest-first."""
    now_ts = datetime.now(timezone.utc).timestamp()
    max_age_sec = max(1, max_age_days) * 86_400
    max_bytes = int(max(0.5, max_total_gb) * (1024 ** 3))

    live: list[dict] = []
    for r in records:
        bak = Path(r.get("backup_path", ""))
        if not bak.exists():
            continue
        # Never expire the newest entry if called right after creation
        created_raw = r.get("created_at", "")
        if len(records) > 1 and created_raw and r is not records[-1]:
            try:
                dt = datetime.fromisoformat(created_raw)
                if now_ts - dt.timestamp() > max_age_sec:
                    bak.unlink(missing_ok=True)
                    continue
            except Exception:
                pass
        live.append(r)

    if max_keep > 0 and len(live) > max_keep:
        for old in live[:-max_keep]:
            Path(old.get("backup_path", "")).unlink(missing_ok=True)
        live = live[-max_keep:]

    # Enforce total backup byte budget while always keeping at least the newest backup
    total = sum(int(r.get("backup_size_bytes") or Path(r["backup_path"]).stat().st_size) for r in live)
    while len(live) > 1 and total > max_bytes:
        oldest = live.pop(0)
        sz = int(oldest.get("backup_size_bytes") or 0)
        Path(oldest.get("backup_path", "")).unlink(missing_ok=True)
        total -= sz

    return live


def register_backup(
    original_path: str,
    backup_path: str,
    output_path: str,
    engine_used: str,
    max_keep: int = 20,
    max_age_days: int = 14,
    max_total_gb: float = 10.0,
) -> UndoRecord:
    bak = Path(backup_path).resolve()
    size_bytes = bak.stat().st_size if bak.exists() else 0
    record = UndoRecord(
        tx_id=uuid.uuid4().hex[:8],
        created_at=datetime.now(timezone.utc).isoformat(),
        original_path=str(Path(original_path).resolve()),
        backup_path=str(bak),
        output_path=str(Path(output_path).resolve()),
        engine_used=engine_used,
        backup_size_bytes=size_bytes,
    )
    records = _load_records()
    records.append(asdict(record))
    records = _prune_records(records, max_keep=max_keep, max_age_days=max_age_days, max_total_gb=max_total_gb)
    _save_records(records)
    return record


def list_undo_records() -> list[UndoRecord]:
    raw = _load_records()
    out: list[UndoRecord] = []
    live: list[dict] = []
    for item in raw:
        bak = Path(item.get("backup_path", ""))
        if bak.exists():
            if not item.get("backup_size_bytes"):
                item["backup_size_bytes"] = bak.stat().st_size
            live.append(item)
            out.append(UndoRecord(**item))
    if len(live) != len(raw):
        _save_records(live)
    return list(reversed(out))


def delete_backup(tx_id: str) -> tuple[bool, str]:
    """Delete a backup file permanently to reclaim disk space."""
    records = _load_records()
    for i, rec in enumerate(records):
        if rec.get("tx_id") == tx_id:
            bak = Path(rec["backup_path"])
            if bak.exists():
                bak.unlink(missing_ok=True)
            records.pop(i)
            _save_records(records)
            return True, f"Deleted backup for {Path(rec['original_path']).name}."
    return False, f"Backup '{tx_id}' not found."


def purge_all_backups() -> tuple[int, int]:
    """Delete all active backup files and return (count_removed, bytes_freed)."""
    records = _load_records()
    removed = 0
    freed = 0
    for rec in records:
        bak = Path(rec["backup_path"])
        if bak.exists():
            freed += bak.stat().st_size
            bak.unlink(missing_ok=True)
            removed += 1
    _save_records([])
    return removed, freed


def perform_undo(tx_id: Optional[str] = None) -> tuple[bool, str, Optional[UndoRecord]]:
    """Restore original video from an instant same-filesystem backup."""
    records = _load_records()
    if not records:
        return False, "No undo history available.", None

    target_idx = -1
    if tx_id:
        for i, rec in enumerate(records):
            if rec.get("tx_id") == tx_id:
                target_idx = i
                break
        if target_idx == -1:
            return False, f"Undo transaction '{tx_id}' not found.", None
    else:
        for i in range(len(records) - 1, -1, -1):
            if Path(records[i]["backup_path"]).exists():
                target_idx = i
                break
        if target_idx == -1:
            return False, "No active backup files exist to restore.", None

    rec = UndoRecord(**records[target_idx])
    backup = Path(rec.backup_path)
    orig = Path(rec.original_path)
    out = Path(rec.output_path)

    if not backup.exists():
        return False, f"Backup file missing: {backup}", rec

    # Remove or trash the muxed output before restoring original
    if out.exists():
        try:
            from send2trash import send2trash
            send2trash(str(out))
        except Exception:
            out.unlink(missing_ok=True)

    os.replace(str(backup), str(orig))
    records.pop(target_idx)
    _save_records(records)
    return True, f"Restored {orig.name} from backup.", rec
