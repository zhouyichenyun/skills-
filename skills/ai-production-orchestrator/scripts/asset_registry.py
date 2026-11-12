#!/usr/bin/env python3
"""Versioned local production assets with technical checks and evidence-based selection.

No generation requests, uploads, source edits, or automatic visual approval.
Python standard library; FFmpeg/FFprobe and the sibling story board helper.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

KINDS = {"image", "video", "audio"}
ROLES = {"shot_video", "broll", "character", "scene", "keyframe", "reference",
         "dialogue", "voiceover", "music", "sfx", "ambience"}
STATUSES = {"generated", "verified", "reviewed", "selected", "rejected"}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"),
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON number")))


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        while block := source.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def local_file(value, base=None):
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError("A local file path is required")
    value = str(value)
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value) and not re.match(r"^[A-Za-z]:[\\/]", value):
        raise ValueError("URLs and executable path schemes are not accepted")
    path = Path(value).expanduser()
    if base is not None and not path.is_absolute():
        path = Path(base) / path
    if path.is_symlink():
        raise ValueError("Direct symbolic-link inputs are not accepted")
    path = path.resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"Missing or empty local file: {path}")
    return path


@contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name("." + path.name + ".lock")
    if lock.is_symlink():
        raise ValueError("Registry lock cannot be a symbolic link")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        os.close(descriptor)
        raise ValueError("Registry lock must be an independent regular file")
    stream = os.fdopen(descriptor, "r+b")
    acquired = False
    try:
        if metadata.st_size == 0:
            stream.write(b" ")
            stream.flush()
            os.fsync(stream.fileno())
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError(f"Registry is locked by another live operation: {lock}") from exc
        acquired = True
        yield
    finally:
        if acquired:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def atomic_write(path, data):
    path = Path(path)
    encoded = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load(path):
    raw = Path(path)
    if raw.is_symlink():
        raise ValueError("Registry cannot be a symbolic link")
    path = raw.resolve()
    data = read_json(path)
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("Registry must be an object with integer version=1")
    if not isinstance(data.get("assets"), list):
        raise ValueError("Registry assets must be a list")
    errors = structure_errors(data)
    if errors:
        raise ValueError("Registry structure invalid: " + "; ".join(errors))
    return path, data


def structure_errors(data):
    errors, indexed, versions, selected_logical, selected_shots = [], {}, set(), set(), set()
    for row in data.get("assets", []):
        if not isinstance(row, dict):
            errors.append("Asset record must be an object")
            continue
        ident, logical = row.get("id"), row.get("logical_id")
        if not isinstance(ident, str) or not ident.strip() or ident in indexed:
            errors.append(f"Invalid/duplicate asset id: {ident}")
            continue
        indexed[ident] = row
        logical_ok = isinstance(logical, str) and bool(logical.strip())
        if not logical_ok:
            errors.append(f"{ident}: logical_id is required")
        version = row.get("version")
        if type(version) is not int or version < 1:
            errors.append(f"{ident}: version must be an integer >= 1")
        elif logical_ok and (logical, version) in versions:
            errors.append(f"{ident}: duplicate logical version")
        elif logical_ok:
            versions.add((logical, version))
        if (not isinstance(row.get("kind"), str) or row["kind"] not in KINDS
                or not isinstance(row.get("role"), str) or row["role"] not in ROLES
                or not isinstance(row.get("status"), str) or row["status"] not in STATUSES):
            errors.append(f"{ident}: unsupported kind/role/status")
        if not all(isinstance(row.get(key), dict) for key in ("review", "technical_verification", "provenance")):
            errors.append(f"{ident}: review, technical_verification and provenance must be objects")
        if not isinstance(row.get("path"), str) or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256", ""))):
            errors.append(f"{ident}: immutable path and SHA256 are required")
        refs = row.get("reference_asset_ids", [])
        if not isinstance(refs, list) or not all(isinstance(value, str) and value.strip() for value in refs):
            errors.append(f"{ident}: reference_asset_ids must contain strings")
        if row.get("status") == "selected" and logical_ok:
            if logical in selected_logical:
                errors.append(f"{ident}: multiple selected versions of {logical}")
            selected_logical.add(logical)
            shot_key = (row.get("shot_id"), row.get("role"))
            if row.get("shot_id") and not isinstance(row["shot_id"], str):
                errors.append(f"{ident}: shot_id must be a string")
                continue
            if row.get("shot_id") and isinstance(row.get("role"), str):
                if shot_key in selected_shots:
                    errors.append(f"{ident}: multiple selected assets for shot/role {shot_key}")
                selected_shots.add(shot_key)
    for ident, row in indexed.items():
        refs = row.get("reference_asset_ids", [])
        if isinstance(refs, list):
            for value in refs:
                if isinstance(value, str) and value not in indexed:
                    errors.append(f"{ident}: missing reference asset {value}")
    visited, active = set(), set()
    def visit(ident):
        if ident in active:
            errors.append(f"{ident}: reference cycle")
            return
        if ident in visited:
            return
        active.add(ident)
        refs = indexed[ident].get("reference_asset_ids", [])
        if isinstance(refs, list):
            for value in refs:
                if isinstance(value, str) and value in indexed:
                    visit(value)
        active.remove(ident)
        visited.add(ident)
    for ident in indexed:
        visit(ident)
    return errors


def run(command):
    result = subprocess.run([str(item) for item in command], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
    if result.returncode:
        raise ValueError(f"Media check failed ({result.returncode}): {result.stderr[-2000:]}")
    return result.stdout


def seconds(value, label, minimum=0):
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"{label} must be a finite number >= {minimum}")
    return float(value)


def audio_available(path, start=0):
    text = run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-xerror",
                "-protocol_whitelist", "file,pipe", "-ss", f"{start:.9f}", "-i", path,
                "-map", "0:a:0", "-vn", "-af", "aresample=48000,asetpts=PTS-STARTPTS",
                "-progress", "pipe:1", "-nostats", "-f", "null", "-"])
    values = re.findall(r"(?m)^out_time_us=(\d+)\s*$", text)
    duration = int(values[-1]) / 1_000_000 if values else 0
    if duration <= 0:
        raise ValueError("Cannot establish actual decoded audio duration")
    return duration


def technical(path, kind):
    path = local_file(path)
    if kind == "image":
        helper = Path(__file__).resolve().parents[2] / "story-canvas-producer" / "scripts" / "story_board.py"
        if not helper.is_file():
            raise ValueError("Sibling story-canvas-producer image checker is required")
        spec = importlib.util.spec_from_file_location("_registry_story_board", helper)
        module = importlib.util.module_from_spec(spec)
        previous_bytecode = sys.dont_write_bytecode
        try:
            sys.dont_write_bytecode = True
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = previous_bytecode
        image = module.image_data(path)
        return {"state": "verified", "check": "bounded image structure", "mime": image["mime"],
                "bytes": image["bytes"], "sha256": image["sha256"],
                "visual_review": "not_performed; structure does not prove identity, aesthetics, or complete decoding"}
    if kind == "video":
        helper = Path(__file__).with_name("compiler.py")
        spec = importlib.util.spec_from_file_location("_registry_compiler", helper)
        module = importlib.util.module_from_spec(spec)
        previous_bytecode = sys.dont_write_bytecode
        try:
            sys.dont_write_bytecode = True
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = previous_bytecode
        video = module.probe_video(path)
        return {"state": "verified", "check": "full primary-video decode",
                "sha256": video["sha256"], "media_duration": video["duration"],
                "stream": {key: video[key] for key in ("fps", "frames", "width", "height")},
                "creative_review": "not_performed"}
    raw = json.loads(run(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
                          "-show_streams", "-show_format", "-of", "json", path]))
    stream = next((item for item in raw.get("streams", []) if item.get("codec_type") == kind), None)
    if stream is None:
        raise ValueError(f"No {kind} stream in declared {kind} asset")
    duration = audio_available(path)
    keys = ("codec_name", "width", "height", "avg_frame_rate", "sample_rate", "channels", "duration", "duration_ts", "time_base")
    return {"state": "verified", "check": "full declared-stream decode", "sha256": digest(path),
            "stream": {key: stream[key] for key in keys if key in stream},
            "media_duration": duration,
            "container_duration": raw.get("format", {}).get("duration"),
            "creative_review": "not_performed"}


def unchanged(row, base):
    path = local_file(row["path"], base)
    if digest(path) != row["sha256"]:
        raise ValueError(f"Asset bytes changed: {row['id']}; add a new version instead of overwriting")
    return path


def receipt_record(path, task_id, provider, asset_hash, request_id=None, asset_path=None):
    path = local_file(path)
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise ValueError("A structured completion receipt is required")
    state = raw.get("provider_status", raw.get("status"))
    if str(state).lower() not in ("succeeded", "completed"):
        raise ValueError("Receipt must record actual provider completion")
    mode = raw.get("mode", raw.get("provider_mode"))
    if mode is None:
        mode = "synchronous" if raw.get("tool_receipt") and raw.get("request_id") and not raw.get("task_id") else "asynchronous"
    record = {"provider": provider or raw.get("provider") or "host_recorded_provider",
              "mode": mode, "receipt_path": str(path), "receipt_sha256": digest(path),
              "provider_status": str(state).lower(),
              "trust_boundary": "Host-saved receipt; this utility does not authenticate or submit provider tasks"}
    if mode == "synchronous":
        found_request = raw.get("request_id")
        if not isinstance(found_request, str) or not found_request.strip() or request_id and request_id != found_request:
            raise ValueError("Synchronous receipt needs a matching real request_id")
        if task_id or raw.get("task_id"):
            raise ValueError("Synchronous tools must not invent a remote task ID")
        tool_receipt = local_file(raw.get("tool_receipt"), path.parent)
        if tool_receipt == path:
            raise ValueError("Raw tool receipt must be distinct from normalized completion receipt")
        read_json(tool_receipt)
        bound = raw.get("artifact_sha256") == asset_hash
        for item in raw.get("verified_artifacts", raw.get("artifacts", [])):
            if not isinstance(item, dict):
                continue
            if item.get("sha256") == asset_hash:
                bound = True
            elif item.get("path") and asset_path:
                artifact = Path(item["path"]).expanduser()
                artifact = (path.parent / artifact if not artifact.is_absolute() else artifact).resolve()
                actual = Path(asset_path).resolve()
                if artifact.is_dir() and actual.is_relative_to(artifact) and digest(actual) == asset_hash:
                    bound = True
                elif artifact.is_file() and artifact == actual and digest(artifact) == asset_hash:
                    bound = True
        if not bound:
            raise ValueError("Synchronous completion must bind the actual local artifact/hash")
        record.update(request_id=found_request, task_id=None, tool_receipt_path=str(tool_receipt),
                      tool_receipt_sha256=digest(tool_receipt), artifact_sha256=asset_hash)
    elif mode == "asynchronous":
        found_id = raw.get("task_id", raw.get("taskId", raw.get("id")))
        if not task_id or str(found_id) != task_id:
            raise ValueError("Asynchronous receipt must match the real task ID")
        record.update(task_id=task_id, request_id=raw.get("request_id"))
    else:
        raise ValueError("Receipt mode must be synchronous or asynchronous")
    if raw.get("artifact_sha256") and raw["artifact_sha256"] != asset_hash:
        raise ValueError("Receipt artifact SHA256 does not match the real file")
    return record


def provenance_errors(row):
    provenance = row.get("provenance", {})
    if not provenance.get("receipt_path"):
        return []
    try:
        path = local_file(provenance["receipt_path"])
        if digest(path) != provenance.get("receipt_sha256"):
            return ["Provider receipt changed"]
        checked = receipt_record(path, provenance.get("task_id"), provenance.get("provider"),
                                 row["sha256"], provenance.get("request_id"), row["path"])
        if provenance.get("tool_receipt_path") and (checked.get("tool_receipt_path") != provenance["tool_receipt_path"]
                or checked.get("tool_receipt_sha256") != provenance.get("tool_receipt_sha256")):
            return ["Raw synchronous tool receipt changed"]
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return [f"Provider provenance is invalid: {exc}"]
    return []


def selected_range(args, checked, source):
    start = seconds(args.source_start, "source_start")
    duration = None if args.duration is None else seconds(args.duration, "duration", 1e-9)
    if args.kind == "image":
        if start:
            raise ValueError("Image source_start must be zero")
        if args.role == "broll" and duration is None:
            raise ValueError("Image B-roll needs explicit --duration")
    else:
        available = seconds(checked.get("media_duration"), "verified media duration", 1e-9)
        if start >= available:
            raise ValueError("source_start is outside the verified media stream")
        remaining = audio_available(source, start) if args.kind == "audio" and start else available - start
        if duration is None:
            duration = remaining
        if duration > remaining + 1e-6:
            raise ValueError("Selected duration exceeds the actual verified source range")
    result = {"source_start": start}
    if duration is not None:
        result["duration"] = duration
    if args.role == "broll" or args.at is not None:
        result["at"] = seconds(0 if args.at is None else args.at, "at")
    return result


def range_errors(row):
    try:
        start = seconds(row.get("source_start", 0), "source_start")
        length = row.get("duration")
        if length is not None:
            length = seconds(length, "duration", 1e-9)
        if row["kind"] == "image":
            if start:
                raise ValueError("Image source_start must be zero")
            if row["role"] == "broll" and length is None:
                raise ValueError("Image B-roll duration is required")
        elif length is not None:
            available = row.get("technical_verification", {}).get("media_duration")
            if available is not None and start + length > seconds(available, "verified media duration", 1e-9) + 1e-6:
                raise ValueError("Declared range exceeds verified media duration")
        if row["role"] == "broll":
            seconds(row.get("at", 0), "at")
    except (ValueError, KeyError) as exc:
        return [str(exc)]
    return []


def event(data, action, ident, **extra):
    revision = data.get("revision", 0)
    if type(revision) is not int or revision < 0:
        raise ValueError("Invalid registry revision")
    data["revision"] = revision + 1
    data["updated_at"] = now()
    data.setdefault("events", []).append({"revision": data["revision"], "at": data["updated_at"],
                                         "action": action, "asset_id": ident, **extra})


def find(data, ident):
    row = next((item for item in data["assets"] if item["id"] == ident), None)
    if row is None:
        raise ValueError(f"Unknown asset ID: {ident}")
    return row


def init(path):
    path = Path(path).expanduser().resolve()
    with locked(path):
        if path.exists():
            raise ValueError("Registry exists; init never overwrites it")
        atomic_write(path, {"version": 1, "revision": 0, "created_at": now(), "assets": [], "events": []})
    return {"status": "initialized", "registry": str(path)}


def add(args):
    path = Path(args.registry).expanduser().resolve()
    with locked(path):
        path, data = load(path)
        if any(row["id"] == args.id for row in data["assets"]):
            raise ValueError("Asset ID is immutable and already registered")
        if not args.id.strip() or not args.logical_id.strip():
            raise ValueError("IDs must be nonempty")
        if ((args.role == "shot_video" and args.kind != "video")
                or (args.role in ("character", "scene", "keyframe") and args.kind != "image")
                or (args.role in ("dialogue", "voiceover", "music", "sfx", "ambience") and args.kind != "audio")):
            raise ValueError("Asset kind conflicts with its production role")
        source = local_file(args.file)
        if source == path:
            raise ValueError("Registry cannot be its own media asset")
        checked = technical(source, args.kind)
        sha = digest(source)
        if checked.get("sha256") != sha:
            raise ValueError("Source changed during technical verification")
        selected = selected_range(args, checked, source)
        references = list(dict.fromkeys(args.reference or []))
        for ident in references:
            unchanged(find(data, ident), path.parent)
        provenance = {"provider": "user_supplied", "task_id": None,
                      "trust_boundary": "Registered existing local material; no generation claim"}
        if args.receipt:
            provenance = receipt_record(args.receipt, args.task_id, args.provider, sha, args.request_id, source)
            if any(row.get("provenance", {}).get("provider") == provenance["provider"]
                   and row.get("provenance", {}).get("task_id") == provenance["task_id"]
                   and row.get("provenance", {}).get("request_id") == provenance.get("request_id")
                   and row["sha256"] == sha for row in data["assets"]):
                raise ValueError("Same provider task and asset bytes are already registered")
        elif args.task_id or args.provider or args.request_id:
            raise ValueError("Provider/task claims require a matching saved completion receipt")
        version = max([row["version"] for row in data["assets"] if row["logical_id"] == args.logical_id] or [0]) + 1
        record = {"id": args.id, "logical_id": args.logical_id, "version": version,
                  "kind": args.kind, "role": args.role, "path": str(source),
                  "status": "verified", "sha256": sha, "bytes": source.stat().st_size,
                  "reference_asset_ids": references, "review": {"status": "pending", "evidence": [], "evidence_files": [], "hard_failures": []},
                  "technical_verification": checked, "provenance": provenance, "created_at": now(), **selected}
        if args.shot_id:
            record["shot_id"] = args.shot_id
        data["assets"].append(record)
        errors = structure_errors(data)
        if errors:
            raise ValueError("; ".join(errors))
        event(data, "add", args.id, logical_id=args.logical_id, version=version)
        atomic_write(path, data)
    return {"status": "verified_asset_registered", "asset": record}


def verify(registry, ident=None):
    path = Path(registry).expanduser().resolve()
    with locked(path):
        path, data = load(path)
        rows = [find(data, ident)] if ident else data["assets"]
        for row in rows:
            source = unchanged(row, path.parent)
            row["technical_verification"] = technical(source, row["kind"])
            if row["technical_verification"].get("sha256") != row["sha256"] or digest(source) != row["sha256"]:
                raise ValueError("Source changed during technical verification")
            if row["status"] == "generated":
                row["status"] = "verified"
        event(data, "verify", ident, count=len(rows))
        atomic_write(path, data)
    return {"status": "technical_verification_passed", "assets": [row["id"] for row in rows],
            "creative_review": "not_performed"}


def review(registry, ident, evidence_path):
    path = Path(registry).expanduser().resolve()
    with locked(path):
        path, data = load(path)
        row = find(data, ident)
        unchanged(row, path.parent)
        evidence_path = local_file(evidence_path)
        if evidence_path == path:
            raise ValueError("Registry cannot serve as its own mutable review evidence")
        raw = read_json(evidence_path)
        if not isinstance(raw, dict) or raw.get("status") != "reviewed":
            raise ValueError("Review file must explicitly record status=reviewed")
        observations, files, failures = raw.get("observations"), raw.get("evidence"), raw.get("hard_failures", [])
        if not isinstance(observations, list) or not observations or not all(isinstance(item, str) and item.strip() for item in observations):
            raise ValueError("Review requires nonempty actual observations, not a boolean")
        if not isinstance(files, list) or not files:
            raise ValueError("Review requires real evidence files")
        if not isinstance(failures, list) or not all(isinstance(item, str) and item.strip() for item in failures):
            raise ValueError("hard_failures must contain nonempty strings")
        proofs = []
        for item in files:
            item = {"path": item, "note": "See recorded observations"} if isinstance(item, str) else item
            if not isinstance(item, dict) or not isinstance(item.get("note"), str) or not item["note"].strip():
                raise ValueError("Each evidence file needs an observation note")
            proof = local_file(item.get("path"), evidence_path.parent)
            if proof == path:
                raise ValueError("Registry cannot serve as its own mutable review evidence")
            proofs.append({"path": str(proof), "sha256": digest(proof), "note": item["note"]})
        if row["review"].get("status") == "reviewed":
            row.setdefault("review_history", []).append(row["review"])
        row["review"] = {"status": "reviewed", "evidence": observations, "evidence_files": proofs,
                         "hard_failures": failures, "record_path": str(evidence_path),
                         "record_sha256": digest(evidence_path), "recorded_at": now(),
                         "trust_boundary": "Host-declared actual observations; utility does not perform visual/listening review"}
        row["status"] = "rejected" if failures else "reviewed"
        event(data, "review", ident, hard_failures=len(failures))
        atomic_write(path, data)
    return {"status": row["status"], "asset_id": ident}


def proof_errors(row):
    review = row.get("review", {})
    errors = []
    if (review.get("status") != "reviewed" or not isinstance(review.get("evidence"), list)
            or not review["evidence"] or not all(isinstance(s, str) and s.strip() for s in review["evidence"])):
        errors.append("Actual observation evidence is missing")
    if review.get("hard_failures"):
        errors.append("Hard failures prevent selection")
    if not isinstance(review.get("hard_failures"), list):
        errors.append("Malformed hard_failures")
    files = review.get("evidence_files")
    if not isinstance(files, list) or not files:
        errors.append("Evidence files are missing")
        files = []
    for proof in files:
        try:
            if not isinstance(proof, dict) or not isinstance(proof.get("note"), str) or not proof["note"].strip():
                raise ValueError("Evidence file requires an actual observation note")
            source = local_file(proof["path"])
            if digest(source) != proof["sha256"]:
                errors.append("Review evidence file changed")
        except (ValueError, OSError, KeyError, TypeError) as exc:
            errors.append(f"Invalid review evidence: {exc}")
    record = review.get("record_path")
    try:
        if not record or digest(local_file(record)) != review.get("record_sha256"):
            errors.append("Review record changed or is missing")
    except (ValueError, OSError):
        errors.append("Review record changed or is missing")
    return errors


def select(registry, ident):
    path = Path(registry).expanduser().resolve()
    with locked(path):
        path, data = load(path)
        row = find(data, ident)
        unchanged(row, path.parent)
        checked = row.get("technical_verification", {})
        if checked.get("state") != "verified" or checked.get("sha256") != row["sha256"]:
            raise ValueError("Technical verification is missing or stale")
        errors = proof_errors(row)
        errors.extend(provenance_errors(row))
        errors.extend(range_errors(row))
        if errors:
            raise ValueError("; ".join(errors))
        for reference in row.get("reference_asset_ids", []):
            unchanged(find(data, reference), path.parent)
        retired = []
        for old in data["assets"]:
            same_target = old["logical_id"] == row["logical_id"]
            same_shot = row.get("shot_id") and old.get("shot_id") == row["shot_id"] and old["role"] == row["role"]
            if old["id"] != ident and old["status"] == "selected" and (same_target or same_shot):
                old["status"] = "reviewed"
                retired.append(old["id"])
        if row["status"] == "selected" and not retired:
            return {"status": "already_selected", "asset_id": ident, "registry_unchanged": True}
        row["status"] = "selected"
        event(data, "select", ident, retired_selected_versions=retired)
        atomic_write(path, data)
    return {"status": "selected", "asset_id": ident, "retired_selected_versions": retired}


def validate(registry, full_media=False):
    path, data = load(registry)
    errors = []
    for row in data["assets"]:
        try:
            source = unchanged(row, path.parent)
            errors.extend(f"{row['id']}: {message}" for message in range_errors(row))
            if full_media:
                technical(source, row["kind"])
            if row["status"] in ("reviewed", "selected"):
                errors.extend(f"{row['id']}: {message}" for message in proof_errors(row))
                checked = row.get("technical_verification", {})
                if checked.get("state") != "verified" or checked.get("sha256") != row["sha256"]:
                    errors.append(f"{row['id']}: technical verification is missing or stale")
            errors.extend(f"{row['id']}: {message}" for message in provenance_errors(row))
        except (ValueError, OSError) as exc:
            errors.append(f"{row['id']}: {exc}")
    return {"schema": "production-asset-registry.validation/v1", "registry": str(path),
            "registry_sha256": digest(path), "revision": data.get("revision", 0),
            "status": "valid_pending_actual_creative_review" if not errors else "invalid",
            "assets": len(data["assets"]), "selected": [row["id"] for row in data["assets"] if row["status"] == "selected"],
            "errors": errors, "full_media_decode_requested": full_media,
            "creative_review": "Not performed by this utility; host observations remain an explicit trust boundary"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init"); p.add_argument("registry")
    p = sub.add_parser("add"); p.add_argument("registry")
    p.add_argument("--id", required=True); p.add_argument("--logical-id", required=True)
    p.add_argument("--file", required=True); p.add_argument("--kind", choices=sorted(KINDS), required=True)
    p.add_argument("--role", choices=sorted(ROLES), required=True); p.add_argument("--shot-id")
    p.add_argument("--source-start", type=float, default=0); p.add_argument("--duration", type=float); p.add_argument("--at", type=float)
    p.add_argument("--reference", action="extend", nargs="+", default=[]); p.add_argument("--receipt")
    p.add_argument("--task-id"); p.add_argument("--request-id"); p.add_argument("--provider")
    p = sub.add_parser("verify"); p.add_argument("registry"); p.add_argument("id", nargs="?")
    p = sub.add_parser("review"); p.add_argument("registry"); p.add_argument("id"); p.add_argument("--evidence", required=True)
    p = sub.add_parser("select"); p.add_argument("registry"); p.add_argument("id")
    p = sub.add_parser("validate"); p.add_argument("registry"); p.add_argument("--full-media", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "init": result = init(args.registry)
        elif args.command == "add": result = add(args)
        elif args.command == "verify": result = verify(args.registry, args.id)
        elif args.command == "review": result = review(args.registry, args.id, args.evidence)
        elif args.command == "select": result = select(args.registry, args.id)
        else: result = validate(args.registry, args.full_media)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 10 if result.get("errors") else 0
    except (ValueError, OSError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
