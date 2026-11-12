#!/usr/bin/env python3
"""Evidence-based video QC and bounded, non-destructive technical repairs.

Original implementation. Python standard library + FFmpeg/FFprobe.
No uploads, network calls, source overwrites, content generation or blind cuts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

VERSION = "1.0.0"
STAMP = r"(\d{2,}:\d{2}:\d{2}[,.]\d{3})"


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def run(argv, timeout=1200):
    """Never shell-expand media paths. A failed detector is not a clean result."""
    try:
        p = subprocess.run([str(x) for x in argv], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return {"argv": [str(x) for x in argv], "returncode": p.returncode,
                "stdout": p.stdout, "stderr": p.stderr}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"argv": [str(x) for x in argv], "returncode": -1,
                "stdout": "", "stderr": str(exc)}


def number(value):
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


def rate(value):
    try:
        a, b = str(value).split("/")
        return float(a) / float(b)
    except (ValueError, ZeroDivisionError):
        return number(value)


def issue(items, code, severity, detail, start=None, end=None, **extra):
    value = {"code": code, "severity": severity, "detail": detail,
             "start": start, "end": end}
    value.update(extra)
    items.append(value)


def probe(path):
    result = run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path])
    if result["returncode"]:
        raise ValueError("ffprobe failed: " + result["stderr"][-1500:])
    data = json.loads(result["stdout"])
    videos = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
    audios = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    video, audio = (videos or [None])[0], (audios or [None])[0]
    duration = number(data.get("format", {}).get("duration"))
    if duration is None or duration <= 0:
        duration = max([number(s.get("duration")) or 0 for s in data.get("streams", [])] or [0])
    if duration <= 0:
        raise ValueError("No reliable positive media duration")
    return {"duration": duration, "video": video, "audio": audio,
            "fps": rate(video.get("avg_frame_rate")) if video else None,
            "format": data.get("format", {}), "streams": data.get("streams", [])}


def seconds(stamp):
    h, m, s = stamp.replace(",", ".").split(":")
    if int(m) >= 60 or float(s) >= 60:
        raise ValueError("Invalid SRT timestamp")
    return int(h) * 3600 + int(m) * 60 + float(s)


def subtitle_text(text):
    return re.sub(r"\s+", "", re.sub(r"<[^>]+>|\{[^}]+\}", "", text))


def read_srt(path):
    text = Path(path).read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    cues, errors = [], []
    blocks = re.split(r"\n[ \t]*\n", text.strip()) if text.strip() else []
    for n, block in enumerate(blocks, 1):
        lines = block.splitlines()
        if lines and re.fullmatch(r"\d+", lines[0].strip()):
            lines = lines[1:]
        match = re.fullmatch(STAMP + r"\s*-->\s*" + STAMP + r"(?:\s+.*)?", lines[0].strip()) if lines else None
        if not match:
            errors.append({"block": n, "detail": "Missing or malformed SRT timing line"})
            continue
        try:
            start, end = seconds(match[1]), seconds(match[2])
        except ValueError as exc:
            errors.append({"block": n, "detail": str(exc)})
            continue
        cues.append({"index": n, "start": start, "end": end, "text": "\n".join(lines[1:]).strip()})
    return cues, errors


def subtitle_checks(path, duration, cps_limit):
    findings = []
    try:
        cues, errors = read_srt(path)
    except (OSError, UnicodeError) as exc:
        return [], [{"code": "subtitle_unreadable", "severity": "fail", "detail": str(exc), "start": None, "end": None}]
    for err in errors:
        issue(findings, "subtitle_parse", "fail", err["detail"], block=err["block"])
    if not cues:
        issue(findings, "subtitle_no_cues", "warning", "No readable subtitle cues")
    previous = None
    for cue in cues:
        a, b, text = cue["start"], cue["end"], cue["text"]
        ident = {"cue": cue["index"]}
        if a >= b:
            issue(findings, "subtitle_duration", "fail", "Cue end must be after start", a, b, **ident)
        if a < 0 or b > duration + 0.02 or a >= duration:
            issue(findings, "subtitle_bounds", "warning", "Cue is outside encoded media duration", a, b, **ident)
        if not subtitle_text(text):
            issue(findings, "subtitle_empty", "warning", "Cue has no visible text", a, b, **ident)
        if previous and a < previous["start"]:
            issue(findings, "subtitle_order", "warning", "Cue order is not chronological", a, b, **ident)
        if previous and a < previous["end"] - 0.001:
            issue(findings, "subtitle_overlap", "warning", "Adjacent cues overlap; intended dialogue overlaps need visual judgment", a, min(b, previous["end"]), **ident)
        if b > a:
            cps = len(subtitle_text(text)) / (b - a)
            if cps > cps_limit:
                issue(findings, "subtitle_speed", "review", "Reading speed heuristic exceeds configured characters/second", a, b,
                      chars_per_second=round(cps, 2), threshold=cps_limit, **ident)
        previous = cue
    return cues, findings


def loudness(path, target):
    result = run(["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-i", path,
                  "-map", "0:a:0", "-vn", "-af",
                  f"loudnorm=I={target}:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"])
    if result["returncode"]:
        return None, result
    match = re.findall(r"\{\s*\"input_i\".*?\}", result["stderr"], re.S)
    try:
        return json.loads(match[-1]), result
    except (IndexError, json.JSONDecodeError):
        return None, result


def segments(log, start_pattern, end_pattern, duration):
    # Pair events in log order, not by numeric equality. A previous region may
    # end at exactly the timestamp where another region starts; searching all
    # end timestamps can wrongly match that previous end and emit a zero span.
    events = [(m.start(), "start", float(m[1])) for m in re.finditer(start_pattern, log)]
    events += [(m.start(), "end", float(m[1])) for m in re.finditer(end_pattern, log)]
    spans, active = [], None
    for _, kind, value in sorted(events):
        if kind == "start":
            if active is None:
                active = max(0.0, value)
        elif active is not None:
            end = min(duration, value)
            if end > active + 1e-6:
                spans.append((active, end))
            active = None
    if active is not None and duration > active + 1e-6:
        spans.append((active, duration))
    return spans


def scan(path, srt, out, target=-16.0, cps_limit=18.0, extract_frames=False):
    path, out = Path(path).resolve(), Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    report = {"schema": "video-review-autofix.scan/v1", "version": VERSION,
              "source": str(path), "source_sha256": sha256(path), "target_lufs": target,
              "findings": [], "coverage": {}, "visual_evidence": [],
              "overall_acceptance": "pending_visual_and_listening_review"}
    logs, findings, coverage = {}, report["findings"], report["coverage"]
    for check in ("decode", "black", "freeze", "silence", "loudness", "av_structure", "subtitles"):
        coverage[check] = {"state": "not_run"}
    for check in ("semantic_broll_match", "lip_sync", "ai_anatomy", "skin_tone", "subtitle_occlusion", "creative_rhythm"):
        coverage[check] = {"state": "requires_visual_or_listening_review", "automated": False}
    try:
        meta = probe(path)
        report["media"] = meta
    except (ValueError, json.JSONDecodeError) as exc:
        issue(findings, "probe_failed", "fail", str(exc))
        for item in coverage.values():
            if item.get("state") == "not_run":
                item.update(state="blocked", reason="probe failed")
        report["machine_status"] = "failed"
        dump(out / "report.json", report)
        return report
    duration, video, audio = meta["duration"], meta["video"], meta["audio"]
    if not video:
        issue(findings, "no_video", "fail", "No primary video stream")
    graph = "blackdetect=d=0.1:pic_th=0.98:pix_th=0.10,freezedetect=n=0.003:d=0.5"
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-xerror", "-v", "info", "-i", path,
           "-map", "0:v:0?", "-map", "0:a:0?"]
    if video:
        cmd += ["-vf", graph]
    cmd += ["-f", "null", "-"]
    result = run(cmd)
    logs["decode_video"] = result
    decode_error = bool(re.search(r"Error while decoding|Error submitting|Invalid data found|corrupt decoded frame|Packet corrupt", result["stderr"], re.I))
    if result["returncode"] or decode_error:
        issue(findings, "decode_failed", "fail", result["stderr"][-2000:])
        coverage["decode"] = {"state": "failed", "returncode": result["returncode"], "decode_error_logged": decode_error}
    else:
        coverage["decode"] = {"state": "checked", "scope": "full-file decode"}
    for name, key in (("black", "black"), ("freeze", "freeze")):
        coverage[name] = {"state": "checked" if video and result["returncode"] == 0 and not decode_error else "blocked",
                          "note": "Candidate regions only; static/dark creative content can be intentional"}
        if video:
            spans = segments(result["stderr"], key + r"_start:\s*([0-9.]+)", key + r"_end:\s*([0-9.]+)", duration)
            for start, end in spans:
                issue(findings, key + "_candidate", "review", "Potential defect; inspect before cutting or replacing", round(start, 3), round(end, 3))
    if audio:
        silence = run(["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-i", path, "-map", "0:a:0", "-vn",
                       "-af", "silencedetect=n=-45dB:d=0.7", "-f", "null", "-"])
        logs["silence"] = silence
        coverage["silence"] = {"state": "checked" if silence["returncode"] == 0 else "failed",
                               "threshold_db": -45, "minimum_seconds": 0.7}
        if silence["returncode"]:
            issue(findings, "silence_detector_failed", "fail", silence["stderr"][-1000:])
        else:
            for start, end in segments(silence["stderr"], r"silence_start:\s*(-?[0-9.]+)", r"silence_end:\s*([0-9.]+)", duration):
                issue(findings, "silence_candidate", "review", "Long quiet region; preserve intentional pauses", round(start, 3), round(end, 3))
        measured, lr = loudness(path, target)
        logs["loudness"] = lr
        report["loudness"] = measured
        coverage["loudness"] = {"state": "checked" if measured else "failed", "target_lufs": target,
                                "tolerance_lu": 1.0, "true_peak_ceiling_dbtp": -1.5}
        if measured:
            integrated, peak = number(measured.get("input_i")), number(measured.get("input_tp"))
            if integrated is None:
                issue(findings, "loudness_not_measurable", "review", "Silent or insufficient audio; normalization cannot reconstruct speech")
            elif abs(integrated - target) > 1:
                issue(findings, "loudness_target", "warning", "Integrated loudness differs from chosen target", measured_lufs=integrated, target_lufs=target)
            if peak is not None and peak > -1.3:
                issue(findings, "true_peak", "warning", "True peak exceeds -1.5 dBTP ceiling with 0.2 dB tolerance", measured_dbtp=peak)
        else:
            issue(findings, "loudness_detector_failed", "fail", lr["stderr"][-1000:])
    else:
        coverage["silence"] = coverage["loudness"] = {"state": "skipped", "reason": "No audio stream"}
        issue(findings, "missing_audio", "review", "No audio stream; unexpected for talking-head delivery unless intentionally silent")
    if video and audio:
        vd, ad = number(video.get("duration")), number(audio.get("duration"))
        vs, aus = number(video.get("start_time")), number(audio.get("start_time"))
        threshold = max(0.08, 2 / (meta["fps"] or 25))
        coverage["av_structure"] = {"state": "checked" if vd is not None and ad is not None else "partial",
                                    "threshold_seconds": threshold,
                                    "note": "Metadata alignment is not perceptual lip-sync verification"}
        if vd is not None and ad is not None and abs(vd - ad) > threshold:
            issue(findings, "av_duration_difference", "review", "Audio/video duration mismatch; do not blindly trim a creative tail", video_seconds=vd, audio_seconds=ad)
        if vs is not None and aus is not None and abs(vs - aus) > threshold:
            issue(findings, "av_start_difference", "review", "Audio/video start-time mismatch; inspect actual synchronization", video_start=vs, audio_start=aus)
    else:
        coverage["av_structure"] = {"state": "skipped", "reason": "Requires video and audio streams"}
    if srt:
        cues, sub_findings = subtitle_checks(srt, duration, cps_limit)
        findings.extend(sub_findings)
        coverage["subtitles"] = {"state": "checked" if not any(f["code"] in ("subtitle_parse", "subtitle_unreadable") for f in sub_findings) else "partial",
                                 "source": str(Path(srt).resolve()), "cues": len(cues), "max_chars_per_second": cps_limit,
                                 "note": "Sidecar timing/text checks only; burned-in placement and spoken accuracy need review"}
    else:
        coverage["subtitles"] = {"state": "skipped", "reason": "No SRT supplied; burned-in subtitle OCR is not implemented"}
    if extract_frames and video and result["returncode"] == 0 and not decode_error:
        times = {round(duration * i / 8, 3) for i in range(8)}
        for f in findings:
            if f.get("start") is not None:
                times.add(round(max(0, min(duration - 0.04, f["start"])), 3))
        frame_dir = out / "frames"
        frame_dir.mkdir()
        for index, at in enumerate(sorted(times)[:24], 1):
            dest = frame_dir / f"frame-{index:02d}-{at:.3f}.jpg"
            fr = run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-n", "-ss", at,
                      "-i", path, "-frames:v", "1", "-vf", "scale=960:-2", dest])
            if fr["returncode"] == 0 and dest.is_file():
                report["visual_evidence"].append({"at": at, "path": str(dest)})
            else:
                logs[f"frame_{index}"] = fr
                issue(findings, "frame_extract_failed", "warning", "Could not extract a review frame", at)
    report["machine_status"] = "failed" if any(f["severity"] == "fail" for f in findings) else "findings_present" if findings else "checks_passed"
    report["checked_items"] = [name for name, value in coverage.items() if value["state"] == "checked"]
    dump(out / "report.json", report)
    dump(out / "commands.json", logs)
    lines = ["# Video QC", "", f"Machine status: {report['machine_status']}",
             "Overall acceptance: pending real visual and listening review.", "",
             "| Code | Severity | Start | End | Detail |", "|---|---|---:|---:|---|"]
    for f in findings:
        detail = str(f["detail"]).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {f['code']} | {f['severity']} | {f.get('start')} | {f.get('end')} | {detail} |")
    lines += ["", "## Coverage", ""] + [f"- {k}: {v['state']}" for k, v in coverage.items()]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def stamp(value):
    total = max(0, round(value * 1000))
    hours, total = divmod(total, 3600000)
    minutes, total = divmod(total, 60000)
    secs, millis = divmod(total, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def fix_srt(source, destination, duration):
    cues, errors = read_srt(source)
    if errors:
        return None, [{"action": "not_fixed", "reason": "Malformed cues require explicit reconstruction; no text was discarded", "errors": errors}]
    changes, kept = [], []
    for cue in cues:
        c = dict(cue)
        if not subtitle_text(c["text"]):
            changes.append({"cue": c["index"], "action": "remove_empty_cue"})
            continue
        # A short encoding/container rounding overrun can be clamped. A long
        # overrun may be the wrong subtitle version and must not be crushed into
        # the last frame. Preserve its timing/text for explicit review.
        if c["start"] < duration and 0 < c["end"] - duration <= 0.25:
            changes.append({"cue": c["index"], "action": "clamp_short_end_overrun", "before": c["end"], "after": duration})
            c["end"] = duration
        kept.append(c)
    if not changes:
        return None, []
    chunks = [f"{i}\n{stamp(c['start'])} --> {stamp(c['end'])}\n{c['text']}" for i, c in enumerate(kept, 1)]
    Path(destination).write_text("\n\n".join(chunks) + "\n", encoding="utf-8")
    return Path(destination), changes


def repair(args):
    original = Path(args.input).resolve()
    output = Path(args.out).resolve()
    output.mkdir(parents=True, exist_ok=False)
    before_hash = sha256(original)
    original_srt_hash = sha256(args.srt) if args.srt else None
    current, current_srt = original, Path(args.srt).resolve() if args.srt else None
    first = scan(current, current_srt, output / "before", args.target_lufs, args.max_cps, args.extract_frames)
    report, rounds = first, []
    for index in range(1, args.max_rounds + 1):
        actions, entry = [], {"round": index}
        previous_srt = current_srt
        codes = {f["code"] for f in report["findings"]}
        meta = report.get("media")
        if not meta:
            break
        round_dir = output / f"round-{index}"
        round_dir.mkdir()
        if args.fix_subtitles and current_srt:
            fixed, changes = fix_srt(current_srt, round_dir / "captions.srt", meta["duration"])
            if changes:
                entry["subtitle_changes"] = changes
            if fixed:
                current_srt = fixed
                actions.append("subtitle_boundary_or_empty_cue_fix")
        normalize = args.normalize_loudness and bool(codes & {"loudness_target", "true_peak"})
        transcode = args.transcode and index == 1
        remux = args.remux and index == 1
        if normalize or transcode or remux:
            dest = round_dir / "video.mp4"
            cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-n", "-i", current,
                   "-map", "0:v:0?", "-map", "0:a:0?"]
            if transcode:
                cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p"]
                actions.append("delivery_transcode_primary_streams")
            else:
                cmd += ["-c:v", "copy"]
            if normalize:
                measured, measurement = loudness(current, args.target_lufs)
                entry["loudnorm_measurement"] = measurement
                fields = {"measured_I": "input_i", "measured_TP": "input_tp", "measured_LRA": "input_lra", "measured_thresh": "input_thresh", "offset": "target_offset"}
                if not measured or any(number(measured.get(value)) is None for value in fields.values()):
                    entry["blocked"] = "Loudness measurements not finite; no normalization was attempted"
                    current_srt = previous_srt
                    rounds.append(entry)
                    break
                filt = f"loudnorm=I={args.target_lufs}:TP=-1.5:LRA=11:linear=true:print_format=json"
                filt += "".join(f":{key}={measured[value]}" for key, value in fields.items())
                # loudnorm can emit a filter padding tail on short clips. Keep
                # the measured source audio duration, rather than trimming to
                # video duration (which could destroy an intentional audio tail).
                source_audio_duration = number(meta["audio"].get("duration")) if meta["audio"] else None
                if source_audio_duration is not None and source_audio_duration > 0:
                    filt += f",atrim=duration={source_audio_duration:.9f}"
                cmd += ["-af", filt]
                actions.append("two_pass_loudness")
            if normalize or (transcode and meta["audio"]):
                cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
            else:
                cmd += ["-c:a", "copy"]
            cmd += ["-movflags", "+faststart", dest]
            result = run(cmd)
            entry["media_command"] = result
            if result["returncode"]:
                entry["blocked"] = "Repair command failed; previous checked file retained"
                current_srt = previous_srt
                if dest.exists():
                    entry["failed_partial_output"] = str(dest)
                rounds.append(entry)
                break
            current = dest
            if remux:
                actions.append("remux_primary_streams")
        entry["actions"] = actions
        if not actions:
            entry["note"] = "No further authorized deterministic repair; semantic findings require review"
            rounds.append(entry)
            break
        report = scan(current, current_srt, round_dir / "after", args.target_lufs, args.max_cps, args.extract_frames)
        entry.update(after_report=str(round_dir / "after" / "report.json"), media=str(current), subtitles=str(current_srt) if current_srt else None)
        rounds.append(entry)
    unchanged = sha256(original) == before_hash
    if args.srt:
        unchanged = unchanged and sha256(args.srt) == original_srt_hash
    blocked = any("blocked" in item for item in rounds)
    manifest = {"schema": "video-review-autofix.repair/v1", "source": str(original), "source_sha256": before_hash,
                "source_unchanged": unchanged, "before_report": str(output / "before" / "report.json"),
                "after_report": next((r["after_report"] for r in reversed(rounds) if "after_report" in r), str(output / "before" / "report.json")),
                "output_video": str(current), "output_srt": str(current_srt) if current_srt else None,
                "rounds": rounds, "max_rounds": args.max_rounds,
                "remaining_findings": report["findings"], "coverage": report["coverage"],
                "status": "blocked" if blocked or not unchanged else "unresolved_findings" if report["findings"] else "machine_checks_passed_pending_visual_review",
                "overall_acceptance": "pending_visual_and_listening_review",
                "subtitle_repair_scope": "sidecar_only; burned-in captions require job update and rerender"}
    if args.job:
        job_path = Path(args.job).resolve()
        job = json.loads(job_path.read_text(encoding="utf-8"))
        if job.get("version") != 1:
            raise ValueError("Only job version 1 is supported")
        manifest["edit_handoff"] = make_handoff(job_path, job, report["findings"], output,
                                                 str(current_srt) if current_srt else None,
                                                 bool(current_srt and current_srt != Path(args.srt).resolve()))
    dump(output / "repair.json", manifest)
    return manifest


def make_handoff(job_path, job, findings, output, corrected_srt, srt_changed):
    """Map encoded times back to v1 edit-job regions without mutating the job."""
    fps = float(Fraction(str(job.get("fps", 30))))
    if not 1 <= fps <= 240:
        raise ValueError("Job FPS must be 1..240")
    ranges, cursor_frames = [], 0
    for index, clip in enumerate(job.get("clips", [])):
        start, end = number(clip.get("start")), number(clip.get("end"))
        if start is None or end is None or end <= start:
            continue
        frames = round((end - start) * fps)
        if frames < 1:
            continue
        ranges.append({"kind": "clip", "index": index, "id": clip.get("id"),
                       "timeline_start": cursor_frames / fps, "timeline_end": (cursor_frames + frames) / fps,
                       "timeline_start_frame": cursor_frames, "timeline_frames": frames,
                       "source": str((job_path.parent / clip["source"]).resolve()), "source_start": start})
        cursor_frames += frames
    for index, clip in enumerate(job.get("broll", [])):
        at, duration = number(clip.get("at")), number(clip.get("duration"))
        if at is None or duration is None or duration <= 0:
            continue
        at_frame, frames = round(at * fps), round(duration * fps)
        if frames < 1:
            continue
        ranges.append({"kind": "broll", "index": index, "timeline_start": at_frame / fps,
                       "timeline_end": (at_frame + frames) / fps,
                       "timeline_start_frame": at_frame, "timeline_frames": frames,
                       "source": str((job_path.parent / clip["source"]).resolve()),
                       "source_start": number(clip.get("source_start")) or 0.0})
    requests = []
    for finding in findings:
        at, end = finding.get("start"), finding.get("end")
        mapped = []
        if at is not None:
            last = end if end is not None and end > at else at + 0.001
            for region in ranges:
                if region["timeline_end"] > at and region["timeline_start"] < last:
                    item = dict(region)
                    item["suspected_source_at"] = round(region["source_start"] + max(0, at - region["timeline_start"]), 4)
                    mapped.append(item)
        code = finding["code"]
        if code.startswith("subtitle_"):
            field, suggestion = "subtitles", "Review cue timing/text against actual speech; preserve meaning and regenerate burned-in output if necessary"
        elif code in ("black_candidate", "freeze_candidate"):
            field, suggestion = "clips/broll", "Inspect the region; retain intentional stillness/black or replace only a verified defective source, then rerender"
        elif code == "silence_candidate":
            field, suggestion = "clips/audio", "Listen to the region; preserve intentional pauses, revise only verified dead-air or missing-audio segments"
        elif code in ("loudness_target", "true_peak", "loudness_not_measurable"):
            field, suggestion = "audio", "Check primary speech source and target loudness; never reconstruct missing speech by normalization"
        else:
            field, suggestion = "clips/audio", "Inspect source evidence and the earliest responsible edit; do not blindly offset or trim streams"
        requests.append({"finding": finding, "job_field": field, "mapped_regions": mapped, "suggested_action": suggestion})
    payload = {"schema": "video-review-autofix.rerender-request/v1", "job": str(job_path),
               "job_sha256": sha256(job_path), "job_was_modified": False,
               "mapping": "per-clip Python round(duration*fps), cumulative integer output frames; B-roll at/duration rounded independently",
               "corrected_sidecar_srt": corrected_srt if srt_changed else None,
               "already_changed_job_fields": [], "requests": requests,
               "next_step": "Apply evidence-backed changes to a new job copy; invoke the auto-editor, then scan the actual encoded result. No rerender was executed here."}
    path = output / "rerender-request.json"
    dump(path, payload)
    return {"request": str(path), "job": str(job_path), "job_sha256": payload["job_sha256"], "rerender_executed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("scan", "repair"):
        p = commands.add_parser(command)
        p.add_argument("input", help="Local encoded video path")
        p.add_argument("--srt", help="Optional UTF-8 SRT sidecar")
        p.add_argument("--out", required=True, help="New directory; existing destinations are never overwritten")
        p.add_argument("--target-lufs", type=float, default=-16)
        p.add_argument("--max-cps", type=float, default=18)
        p.add_argument("--extract-frames", action="store_true")
        if command == "repair":
            p.add_argument("--normalize-loudness", action="store_true")
            p.add_argument("--fix-subtitles", action="store_true")
            group = p.add_mutually_exclusive_group()
            group.add_argument("--transcode", action="store_true", help="Explicitly authorize delivery H.264/AAC re-encode; does not reconstruct missing content")
            group.add_argument("--remux", action="store_true", help="Explicitly authorize primary-stream MP4 remux")
            p.add_argument("--max-rounds", type=int, choices=(1, 2), default=2)
            p.add_argument("--job", help="Version 1 edit job to record for downstream rerender handoff; no arbitrary commands executed")
    args = parser.parse_args()
    if not -40 <= args.target_lufs <= -5 or args.max_cps <= 0:
        parser.error("target LUFS must be -40..-5 and max CPS must be positive")
    for binary in ("ffmpeg", "ffprobe"):
        if not shutil.which(binary):
            parser.exit(5, f"Missing dependency: {binary}\n")
    if not Path(args.input).is_file():
        parser.exit(3, "Input file does not exist\n")
    try:
        if args.command == "scan":
            report = scan(args.input, args.srt, args.out, args.target_lufs, args.max_cps, args.extract_frames)
            print(json.dumps({"report": str(Path(args.out).resolve() / "report.json"), "machine_status": report["machine_status"], "findings": len(report["findings"])}))
            return 10 if report["findings"] else 0
        result = repair(args)
        print(json.dumps({"report": str(Path(args.out).resolve() / "repair.json"), "status": result["status"], "output_video": result["output_video"], "findings": len(result["remaining_findings"])}))
        return 10 if result["remaining_findings"] or result["status"] == "blocked" else 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
