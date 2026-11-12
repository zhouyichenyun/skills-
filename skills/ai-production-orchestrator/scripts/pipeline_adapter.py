#!/usr/bin/env python3
"""Allowlisted local executors for the production orchestrator.

No arbitrary commands, provider API calls, uploads or source-file rewrites.
Successful execution is distinct from creative/native-software acceptance.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
from xml.etree import ElementTree as ET

ACTIONS = frozenset((
    "preflight", "transcribe", "plan_edit", "compile_story", "prepare_audio_plan", "render",
    "export_nle", "mix_audio", "mux_audio", "export_audio_nle", "qc_scan",
    "qc_repair", "build_board", "validate_board", "package_delivery",
))
SCRIPTS = {
    "editor": ("talking-head-auto-editor", "scripts/editor.py"),
    "audio": ("audio-production-director", "scripts/audio.py"),
    "audio_nle": ("audio-production-director", "scripts/audio_nle.py"),
    "qc": ("video-review-autofix", "scripts/video_qc.py"),
    "board": ("story-canvas-producer", "scripts/story_board.py"),
}
MEDIA_SUFFIXES = frozenset((".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".wav", ".mp3", ".flac", ".m4a", ".aac"))


class Blocked(ValueError):
    """A real external/input dependency is absent, rather than falsely complete."""


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"),
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON number")))


def write_json(path, data):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def local(value, project, *, directory=False, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("A nonempty local input path is required")
    if value.startswith("@"):
        raise Blocked("Unresolved upstream artifact reference: " + value)
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        raise ValueError("Inputs must be local files; provider downloads are separate recorded actions")
    path = Path(value).expanduser()
    path = (project / path if not path.is_absolute() else path).resolve()
    if directory and not path.is_dir():
        raise Blocked("Missing input directory: " + str(path))
    if not directory and not path.is_file():
        raise Blocked("Missing input file: " + str(path))
    return path


def number(value, name, minimum=None, maximum=None):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(name + " must be a finite number")
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ValueError(name + " is outside its allowed range")
    return value


def interpreter(value=None):
    if value is None:
        return sys.executable
    if not isinstance(value, str) or not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", Path(value).name, re.I):
        raise ValueError("python must identify a Python executable, not arbitrary commands or arguments")
    path = Path(value).expanduser()
    if not path.is_absolute():
        found = shutil.which(value)
        if not found:
            raise Blocked("Requested Python interpreter is unavailable")
        path = Path(found)
    if not path.is_file():
        raise Blocked("Requested Python interpreter does not exist")
    result = subprocess.run([str(path), "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=15)
    if result.returncode or not re.match(r"Python 3\.", result.stdout.strip() or result.stderr.strip()):
        raise ValueError("Requested executable is not a usable Python 3 interpreter")
    return str(path)


def process(argv, log, *, allowed=(0,), timeout=1800):
    result = subprocess.run([str(value) for value in argv], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    log.append({"argv": [str(value) for value in argv], "returncode": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr})
    if result.returncode not in allowed:
        raise ValueError(f"Local action failed ({result.returncode}): {argv[0]}\n{result.stderr[-3000:]}")
    return result


def probe(path, log):
    result = process(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_streams", "-show_format", "-of", "json", path], log)
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    if not streams:
        raise ValueError("Media has no streams: " + str(path))
    process(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe", "-i", path, "-f", "null", "-"], log)
    return {"format": data.get("format", {}), "streams": streams, "decode": "passed"}


def script(name, root):
    skill, relative = SCRIPTS[name]
    path = root / skill / relative
    if not path.is_file():
        raise Blocked("Required installed skill script is missing: " + str(path))
    return path


def artifact(path, role):
    return {"path": str(path.resolve()), "role": role}


def qc_gate(report):
    """Do not equate exit 10 with corruption, nor candidate findings with intent."""
    findings = report.get("findings", report.get("remaining_findings", []))
    failures = [finding for finding in findings if finding.get("severity") == "fail"]
    warnings = [finding for finding in findings if finding.get("severity") == "warning"]
    candidates = [finding for finding in findings if finding.get("severity") == "review"]
    state = "failed" if failures or report.get("machine_status") == "failed" else "review_required" if findings else "passed"
    return {"machine_gate": state, "technical_failures": failures, "technical_warnings": warnings,
            "review_candidates": candidates, "creative_review": "pending",
            "overall_acceptance": "pending_visual_and_listening_review",
            "coverage": report.get("coverage", {})}


def compiler_module():
    path = Path(__file__).with_name("compiler.py")
    if not path.is_file():
        raise Blocked("Story compiler is not installed")
    spec = importlib.util.spec_from_file_location("production_story_compiler", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _preflight(params, project, out, root, logs):
    destination = out / "capabilities.json"
    preflight = Path(__file__).with_name("preflight.py")
    if preflight.is_file():
        args = [sys.executable, preflight, "--skills-root", root, "--out", destination]
        # Keep provider credentials out of command lines and logs.
        if params.get("asr_model"):
            args += ["--asr-model", local(params["asr_model"], project, directory=True)]
        if params.get("python"):
            args += ["--python", interpreter(params["python"])]
        process(args, logs, allowed=(0, 10))
        result = read_json(destination)
        missing = result.get("local_gate", {}).get("missing", result.get("missing_required", result.get("missing", [])))
        metrics = {"machine_gate": "blocked" if missing else "passed", "capabilities": result,
                   "creative_review": "not_applicable", "native_ui_verified": False}
        return {"status": "blocked" if missing else "succeeded", "artifacts": [artifact(destination, "capabilities")], "metrics": metrics}
    dependencies = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe")}
    missing = [name for name, value in dependencies.items() if not value]
    required = params.get("required_skills", [item[0] for item in SCRIPTS.values()])
    if not isinstance(required, list) or any(not isinstance(value, str) or "/" in value or "\\" in value for value in required):
        raise ValueError("required_skills must be a list of skill directory names")
    for name in sorted(set(required)):
        if not (root / name / "SKILL.md").is_file():
            missing.append("skill:" + name)
    value = {"local_binaries": dependencies, "missing_required": missing, "skills_root": str(root),
             "remote_provider_authenticated": "not_checked", "provider_credits": "not_checked",
             "user_windows_connected": False, "native_ui_verified": False}
    write_json(destination, value)
    return {"status": "blocked" if missing else "succeeded", "artifacts": [artifact(destination, "capabilities")],
            "metrics": {"machine_gate": "blocked" if missing else "passed", "creative_review": "not_applicable", **value}}


def _run_action(action, params, project, out, root, logs):
    py = interpreter(params.get("python"))
    if action == "preflight":
        return _preflight(params, project, out, root, logs)
    if action == "compile_story":
        result = compiler_module().compile_story(local(params.get("board"), project), local(params.get("registry"), project),
            out, fps=params.get("fps", 25), width=params.get("width", 1920), height=params.get("height", 1080),
            name=params.get("name"))
        return result
    if action == "prepare_audio_plan":
        return prepare_audio_plan(params, project, out, root, logs)
    artifacts, metrics = [], {"machine_gate": "passed", "creative_review": "pending", "native_ui_verified": False}
    if action == "transcribe":
        source = local(params.get("video"), project)
        model = local(params.get("asr_model"), project, directory=True)
        words = out / "words.json"
        args = [py, script("editor", root), "transcribe", source, "--model", model, "--out", words]
        if params.get("language"):
            args += ["--language", str(params["language"])]
        process(args, logs)
        value = read_json(words)
        if not isinstance(value.get("words"), list):
            raise ValueError("Transcription produced no word list")
        artifacts = [artifact(words, "transcript")]
        metrics.update(words=len(value["words"]), transcription_review="machine_only",
                       machine_gate="review_required" if not value["words"] else "passed")
    elif action == "plan_edit":
        source, job = local(params.get("video"), project), out / "job.json"
        args = [py, script("editor", root), "plan", source, "--out", job]
        for key, flag in (("transcript", "--transcript"),):
            if params.get(key):
                args += [flag, local(params[key], project)]
        for key, flag in (("name", "--name"), ("fps", "--fps"), ("width", "--width"), ("height", "--height"),
                          ("max_pause", "--max-pause"), ("keep_pause", "--keep-pause")):
            if key in params:
                args += [flag, str(params[key])]
        if params.get("trim_measured_silence") is True:
            args += ["--trim-measured-silence"]
        process(args, logs)
        time_map = out / "time-map.json"
        compiler_module().build_time_map(job, time_map)
        artifacts = [artifact(job, "job"), artifact(time_map, "time_map")]
        metrics.update(planning="Measured pause draft; semantic take/word decisions still require actual review",
                       timeline=read_json(time_map))
    elif action == "render":
        job, video = local(params.get("job"), project), out / "picture.mp4"
        args = [py, script("editor", root), "render", job, "--out", video]
        if params.get("burn_subtitles") is False:
            args += ["--no-burn-subtitles"]
        if "crf" in params:
            args += ["--crf", str(int(number(params["crf"], "crf", 0, 51)))]
        process(args, logs)
        report = read_json(video.with_suffix(".render.json"))
        if report.get("decode_check") != "passed":
            raise ValueError("Render did not establish full decode success")
        artifacts = [artifact(video, "video"), artifact(video.with_suffix(".render.json"), "render_report")]
        if video.with_suffix(".srt").stat().st_size:
            artifacts.append(artifact(video.with_suffix(".srt"), "srt"))
        metrics.update(frame_count=report.get("verified_frames"), duration=report.get("duration"),
                       color_note=report.get("color_note"), audio=report.get("audio"))
    elif action == "export_nle":
        nle = out / "nle"
        args = [py, script("editor", root), "export", local(params.get("job"), project), "--out-dir", nle]
        if params.get("jianying_template"):
            args += ["--jianying-template", local(params["jianying_template"], project, directory=True)]
        process(args, logs)
        xml = nle / "resolve-premiere.xml"
        ET.parse(xml)
        artifacts = [artifact(nle, "nle_dir"), artifact(xml, "xml"), artifact(nle / "compatibility.md", "compatibility")]
        if (nle / "captions.srt").stat().st_size:
            artifacts.append(artifact(nle / "captions.srt", "srt"))
        metrics.update(native_ui_verified=False, interchange="FCP7 XML/EDL structure only; Jianying is version-sensitive best effort")
    elif action == "mix_audio":
        directory = out / "mix"
        process([py, script("audio", root), "mix", local(params.get("audio_plan"), project), "--out-dir", directory], logs)
        report = read_json(directory / "report.json")
        if report.get("status") == "failed":
            raise ValueError("Audio mixer reported failure despite process completion")
        artifacts = [artifact(directory, "mix_dir"), artifact(directory / "mix.wav", "mix"),
                     artifact(directory / "stems", "stems_dir"), artifact(directory / "report.json", "mix_report")]
        metrics.update(machine_gate="passed" if report.get("status") == "technical_checks_passed_pending_listening_review" else "review_required",
                       audio_status=report.get("status"), issues=report.get("issues", []), after=report.get("after"),
                       listening_review="pending")
    elif action == "mux_audio":
        video = out / "final.mp4"
        process([py, script("audio", root), "mux", local(params.get("video"), project), "--audio",
                 local(params.get("audio"), project), "--out", video], logs)
        report = read_json(video.with_suffix(".audio.json"))
        if not report.get("video_stream_copy") or not report.get("sources_unchanged"):
            raise ValueError("Mux did not verify preserved picture/source streams")
        artifacts = [artifact(video, "video"), artifact(video.with_suffix(".audio.json"), "mux_report")]
        metrics.update(video_frames=report.get("video_frames"), video_stream_copy=True,
                       final_audio_rescan="required", listening_review="pending")
    elif action == "export_audio_nle":
        xml = out / "with-sound.xml"
        mode = params.get("mode", "stems")
        if mode not in ("stems", "mix"):
            raise ValueError("Audio NLE mode must be stems or mix")
        process([py, script("audio_nle", root), "--timeline", local(params.get("timeline"), project),
                 "--audio-dir", local(params.get("mix_dir"), project, directory=True), "--out", xml, "--mode", mode], logs)
        ET.parse(xml)
        artifacts = [artifact(xml, "xml")]
        metrics.update(audio_mode=mode, native_ui_verified=False, original_audio_replaced=True)
    elif action in ("qc_scan", "qc_repair"):
        directory = out / "qc"
        args = [py, script("qc", root), "scan" if action == "qc_scan" else "repair",
                local(params.get("video"), project), "--out", directory]
        if params.get("srt"):
            args += ["--srt", local(params["srt"], project)]
        if params.get("extract_frames", True):
            args += ["--extract-frames"]
        if "target_lufs" in params:
            args += ["--target-lufs", str(number(params["target_lufs"], "target_lufs", -40, -5))]
        if action == "qc_repair":
            for field, flag in (("normalize_loudness", "--normalize-loudness"), ("fix_subtitles", "--fix-subtitles")):
                if params.get(field) is True:
                    args += [flag]
            if params.get("transcode") and params.get("remux"):
                raise ValueError("Choose remux or transcode, not both")
            for field, flag in (("transcode", "--transcode"), ("remux", "--remux")):
                if params.get(field) is True:
                    args += [flag]
            if params.get("job"):
                args += ["--job", local(params["job"], project)]
            rounds = params.get("max_rounds", 2)
            if type(rounds) is not int or rounds not in (1, 2):
                raise ValueError("QC repair is bounded to one or two rounds")
            args += ["--max-rounds", str(rounds)]
        process(args, logs, allowed=(0, 10))
        report_path = directory / ("report.json" if action == "qc_scan" else "repair.json")
        report = read_json(report_path)
        metrics.update(qc_gate(report))
        artifacts = [artifact(report_path, "qc_report"), artifact(directory, "qc_dir")]
        if action == "qc_repair":
            original_video = local(params.get("video"), project)
            current = Path(report["output_video"]).resolve()
            # A no-op repair still emits a new owned artifact, making @repair:video deterministic.
            # It never moves or overwrites the source to satisfy the engine's ownership rule.
            if not current.is_relative_to(out):
                unchanged = out / ("video-retained" + current.suffix)
                shutil.copyfile(current, unchanged)
                if sha256(current) != sha256(unchanged):
                    raise ValueError("Retained repair video copy checksum mismatch")
                current = unchanged
            artifacts.append(artifact(current, "video"))
            if report.get("output_srt"):
                current_srt = Path(report["output_srt"]).resolve()
                if not current_srt.is_relative_to(out):
                    retained_srt = out / "captions-retained.srt"
                    shutil.copyfile(current_srt, retained_srt)
                    current_srt = retained_srt
                if current_srt.stat().st_size:
                    artifacts.append(artifact(current_srt, "srt"))
            metrics["result_video"] = str(current)
            metrics["result_srt"] = str(current_srt) if report.get("output_srt") else None
            metrics["requires_final_scan"] = True
            metrics["video_bytes_changed"] = sha256(current) != sha256(original_video)
            metrics["source_overwritten"] = False
            if report.get("status") == "blocked":
                metrics["machine_gate"] = "failed"
        if metrics["machine_gate"] == "failed":
            return {"status": "failed", "artifacts": artifacts, "metrics": metrics, "error": "QC found a verified technical failure"}
    elif action in ("build_board", "validate_board"):
        board = local(params.get("board"), project)
        destination = out / ("story-canvas.html" if action == "build_board" else "board-check.json")
        process([py, script("board", root), "build" if action == "build_board" else "validate", board,
                 "--out", destination], logs, allowed=(0, 10))
        if action == "build_board":
            check = out / "board-check.json"
            process([py, script("board", root), "validate", board, "--out", check], logs, allowed=(0, 10))
        else:
            check = destination
        value = read_json(check)
        artifacts = [artifact(check, "board_report")]
        if action == "build_board":
            artifacts.append(artifact(destination, "canvas"))
        metrics.update(machine_gate="failed" if value.get("errors") else "passed", board_status=value.get("status"),
                       errors=value.get("errors"), warnings=value.get("warnings"), visual_identity="pending")
        if value.get("errors"):
            return {"status": "blocked", "artifacts": artifacts, "metrics": metrics,
                    "error": "Board references/selection/explicit continuity must be corrected first"}
    elif action == "package_delivery":
        return package_delivery(params, project, out, logs)
    else:
        raise ValueError("Unsupported allowlisted local action: " + action)
    return {"status": "succeeded", "artifacts": artifacts, "metrics": metrics}


def prepare_audio_plan(params, project, out, root, logs):
    job = local(params.get("job"), project)
    time_map_path = out / "time-map.json"
    time_map = compiler_module().build_time_map(job, time_map_path)
    duration = time_map["duration"]
    mode = params.get("voice_mode", "preserve")
    if mode not in ("preserve", "replace", "add_narration"):
        raise ValueError("voice_mode must be preserve, replace or add_narration")
    extras = params.get("tracks", [])
    if not isinstance(extras, list) or not all(isinstance(value, dict) for value in extras):
        raise ValueError("tracks must be a list of local audio cues")
    if mode == "preserve" and any(value.get("role") in ("dialogue", "voiceover") for value in extras):
        raise ValueError("preserve retains existing dialogue; use add_narration for an explicit extra voice or replace to replace it")
    if mode == "replace" and not any(value.get("role") == "voiceover" for value in extras):
        raise Blocked("replace requires an actual voiceover source, not a planned prompt")
    if any(value.get("role") == "dialogue" for value in extras):
        raise ValueError("Original dialogue is supplied only by the measured picture soundtrack")
    tracks, artifacts, inputs = [], [artifact(time_map_path, "time_map")], [job]
    tail = None
    if mode in ("preserve", "add_narration"):
        video = local(params.get("video"), project)
        measured = probe(video, logs)
        audio = next((value for value in measured["streams"] if value.get("codec_type") == "audio"), None)
        if not audio:
            raise Blocked("Picture has no original audio; supply a real voiceover with voice_mode=replace")
        dialogue = out / "dialogue.wav"
        process(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-n", "-i", video,
                 "-map", "0:a:0", "-vn", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le", dialogue], logs)
        raw = probe(dialogue, logs)
        stream = next(value for value in raw["streams"] if value.get("codec_type") == "audio")
        from fractions import Fraction
        source_seconds = float(int(stream["duration_ts"]) * Fraction(stream["time_base"]))
        available = round(source_seconds * 48000) / 48000
        if available <= 0:
            raise Blocked("Original dialogue is empty after actual decode")
        selected = min(duration, available)
        tracks.append({"id": "DIALOGUE_ORIGINAL", "role": "dialogue", "source": str(dialogue),
                       "source_start": 0, "at": 0, "duration": selected, "gain_db": 0,
                       "fade_in": 0, "fade_out": 0, "loop": False, "duck": False})
        artifacts.append(artifact(dialogue, "dialogue"))
        inputs.append(video)
        tail = {"decoded_source_seconds": available, "selected_seconds": selected,
                "programme_tail_without_source_seconds": max(0, duration - selected),
                "note": "Any programme tail is explicit timeline silence, not invented speech"}
    for index, source_track in enumerate(extras, 1):
        track = dict(source_track)
        source = local(track.get("source"), project)
        inputs.append(source)
        track["source"] = str(source)
        track.setdefault("id", f"EXTRA_{index:03d}")
        track.setdefault("at", 0)
        track.setdefault("source_start", 0)
        if "duration" not in track:
            info = probe(source, logs)
            audio = next((value for value in info["streams"] if value.get("codec_type") == "audio"), None)
            if not audio or not audio.get("duration"):
                raise Blocked("Explicit cue duration is required when the source audio stream duration is not reliable")
            available = number(float(audio["duration"]), "source audio duration", 0)
            track["duration"] = min(available - track["source_start"], duration - track["at"])
        tracks.append(track)
    plan = {"version": 1, "name": params.get("name", "Production soundtrack"), "duration": duration,
            "sample_rate": 48000, "channels": 2, "target_lufs": params.get("target_lufs", -16),
            "true_peak_db": params.get("true_peak_db", -1.5),
            "ducking": params.get("ducking", {"enabled": True, "threshold_db": -30, "ratio": 8, "attack_ms": 15, "release_ms": 250}),
            "tracks": tracks, "voice_mode": mode,
            "timeline": {"job": str(job), "job_sha256": sha256(job), "total_frames": time_map["total_frames"], "fps": time_map["fps"]}}
    destination = out / "audio-plan.json"
    write_json(destination, plan)
    spec = importlib.util.spec_from_file_location("production_audio_validation", script("audio", root))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.load_plan(destination)
    artifacts.insert(0, artifact(destination, "audio_plan"))
    return {"status": "succeeded", "artifacts": artifacts,
            "metrics": {"machine_gate": "passed", "duration": duration, "total_frames": time_map["total_frames"],
                        "voice_mode": mode, "original_dialogue": tail, "creative_review": "pending",
                        "speech_overlap_review": mode == "add_narration",
                        "input_files": [{"path": str(path), "sha256": sha256(path)} for path in dict.fromkeys(inputs)]}}


def dynamic_inputs(action, params, project, root=None):
    """Hash nested source files, not just the JSON document that names them."""
    paths = {Path(__file__).resolve()}
    helpers = {
        "preflight": ["preflight.py"], "compile_story": ["compiler.py"],
        "plan_edit": ["compiler.py"], "prepare_audio_plan": ["compiler.py"],
    }
    for name in helpers.get(action, []):
        candidate = Path(__file__).with_name(name)
        if candidate.is_file():
            paths.add(candidate.resolve())
    executors = {
        "transcribe": ("editor",), "plan_edit": ("editor",), "render": ("editor",),
        "export_nle": ("editor",), "prepare_audio_plan": ("audio",),
        "mix_audio": ("audio",), "mux_audio": ("audio",), "export_audio_nle": ("audio_nle",),
        "qc_scan": ("qc",), "qc_repair": ("qc",), "build_board": ("board",), "validate_board": ("board",),
    }
    if root is not None:
        paths.update(script(name, root).resolve() for name in executors.get(action, ()))
    for key in ("video", "transcript", "job", "board", "registry", "audio_plan", "audio", "timeline", "srt", "qc_report", "review_summary"):
        value = params.get(key)
        if isinstance(value, str) and not value.startswith("@"):
            candidate = Path(value).expanduser()
            candidate = (project / candidate if not candidate.is_absolute() else candidate).resolve()
            if candidate.is_file():
                paths.add(candidate)
                if key in ("job", "board", "registry", "audio_plan"):
                    data = read_json(candidate)
                    rows = data.get("clips", []) + data.get("broll", []) + data.get("tracks", []) + data.get("assets", [])
                    values = [row.get("source", row.get("path")) for row in rows if isinstance(row, dict)]
                    if data.get("grade", {}).get("lut"):
                        values.append(data["grade"]["lut"])
                    for value in values:
                        if isinstance(value, str) and not re.match(r"^[A-Za-z]+://", value):
                            path = Path(value).expanduser()
                            path = (candidate.parent / path if not path.is_absolute() else path).resolve()
                            if path.is_file():
                                paths.add(path)
    for item in params.get("files", []) + params.get("tracks", []):
        if isinstance(item, dict):
            value = item.get("path", item.get("source"))
            if isinstance(value, str):
                path = Path(value).expanduser()
                path = (project / path if not path.is_absolute() else path).resolve()
                if path.is_file():
                    paths.add(path)
                elif path.is_dir():
                    paths.update(_file_items(path))
    if params.get("mix_dir"):
        directory = local(params["mix_dir"], project, directory=True)
        paths.update(_file_items(directory))
    if params.get("asr_model"):
        directory = local(params["asr_model"], project, directory=True)
        paths.update(_file_items(directory))
    return [{"path": str(path), "sha256": sha256(path)} for path in sorted(paths)]


def _file_items(directory):
    if directory.is_symlink():
        raise ValueError("Delivery symlinks are not allowed")
    for item in sorted(directory.rglob("*")):
        if item.is_symlink():
            raise ValueError("Delivery directory contains a symlink")
        if item.is_file():
            yield item


def _review_state(params, project, qc, video_sha):
    acceptance = params.get("acceptance", "technical")
    if acceptance not in ("technical", "production"):
        raise ValueError("acceptance must be technical or production")
    if acceptance == "technical":
        return "technical_ready_pending_creative_review", {"state": "pending", "checks": {}}
    if not params.get("review_summary"):
        raise Blocked("Production acceptance requires the recorded review summary")
    path = local(params["review_summary"], project)
    value = read_json(path)
    if value.get("subject_sha256", value.get("final_video_sha256")) != video_sha:
        raise Blocked("Creative review is stale or belongs to another video")
    required = params.get("required_checks", ["visual", "listening", "continuity"])
    declared = value.get("required_checks", [])
    checks = value.get("checks", {})
    if (not isinstance(required, list) or not required or not isinstance(declared, list)
            or not all(isinstance(item, str) and item.strip() for item in required + declared)
            or not isinstance(checks, dict)):
        raise ValueError("Review summary must declare nonempty required_checks and checks")
    required = list(dict.fromkeys(required + declared))
    for name in required:
        check = checks.get(name)
        if not isinstance(check, dict) or check.get("state") != "passed" or not str(check.get("notes", "")).strip():
            raise Blocked("Required creative review remains incomplete: " + str(name))
        evidence = check.get("evidence", [])
        if not isinstance(evidence, list) or not evidence:
            raise Blocked("Review needs real evidence: " + str(name))
        for item in evidence:
            local(item, project)
    # A candidate is not a defect until reviewed, but cannot disappear silently.
    candidates = [item for item in qc.get("findings", []) if item.get("severity") == "review"]
    dispositions = value.get("finding_dispositions", [])
    if candidates:
        if not isinstance(dispositions, list) or len(dispositions) != len(candidates):
            raise Blocked("Every QC review candidate needs an explicit disposition")
        for index, finding in enumerate(candidates):
            disposition = next((item for item in dispositions if isinstance(item, dict) and item.get("finding_index") == index), None)
            if not disposition or disposition.get("decision") not in ("intentional", "verified_resolved") or not str(disposition.get("evidence", "")).strip():
                raise Blocked("QC candidate disposition is missing or unsubstantiated")
    return "production_accepted", {"state": "passed", "summary": str(path), "sha256": sha256(path), "checks": checks}


RELINK_SCRIPT = '''#!/usr/bin/env python3
"""Relink known FCP7 media paths to this delivery folder; never overwrite XML."""
from pathlib import Path
import json, re
from urllib.parse import unquote
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent
mapping = json.loads((ROOT / "source-map.json").read_text(encoding="utf-8"))
known = {}
for row in mapping["sources"]:
    candidate = ROOT / row["bundle_path"]
    if candidate.is_symlink() or not candidate.resolve().is_relative_to(ROOT) or not candidate.is_file():
        raise ValueError("Bundled source is missing or outside this delivery")
    known[unquote(row["original_uri"])] = candidate.resolve().as_uri()
results = []
for source in sorted(ROOT.rglob("*.xml")):
    if source.name.endswith(".relinked.xml"):
        continue
    raw = source.read_bytes()
    if re.search(br"<!ENTITY|<!DOCTYPE[^>]+(?:SYSTEM|PUBLIC)", raw, re.I):
        raise ValueError("External XML definitions are not allowed")
    tree = ET.fromstring(raw)
    if tree.tag != "xmeml":
        continue
    changed, unresolved = 0, []
    for node in tree.findall(".//pathurl"):
        old = unquote(node.text or "")
        if old in known:
            node.text = known[old]
            changed += 1
        elif old:
            unresolved.append(old)
    destination = source.with_name(source.stem + ".relinked.xml")
    with destination.open("xb") as output:
        output.write(b'<?xml version="1.0" encoding="UTF-8"?>\\n<!DOCTYPE xmeml>\\n' + ET.tostring(tree, encoding="utf-8"))
    results.append({"output": str(destination), "changed_paths": changed, "unresolved_paths": unresolved})
print(json.dumps({"relinked": results, "native_ui_verified": False}, ensure_ascii=False, indent=2))
'''


def bundle_sources(params, project, directory, logs):
    """Bundle only real source files explicitly named by the selected edit job."""
    if params.get("include_source_media") is not True:
        return [], {"enabled": False, "bytes": 0, "files": 0}
    job_path = local(params.get("job"), project)
    job = read_json(job_path)
    values = [row.get("source") for row in job.get("clips", []) + job.get("broll", []) if isinstance(row, dict)]
    if job.get("grade", {}).get("lut"):
        values.append(job["grade"]["lut"])
    sources = list(dict.fromkeys(local(value, job_path.parent) for value in values))
    if not sources:
        raise ValueError("Portable source delivery needs a nonempty edit job")
    total = sum(source.stat().st_size for source in sources)
    maximum = int(number(params.get("max_source_bytes", 2 * 1024 ** 3), "max_source_bytes", 1))
    if total > maximum:
        raise Blocked(f"Source media require {total} bytes, above the {maximum}-byte package limit")
    records, copied = [], []
    for source in sources:
        if source.is_symlink():
            raise ValueError("Source-media symlinks must be resolved before bundling")
        if source.suffix.lower() != ".cube":
            probe(source, logs)
        digest = sha256(source)
        target = directory / "source_media" / digest[:16] / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(source, target)
            if sha256(target) != digest:
                raise ValueError("Bundled source checksum mismatch")
            copied.append({"role": "source_media", "path": target.relative_to(directory).as_posix(),
                           "bytes": target.stat().st_size, "sha256": digest, "source": str(source)})
        records.append({"original_path": str(source), "original_uri": source.as_uri(),
                        "bundle_path": target.relative_to(directory).as_posix(), "bytes": source.stat().st_size,
                        "sha256": digest})
    return copied, {"enabled": True, "bytes": total, "files": len(sources), "max_source_bytes": maximum,
                    "sources": records, "job_sha256": sha256(job_path)}


def package_delivery(params, project, out, logs):
    items = params.get("files")
    if not isinstance(items, list) or not items:
        raise ValueError("Delivery requires a nonempty files list of {path,role}")
    seen_roles, sources, videos = set(), [], []
    for item in items:
        if not isinstance(item, dict) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", str(item.get("role", ""))):
            raise ValueError("Delivery items need a safe role name")
        role = item["role"]
        if role in seen_roles:
            raise ValueError("Delivery role must be unique: " + role)
        seen_roles.add(role)
        raw = item.get("path")
        if not isinstance(raw, str):
            raise ValueError("Delivery path must be a local string")
        path = Path(raw).expanduser()
        path = (project / path if not path.is_absolute() else path).resolve()
        if not path.exists():
            raise Blocked("Declared delivery artifact is missing: " + str(path))
        if path == out or out.is_relative_to(path) or path.is_symlink():
            raise ValueError("Delivery cannot recursively copy its own output or symlinks")
        if not path.is_file() and not path.is_dir():
            raise ValueError("Unsupported delivery input")
        files = list(_file_items(path)) if path.is_dir() else [path]
        if not files:
            raise ValueError("Delivery directory is empty")
        for file in files:
            if file.suffix.lower() in MEDIA_SUFFIXES:
                measured = probe(file, logs)
                if role == "video":
                    videos.append((file, measured))
        sources.append((role, path, files))
    if not videos:
        raise Blocked("A final decoded video artifact with role=video is required for this delivery profile")
    if len(videos) != 1:
        raise ValueError("Delivery must identify exactly one final video")
    qc_path = local(params.get("qc_report"), project)
    qc = read_json(qc_path)
    if qc.get("schema") != "video-review-autofix.scan/v1" or qc.get("machine_status") not in ("checks_passed", "findings_present", "failed"):
        raise ValueError("Delivery needs a genuine scan report from the final encoded video")
    video, video_meta = videos[0]
    if qc.get("source_sha256") != sha256(video):
        raise Blocked("QC report is stale or belongs to another video; rescan the final encoded delivery")
    gate = qc_gate(qc)
    if gate["technical_failures"] or gate["technical_warnings"]:
        raise Blocked("Delivery has unresolved technical failures/warnings")
    if qc.get("coverage", {}).get("decode", {}).get("state") != "checked":
        raise Blocked("Delivery requires successful full-file decode coverage")
    profile = params.get("delivery_profile", {})
    if not isinstance(profile, dict):
        raise ValueError("delivery_profile must be an object")
    profile_keys = {"width", "height", "fps", "duration_seconds", "duration", "require_audio", "audio_required",
                    "video_codec", "codec", "audio_codec", "audio_sample_rate", "audio_channels", "format"}
    if set(profile) - profile_keys:
        raise ValueError("Unsupported delivery profile fields: " + ", ".join(sorted(set(profile) - profile_keys)))
    stream = next((row for row in video_meta["streams"] if row.get("codec_type") == "video"), None)
    if not stream:
        raise ValueError("Final video artifact has no video stream")
    for key in ("width", "height"):
        if key in profile and profile[key] != stream.get(key):
            raise Blocked("Encoded delivery does not match profile." + key)
    if "fps" in profile:
        from fractions import Fraction
        expected, actual = Fraction(str(profile["fps"])), Fraction(str(stream.get("avg_frame_rate", "0/1")))
        if expected <= 0 or abs(float(expected - actual)) > 0.001:
            raise Blocked("Encoded delivery does not match profile.fps")
    for field, alias in (("duration_seconds", "duration"), ("require_audio", "audio_required"), ("video_codec", "codec")):
        if field in profile and alias in profile and profile[field] != profile[alias]:
            raise ValueError("Conflicting delivery profile aliases: " + field + "/" + alias)
    expected_duration = profile.get("duration_seconds", profile.get("duration"))
    if expected_duration is not None:
        expected_duration = number(expected_duration, "profile.duration_seconds", 0.001)
        actual_duration = float(stream.get("duration", video_meta["format"].get("duration", "nan")))
        from fractions import Fraction
        actual_rate = Fraction(str(stream.get("avg_frame_rate", "0/1")))
        tolerance = 1 / float(actual_rate) + 0.025 if actual_rate > 0 else 0.05
        if not math.isfinite(actual_duration) or abs(actual_duration - expected_duration) > tolerance:
            raise Blocked("Encoded delivery does not match profile.duration_seconds")
    audios = [row for row in video_meta["streams"] if row.get("codec_type") == "audio"]
    require_audio = profile.get("require_audio", profile.get("audio_required", False))
    if not isinstance(require_audio, bool):
        raise ValueError("profile.require_audio must be a boolean")
    if require_audio and not audios:
        raise Blocked("Delivery profile requires audio")
    expected_codec = profile.get("video_codec", profile.get("codec"))
    if expected_codec is not None and expected_codec != stream.get("codec_name"):
        raise Blocked("Encoded delivery does not match profile.video_codec")
    if "format" in profile and str(profile["format"]).lower().lstrip(".") != video.suffix.lower().lstrip("."):
        raise Blocked("Encoded delivery does not match profile.format")
    for field, stream_key in (("audio_codec", "codec_name"), ("audio_sample_rate", "sample_rate"), ("audio_channels", "channels")):
        if field in profile:
            if not audios or str(profile[field]) != str(audios[0].get(stream_key)):
                raise Blocked("Encoded delivery does not match profile." + field)
    acceptance, review = _review_state(params, project, qc, sha256(video))
    directory = out / "delivery"
    directory.mkdir()
    copied = []
    for role, source, files in sources:
        role_dir = directory / role
        role_dir.mkdir()
        for file in files:
            relative = file.relative_to(source) if source.is_dir() else Path(file.name)
            target = role_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = sha256(file)
            shutil.copyfile(file, target)
            if sha256(target) != digest:
                raise ValueError("Delivery copy checksum mismatch")
            copied.append({"role": role, "path": target.relative_to(directory).as_posix(), "bytes": target.stat().st_size,
                           "sha256": digest, "source": str(file)})
    source_copies, source_bundle = bundle_sources(params, project, directory, logs)
    copied.extend(source_copies)
    if source_bundle["enabled"]:
        rows = list(source_bundle["sources"])
        original_uris = {row["original_uri"] for row in rows}
        for row in copied:
            original = Path(row["source"])
            if original.suffix.lower() in (".wav", ".mp3", ".flac", ".m4a", ".aac") and original.as_uri() not in original_uris:
                rows.append({"original_path": str(original), "original_uri": original.as_uri(),
                             "bundle_path": row["path"], "sha256": row["sha256"], "bytes": row["bytes"]})
                original_uris.add(original.as_uri())
        write_json(directory / "source-map.json", {"schema": "ai-production.source-map/v1", "sources": rows,
                    "note": "Run relink_xml.py on the destination workstation; originals remain unchanged"})
        (directory / "relink_xml.py").write_text(RELINK_SCRIPT, encoding="utf-8")
    shutil.copyfile(qc_path, directory / "qc-report.json")
    losses = params.get("losses", [
        "Native Jianying/Resolve/Premiere UI import has not been verified by the local adapter.",
        "XML/EDL do not necessarily preserve burned subtitles, LUT/grade nodes, crop or loudness processing.",
        "Referenced source media need relinking on a different Windows workstation unless supplied.",
    ])
    if not isinstance(losses, list) or not all(isinstance(value, str) for value in losses):
        raise ValueError("losses must be a list of strings")
    (directory / "README.md").write_text(
        "# Production delivery\n\nAcceptance: " + acceptance +
        "\n\nNative NLE UI import has not been verified. Source-media paths may need relinking.\n" +
        ("\nSource media are bundled. Run: python relink_xml.py ; import the new *.relinked.xml copies.\n" if source_bundle["enabled"] else "") + "\n" +
        "\n".join("- " + value for value in losses) + "\n", encoding="utf-8")
    for filename in ("qc-report.json", "README.md", "source-map.json", "relink_xml.py"):
        path = directory / filename
        if path.is_file():
            copied.append({"role": "delivery_metadata", "path": filename, "bytes": path.stat().st_size,
                           "sha256": sha256(path)})
    manifest = {"schema": "ai-production.delivery/v1", "acceptance": acceptance, "review": review,
                "files": copied, "final_video_sha256": sha256(video), "video": video_meta, "delivery_profile": profile,
                "verified_profile_keys": sorted(profile),
                "qc_report_sha256": sha256(qc_path), "machine_gate": "passed", "review_candidates": gate["review_candidates"],
                "native_ui_verified": False, "interchange_losses": losses, "source_bundle": source_bundle}
    write_json(directory / "manifest.json", manifest)
    return {"status": "succeeded", "artifacts": [artifact(directory, "delivery_dir"), artifact(directory / "manifest.json", "delivery_manifest")],
            "metrics": {"machine_gate": "passed", "acceptance": acceptance, "creative_review": review["state"],
                        "native_ui_verified": False, "file_count": len(copied), "source_bundle_bytes": source_bundle["bytes"]}}


def execute(action, params, project_dir, output_dir, skills_root):
    project, out, root = Path(project_dir).resolve(), Path(output_dir).resolve(), Path(skills_root).resolve()
    result, logs = None, []
    try:
        if action not in ACTIONS:
            raise ValueError("Unsupported allowlisted action: " + str(action))
        if not isinstance(params, dict):
            raise ValueError("Step params must be an object")
        if not project.is_dir() or not root.is_dir():
            raise ValueError("project_dir and skills_root must exist")
        if not out.is_relative_to(project) or out == project:
            raise ValueError("Attempt output must be a child of the project")
        if out.exists() and (not out.is_dir() or out.is_symlink() or any(out.iterdir())):
            raise ValueError("Attempt output must be a new or empty directory")
        out.mkdir(parents=True, exist_ok=True)
        result = _run_action(action, params, project, out, root, logs)
        metrics = result.setdefault("metrics", {})
        seen = {value["path"]: value for value in metrics.get("input_files", [])}
        seen.update({value["path"]: value for value in dynamic_inputs(action, params, project, root)})
        metrics["input_files"] = list(seen.values())
    except Blocked as exc:
        result = {"status": "blocked", "artifacts": [], "metrics": {"machine_gate": "blocked"}, "error": str(exc)}
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        result = {"status": "failed", "artifacts": [], "metrics": {"machine_gate": "failed"}, "error": str(exc)}
    if out.is_dir() and out.is_relative_to(project):
        log = out / "adapter-commands.json"
        record = out / "adapter-result.json"
        if not log.exists() and not record.exists():
            write_json(log, logs)
            write_json(record, result)
    return result
