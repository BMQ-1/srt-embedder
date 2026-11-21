"""Integration and unit tests for SRT Embedder."""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import pytest

from srt_embedder.engine import (
    audit_library,
    check_sync,
    detect_installed_fonts,
    embed_subtitles,
    extract_subtitle_stream,
    find_batch_pairs,
    find_companion_subtitles,
    probe_video,
    scan_batch_directory,
)
from srt_embedder.models import EmbedOptions, HardsubStyle, SubtitleInput
from srt_embedder.profiles import get_profile, load_config, save_custom_profile, set_active_profile
from srt_embedder.subtitle import (
    compare_srt_with_reference,
    extract_subtitles_from_zip,
    get_preview_cues,
    inspect_srt,
    merge_dual_subtitles,
    ms_to_srt_time,
    parse_offset_ms,
    parse_speed_factor,
)
from srt_embedder.tools import detect_hw_encoders, discover_tools
from srt_embedder.undo import delete_backup, list_undo_records, perform_undo, purge_all_backups
from srt_embedder.watcher import FolderWatcher


def _make_sample_video(out_path: Path) -> None:
    tools = discover_tools()
    ffmpeg = tools["ffmpeg"].path
    assert ffmpeg, "ffmpeg is required for integration tests"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:d=4",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            "4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(out_path),
        ],
        check=True,
    )


def test_postel_offset_parser() -> None:
    assert parse_offset_ms("+500ms") == 500
    assert parse_offset_ms("-1.5s") == -1500
    assert parse_offset_ms("1,250") == 1250
    assert parse_offset_ms("") == 0


def test_cp1256_arabic_and_malformed_srt_repair(tmp_path: Path) -> None:
    # Write malformed SRT in Windows-1256 Arabic with dot milliseconds and missing cue index
    bad_srt = (
        "00:00:00.500 --> 00:00:02.000\n"
        "مرحبا بالعالم\n\n"
        "99\n"
        "00:00:03,000 --> 00:00:02,000\n"
        "السطر الثاني\n"
    )
    srt_path = tmp_path / "sample.ar.srt"
    srt_path.write_bytes(bad_srt.encode("cp1256"))

    rep = inspect_srt(str(srt_path), offset_ms=500)
    assert rep.detected_lang_code == "ara"
    assert rep.cue_count == 2
    assert rep.repaired_cues >= 2
    assert "00:00:01,000 --> 00:00:02,500" in rep.normalized_content
    assert "مرحبا بالعالم" in rep.normalized_content


def test_embed_mkv_and_ffmpeg_fallback_and_undo(tmp_path: Path) -> None:
    mkv = tmp_path / "clip.mkv"
    _make_sample_video(mkv)

    srt = tmp_path / "clip.ar.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:02,500\n<font color=\"#fff\">تجربة</font>\n", encoding="utf-8-sig")

    # 1. Primary auto (mkvmerge) side-by-side
    res1 = embed_subtitles(
        EmbedOptions(
            video_path=str(mkv),
            subtitles=[SubtitleInput(path=str(srt), lang_code="auto")],
        )
    )
    assert res1.success, res1.error
    assert res1.subtitle_streams_after == 1
    assert Path(res1.output_path).exists()

    # 2. Explicit ffmpeg mode + replace_original + undo
    res2 = embed_subtitles(
        EmbedOptions(
            video_path=str(mkv),
            subtitles=[SubtitleInput(path=str(srt), lang_code="ara", title="Arabic Test")],
            mode="ffmpeg",
            replace_original=True,
            create_backup=True,
        )
    )
    assert res2.success, res2.error
    assert res2.undo_tx_id is not None
    assert len(probe_video(str(mkv)).subtitle_streams) == 1

    # Undo should restore original 0-subtitle clip.mkv
    ok, _msg, _rec = perform_undo(res2.undo_tx_id)
    assert ok
    assert len(probe_video(str(mkv)).subtitle_streams) == 0


def test_mp4_mov_text_and_avi_mkv_fallback(tmp_path: Path) -> None:
    mp4 = tmp_path / "clip.mp4"
    _make_sample_video(mp4)

    srt = tmp_path / "clip.eng.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:02,500\nHello world\n", encoding="utf-8")

    res_mp4 = embed_subtitles(
        EmbedOptions(
            video_path=str(mp4),
            subtitles=[SubtitleInput(path=str(srt))],
        )
    )
    assert res_mp4.success, res_mp4.error
    assert Path(res_mp4.output_path).suffix.lower() == ".mp4"
    assert res_mp4.subtitle_streams_after == 1

    # AVI should automatically fall back to lossless .mkv
    avi = tmp_path / "clip.avi"
    subprocess.run(
        [discover_tools()["ffmpeg"].path, "-y", "-v", "error", "-i", str(mp4), "-c", "copy", str(avi)],
        check=True,
    )
    res_avi = embed_subtitles(
        EmbedOptions(
            video_path=str(avi),
            subtitles=[SubtitleInput(path=str(srt))],
            fallback_to_mkv=True,
        )
    )
    assert res_avi.success, res_avi.error
    assert Path(res_avi.output_path).suffix.lower() == ".mkv"
    assert len(res_avi.fallbacks_triggered) >= 1


def test_batch_pairing(tmp_path: Path) -> None:
    (tmp_path / "Movie.2025.1080p.mkv").write_bytes(b"0")
    (tmp_path / "Movie.2025.ar.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")
    pairs = find_batch_pairs(str(tmp_path))
    assert len(pairs) == 1
    assert len(pairs[0].srt_paths) == 1


def test_vtt_ass_zip_conversion_and_preview(tmp_path: Path) -> None:
    vtt = tmp_path / "demo.en.vtt"
    vtt.write_text("WEBVTT\n\n00:01.000 --> 00:03.500 align:start\nHello WebVTT\n", encoding="utf-8")
    rep_vtt = inspect_srt(str(vtt))
    assert rep_vtt.source_format == "vtt"
    assert rep_vtt.cue_count == 1
    assert len(rep_vtt.sample_cues) == 1
    assert "00:00:01,000 --> 00:00:03,500" in rep_vtt.normalized_content

    ass = tmp_path / "demo.ar.ass"
    ass.write_text("[Events]\nDialogue: 0,0:00:02.00,0:00:04.50,Default,,0,0,0,,{\\an8}مرحبا\n", encoding="utf-8")
    rep_ass = inspect_srt(str(ass))
    assert rep_ass.source_format == "ass"
    assert rep_ass.detected_lang_code == "ara"
    assert "مرحبا" in rep_ass.normalized_content

    zpath = tmp_path / "subs.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("nested/movie.srt", "1\n00:00:01,000 --> 00:00:02,000\nZip cue\n")
        zf.writestr("../../evil.srt", "bad")
    extracted = extract_subtitles_from_zip(str(zpath))
    assert len(extracted) == 2  # safe basename extraction inside cache_root
    assert all(Path(p).exists() for p in extracted)


def test_replace_same_lang_and_backup_delete(tmp_path: Path) -> None:
    mkv = tmp_path / "movie.mkv"
    _make_sample_video(mkv)
    srt_ar1 = tmp_path / "movie.ara.srt"
    srt_ar1.write_text("1\n00:00:00,500 --> 00:00:01,500\nاول\n", encoding="utf-8")
    srt_ar2 = tmp_path / "movie.ar2.srt"
    srt_ar2.write_text("1\n00:00:00,500 --> 00:00:01,500\nثاني\n", encoding="utf-8")

    # Embed first Arabic track in-place
    r1 = embed_subtitles(EmbedOptions(video_path=str(mkv), subtitles=[SubtitleInput(path=str(srt_ar1), lang_code="ara")], replace_original=True))
    assert r1.success, r1.error
    assert len(probe_video(str(mkv)).subtitle_streams) == 1

    # Replace same language -> should still have 1 subtitle stream, not 2
    r2 = embed_subtitles(EmbedOptions(video_path=str(mkv), subtitles=[SubtitleInput(path=str(srt_ar2), lang_code="ara")], existing_subs="replace-same-lang", replace_original=True))
    assert r2.success, r2.error
    assert len(probe_video(str(mkv)).subtitle_streams) == 1

    # Clean up test backups so user's undo history stays spotless
    for tx in [r1.undo_tx_id, r2.undo_tx_id]:
        if tx:
            ok, _ = delete_backup(tx)
            assert ok


def test_recursive_batch_scan_and_unmatched(tmp_path: Path) -> None:
    ep1 = tmp_path / "Ep1"
    ep1.mkdir()
    (ep1 / "Show.S01E01.mkv").write_bytes(b"0")
    (ep1 / "Show.S01E01.ar.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")
    (tmp_path / "Orphan.mkv").write_bytes(b"0")
    pairs, un_v, un_s = scan_batch_directory(str(tmp_path), recursive=True)
    assert len(pairs) == 1
    assert len(un_v) == 1
    assert len(un_s) == 0


def test_clean_sdh_and_ads(tmp_path: Path) -> None:
    srt = tmp_path / "dirty.en.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nDownloaded from OpenSubtitles.org\n\n"
        "2\n00:00:03,000 --> 00:00:04,500\n[MUSIC PLAYING]\n\n"
        "3\n00:00:05,000 --> 00:00:07,000\nDOC LAMBERT: (sighs) Hello Ben.\n",
        encoding="utf-8",
    )
    rep = inspect_srt(str(srt), clean_sdh=True, clean_ads=True)
    assert rep.cue_count == 1
    assert "Hello Ben." in rep.normalized_content
    assert "OpenSubtitles" not in rep.normalized_content
    assert "MUSIC" not in rep.normalized_content
    assert "DOC LAMBERT" not in rep.normalized_content


def test_bilingual_merge_extract_and_hardsub_style(tmp_path: Path) -> None:
    mkv = tmp_path / "clip.mkv"
    _make_sample_video(mkv)
    ar_srt = tmp_path / "clip.ar.srt"
    en_srt = tmp_path / "clip.en.srt"
    ar_srt.write_text("1\n00:00:00,400 --> 00:00:01,800\nمرحبا\n", encoding="utf-8")
    en_srt.write_text("1\n00:00:00,400 --> 00:00:01,800\nHello\n", encoding="utf-8")

    # 1. Merge bilingual .ass
    merged_path, count = merge_dual_subtitles(str(ar_srt), str(en_srt))
    assert Path(merged_path).exists()
    assert count == 2
    assert "Style: Bottom" in Path(merged_path).read_text(encoding="utf-8")

    # 2. Embed bilingual .ass into mkv, then extract it back
    res = embed_subtitles(EmbedOptions(video_path=str(mkv), subtitles=[SubtitleInput(path=merged_path, lang_code="ara")]))
    assert res.success, res.error
    info = probe_video(res.output_path)
    assert len(info.subtitle_streams) == 1
    ok, _msg, ext_path = extract_subtitle_stream(res.output_path, info.subtitle_streams[0].index)
    assert ok and Path(ext_path).exists()

    # 3. GPU encoder detection + Hardsub burn-in with custom style
    encs = detect_hw_encoders()
    assert "libx264" in encs
    hard_out = tmp_path / "burned.mp4"
    res_hard = embed_subtitles(
        EmbedOptions(
            video_path=str(mkv),
            subtitles=[SubtitleInput(path=str(en_srt))],
            output_path=str(hard_out),
            mode="hardsub",
            hardsub_style=HardsubStyle(font_size=26, primary_color="#F59E0B", encoder="libx264"),
        )
    )
    assert res_hard.success, res_hard.error
    assert hard_out.exists()


def test_watch_daemon_scan_once(tmp_path: Path) -> None:
    mkv = tmp_path / "Show.S01E01.mkv"
    _make_sample_video(mkv)
    sub = tmp_path / "Show.S01E01.en.srt"
    sub.write_text("1\n00:00:00,500 --> 00:00:01,500\nWatched\n", encoding="utf-8")

    manifest = tmp_path / "manifest.json"
    events: list[dict] = []
    watcher = FolderWatcher(
        str(tmp_path),
        sub_cleanup="archive",
        manifest_path=manifest,
        on_event=events.append,
    )
    watcher.scan_once()
    assert any(ev["kind"] == "success" for ev in events)
    assert (tmp_path / "Show.S01E01 [subtitled].mkv").exists()
    assert not sub.exists()
    assert (tmp_path / "_archived_subs" / "Show.S01E01.en.srt").exists()
    assert manifest.exists()

    # Restarted watcher with same manifest should not re-process
    events2: list[dict] = []
    watcher2 = FolderWatcher(str(tmp_path), manifest_path=manifest, on_event=events2.append)
    watcher2.scan_once()
    assert not any(ev["kind"] == "paired" for ev in events2)


def test_companion_discovery_and_episode_pairing(tmp_path: Path) -> None:
    # 1. Companion discovery in Subs/<VideoStem>/ and Subs/
    vid = tmp_path / "Primate.2025.1080p.mkv"
    vid.write_bytes(b"0")
    subs_dir = tmp_path / "Subs" / "Primate.2025.1080p"
    subs_dir.mkdir(parents=True)
    ar_sub = subs_dir / "Arabic.srt"
    ar_sub.write_text("1\n00:00:01,000 --> 00:00:02,000\nمرحبا\n", encoding="utf-8")

    companions = find_companion_subtitles(str(vid))
    assert str(ar_sub) in companions

    # 2. Season/Episode-aware matching across differing release names
    ep_dir = tmp_path / "season"
    ep_dir.mkdir()
    (ep_dir / "Series.S02E01.1080p.mkv").write_bytes(b"0")
    (ep_dir / "Series.S02E02.1080p.mkv").write_bytes(b"0")
    (ep_dir / "Completely.Different.Name.2x02.ar.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nEp2\n", encoding="utf-8")

    pairs, un_v, _ = scan_batch_directory(str(ep_dir))
    assert len(pairs) == 1
    assert pairs[0].confidence == "episode"
    assert Path(pairs[0].video_path).name == "Series.S02E02.1080p.mkv"
    assert len(un_v) == 1


def test_audio_vad_sync_and_auto_sync_embed(tmp_path: Path) -> None:
    tools = discover_tools()
    ffmpeg = tools["ffmpeg"].path
    assert ffmpeg

    # Create a 12s clip with 3 clear spoken-like tone bursts at 2.0s, 5.5s, 9.0s separated by silence
    mkv = tmp_path / "speech_test.mkv"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:d=12",
            "-f",
            "lavfi",
            "-i",
            "sine=f=440:d=12,volume='if(between(t,2.0,3.2)+between(t,5.5,6.8)+between(t,9.0,10.2),0.8,0)':eval=frame",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(mkv),
        ],
        check=True,
    )

    # Subtitle intentionally shifted +2.0s late (at 4.0s, 7.5s, 11.0s)
    srt = tmp_path / "speech_test.en.srt"
    srt.write_text(
        "1\n00:00:04,000 --> 00:00:05,200\nLine one\n\n"
        "2\n00:00:07,500 --> 00:00:08,800\nLine two\n\n"
        "3\n00:00:11,000 --> 00:00:12,000\nLine three\n",
        encoding="utf-8",
    )

    rep = check_sync(str(mkv), str(srt))
    assert rep.comparable, rep.reason
    assert rep.method == "audio-vad"
    assert rep.verdict == "late"
    assert -2400 <= rep.recommended_offset_ms <= -1600

    res = embed_subtitles(
        EmbedOptions(
            video_path=str(mkv),
            subtitles=[SubtitleInput(path=str(srt), lang_code="eng")],
            auto_sync=True,
        )
    )
    assert res.success, res.error
    assert any("Auto-sync applied" in w for w in res.warnings)


def test_profiles_and_library_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SRT_EMBEDDER_CONFIG", str(tmp_path / "cfg.json"))
    cfg = load_config()
    assert cfg["active_profile"] == "cinema-default"

    slug, updated = save_custom_profile("Night Mode", {"mode": "soft-mkv", "auto_sync": True})
    assert slug == "night-mode"
    assert updated["active_profile"] == "night-mode"
    set_active_profile("arabic-rtl")
    key, prof = get_profile()
    assert key == "arabic-rtl"
    assert prof["default_lang"] == "ara"

    # Library audit: 1 ready (has ara), 1 pairable (no sub inside, .srt nearby), 1 missing
    lib = tmp_path / "library"
    lib.mkdir()
    v_ready = lib / "Ready.mkv"
    _make_sample_video(v_ready)
    s_ready = lib / "tmp_ready.ara.srt"
    s_ready.write_text("1\n00:00:00,500 --> 00:00:01,500\nجاهز\n", encoding="utf-8")
    r = embed_subtitles(EmbedOptions(video_path=str(v_ready), subtitles=[SubtitleInput(path=str(s_ready), lang_code="ara")], replace_original=True, create_backup=False))
    assert r.success
    s_ready.unlink()

    v_pair = lib / "Pairable.mkv"
    _make_sample_video(v_pair)
    (lib / "Pairable.ar.srt").write_text("1\n00:00:00,500 --> 00:00:01,500\nمتاح\n", encoding="utf-8")

    v_miss = lib / "Missing.mkv"
    _make_sample_video(v_miss)

    audit = audit_library(str(lib), target_lang="ara", recursive=False)
    assert audit.total_videos == 3
    assert audit.ready_count == 1
    assert audit.pairable_count == 1
    assert audit.missing_count == 1


def test_fps_drift_correction_zip_collisions_and_hardsub_guard(tmp_path: Path) -> None:
    # 1. FPS drift fit (23.976 -> 25 fps ratio ~ 0.95904 on target timestamps)
    ref_blocks = []
    tgt_blocks = []
    ratio = 23.976 / 25.0
    for idx, sec in enumerate(range(30, 600, 60), start=1):
        ref_ms = sec * 1000
        tgt_ms = int(round((ref_ms - 500) * ratio))
        ref_blocks.append(f"{idx}\n{ms_to_srt_time(ref_ms)} --> {ms_to_srt_time(ref_ms + 1500)}\nRef {idx}")
        tgt_blocks.append(f"{idx}\n{ms_to_srt_time(tgt_ms)} --> {ms_to_srt_time(tgt_ms + 1500)}\nTgt {idx}")

    rep = compare_srt_with_reference("\n\n".join(ref_blocks), "\n\n".join(tgt_blocks))
    assert rep.comparable and rep.verdict == "drifting"
    assert abs(rep.recommended_speed - (25.0 / 23.976)) < 0.005
    assert parse_speed_factor("25/23.976") > 1.04

    srt_path = tmp_path / "drift.srt"
    srt_path.write_text("\n\n".join(tgt_blocks), encoding="utf-8")
    fixed = inspect_srt(str(srt_path), offset_ms=rep.recommended_offset_ms, speed_factor=rep.recommended_speed)
    assert abs(fixed.first_timestamp_ms - 30_000) < 300
    assert len(get_preview_cues(str(srt_path))) == len(ref_blocks)

    # 2. Collision-safe zip extraction
    zpath = tmp_path / "dups.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("Disc1/eng.srt", "1\n00:00:01,000 --> 00:00:02,000\nOne\n")
        zf.writestr("Disc2/eng.srt", "1\n00:00:01,000 --> 00:00:02,000\nTwo\n")
    extracted = extract_subtitles_from_zip(str(zpath))
    assert len(extracted) == 2 and len({Path(p).name for p in extracted}) == 2

    # 3. Hardsub multi-track warning + fonts detection
    assert len(detect_installed_fonts()) >= 1
    mkv = tmp_path / "tiny.mkv"
    _make_sample_video(mkv)
    s1 = tmp_path / "s1.srt"
    s2 = tmp_path / "s2.srt"
    s1.write_text("1\n00:00:00,500 --> 00:00:01,500\nFirst\n", encoding="utf-8")
    s2.write_text("1\n00:00:00,500 --> 00:00:01,500\nSecond\n", encoding="utf-8")
    res = embed_subtitles(
        EmbedOptions(
            video_path=str(mkv),
            subtitles=[SubtitleInput(path=str(s1)), SubtitleInput(path=str(s2))],
            mode="hardsub",
            hardsub_style=HardsubStyle(encoder="libx264"),
        )
    )
    assert res.success, res.error
    assert any("Hardsub burns only the first queued subtitle" in w for w in res.warnings)


def test_global_formats_cjk_rtl_fonts_naming_and_smart_rules(tmp_path: Path) -> None:
    # 1. TTML / DFXP conversion
    ttml = tmp_path / "netflix.ttml"
    ttml.write_text(
        '<tt xmlns="http://www.w3.org/ns/ttml"><body><div>'
        '<p begin="00:00:01.000" end="00:00:03.250">Hello<br/>World</p>'
        '<p begin="4.5s" dur="1.5s">Second line</p>'
        "</div></body></tt>",
        encoding="utf-8",
    )
    rep_ttml = inspect_srt(str(ttml))
    assert rep_ttml.source_format == "ttml"
    assert rep_ttml.cue_count == 2
    assert "00:00:01,000 --> 00:00:03,250" in rep_ttml.normalized_content
    assert "Hello\nWorld" in rep_ttml.normalized_content

    # 2. MicroDVD .sub frame-based conversion
    sub_f = tmp_path / "movie.sub"
    sub_f.write_text("{1}{1}25.000\n{25}{75}Line one|Line two\n", encoding="utf-8")
    rep_sub = inspect_srt(str(sub_f), video_fps=25.0)
    assert rep_sub.cue_count == 1
    assert "00:00:01,000 --> 00:00:03,000" in rep_sub.normalized_content

    # 3. Japanese CP932 Shift-JIS decoding + Arabic RTL BiDi punctuation fix
    jp_srt = tmp_path / "anime.ja.srt"
    jp_srt.write_bytes("1\n00:00:01,000 --> 00:00:02,000\nこんにちは世界\n".encode("cp932"))
    rep_jp = inspect_srt(str(jp_srt))
    assert rep_jp.detected_lang_code == "jpn"
    assert "こんにちは世界" in rep_jp.normalized_content

    ar_srt = tmp_path / "film.ar.srt"
    ar_srt.write_text("1\n00:00:01,000 --> 00:00:02,000\n!مرحبا يا صديقي\n", encoding="utf-8")
    rep_ar = inspect_srt(str(ar_srt), fix_rtl_bidi=True)
    assert "\u202bمرحبا يا صديقي!\u202c" in rep_ar.normalized_content

    # 4. ASS Font attachment + Plex naming + Foreign audio default rule
    mkv = tmp_path / "ForeignFilm.mkv"
    _make_sample_video(mkv)
    fonts_dir = tmp_path / "Fonts"
    fonts_dir.mkdir()
    (fonts_dir / "CustomFont.ttf").write_bytes(b"\x00\x01\x00\x00fake-ttf")
    ass = tmp_path / "ForeignFilm.ara.ass"
    ass.write_text("[Events]\nDialogue: 0,0:00:00.50,0:00:01.50,Default,,0,0,0,,مرحبا\n", encoding="utf-8")
    en = tmp_path / "ForeignFilm.eng.srt"
    en.write_text("1\n00:00:00,500 --> 00:00:01,500\nHello\n", encoding="utf-8")

    res = embed_subtitles(
        EmbedOptions(
            video_path=str(mkv),
            subtitles=[
                SubtitleInput(path=str(en), lang_code="eng", is_default=True),
                SubtitleInput(path=str(ass), lang_code="ara", is_default=False),
            ],
            output_template="plex",
            prefer_sub_lang_when_foreign_audio="ara",
            attach_fonts=True,
        )
    )
    assert res.success, res.error
    assert "ForeignFilm (eng,ara).mkv" in res.output_path
    assert any("Attaching 1 font file" in w for w in res.warnings)
    assert any("Smart rule: marked 'ara' as default subtitle" in w for w in res.warnings)
    out_info = probe_video(res.output_path)
    ara_stream = next(s for s in out_info.subtitle_streams if s.language == "ara")
    assert ara_stream.is_default is True
