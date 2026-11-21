"""Feelcraft CLI for SRT Embedder."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import signal
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from . import __version__


def _format_bytes(n: int) -> str:
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024.0 or unit == "TB":
            return f"{val:,.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{n} B"


def _format_duration(sec: float) -> str:
    total = max(0, int(round(sec)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _make_console(no_color: bool = False):
    from rich.console import Console

    disable_color = no_color or bool(os.environ.get("NO_COLOR")) or not sys.stdout.isatty()
    return Console(no_color=disable_color, highlight=False)


def _cmd_doctor(args: argparse.Namespace) -> int:
    from .tools import detect_hw_encoders, discover_tools

    if getattr(args, "install", None):
        from .bootstrap import bootstrap_tool

        console = _make_console(args.no_color)
        for target in ([args.install] if args.install != "all" else ["ffmpeg", "mkvmerge"]):
            console.print(f"Installing [bold]{target}[/bold]...")
            ok, msg = bootstrap_tool(target, progress_cb=lambda pct, st: print(f"  [{pct:3.0f}%] {st}"))
            console.print(f"  [{'green' if ok else 'red'}]{msg}[/{'green' if ok else 'red'}]")

    update_info = None
    if getattr(args, "check_update", False):
        from .bootstrap import check_for_updates

        update_info = check_for_updates()

    tools = discover_tools()
    hw = detect_hw_encoders(tools["ffmpeg"].path) if tools["ffmpeg"].available else ["libx264"]
    if args.json:
        payload = {"tools": {k: asdict(v) for k, v in tools.items()}, "hw_encoders": hw}
        if update_info is not None:
            payload["update"] = update_info
        print(json.dumps(payload, indent=2))
        return 0

    from rich.table import Table

    console = _make_console(args.no_color)
    table = Table(title="SRT Embedder Environment", box=None, header_style="bold")
    table.add_column("Tool", style="bold")
    table.add_column("Status")
    table.add_column("Version")
    table.add_column("Path", style="dim")

    for name, st in tools.items():
        status = "[green]READY[/green]" if st.available else "[red]MISSING[/red]"
        table.add_row(name, status, st.version or "—", st.path or "not found")

    console.print(table)
    console.print(f"[dim]Hardsub Encoders: {', '.join(hw)}[/dim]")
    if update_info is not None:
        console.print(f"[cyan]Updates:[/cyan] {update_info.get('message', '')}")
    return 0 if (tools["mkvmerge"].available or tools["ffmpeg"].available) else 1


def _cmd_inspect(args: argparse.Namespace) -> int:
    from .engine import probe_video
    from .subtitle import SUBTITLE_EXTENSIONS, inspect_srt, ms_to_srt_time

    target = Path(args.path)
    if not target.exists():
        print(f"Error: File not found: {target}", file=sys.stderr)
        return 2

    if target.suffix.lower() in SUBTITLE_EXTENSIONS:
        rep = inspect_srt(str(target))
        if args.json:
            payload = asdict(rep)
            payload.pop("normalized_content", None)
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0

        from rich.markup import escape
        from rich.panel import Panel
        from rich.table import Table

        console = _make_console(args.no_color)
        table = Table(show_header=False, box=None)
        table.add_column("Field", style="dim")
        table.add_column("Value")
        table.add_row("File", escape(rep.filename))
        table.add_row("Format / Size", f"{rep.source_format.upper()} · {rep.size_bytes:,} bytes")
        table.add_row("Encoding", f"{rep.encoding_detected}{' (BOM)' if rep.has_bom else ''}")
        table.add_row("Language", f"{rep.detected_lang_name} ({rep.detected_lang_code})")
        table.add_row("Cues", f"{rep.cue_count:,} (auto-repaired: {rep.repaired_cues})")
        table.add_row("Span", f"{ms_to_srt_time(rep.first_timestamp_ms)} -> {ms_to_srt_time(rep.last_timestamp_ms)}")
        if rep.warnings:
            table.add_row("Notes", escape("\n".join(rep.warnings)))
        if rep.sample_cues:
            preview = "\n".join(f"#{c['index']} [{c['start']}] {c['text']}" for c in rep.sample_cues[:3])
            table.add_row("Preview", escape(preview))
        console.print(Panel(table, title="Subtitle Inspection", border_style="yellow"))
        return 0

    info = probe_video(str(target))
    if args.json:
        print(json.dumps(asdict(info), indent=2, ensure_ascii=False))
        return 0

    from rich.markup import escape
    from rich.table import Table

    console = _make_console(args.no_color)
    console.print(
        f"[bold]{escape(info.filename)}[/bold]  "
        f"[dim]{escape(info.container.upper())} · {_format_duration(info.duration_sec)} · "
        f"{_format_bytes(info.size_bytes)} · {info.width}x{info.height} @ {info.fps or '?'} fps[/dim]"
    )

    table = Table(box=None, header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Type")
    table.add_column("Codec")
    table.add_column("Lang")
    table.add_column("Flags")
    table.add_column("Title / Info")

    for s in info.streams:
        flags = []
        if s.is_default:
            flags.append("default")
        if s.is_forced:
            flags.append("forced")
        table.add_row(
            str(s.index),
            s.codec_type,
            s.codec_name,
            s.language,
            ", ".join(flags) or "—",
            s.title or s.extra or "—",
        )
    console.print(table)
    return 0


def _cmd_sync_check(args: argparse.Namespace) -> int:
    from .engine import check_sync

    if not args.json:
        sys.stdout.write("Checking subtitle sync against embedded track...\r")
        sys.stdout.flush()

    report = check_sync(args.video, args.srt)
    if args.json:
        print(json.dumps(asdict(report), indent=2, ensure_ascii=False))
        return 0 if report.comparable else 1

    from rich.table import Table

    console = _make_console(args.no_color)
    if not report.comparable:
        console.print(f"[yellow]Cannot compare:[/yellow] {report.reason}")
        return 1

    color = "green" if report.verdict == "in-sync" else "yellow"
    method_label = "audio speech VAD" if report.method == "audio-vad" else f"embedded stream #{report.ref_stream_index}"
    console.print(
        f"\nVerdict: [{color} bold]{report.verdict.upper()}[/{color} bold]  "
        f"[dim](via {method_label} {report.ref_language} · "
        f"median delta: {report.median_diff_ms:+d} ms · drift: {report.drift_ms} ms)[/dim]"
    )

    table = Table(box=None, header_style="bold")
    table.add_column("Min", justify="right", style="dim")
    table.add_column("Reference Onset")
    table.add_column("External SRT")
    table.add_column("Delta (ms)", justify="right")

    for s in report.samples:
        d_style = "green" if abs(s.diff_ms) <= 150 else "yellow"
        table.add_row(
            str(s.minute),
            f"{s.ref_time}  [dim]{s.ref_text}[/dim]",
            f"{s.srt_time}  [dim]{s.srt_text}[/dim]",
            f"[{d_style}]{s.diff_ms:+d}[/{d_style}]",
        )
    console.print(table)
    if report.recommended_speed != 1.0:
        console.print(
            f"[cyan]Tip:[/cyan] Apply [bold]--speed {report.recommended_speed} --offset {report.recommended_offset_ms:+d}ms[/bold] "
            f"(or [bold]--auto-sync[/bold]) when embedding."
        )
    elif report.recommended_offset_ms != 0:
        console.print(f"[cyan]Tip:[/cyan] Apply [bold]--offset {report.recommended_offset_ms:+d}ms[/bold] when embedding.")
    return 0


def _resolve_profile_opts(args: argparse.Namespace) -> dict:
    from .profiles import get_profile

    prof_name = getattr(args, "profile", None)
    if prof_name:
        _, prof = get_profile(prof_name)
    else:
        _, prof = get_profile(None)

    def _pick(arg_name: str, prof_key: str, default_val):
        val = getattr(args, arg_name, None)
        return prof.get(prof_key, default_val) if val is None else val

    return {
        "mode": _pick("mode", "mode", "auto"),
        "existing_subs": _pick("existing", "existing_subs", "keep"),
        "lang": _pick("lang", "default_lang", "auto"),
        "replace": bool(_pick("replace", "replace_original", False)),
        "fallback_to_mkv": bool(prof.get("fallback_to_mkv", True)) and not bool(getattr(args, "no_mkv_fallback", False)),
        "clean_sdh": bool(_pick("clean_sdh", "clean_sdh", False)),
        "clean_ads": bool(_pick("clean_ads", "clean_ads", False)),
        "strip_tags": bool(_pick("strip_tags", "strip_html_tags", False)),
        "fix_rtl_bidi": bool(_pick("fix_rtl", "fix_rtl_bidi", False)),
        "auto_sync": bool(_pick("auto_sync", "auto_sync", False)),
        "auto_sync_threshold_ms": int(prof.get("auto_sync_threshold_ms", 250)),
        "output_template": str(_pick("naming", "output_template", "default")),
        "prefer_sub_lang_when_foreign_audio": str(_pick("prefer_sub", "prefer_sub_lang_when_foreign_audio", "")),
        "attach_fonts": not bool(getattr(args, "no_attach_fonts", False)),
        "sub_cleanup": (
            "trash" if getattr(args, "trash_subs", False)
            else ("archive" if getattr(args, "archive_subs", False) else prof.get("sub_cleanup", "keep"))
        ),
        "crf": int(prof.get("crf", 18)),
        "preset": str(prof.get("preset", "medium")),
        "hardsub_style": prof.get("hardsub_style") or {},
    }


def _cmd_embed(args: argparse.Namespace) -> int:
    from .engine import embed_subtitles, find_companion_subtitles
    from .models import EmbedOptions, HardsubStyle, SubtitleInput
    from .subtitle import extract_subtitles_from_zip, parse_offset_ms, parse_speed_factor

    cancel_event = threading.Event()

    def _sigint(_signum, _frame):
        cancel_event.set()

    prev_handler = signal.signal(signal.SIGINT, _sigint)
    p_opts = _resolve_profile_opts(args)

    raw_srts = list(args.srts or [])
    if not raw_srts:
        raw_srts = find_companion_subtitles(args.video)
        if not raw_srts:
            print(f"Error: No subtitles given and no companion subtitles found for: {args.video}", file=sys.stderr)
            signal.signal(signal.SIGINT, prev_handler)
            return 2

    expanded_srts: list[str] = []
    for raw_sub in raw_srts:
        if Path(raw_sub).suffix.lower() == ".zip" and Path(raw_sub).is_file():
            expanded_srts.extend(extract_subtitles_from_zip(raw_sub))
        else:
            expanded_srts.append(raw_sub)

    offset_ms = parse_offset_ms(args.offset) if args.offset else 0
    speed_factor = parse_speed_factor(getattr(args, "speed", 1.0))
    subs = [
        SubtitleInput(
            path=srt_path,
            lang_code=p_opts["lang"],
            title=args.title if idx == 0 else "",
            is_default=(not args.no_default) if idx == 0 else False,
            is_forced=args.forced if idx == 0 else False,
            offset_ms=offset_ms,
            speed_factor=speed_factor,
            strip_html_tags=p_opts["strip_tags"],
            clean_sdh=p_opts["clean_sdh"],
            clean_ads=p_opts["clean_ads"],
            fix_rtl_bidi=p_opts["fix_rtl_bidi"],
        )
        for idx, srt_path in enumerate(expanded_srts)
    ]

    hs = p_opts["hardsub_style"]
    style_obj = HardsubStyle(
        font_name=str(hs.get("font_name", "Arial")),
        font_size=int(hs.get("font_size", 24)),
        primary_color=str(hs.get("primary_color", "#FFFFFF")),
        outline_color=str(hs.get("outline_color", "#000000")),
        outline_width=int(hs.get("outline_width", 2)),
        margin_v=int(hs.get("margin_v", 28)),
        encoder=args.hw_encoder or str(hs.get("encoder", "auto")),  # type: ignore[arg-type]
    )

    opts = EmbedOptions(
        video_path=args.video,
        subtitles=subs,
        output_path=args.output,
        mode=p_opts["mode"],
        existing_subs=p_opts["existing_subs"],
        fallback_to_mkv=p_opts["fallback_to_mkv"],
        replace_original=p_opts["replace"],
        create_backup=not args.no_backup,
        dry_run=args.dry_run,
        auto_sync=p_opts["auto_sync"],
        auto_sync_threshold_ms=p_opts["auto_sync_threshold_ms"],
        output_template=p_opts["output_template"],
        attach_fonts=p_opts["attach_fonts"],
        prefer_sub_lang_when_foreign_audio=p_opts["prefer_sub_lang_when_foreign_audio"],
        crf=p_opts["crf"],
        preset=p_opts["preset"],
        hardsub_style=style_obj,
    )

    console = _make_console(args.no_color)
    try:
        if args.json or args.dry_run or not sys.stdout.isatty():
            res = embed_subtitles(opts, cancel_event=cancel_event)
        else:
            from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

            with Progress(
                SpinnerColumn(),
                TextColumn("[bold]{task.fields[phase]}[/bold]"),
                BarColumn(bar_width=28),
                TextColumn("[tabular_nums]{task.percentage:>3.0f}%[/tabular_nums]"),
                TextColumn("[dim]{task.fields[detail]}[/dim]"),
                TimeElapsedColumn(),
                console=console,
                transient=True,
            ) as progress:
                task_id = progress.add_task("mux", total=100.0, phase="Preparing", detail=Path(args.video).name)

                def _on_prog(pct: float, phase: str, detail: str) -> None:
                    progress.update(task_id, completed=pct, phase=phase, detail=detail)

                res = embed_subtitles(opts, progress_cb=_on_prog, cancel_event=cancel_event)
    finally:
        signal.signal(signal.SIGINT, prev_handler)

    if args.json:
        print(json.dumps(asdict(res), indent=2, ensure_ascii=False))
        return 0 if res.success else 1

    if res.dry_run:
        from rich.markup import escape

        console.print("[cyan]Dry run[/cyan] — no files modified.")
        console.print(f"  Engine : [bold]{escape(res.engine_used)}[/bold]")
        console.print(f"  Output : {escape(res.output_path)}")
        console.print(f"  Command: [dim]{escape(' '.join(res.command_preview))}[/dim]")
        return 0

    if not res.success:
        from rich.markup import escape

        console.print(f"[red]Failed:[/red] {escape(res.error)}")
        return 1

    from rich.markup import escape
    from rich.panel import Panel

    lines = [
        f"[bold]Output:[/bold]  {escape(res.output_path)}",
        f"[bold]Engine:[/bold]  {escape(res.engine_used)}  [dim]({res.elapsed_sec:.2f}s · {_format_bytes(res.output_size_bytes)} · {res.subtitle_streams_after} sub stream(s))[/dim]",
    ]
    if res.fallbacks_triggered:
        lines.append("[yellow]Fallbacks:[/yellow] " + escape(" | ".join(res.fallbacks_triggered)))
    if res.warnings:
        lines.append("[dim]Notes: " + escape(" | ".join(res.warnings)) + "[/dim]")
    if res.undo_tx_id:
        lines.append(f"[cyan]Undo available:[/cyan] Run [bold]srt-embedder undo {escape(res.undo_tx_id)}[/bold]")

    console.print(Panel("\n".join(lines), title="Subtitle Embedded", border_style="green"))
    return 0


def _cmd_batch(args: argparse.Namespace) -> int:
    from .engine import embed_subtitles, scan_batch_directory
    from .models import EmbedOptions, SubtitleInput
    from .watcher import _cleanup_consumed_subs

    p_opts = _resolve_profile_opts(args)
    pairs, unmatched_vids, unmatched_subs = scan_batch_directory(args.directory, recursive=args.recursive)
    console = _make_console(args.no_color)

    if not pairs:
        if args.json:
            print(json.dumps({"pairs": [], "results": [], "unmatched_videos": unmatched_vids, "unmatched_subs": unmatched_subs}))
        else:
            console.print(f"[yellow]No matching video + subtitle pairs found in:[/yellow] {args.directory}")
        return 1

    from rich.markup import escape

    results = []
    watch_root = Path(args.directory).resolve()
    for i, pair in enumerate(pairs, start=1):
        if not args.json:
            console.print(
                f"[bold][{i}/{len(pairs)}][/bold] {escape(Path(pair.video_path).name)} "
                f"[dim]<-- {escape(', '.join(Path(s).name for s in pair.srt_paths))} ({escape(pair.confidence)})[/dim]"
            )
        subs = [
            SubtitleInput(
                path=s,
                lang_code=p_opts["lang"],
                is_default=(idx == 0),
                strip_html_tags=p_opts["strip_tags"],
                clean_sdh=p_opts["clean_sdh"],
                clean_ads=p_opts["clean_ads"],
            )
            for idx, s in enumerate(pair.srt_paths)
        ]
        res = embed_subtitles(
            EmbedOptions(
                video_path=pair.video_path,
                subtitles=subs,
                mode=p_opts["mode"],
                existing_subs=p_opts["existing_subs"],
                fallback_to_mkv=p_opts["fallback_to_mkv"],
                replace_original=p_opts["replace"],
                auto_sync=p_opts["auto_sync"],
                auto_sync_threshold_ms=p_opts["auto_sync_threshold_ms"],
                dry_run=args.dry_run,
            )
        )
        results.append(res)
        if res.success and not args.dry_run and p_opts["sub_cleanup"] != "keep":
            _cleanup_consumed_subs(pair.srt_paths, watch_root, p_opts["sub_cleanup"])
        if not args.json:
            if res.dry_run:
                console.print(f"  [cyan]PLAN[/cyan] -> {escape(Path(res.output_path).name)} [dim]({escape(res.engine_used)})[/dim]")
            elif res.success:
                console.print(f"  [green]OK[/green] -> {escape(Path(res.output_path).name)} [dim]({escape(res.engine_used)}, {res.elapsed_sec:.2f}s)[/dim]")
            else:
                console.print(f"  [red]FAIL[/red] -> {escape(res.error)}")

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2, ensure_ascii=False))
    elif unmatched_vids or unmatched_subs:
        console.print(f"[dim]Unmatched: {len(unmatched_vids)} video(s), {len(unmatched_subs)} subtitle(s)[/dim]")

    return 0 if all(r.success for r in results) else 1


def _cmd_undo(args: argparse.Namespace) -> int:
    from .undo import delete_backup, list_undo_records, perform_undo, purge_all_backups

    console = _make_console(args.no_color)
    if args.purge:
        removed, freed = purge_all_backups()
        if args.json:
            print(json.dumps({"purged": removed, "freed_bytes": freed}))
        else:
            console.print(f"[green]Purged {removed} backup(s)[/green] ({_format_bytes(freed)} freed).")
        return 0

    if args.delete:
        ok, msg = delete_backup(args.delete)
        if args.json:
            print(json.dumps({"success": ok, "message": msg}))
        else:
            console.print(f"[{'green' if ok else 'red'}]{msg}[/{'green' if ok else 'red'}]")
        return 0 if ok else 1

    if args.list:
        recs = list_undo_records()
        if args.json:
            print(json.dumps([asdict(r) for r in recs], indent=2))
            return 0
        if not recs:
            console.print("[dim]No active undo backups found.[/dim]")
            return 0
        from rich.markup import escape
        from rich.table import Table

        t = Table(title="Undo Backups", box=None)
        t.add_column("TX ID", style="bold")
        t.add_column("Size", justify="right", style="dim")
        t.add_column("Original File")
        t.add_column("Engine", style="dim")
        for r in recs:
            t.add_row(r.tx_id, _format_bytes(r.backup_size_bytes), escape(r.original_path), r.engine_used)
        console.print(t)
        return 0

    ok, msg, rec = perform_undo(args.tx_id)
    if args.json:
        print(json.dumps({"success": ok, "message": msg, "record": asdict(rec) if rec else None}, indent=2))
        return 0 if ok else 1

    if ok:
        console.print(f"[green]Restored:[/green] {msg}")
        return 0
    console.print(f"[red]Undo failed:[/red] {msg}")
    return 1


def _cmd_gui(args: argparse.Namespace) -> int:
    from .gui import launch_gui

    launch_gui(getattr(args, "paths", None) or None)
    return 0


def _cmd_extract(args: argparse.Namespace) -> int:
    from .engine import extract_subtitle_stream

    ok, msg, out_path = extract_subtitle_stream(args.video, args.stream, args.output)
    if args.json:
        print(json.dumps({"success": ok, "message": msg, "output_path": out_path}, indent=2))
        return 0 if ok else 1
    console = _make_console(args.no_color)
    from rich.markup import escape

    console.print(f"[{'green' if ok else 'red'}]{escape(msg)}[/{'green' if ok else 'red'}]")
    return 0 if ok else 1


def _cmd_merge(args: argparse.Namespace) -> int:
    from .subtitle import merge_dual_subtitles

    out_path, count = merge_dual_subtitles(args.bottom, args.top, args.output)
    if args.json:
        print(json.dumps({"output_path": out_path, "events": count}, indent=2))
        return 0
    console = _make_console(args.no_color)
    from rich.markup import escape

    console.print(f"[green]Merged {count} cues[/green] -> {escape(out_path)}")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    from .watcher import FolderWatcher

    p_opts = _resolve_profile_opts(args)
    console = _make_console(args.no_color)
    stop_event = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())

    def _on_ev(ev: dict) -> None:
        if args.json:
            print(json.dumps(ev, ensure_ascii=False), flush=True)
            return
        if getattr(args, "tray", False):
            return
        color = {"success": "green", "error": "red", "paired": "cyan"}.get(ev["kind"], "dim")
        from rich.markup import escape

        console.print(f"[dim]{ev['timestamp']}[/dim] [{color}]{escape(ev['message'])}[/{color}]")

    watcher = FolderWatcher(
        directory=args.directory,
        recursive=args.recursive,
        mode=p_opts["mode"],
        existing_subs=p_opts["existing_subs"],
        replace_original=p_opts["replace"],
        default_lang=p_opts["lang"],
        auto_sync=p_opts["auto_sync"],
        clean_sdh=p_opts["clean_sdh"],
        clean_ads=p_opts["clean_ads"],
        strip_html_tags=p_opts["strip_tags"],
        sub_cleanup=p_opts["sub_cleanup"],
        on_event=_on_ev,
    )
    if getattr(args, "tray", False):
        from .tray import run_watch_tray

        run_watch_tray(watcher)
        return 0

    watcher.start()
    try:
        while watcher.is_running and not stop_event.is_set():
            stop_event.wait(timeout=0.5)
    finally:
        watcher.stop()
    return 0


def _cmd_quick(args: argparse.Namespace) -> int:
    from .engine import embed_subtitles, find_companion_subtitles
    from .models import EmbedOptions, HardsubStyle, SubtitleInput
    from .tray import notify_desktop

    target = Path(args.target)
    if not target.exists():
        print(f"Error: Target not found: {target}", file=sys.stderr)
        return 2

    if target.is_dir():
        args.directory = str(target)
        if not hasattr(args, "recursive"):
            args.recursive = False
        rc = _cmd_batch(args)
        if getattr(args, "notify", False):
            notify_desktop("SRT Embedder Quick-Embed", f"Finished batch folder: {target.name}" if rc == 0 else f"Batch finished with warnings in {target.name}")
        return rc

    p_opts = _resolve_profile_opts(args)
    companions = find_companion_subtitles(str(target))
    console = _make_console(args.no_color)
    if not companions:
        msg = f"No matching subtitle files found near {target.name}."
        if args.json:
            print(json.dumps({"success": False, "error": msg}))
        else:
            console.print(f"[yellow]{msg}[/yellow]")
        if getattr(args, "notify", False):
            notify_desktop("SRT Embedder", msg)
        return 1

    hs = p_opts["hardsub_style"]
    subs = [
        SubtitleInput(
            path=s,
            lang_code=p_opts["lang"],
            is_default=(idx == 0),
            strip_html_tags=p_opts["strip_tags"],
            clean_sdh=p_opts["clean_sdh"],
            clean_ads=p_opts["clean_ads"],
        )
        for idx, s in enumerate(companions)
    ]
    res = embed_subtitles(
        EmbedOptions(
            video_path=str(target),
            subtitles=subs,
            mode=p_opts["mode"],
            existing_subs=p_opts["existing_subs"],
            fallback_to_mkv=p_opts["fallback_to_mkv"],
            replace_original=p_opts["replace"],
            auto_sync=p_opts["auto_sync"],
            auto_sync_threshold_ms=p_opts["auto_sync_threshold_ms"],
            crf=p_opts["crf"],
            preset=p_opts["preset"],
            hardsub_style=HardsubStyle(
                font_name=str(hs.get("font_name", "Arial")),
                font_size=int(hs.get("font_size", 24)),
                primary_color=str(hs.get("primary_color", "#FFFFFF")),
                outline_color=str(hs.get("outline_color", "#000000")),
                outline_width=int(hs.get("outline_width", 2)),
                margin_v=int(hs.get("margin_v", 28)),
                encoder=str(hs.get("encoder", "auto")),  # type: ignore[arg-type]
            ),
            dry_run=args.dry_run,
        )
    )
    if args.json:
        print(json.dumps(asdict(res), indent=2, ensure_ascii=False))
    elif res.success:
        from rich.markup import escape

        console.print(f"[green]Embedded {len(companions)} sub(s)[/green] -> {escape(Path(res.output_path).name)} [dim]({escape(res.engine_used)})[/dim]")
    else:
        from rich.markup import escape

        console.print(f"[red]Failed:[/red] {escape(res.error)}")

    if getattr(args, "notify", False):
        if res.success:
            notify_desktop("SRT Embedder", f"Embedded {len(companions)} subtitle(s) into {Path(res.output_path).name}")
        else:
            notify_desktop("SRT Embedder Failed", res.error[:160])
    return 0 if res.success else 1


def _cmd_audit(args: argparse.Namespace) -> int:
    from .engine import audit_library, embed_subtitles
    from .models import EmbedOptions, SubtitleInput

    report = audit_library(args.directory, target_lang=args.lang, recursive=not args.no_recursive)
    console = _make_console(args.no_color)

    if getattr(args, "embed_pairable", False):
        p_opts = _resolve_profile_opts(args)
        embedded_results = []
        for item in report.items:
            if item.status != "pairable":
                continue
            subs = [
                SubtitleInput(
                    path=s,
                    lang_code=args.lang if args.lang != "auto" else p_opts["lang"],
                    is_default=(idx == 0),
                    strip_html_tags=p_opts["strip_tags"],
                    clean_sdh=p_opts["clean_sdh"],
                    clean_ads=p_opts["clean_ads"],
                )
                for idx, s in enumerate(item.companion_subs)
            ]
            res = embed_subtitles(
                EmbedOptions(
                    video_path=item.video_path,
                    subtitles=subs,
                    mode=p_opts["mode"],
                    existing_subs=p_opts["existing_subs"],
                    replace_original=p_opts["replace"],
                    auto_sync=p_opts["auto_sync"],
                    dry_run=args.dry_run,
                )
            )
            embedded_results.append(res)
            if not args.json:
                console.print(
                    f"  [{'green' if res.success else 'red'}]{'OK' if res.success else 'FAIL'}[/] "
                    f"{item.filename} -> {Path(res.output_path).name if res.output_path else res.error}"
                )
        if args.json:
            print(json.dumps({"audit": asdict(report), "embedded": [asdict(r) for r in embedded_results]}, indent=2, ensure_ascii=False))
        return 0 if all(r.success for r in embedded_results) else 1

    if args.json:
        print(json.dumps(asdict(report), indent=2, ensure_ascii=False))
        return 0

    from rich.markup import escape
    from rich.table import Table

    console.print(
        f"[bold]Library Audit[/bold] [dim]({escape(report.directory)} · target={report.target_lang})[/dim]\n"
        f"  [green]Ready: {report.ready_count}[/green] · [cyan]Pairable: {report.pairable_count}[/cyan] · [yellow]Missing: {report.missing_count}[/yellow]"
    )
    table = Table(box=None, header_style="bold")
    table.add_column("Status")
    table.add_column("Video")
    table.add_column("Details", style="dim")
    for item in report.items:
        color = {"ready": "green", "pairable": "cyan", "missing": "yellow"}[item.status]
        detail = (
            f"embedded: {', '.join(item.embedded_langs) or 'none'}"
            if item.status == "ready"
            else (f"companions: {', '.join(Path(s).name for s in item.companion_subs)}" if item.companion_subs else "no external subs nearby")
        )
        table.add_row(f"[{color}]{item.status.upper()}[/{color}]", escape(item.filename), escape(detail))
    console.print(table)
    return 0


def _cmd_profile(args: argparse.Namespace) -> int:
    from .profiles import delete_custom_profile, get_profile, load_config, save_custom_profile, set_active_profile

    console = _make_console(args.no_color)
    cfg = load_config()

    if args.action == "list":
        if args.json:
            print(json.dumps({"active": cfg["active_profile"], "profiles": cfg["profiles"]}, indent=2, ensure_ascii=False))
            return 0
        for key, prof in cfg["profiles"].items():
            mark = "[green]*[/green]" if key == cfg["active_profile"] else " "
            console.print(f" {mark} [bold]{key}[/bold] — {prof.get('label', key)} [dim](mode={prof.get('mode')}, auto_sync={prof.get('auto_sync')})[/dim]")
        return 0

    if args.action == "show":
        key, prof = get_profile(args.name)
        print(json.dumps({"name": key, "profile": prof}, indent=2, ensure_ascii=False))
        return 0

    if args.action == "use":
        if not args.name:
            print("Error: Specify profile name to activate.", file=sys.stderr)
            return 2
        updated = set_active_profile(args.name)
        if args.json:
            print(json.dumps({"active": updated["active_profile"]}))
        else:
            console.print(f"[green]Active profile set to:[/green] {updated['active_profile']}")
        return 0

    if args.action == "save":
        if not args.name:
            print("Error: Specify profile name to save.", file=sys.stderr)
            return 2
        slug, updated = save_custom_profile(
            args.name,
            {
                "mode": args.mode or "auto",
                "existing_subs": args.existing or "keep",
                "default_lang": args.lang or "auto",
                "auto_sync": bool(args.auto_sync),
                "clean_sdh": bool(args.clean_sdh),
                "clean_ads": bool(args.clean_ads),
                "replace_original": bool(args.replace),
            },
        )
        if args.json:
            print(json.dumps({"saved": slug, "active": updated["active_profile"]}))
        else:
            console.print(f"[green]Saved & activated profile:[/green] {slug}")
        return 0

    if args.action == "delete":
        ok, msg, _ = delete_custom_profile(args.name or "")
        if args.json:
            print(json.dumps({"success": ok, "message": msg}))
        else:
            console.print(f"[{'green' if ok else 'red'}]{msg}[/{'green' if ok else 'red'}]")
        return 0 if ok else 1

    return 0


def _cmd_shell(args: argparse.Namespace) -> int:
    from .shell_ext import install_shell_menu, is_shell_installed, uninstall_shell_menu

    console = _make_console(args.no_color)
    if args.action == "status":
        installed = is_shell_installed()
        if args.json:
            print(json.dumps({"installed": installed}))
        else:
            console.print("Explorer menu: " + ("[green]INSTALLED[/green]" if installed else "[dim]NOT INSTALLED[/dim]"))
        return 0
    ok, msg = install_shell_menu() if args.action == "install" else uninstall_shell_menu()
    if args.json:
        print(json.dumps({"success": ok, "message": msg}))
    else:
        console.print(f"[{'green' if ok else 'red'}]{msg}[/{'green' if ok else 'red'}]")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS, help="Disable terminal colors.")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Output structured JSON.")

    parser = argparse.ArgumentParser(
        prog="srt-embedder",
        description="Embed SRT subtitles into videos with smart encoding repair, container fallbacks, sync checking, and instant undo.",
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--no-color", action="store_true", default=False, help="Disable terminal colors.")
    parser.add_argument("--json", action="store_true", default=False, help="Output structured JSON.")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # gui
    p_gui = sub.add_parser("gui", parents=[common], help="Launch the desktop studio GUI (default if no command).")
    p_gui.add_argument("paths", nargs="*", help="Optional files or folder to pre-load in GUI.")
    p_gui.set_defaults(func=_cmd_gui)

    # embed
    p_emb = sub.add_parser("embed", aliases=["e", "mux"], parents=[common], help="Embed one or more SRT files into a video (or auto-discover nearby subs).")
    p_emb.add_argument("video", help="Input video file.")
    p_emb.add_argument("srts", nargs="*", help="Subtitle files (.srt/.vtt/.ass/.zip); omit to auto-discover nearby.")
    p_emb.add_argument("-o", "--output", help="Explicit output file path.")
    p_emb.add_argument("--profile", help="Apply saved automation profile.")
    p_emb.add_argument("-l", "--lang", default=None, help="Language code or 'auto' (e.g. ara, eng, spa).")
    p_emb.add_argument("-t", "--title", default="", help="Track title for the primary subtitle.")
    p_emb.add_argument("--offset", default="0", help="Shift timestamps (e.g. +500ms, -1.5s).")
    p_emb.add_argument("--speed", default="1.0", help="Time stretch ratio for FPS drift (e.g. 25/23.976 or 1.0427).")
    p_emb.add_argument("--mode", choices=["auto", "mkvmerge", "ffmpeg", "soft-mkv", "hardsub"], default=None)
    p_emb.add_argument("--existing", choices=["keep", "replace-same-lang", "replace-all"], default=None)
    p_emb.add_argument("--no-default", action="store_true", help="Do not mark primary subtitle as default.")
    p_emb.add_argument("--forced", action="store_true", help="Mark primary subtitle as forced.")
    p_emb.add_argument("--strip-tags", action="store_true", default=None, help="Strip HTML/ASS styling tags from SRT.")
    p_emb.add_argument("--clean-sdh", action="store_true", default=None, help="Strip [SDH] descriptions, speaker labels, and music notes.")
    p_emb.add_argument("--clean-ads", action="store_true", default=None, help="Strip translator watermark / URL promo cues.")
    p_emb.add_argument("--fix-rtl", action="store_true", default=None, help="Normalize RTL Arabic/Hebrew BiDi punctuation.")
    p_emb.add_argument("--auto-sync", action="store_true", default=None, help="Auto-detect and fix offset/drift via embedded track or audio VAD.")
    p_emb.add_argument("--naming", default=None, help="Output naming template: default, plex, clean, or custom ({stem},{lang},{langs}).")
    p_emb.add_argument("--prefer-sub", default=None, help="Auto-set this language as default subtitle when primary audio is another language.")
    p_emb.add_argument("--no-attach-fonts", action="store_true", help="Skip attaching .ttf/.otf fonts alongside .ass subtitles in MKV.")
    p_emb.add_argument("--hw-encoder", choices=["auto", "libx264", "h264_nvenc", "hevc_nvenc", "h264_qsv", "h264_amf"], default=None)
    p_emb.add_argument("--no-mkv-fallback", action="store_true", help="Disable automatic fallback to .mkv container.")
    p_emb.add_argument("--replace", action="store_true", default=None, help="Replace original video in-place (creates instant undo backup).")
    p_emb.add_argument("--no-backup", action="store_true", help="Skip creating undo backup when --replace is used.")
    p_emb.add_argument("--dry-run", action="store_true", help="Preview command and fallbacks without modifying files.")
    p_emb.set_defaults(func=_cmd_embed)

    # batch
    p_bat = sub.add_parser("batch", aliases=["b"], parents=[common], help="Auto-pair and embed all videos + subtitles in a folder.")
    p_bat.add_argument("directory", nargs="?", default=".", help="Folder to scan (default: current directory).")
    p_bat.add_argument("-r", "--recursive", action="store_true", help="Scan subfolders recursively.")
    p_bat.add_argument("--profile", help="Apply saved automation profile.")
    p_bat.add_argument("-l", "--lang", default=None)
    p_bat.add_argument("--mode", choices=["auto", "mkvmerge", "ffmpeg", "soft-mkv", "hardsub"], default=None)
    p_bat.add_argument("--existing", choices=["keep", "replace-same-lang", "replace-all"], default=None)
    p_bat.add_argument("--strip-tags", action="store_true", default=None)
    p_bat.add_argument("--clean-sdh", action="store_true", default=None)
    p_bat.add_argument("--clean-ads", action="store_true", default=None)
    p_bat.add_argument("--auto-sync", action="store_true", default=None)
    p_bat.add_argument("--archive-subs", action="store_true", help="Move consumed subs into _archived_subs/ after mux.")
    p_bat.add_argument("--trash-subs", action="store_true", help="Send consumed subs to Recycle Bin after mux.")
    p_bat.add_argument("--replace", action="store_true", default=None)
    p_bat.add_argument("--dry-run", action="store_true")
    p_bat.set_defaults(func=_cmd_batch)

    # inspect
    p_ins = sub.add_parser("inspect", aliases=["i", "probe"], parents=[common], help="Inspect streams in a video or health-check a subtitle file.")
    p_ins.add_argument("path", help="Video or subtitle (.srt/.vtt/.ass/.ssa) file path.")
    p_ins.set_defaults(func=_cmd_inspect)

    # sync-check
    p_syn = sub.add_parser("sync-check", aliases=["sync"], parents=[common], help="Check SRT timing against embedded reference subtitles.")
    p_syn.add_argument("video", help="Video file with an embedded subtitle track.")
    p_syn.add_argument("srt", help="External .srt file to check.")
    p_syn.set_defaults(func=_cmd_sync_check)

    # undo
    p_und = sub.add_parser("undo", parents=[common], help="Restore or manage backups from in-place replacements.")
    p_und.add_argument("tx_id", nargs="?", help="Optional transaction ID (defaults to latest).")
    p_und.add_argument("--list", action="store_true", help="List available undo backups.")
    p_und.add_argument("--delete", metavar="TX_ID", help="Delete one backup permanently to free disk space.")
    p_und.add_argument("--purge", action="store_true", help="Delete all active backups to free disk space.")
    p_und.set_defaults(func=_cmd_undo)

    # doctor
    p_doc = sub.add_parser("doctor", parents=[common], help="Verify mkvmerge, ffmpeg, and ffprobe availability.")
    p_doc.add_argument("--install", nargs="?", const="all", choices=["all", "ffmpeg", "mkvmerge"], help="Download/install missing engines.")
    p_doc.add_argument("--check-update", action="store_true", help="Check GitHub Releases for a newer SRT Embedder version.")
    p_doc.set_defaults(func=_cmd_doctor)

    # extract
    p_ext = sub.add_parser("extract", aliases=["x"], parents=[common], help="Extract an embedded subtitle track to .srt or .ass.")
    p_ext.add_argument("video", help="Input video file.")
    p_ext.add_argument("stream", type=int, help="Subtitle stream index (from 'inspect').")
    p_ext.add_argument("-o", "--output", help="Optional output file path.")
    p_ext.set_defaults(func=_cmd_extract)

    # merge
    p_mrg = sub.add_parser("merge", parents=[common], help="Merge two subtitle files into a bilingual styled .ass file.")
    p_mrg.add_argument("bottom", help="Primary bottom subtitle file.")
    p_mrg.add_argument("top", help="Secondary top subtitle file.")
    p_mrg.add_argument("-o", "--output", help="Output .ass or .srt path.")
    p_mrg.set_defaults(func=_cmd_merge)

    # watch
    p_wat = sub.add_parser("watch", aliases=["w"], parents=[common], help="Watch a folder and auto-embed matching video + subtitle pairs.")
    p_wat.add_argument("directory", nargs="?", default=".", help="Folder to watch.")
    p_wat.add_argument("-r", "--recursive", action="store_true", help="Watch subfolders recursively.")
    p_wat.add_argument("--profile", help="Apply saved automation profile.")
    p_wat.add_argument("-l", "--lang", default=None)
    p_wat.add_argument("--mode", choices=["auto", "mkvmerge", "ffmpeg", "soft-mkv", "hardsub"], default=None)
    p_wat.add_argument("--existing", choices=["keep", "replace-same-lang", "replace-all"], default=None)
    p_wat.add_argument("--clean-sdh", action="store_true", default=None)
    p_wat.add_argument("--clean-ads", action="store_true", default=None)
    p_wat.add_argument("--auto-sync", action="store_true", default=None)
    p_wat.add_argument("--archive-subs", action="store_true", help="Move consumed subs into _archived_subs/ after mux.")
    p_wat.add_argument("--trash-subs", action="store_true", help="Send consumed subs to Recycle Bin after mux.")
    p_wat.add_argument("--tray", action="store_true", help="Run silently in the Windows system tray.")
    p_wat.add_argument("--replace", action="store_true", default=None, help="Replace originals in-place (with undo backup).")
    p_wat.set_defaults(func=_cmd_watch)

    # quick (headless Explorer/automation runner)
    p_qck = sub.add_parser("quick", aliases=["q", "auto"], parents=[common], help="Headless 1-step auto-pair & embed for a video or folder.")
    p_qck.add_argument("target", help="Video file or folder to auto-embed.")
    p_qck.add_argument("-r", "--recursive", action="store_true", help="Scan subfolders when target is a folder.")
    p_qck.add_argument("--profile", help="Profile to apply (defaults to active profile).")
    p_qck.add_argument("--notify", action="store_true", help="Show a Windows desktop notification on completion.")
    p_qck.add_argument("--dry-run", action="store_true")
    p_qck.set_defaults(func=_cmd_quick)

    # audit (library scanner)
    p_aud = sub.add_parser("audit", parents=[common], help="Scan a library folder for videos ready, pairable, or missing target subtitles.")
    p_aud.add_argument("directory", nargs="?", default=".", help="Library directory to scan.")
    p_aud.add_argument("-l", "--lang", default="ara", help="Target subtitle language (e.g. ara, eng, auto).")
    p_aud.add_argument("--no-recursive", action="store_true", help="Only scan top-level directory.")
    p_aud.add_argument("--embed-pairable", action="store_true", help="Immediately embed all pairable videos found.")
    p_aud.add_argument("--profile", help="Profile to use with --embed-pairable.")
    p_aud.add_argument("--dry-run", action="store_true")
    p_aud.set_defaults(func=_cmd_audit)

    # profile
    p_pro = sub.add_parser("profile", parents=[common], help="Manage persistent muxing/automation profiles.")
    p_pro.add_argument("action", choices=["list", "show", "use", "save", "delete"], help="Profile operation.")
    p_pro.add_argument("name", nargs="?", help="Profile name.")
    p_pro.add_argument("--mode", choices=["auto", "mkvmerge", "ffmpeg", "soft-mkv", "hardsub"])
    p_pro.add_argument("--existing", choices=["keep", "replace-same-lang", "replace-all"])
    p_pro.add_argument("-l", "--lang")
    p_pro.add_argument("--auto-sync", action="store_true")
    p_pro.add_argument("--clean-sdh", action="store_true")
    p_pro.add_argument("--clean-ads", action="store_true")
    p_pro.add_argument("--replace", action="store_true")
    p_pro.set_defaults(func=_cmd_profile)

    # shell
    p_sh = sub.add_parser("shell", parents=[common], help="Manage Windows Explorer right-click context menu.")
    p_sh.add_argument("action", choices=["install", "uninstall", "status"], help="Context menu action.")
    p_sh.set_defaults(func=_cmd_shell)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    # Ensure Windows frozen/redirected consoles never crash on Arabic/Unicode output
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    raw_args = list(sys.argv[1:] if argv is None else argv)

    # No arguments -> launch GUI immediately
    if not raw_args:
        from .gui import launch_gui

        launch_gui()
        return 0

    known_cmds = {
        "gui", "embed", "e", "mux", "batch", "b", "inspect", "i", "probe",
        "sync-check", "sync", "undo", "doctor", "extract", "x", "merge",
        "watch", "w", "quick", "q", "auto", "audit", "profile", "shell",
        "-h", "--help", "-V", "--version",
    }

    # If launched directly with file/folder paths (e.g. Explorer right-click or drag onto exe), open GUI preloaded
    first_pos = next((a for a in raw_args if not a.startswith("-")), None)
    if first_pos and first_pos not in known_cmds and Path(first_pos).exists():
        from .gui import launch_gui

        launch_gui([a for a in raw_args if not a.startswith("-") and Path(a).exists()])
        return 0

    # Postel's Law: suggest closest command on typo before argparse exits
    if first_pos and first_pos not in known_cmds and not Path(first_pos).exists():
        close = difflib.get_close_matches(
            first_pos,
            ["gui", "embed", "batch", "inspect", "sync-check", "undo", "doctor", "extract", "merge", "watch", "quick", "audit", "profile", "shell"],
            n=1,
            cutoff=0.55,
        )
        if close:
            print(f"Unknown command '{first_pos}'. Did you mean '{close[0]}'?", file=sys.stderr)
            return 2

    parser = build_parser()
    args = parser.parse_args(raw_args)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)
