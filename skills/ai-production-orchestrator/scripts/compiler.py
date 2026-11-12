#!/usr/bin/env python3
"""Compile reviewed story videos into a real, frame-quantized edit job.

The compiler never turns storyboard images into pretend video assets and never
approves visual quality. It verifies declared reviews, selected references,
actual video streams and source ranges before creating new output files.
"""
from __future__ import annotations

import argparse
from fractions import Fraction
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import subprocess
import sys

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".svg"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"),
                      parse_constant=lambda x: (_ for _ in ()).throw(ValueError(f"Non-finite JSON number: {x}")))


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def number(value, name, minimum=None):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, not a boolean or numeric string")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return float(value)


def fps_value(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("fps must be a number or positive rational string")
    try:
        result = Fraction(str(value)).limit_denominator(100000)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError("Invalid frame rate") from exc
    if not 1 <= float(result) <= 240:
        raise ValueError("fps must be between 1 and 240")
    return result


def local_path(value, base):
    if not isinstance(value, str) or not value.strip() or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        raise ValueError("Expected a nonempty local file path")
    p = Path(value).expanduser()
    return (p if p.is_absolute() else Path(base) / p).resolve()


def write_new(path, payload):
    path = Path(path).resolve()
    if path.exists():
        raise ValueError(f"Output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return path


def command(argv):
    result = subprocess.run([str(x) for x in argv], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise ValueError(f"{argv[0]} failed ({result.returncode}): {result.stderr[-3000:]}")
    return result


def _positive(value):
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) and parsed > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def probe_video(path):
    """Use the primary video stream, never a longer audio/container duration."""
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError(f"Missing video file: {path}")
    if path.suffix.lower() in IMAGE_SUFFIXES:
        raise ValueError("A storyboard image is not a generated story video")
    raw = json.loads(command([
        "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-count_frames", "-show_streams", "-show_format",
        "-of", "json", path,
    ]).stdout)
    streams = [s for s in raw.get("streams", []) if s.get("codec_type") == "video"
               and not s.get("disposition", {}).get("attached_pic")]
    if not streams:
        raise ValueError(f"No real primary video stream: {path}")
    format_name = str(raw.get("format", {}).get("format_name", ""))
    if any(x in format_name for x in ("image2", "png_pipe", "jpeg_pipe", "webp_pipe", "gif", "hls", "dash", "concat")):
        raise ValueError("Still/animated images and media playlists are not single selected story video files")
    video = streams[0]
    raw_rate = video.get("avg_frame_rate")
    if raw_rate in (None, "0/0", "0/1"):
        raw_rate = video.get("r_frame_rate")
    rate = fps_value(raw_rate)
    try:
        frames = int(video.get("nb_read_frames", 0))
    except (TypeError, ValueError):
        frames = 0
    durations = []
    explicit = _positive(video.get("duration"))
    if explicit:
        durations.append(explicit)
    try:
        ticks = int(video["duration_ts"]) * float(Fraction(video["time_base"]))
        if math.isfinite(ticks) and ticks > 0:
            durations.append(ticks)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        pass
    tag = video.get("tags", {}).get("DURATION")
    if isinstance(tag, str):
        try:
            h, m, s = tag.split(":")
            seconds = int(h) * 3600 + int(m) * 60 + float(s)
            if math.isfinite(seconds) and seconds > 0:
                durations.append(seconds)
        except (ValueError, TypeError):
            pass
    if frames > 0:
        durations.append(frames / float(rate))
    if not durations or frames < 1:
        raise ValueError("Cannot establish finite decoded video length")
    duration = min(durations)
    command(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe",
             "-i", path, "-map", f"0:{video['index']}", "-an", "-f", "null", "-"])
    return {"path": str(path), "duration": duration, "fps": str(rate),
            "frames": frames, "width": int(video.get("width", 0)),
            "height": int(video.get("height", 0)), "sha256": digest(path),
            "decode_check": "passed"}


def probe_broll(asset, path):
    if asset.get("kind") == "video":
        return {**probe_video(path), "image": False}
    if asset.get("kind") != "image" or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
        raise ValueError("B-roll must declare a supported real image or video")
    if not path.is_file():
        raise ValueError("B-roll image is missing")
    raw = json.loads(command(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_streams", "-of", "json", path]).stdout)
    video = next((s for s in raw.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video or not int(video.get("width", 0)) or not int(video.get("height", 0)):
        raise ValueError("No valid B-roll image dimensions")
    command(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe",
             "-i", path, "-frames:v", "1", "-f", "null", "-"])
    return {"path": str(path), "image": True, "sha256": digest(path), "decode_check": "passed"}


def build_time_map(job_path, output_path):
    """Write declared source/output mappings on the same grid as editor.py."""
    job_path, output_path = Path(job_path).resolve(), Path(output_path).resolve()
    if job_path == output_path or output_path.exists():
        raise ValueError("Time-map output exists or equals the source job")
    job = read_json(job_path)
    if not isinstance(job, dict) or type(job.get("version")) is not int or job["version"] != 1:
        raise ValueError("Expected job version 1")
    fps = fps_value(job.get("fps", 25))
    clips = job.get("clips")
    if not isinstance(clips, list) or not clips:
        raise ValueError("A job needs real main clips")
    mapped, cursor, seen = [], 0, set()
    for i, clip in enumerate(clips, 1):
        if not isinstance(clip, dict):
            raise ValueError("Each clip must be an object")
        ident = clip.get("id", f"clip-{i:03d}")
        if not isinstance(ident, str) or not ident.strip() or ident in seen:
            raise ValueError("Clip IDs must be nonempty and unique")
        seen.add(ident)
        start = number(clip.get("start"), "clip.start", 0)
        end = number(clip.get("end"), "clip.end", 0)
        if end <= start:
            raise ValueError("Clip end must exceed source start")
        source = local_path(clip.get("source"), job_path.parent)
        if not source.is_file():
            raise ValueError(f"Clip source is missing: {source}")
        frames = round((end - start) * float(fps))
        if frames < 1:
            raise ValueError("Clip duration is below one output frame")
        row = {"clip_id": ident, "source": str(source), "start": start, "end": end,
               "source_start": start, "source_end": end,
               "frames": frames, "output_start_frame": cursor, "output_end_frame": cursor + frames,
               "output_start": cursor / float(fps), "output_end": (cursor + frames) / float(fps),
               "duration": frames / float(fps),
               "source_end_on_output_grid": start + frames / float(fps)}
        if clip.get("shot_id"):
            row["shot_id"] = clip["shot_id"]
        mapped.append(row)
        cursor += frames
    broll = []
    for i, clip in enumerate(job.get("broll", []), 1):
        at = number(clip.get("at"), "broll.at", 0)
        length = number(clip.get("duration"), "broll.duration", 0)
        start = number(clip.get("source_start", 0), "broll.source_start", 0)
        begin, frames = round(at * float(fps)), round(length * float(fps))
        if frames < 1 or begin + frames > cursor:
            raise ValueError("B-roll is outside the integer-frame output timeline")
        source = local_path(clip.get("source"), job_path.parent)
        if not source.is_file():
            raise ValueError("B-roll source is missing")
        broll.append({"id": clip.get("id", f"broll-{i:03d}"), "source": str(source),
                      "source_start": start, "output_start_frame": begin,
                      "output_end_frame": begin + frames, "duration": frames / float(fps)})
    result = {"schema": "ai-production-orchestrator.time-map/v1", "version": 1,
              "job": str(job_path), "job_sha256": digest(job_path), "fps": str(fps),
              "total_frames": cursor, "duration": cursor / float(fps),
              "audio_samples_48000": round(cursor / float(fps) * 48000),
              "clips": mapped, "broll": broll,
              "mapping": "Per-clip Python round((end-start)*fps), then cumulative integer frames",
              "scope": "Declared edit-plan frame grid; actual media ranges are checked by compiler/editor.",
              "source_subtitles_reused_without_remapping": False}
    write_new(output_path, result)
    return result


def _board_gate(board_path):
    path = Path(__file__).resolve().parents[2] / "story-canvas-producer" / "scripts" / "story_board.py"
    if not path.is_file():
        return None, ["story-canvas-producer validator is missing from the selected skills root"]
    spec = importlib.util.spec_from_file_location("_orchestrator_story_board", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    board, maps, previews, eligibility, report = module.inspect(board_path)
    errors = [f"{item.get('subject') or 'board'}: {item['message']}"
              for item in report["issues"] if item["severity"] == "error"]
    return (board, maps, report), errors


def _review_ok(asset):
    review = asset.get("review")
    return (isinstance(review, dict) and review.get("status") == "reviewed"
            and isinstance(review.get("evidence"), list) and bool(review["evidence"])
            and all(isinstance(x, str) and x.strip() for x in review["evidence"])
            and isinstance(review.get("hard_failures"), list) and not review["hard_failures"])


def _review_files(asset, base):
    rows = asset.get("review", {}).get("evidence_files", [])
    if not isinstance(rows, list):
        raise ValueError("review.evidence_files must be a list when provided")
    verified = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Review evidence files need path/hash records")
        path = local_path(row.get("path"), base)
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"Review evidence file is missing/empty: {path}")
        actual = digest(path)
        if row.get("sha256") and row["sha256"] != actual:
            raise ValueError(f"Review evidence hash differs: {path}")
        verified.append({"path": str(path), "sha256": actual})
    for container, path_key, hash_key in (
        (asset.get("review", {}), "record_path", "record_sha256"),
        (asset.get("provenance", {}), "receipt_path", "receipt_sha256"),
    ):
        if not isinstance(container, dict):
            raise ValueError("Review/provenance metadata must be objects")
        if container.get(path_key) or container.get(hash_key):
            path = local_path(container.get(path_key), base)
            expected = container.get(hash_key)
            if not re.fullmatch(r"[0-9a-f]{64}", str(expected)) or not path.is_file() or digest(path) != expected:
                raise ValueError(f"Bound {path_key} changed or is missing")
            verified.append({"path": str(path), "sha256": expected})
    return verified


def _registry_gate(path, registry, allow_legacy):
    ledger = (type(registry.get("revision")) is int and isinstance(registry.get("events"), list)
              and all(isinstance(row, dict) and row.get("logical_id") for row in registry.get("assets", [])))
    if not ledger:
        if not allow_legacy:
            return ["A versioned asset_registry ledger is required; register, verify, review and select assets first"], "blocked"
        return [], "explicit_legacy_host_evidence_only"
    helper = Path(__file__).with_name("asset_registry.py")
    if not helper.is_file():
        return ["asset_registry validator is missing"], "blocked"
    spec = importlib.util.spec_from_file_location("_compiler_asset_registry", helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    checked = module.validate(path, full_media=False)
    return checked.get("errors", []), "versioned_ledger_verified"


def compile_story(board_path, registry_path, output_dir, fps=25, width=1920, height=1080, name=None,
                  allow_legacy_registry=False):
    """Compile only selected, reviewed and source-verified story video assets."""
    out, board_path, registry_path = (Path(p).resolve() for p in (output_dir, board_path, registry_path))
    artifacts = []
    try:
        if any((out / item).exists() for item in ("job.json", "time-map.json")):
            raise ValueError("Compiler outputs already exist; choose a new attempt directory")
        rate = fps_value(fps)
        for label, value in (("width", width), ("height", height)):
            if type(value) is not int or not 16 <= value <= 7680 or value % 2:
                raise ValueError(f"{label} must be an even integer from 16 to 7680")
        gated, reasons = _board_gate(board_path)
        if reasons:
            return {"status": "blocked", "artifacts": [], "reasons": reasons,
                    "next_action": "Repair/review the board before compiling real videos",
                    "metrics": {"machine_gate": "blocked", "visual_review": "not_performed_by_compiler"}}
        board, maps, board_report = gated
        registry = read_json(registry_path)
        if not isinstance(registry, dict) or type(registry.get("version")) is not int or registry["version"] != 1:
            raise ValueError("Expected registry object with integer version=1")
        rows = registry.get("assets")
        if not isinstance(rows, list):
            raise ValueError("Registry assets must be a list")
        registry_errors, registry_mode = _registry_gate(registry_path, registry, allow_legacy_registry)
        if registry_errors:
            return {"status": "blocked", "artifacts": [], "reasons": registry_errors,
                    "next_action": "Register and review assets in the versioned registry, then resume",
                    "metrics": {"machine_gate": "blocked", "registry_mode": registry_mode}}
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip() or row["id"] in seen:
                raise ValueError("Registry asset IDs must be nonempty, unique strings")
            seen.add(row["id"])
        shots = sorted(maps["shots"].values(), key=lambda shot: (shot["order"], shot["id"]))
        if not shots:
            raise ValueError("Story board contains no shots")
        if len({shot["order"] for shot in shots}) != len(shots):
            raise ValueError("Compilation needs globally unique shot.order across continuity groups")
        selected, reasons, used_refs = [], [], set()
        for shot in shots:
            required = set()
            scene = maps["scenes"][shot["scene_id"]]
            if not scene.get("selected_asset_id"):
                reasons.append(f"{shot['id']}: select and review its scene reference first")
            else:
                required.add(scene["selected_asset_id"])
            for cid in shot.get("character_ids", []):
                character = maps["characters"][cid]
                if not character.get("selected_asset_id"):
                    reasons.append(f"{shot['id']}: select and review character {cid} first")
                else:
                    required.add(character["selected_asset_id"])
            if shot.get("selected_asset_id"):
                required.add(shot["selected_asset_id"])
            candidates = [a for a in rows if a.get("shot_id") == shot["id"]
                          and a.get("role") == "shot_video" and a.get("status") == "selected"]
            if len(candidates) != 1:
                reasons.append(f"{shot['id']}: needs exactly one selected real shot_video asset; generate/review missing footage")
                continue
            asset = candidates[0]
            if asset.get("kind") != "video":
                reasons.append(f"{shot['id']}: selected footage is not a video; storyboard images cannot become completed clips")
                continue
            if not _review_ok(asset):
                reasons.append(f"{shot['id']}: video needs reviewed evidence with no hard failures")
                continue
            references = asset.get("reference_asset_ids")
            if not isinstance(references, list) or not all(isinstance(v, str) for v in references):
                reasons.append(f"{shot['id']}: reference_asset_ids must list actual selected reference IDs")
                continue
            if not required.issubset(set(references)):
                reasons.append(f"{shot['id']}: video references are stale/missing; regenerate or re-review against {sorted(required-set(references))}")
                continue
            if asset.get("board_sha256") and asset["board_sha256"] != digest(board_path):
                reasons.append(f"{shot['id']}: video board_sha256 is stale")
                continue
            selected.append((shot, asset))
            used_refs.update(required)
        if reasons:
            return {"status": "blocked", "artifacts": [], "reasons": reasons,
                    "next_action": "Generate/review real shot videos, update selected reference IDs, then resume",
                    "metrics": {"machine_gate": "blocked", "missing_or_unready_shots": len(reasons),
                                "visual_review": "not_performed_by_compiler"}}
        videos, clips, inputs = {}, [], [
            {"path": str(board_path), "sha256": digest(board_path)},
            {"path": str(registry_path), "sha256": digest(registry_path)},
        ]
        if registry_mode == "versioned_ledger_verified":
            for row in rows:
                registered = local_path(row.get("path"), registry_path.parent)
                inputs.append({"path": str(registered), "sha256": digest(registered)})
                inputs.extend(_review_files(row, registry_path.parent))
        for ref_id in sorted(used_refs):
            source = local_path(maps["assets"][ref_id]["path"], board_path.parent)
            parent = next((row for row in rows if row["id"] == ref_id), None)
            if not allow_legacy_registry or parent is not None:
                if parent is None or parent.get("status") != "selected" or not _review_ok(parent):
                    raise ValueError(f"Selected board reference must also be selected and reviewed in registry: {ref_id}")
                registered = local_path(parent.get("path"), registry_path.parent)
                if registered != source or parent.get("sha256") != digest(source):
                    raise ValueError(f"Board reference path/hash differs from selected registry version: {ref_id}")
                inputs.extend(_review_files(parent, registry_path.parent))
            inputs.append({"path": str(source), "sha256": digest(source)})
        for shot, asset in selected:
            inputs.extend(_review_files(asset, registry_path.parent))
            source = local_path(asset.get("path"), registry_path.parent)
            if str(source) not in videos:
                info = probe_video(source)
                videos[str(source)] = info
                inputs.append({"path": str(source), "sha256": info["sha256"]})
            info = videos[str(source)]
            if asset.get("sha256") and asset["sha256"] != info["sha256"]:
                raise ValueError(f"Registry SHA differs from actual video: {asset['id']}")
            start = number(asset.get("source_start", 0), "asset.source_start", 0)
            length = number(asset.get("duration", info["duration"] - start), "asset.duration", 0)
            if length <= 0 or start + length > info["duration"] + 0.0001:
                raise ValueError(f"Video source range is outside actual decoded stream: {asset['id']}")
            frames = round(length * float(rate))
            if frames < 1 or start + frames / float(rate) > info["duration"] + 1 / float(fps_value(info["fps"])):
                raise ValueError(f"Rounded shot range has no usable output frame: {asset['id']}")
            clips.append({"id": shot["id"], "shot_id": shot["id"], "asset_id": asset["id"],
                          "source": str(source), "start": start, "end": start + length,
                          "reference_asset_ids": sorted(set(asset["reference_asset_ids"]))})
        job = {"version": 1, "name": name or board.get("title", "Story production"),
               "fps": str(rate), "width": width, "height": height, "clips": clips,
               "broll": [], "subtitles": [], "grade": {"preset": "neutral", "lut": None},
               "audio": {"target_lufs": -16},
               "provenance": {"board": str(board_path), "board_sha256": digest(board_path),
                              "registry": str(registry_path), "registry_sha256": digest(registry_path),
                              "registry_mode": registry_mode,
                              "source_range_default": "Missing duration uses verified remaining full video stream; explicit ranges are preserved",
                              "visual_quality": "Declared reviewed asset evidence; compiler does not inspect identities"}}
        total = sum(round((c["end"] - c["start"]) * float(rate)) for c in clips)
        for asset in rows:
            if asset.get("role") != "broll" or asset.get("status") != "selected":
                continue
            if not _review_ok(asset):
                raise ValueError(f"B-roll lacks reviewed evidence: {asset['id']}")
            inputs.extend(_review_files(asset, registry_path.parent))
            source = local_path(asset.get("path"), registry_path.parent)
            info = videos.get(str(source)) or probe_broll(asset, source)
            if asset.get("kind") not in ("image", "video") or (asset["kind"] == "image") != bool(info.get("image")):
                raise ValueError(f"B-roll kind differs from actual media: {asset['id']}")
            if str(source) not in videos:
                videos[str(source)] = info
                inputs.append({"path": str(source), "sha256": info["sha256"]})
            if asset.get("sha256") and asset["sha256"] != info["sha256"]:
                raise ValueError(f"Registry SHA differs from actual B-roll: {asset['id']}")
            at = number(asset.get("at"), "broll.at", 0)
            length = number(asset.get("duration"), "broll.duration", 0)
            start = number(asset.get("source_start", 0), "broll.source_start", 0)
            begin, frames = round(at * float(rate)), round(length * float(rate))
            if (frames < 1 or begin + frames > total or
                    info.get("image") and start != 0 or
                    not info.get("image") and start + length > info["duration"] + 0.0001):
                raise ValueError(f"B-roll outside source/program timeline: {asset['id']}")
            job["broll"].append({"id": asset["id"], "source": str(source), "at": at,
                                 "duration": length, "source_start": start, "fit": "cover"})
        ordered = sorted(job["broll"], key=lambda b: round(b["at"] * float(rate)))
        if any(round(a["at"] * float(rate)) + round(a["duration"] * float(rate)) > round(b["at"] * float(rate))
               for a, b in zip(ordered, ordered[1:])):
            raise ValueError("Overlapping B-roll is unsupported by the current renderer")
        out.mkdir(parents=True, exist_ok=True)
        job_path = write_new(out / "job.json", job)
        time_path = out / "time-map.json"
        mapping = build_time_map(job_path, time_path)
        artifacts = [{"path": str(job_path), "role": "job"}, {"path": str(time_path), "role": "time_map"}]
        return {"status": "succeeded", "artifacts": artifacts,
                "metrics": {"machine_gate": "passed", "total_frames": mapping["total_frames"],
                            "duration": mapping["duration"], "fps": str(rate), "shot_count": len(clips),
                            "input_files": list({row["path"]: row for row in inputs}.values()),
                            "selected_reference_ids": sorted(used_refs),
                            "registry_mode": registry_mode,
                            "visual_review": "Declared evidence only; not performed by compiler",
                            "software_ui_verified": False}}
    except (ValueError, OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return {"status": "failed", "artifacts": artifacts, "error": str(exc),
                "metrics": {"machine_gate": "failed", "visual_review": "not_performed_by_compiler"}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    item = commands.add_parser("compile-story")
    item.add_argument("board")
    item.add_argument("registry")
    item.add_argument("--out-dir", required=True)
    item.add_argument("--fps", default="25")
    item.add_argument("--width", type=int, default=1920)
    item.add_argument("--height", type=int, default=1080)
    item.add_argument("--allow-legacy-registry", action="store_true",
                      help="Explicit low-trust compatibility for flat pre-reviewed registries; default requires the versioned ledger")
    item = commands.add_parser("time-map")
    item.add_argument("job")
    item.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        if args.command == "compile-story":
            result = compile_story(args.board, args.registry, args.out_dir, args.fps, args.width, args.height,
                                   allow_legacy_registry=args.allow_legacy_registry)
        else:
            result = {"status": "succeeded", "time_map": build_time_map(args.job, args.out)}
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return {"succeeded": 0, "blocked": 10, "failed": 2}.get(result["status"], 2)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
