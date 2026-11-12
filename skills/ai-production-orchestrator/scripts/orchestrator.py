#!/usr/bin/env python3
"""Durable local production DAG and host-agent tool handoff. No network client."""
from __future__ import annotations
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import uuid

VERSION = "1.0.0"
IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")
REF = re.compile(r"^@([A-Za-z][A-Za-z0-9_-]{0,79}):([A-Za-z][A-Za-z0-9_.-]{0,79})$")
KINDS = {"local", "agent", "provider", "review"}
ACTIVE_PHASES = {"submit_intent", "submitted", "uncertain", "result_recovered"}

def now():
    return datetime.now(timezone.utc).isoformat()

def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"),
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON")))

def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

def finite(value, label, low=0):
    if type(value) not in (int, float) or not math.isfinite(value) or value < low:
        raise ValueError(f"{label} must be a finite number >= {low}")
    return float(value)

def digest(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f"Symlink artifact/input rejected: {path}")
    if path.is_file():
        value = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                value.update(block)
        return {"sha256": value.hexdigest(), "bytes": path.stat().st_size, "type": "file"}
    if path.is_dir():
        files = []
        for item in sorted(path.rglob("*")):
            if item.is_symlink():
                raise ValueError(f"Directory contains a symlink: {item}")
            if item.is_file():
                row = digest(item)
                files.append({"path": item.relative_to(path).as_posix(), **row})
        if not files:
            raise ValueError(f"Empty artifact directory: {path}")
        encoded = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return {"sha256": hashlib.sha256(encoded).hexdigest(),
                "bytes": sum(row["bytes"] for row in files), "type": "directory", "files": len(files)}
    raise ValueError(f"Missing artifact/input: {path}")

def absolute(value, project):
    if not isinstance(value, str) or not value:
        raise ValueError("A nonempty local path is required")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        raise ValueError("Network paths are not artifact files")
    path = Path(value).expanduser()
    return (project / path if not path.is_absolute() else path).resolve()

def inside(path, root):
    return Path(path).resolve().is_relative_to(Path(root).resolve())

def pid_alive(pid):
    """Read-only liveness check. os.kill(pid, 0) is unsafe on Windows."""
    if type(pid) is not int or pid <= 0 or pid > 0xffffffff:
        raise ValueError("Lock PID is invalid")
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() != 87  # Only ERROR_INVALID_PARAMETER proves absent PID.
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

class ProjectLock:
    def __init__(self, project):
        self.path = Path(project) / ".production.lock"
        self.token = uuid.uuid4().hex
    def __enter__(self):
        # Kernel locks are released on process death. Never rename/unlink a lock:
        # a stale-file compare/rename can accidentally displace a new live owner.
        if self.path.is_symlink():
            raise ValueError("Project lock cannot be a symlink")
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            os.close(descriptor)
            raise ValueError("Project lock must be an independent regular file")
        self.stream = os.fdopen(descriptor, "r+b")
        try:
            if self.path.stat().st_size == 0:
                self.stream.write(b" ")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.stream.seek(0)
            metadata = {"pid": os.getpid(), "host": socket.gethostname(), "token": self.token,
                        "created": now(), "locking": "msvcrt" if os.name == "nt" else "flock"}
            data = b" " + json.dumps(metadata).encode("utf-8")
            self.stream.write(data); self.stream.truncate(); self.stream.flush()
            os.fsync(self.stream.fileno())
            return self
        except Exception as exc:
            self.stream.close()
            raise ValueError("Project is held by another live OS lock, or locking is unavailable") from exc
    def __exit__(self, *_):
        try:
            self.stream.seek(0)
            metadata = {"pid": os.getpid(), "host": socket.gethostname(), "token": self.token,
                        "released": now(), "locking": "msvcrt" if os.name == "nt" else "flock"}
            self.stream.write(b" " + json.dumps(metadata).encode()); self.stream.truncate(); self.stream.flush()
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0); msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()

def references(value):
    if isinstance(value, str):
        match = REF.fullmatch(value)
        return [match.groups()] if match else []
    if isinstance(value, list):
        return [ref for item in value for ref in references(item)]
    if isinstance(value, dict):
        return [ref for item in value.values() for ref in references(item)]
    return []

def validate_spec(spec):
    if not isinstance(spec, dict) or type(spec.get("version")) is not int or spec["version"] != 1:
        raise ValueError("project.json requires integer version=1")
    if not isinstance(spec.get("id"), str) or not IDENT.fullmatch(spec["id"]):
        raise ValueError("Project id must be a safe nonempty identifier")
    if spec.get("kind") not in {"talking_head", "story", "mixed"}:
        raise ValueError("kind must be talking_head, story or mixed")
    if not isinstance(spec.get("brief", {}), dict):
        raise ValueError("brief must be an object")
    if not isinstance(spec.get("steps"), list) or not spec["steps"]:
        raise ValueError("A nonempty steps DAG is required")
    steps = {}
    for step in spec["steps"]:
        if not isinstance(step, dict) or not isinstance(step.get("id"), str) or not IDENT.fullmatch(step["id"]):
            raise ValueError("Every stage needs a safe id")
        ident = step["id"]
        if ident in steps:
            raise ValueError(f"Duplicate stage id: {ident}")
        if step.get("kind") not in KINDS:
            raise ValueError(f"Unknown stage kind: {ident}")
        if not isinstance(step.get("action"), str) or not step["action"].strip():
            raise ValueError(f"Stage action is required: {ident}")
        if not isinstance(step.get("params", {}), dict) or not isinstance(step.get("inputs", []), list):
            raise ValueError(f"Stage params/inputs have wrong types: {ident}")
        if not all(isinstance(x, str) for x in step.get("inputs", [])):
            raise ValueError("inputs must be local paths or @stage:role references")
        needs = step.get("needs", [])
        if not isinstance(needs, list) or len(needs) != len(set(needs)) or not all(isinstance(x, str) for x in needs):
            raise ValueError(f"Invalid stage needs: {ident}")
        checks = step.get("required_checks", [])
        if not isinstance(checks, list) or not all(isinstance(x, str) and x.strip() for x in checks) or len(checks) != len(set(checks)):
            raise ValueError(f"Invalid required_checks: {ident}")
        if step["kind"] == "review" and not checks:
            raise ValueError("Review stages must declare nonempty required_checks")
        if step["kind"] == "provider":
            media_type = step.get("expected_media", step.get("params", {}).get("expected_media"))
            if media_type not in {"image", "video", "audio"}:
                raise ValueError("Provider stages must declare expected_media=image/video/audio")
        steps[ident] = step
    order, visiting, visited = [], set(), set()
    def visit(ident):
        if ident not in steps:
            raise ValueError(f"Unknown dependency: {ident}")
        if ident in visiting:
            raise ValueError("Stage dependency cycle")
        if ident in visited:
            return
        visiting.add(ident)
        for parent in steps[ident].get("needs", []):
            visit(parent)
        visiting.remove(ident)
        visited.add(ident)
        order.append(ident)
    for ident in steps:
        visit(ident)
    def ancestors(ident):
        found = set()
        for parent in steps[ident].get("needs", []):
            found.add(parent)
            found.update(ancestors(parent))
        return found
    for ident, step in steps.items():
        for parent, _ in references([step.get("params", {}), step.get("inputs", [])]):
            if parent not in ancestors(ident):
                raise ValueError(f"Reference @{parent} must be a declared ancestor of {ident}")
    limits = spec.get("limits", {})
    if not isinstance(limits, dict):
        raise ValueError("limits must be an object")
    for key, default in (("max_generation_calls", 8), ("max_repair_rounds", 2), ("max_attempts_per_input", 3)):
        value = limits.get(key, default)
        if type(value) is not int or value < 0 or (key == "max_attempts_per_input" and value < 1):
            raise ValueError(f"Invalid {key}")
    finite(limits.get("max_generation_credits", 80), "max_generation_credits")
    if not isinstance(spec.get("permissions", {}), dict) or type(spec.get("permissions", {}).get("generate_media", False)) is not bool:
        raise ValueError("permissions.generate_media must be a boolean")
    return steps, order

def new_stage():
    return {"status": "pending", "attempt": 0, "history": [], "artifacts": []}

class Workflow:
    def __init__(self, project):
        self.project = Path(project).resolve()
        self.spec = read_json(self.project / "project.json")
        self.steps, self.order = validate_spec(self.spec)
        self.state = read_json(self.project / "state.json")
        if self.state.get("project_id") != self.spec["id"]:
            raise ValueError("Project id cannot change across saved state")
        for ident in self.steps:
            self.state["steps"].setdefault(ident, new_stage())
        removed = set(self.state["steps"]) - set(self.steps)
        if removed:
            raise ValueError("Removing stages requires a new project; do not discard saved transactions")
        self.changed = False
    @property
    def limits(self):
        return {"max_generation_calls": 8, "max_generation_credits": 80,
                "max_repair_rounds": 2, "max_attempts_per_input": 3, **self.spec.get("limits", {})}
    def event(self, action, **data):
        self.state.setdefault("events", []).append({"at": now(), "action": action, **data})
        self.changed = True
    def save(self):
        self.state["updated"] = now()
        atomic_json(self.project / "state.json", self.state)
        self.changed = False
    def stage(self, ident):
        if ident not in self.steps:
            raise ValueError(f"Unknown stage: {ident}")
        return self.state["steps"][ident]
    def resolve(self, value):
        if isinstance(value, str):
            match = REF.fullmatch(value)
            if match:
                parent, role = match.groups()
                rows = [a for a in self.stage(parent).get("artifacts", []) if a["role"] == role]
                if len(rows) != 1 or self.stage(parent)["status"] != "succeeded":
                    raise ValueError(f"Unavailable artifact reference: {value}")
                return rows[0]["path"]
            return value
        if isinstance(value, list):
            return [self.resolve(item) for item in value]
        if isinstance(value, dict):
            return {key: self.resolve(item) for key, item in value.items()}
        return value
    def input_path(self, value, base=None):
        path = absolute(value, self.project if base is None else base)
        if (path == self.project or self.project.is_relative_to(path)) and path.is_dir():
            raise ValueError("Input directories cannot contain the project control/output tree")
        if path.parent == self.project and path.name in {"project.json", "state.json", "actions.json", ".production.lock"}:
            raise ValueError("Project control files cannot be hashed as their own stage inputs")
        return path
    def signature(self, ident):
        step = self.steps[ident]
        try:
            params = self.resolve(step.get("params", {}))
        except ValueError:
            params = step.get("params", {})
        inputs = []
        for value in step.get("inputs", []):
            try:
                resolved = self.resolve(value)
                path = self.input_path(resolved)
                inputs.append({"path": str(path), **digest(path)})
            except ValueError as exc:
                inputs.append({"reference": value, "unavailable": str(exc)})
        # Track source files inside the known contracts, not merely their JSON.
        # This is data traversal, never arbitrary Python/template execution.
        nested, seen = [], set()
        known_keys = {"job", "job_path", "board", "board_path", "registry", "registry_path",
                      "asset_registry", "audio_plan", "transcript", "video", "input", "source",
                      "media", "srt", "lut", "model", "file", "files", "path", "qc_report",
                      "review_summary", "final_video", "subject"}
        def add_file(value, base, depth=0):
            if not isinstance(value, str) or not value or depth > 5:
                return
            try:
                value = self.resolve(value)
                candidate = self.input_path(value, base)
                key = str(candidate)
                if key in seen:
                    return
                seen.add(key)
                row = {"path": key, **digest(candidate)}
                nested.append(row)
                if candidate.is_file() and candidate.suffix.lower() == ".json":
                    content = read_json(candidate)
                    if isinstance(content, dict):
                        for collection, field in (("clips", "source"), ("broll", "source"),
                                                  ("tracks", "source"), ("assets", "path")):
                            for item in content.get(collection, []) if isinstance(content.get(collection, []), list) else []:
                                if isinstance(item, dict) and item.get(field):
                                    add_file(item[field], candidate.parent, depth + 1)
                        grade = content.get("grade", {})
                        if isinstance(grade, dict) and grade.get("lut"):
                            add_file(grade["lut"], candidate.parent, depth + 1)
                        if isinstance(content.get("source"), str):
                            add_file(content["source"], candidate.parent, depth + 1)
            except (ValueError, OSError, TypeError):
                nested.append({"path": str(value), "unavailable": True})
        def param_files(value, key=None):
            if isinstance(value, dict):
                for name, item in value.items():
                    param_files(item, name)
            elif isinstance(value, list):
                for item in value:
                    param_files(item, key)
            elif key in known_keys and isinstance(value, str):
                add_file(value, self.project)
        for value in step.get("inputs", []):
            add_file(value, self.project)
        param_files(params)
        dynamic = self.stage(ident).get("dynamic_inputs", self.stage(ident).get("result", {}).get("metrics", {}).get("input_files", []))
        for item in dynamic:
            if isinstance(item, dict) and item.get("path"):
                add_file(item["path"], self.project)
        dependencies = {parent: [{"role": a["role"], "sha256": a["sha256"]}
                                 for a in self.stage(parent).get("artifacts", [])]
                        for parent in step.get("needs", [])}
        adapter = Path(__file__).with_name("pipeline_adapter.py")
        code_hash = hashlib.sha256(adapter.read_bytes()).hexdigest() if step["kind"] == "local" and adapter.is_file() else None
        record = {"step": step, "resolved_params": params, "inputs": inputs, "engine_version": VERSION,
                  "local_adapter_sha256": code_hash,
                  "transitive_inputs": sorted(nested, key=lambda row: row["path"]),
                  "dependencies": dependencies, "brief": self.spec.get("brief", {})}
        return hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    def closure(self, ident):
        found = {ident}
        while True:
            more = {key for key, step in self.steps.items() if set(step.get("needs", [])) & found}
            if more <= found:
                return found
            found |= more
    def reset(self, identifiers, reason):
        for ident in self.order:
            if ident not in identifiers:
                continue
            stage = self.stage(ident)
            external = stage.get("external", {})
            if self.steps[ident]["kind"] == "provider" and external.get("phase") in ACTIVE_PHASES:
                stage.update(status="blocked", block_reason="input_changed_with_unresolved_provider", stale_inputs=True)
                continue
            if stage["status"] != "pending" or stage.get("request_id"):
                history = stage.setdefault("history", [])
                history.append({key: copy.deepcopy(value) for key, value in stage.items() if key != "history"})
                replacement = new_stage()
                replacement["attempt"] = stage["attempt"]
                replacement["history"] = history
                if stage.get("dynamic_inputs"):
                    replacement["dynamic_inputs"] = stage["dynamic_inputs"]
                self.state["steps"][ident] = replacement
        self.event("invalidate", stages=sorted(identifiers), reason=reason)
    def refresh(self):
        invalid = set()
        for ident in self.order:
            stage = self.stage(ident)
            if stage["status"] == "running":
                stage.update(status="failed", error="Interrupted local attempt; inspect and retry into a new attempt directory")
                self.event("interrupted_local", step=ident)
            if stage.get("input_fingerprint") and stage["input_fingerprint"] != self.signature(ident):
                invalid |= self.closure(ident)
                continue
            if stage["status"] == "succeeded":
                try:
                    if not stage.get("artifacts"):
                        raise ValueError("No saved artifact")
                    for artifact in stage["artifacts"]:
                        if digest(artifact["path"])["sha256"] != artifact["sha256"]:
                            raise ValueError("Artifact hash changed")
                    for evidence in stage.get("evidence", []):
                        if digest(evidence["path"])["sha256"] != evidence["sha256"]:
                            raise ValueError("Evidence hash changed")
                except ValueError:
                    if self.steps[ident]["kind"] == "provider" and stage.get("external", {}).get("phase") == "completed":
                        # Lost files are not a reason to order and pay for the same asset again.
                        stage.update(status="blocked", restore_needed=True,
                                     block_reason="restore_existing_provider_output")
                        self.event("provider_output_restore_required", step=ident)
                        descendants = self.closure(ident) - {ident}
                        if descendants:
                            self.reset(descendants, "upstream provider output is missing/modified")
                    else:
                        invalid |= self.closure(ident)
        if invalid:
            self.reset(invalid, "input, parameters, dependency or output hash changed")
        if self.changed:
            self.save()
    def ready(self, ident):
        return all(self.stage(parent)["status"] == "succeeded" for parent in self.steps[ident].get("needs", []))
    def required_inputs(self, ident):
        problems, seen = [], set()
        def inspect(value, base, depth=0):
            if depth > 5:
                return
            try:
                path = self.input_path(self.resolve(value), base)
                if str(path) in seen:
                    return
                seen.add(str(path))
                digest(path)
                if path.is_file() and path.suffix.lower() == ".json":
                    try:
                        obj = read_json(path)
                    except (ValueError, OSError):
                        return  # A host editing stage may legitimately repair invalid JSON text.
                    if not isinstance(obj, dict):
                        return
                    for collection, field in (("clips", "source"), ("broll", "source"), ("tracks", "source"), ("assets", "path")):
                        rows = obj.get(collection, [])
                        if isinstance(rows, list):
                            for row in rows:
                                if isinstance(row, dict) and row.get(field) and row.get("status") not in {"planned", "missing", "rejected"}:
                                    inspect(row[field], path.parent, depth + 1)
                    grade = obj.get("grade", {})
                    if isinstance(grade, dict) and grade.get("lut"):
                        inspect(grade["lut"], path.parent, depth + 1)
            except (ValueError, OSError) as exc:
                problems.append({"input": value, "error": str(exc)})
        for value in self.steps[ident].get("inputs", []):
            inspect(value, self.project)
        return problems
    def begin(self, ident):
        stage = self.stage(ident)
        fingerprint = self.signature(ident)
        missing = self.required_inputs(ident)
        if missing:
            stage.update(status="blocked", block_reason="missing_inputs",
                         input_fingerprint=fingerprint, missing_inputs=missing)
            self.event("missing_inputs", step=ident, inputs=missing)
            self.save()
            return False
        same = sum(1 for old in stage.get("history", []) if old.get("input_fingerprint") == fingerprint)
        if same >= self.limits["max_attempts_per_input"]:
            stage.update(status="blocked", block_reason="attempt_limit")
            self.event("attempt_limit", step=ident)
            return False
        stage["attempt"] += 1
        request = uuid.uuid4().hex
        out = self.project / "runs" / ident / f"attempt-{stage['attempt']:04d}-{request[:8]}"
        out.mkdir(parents=True, exist_ok=False)
        stage.update(request_id=request, input_fingerprint=fingerprint, output_dir=str(out),
                     status="running" if self.steps[ident]["kind"] == "local" else "waiting_external",
                     started=now(), artifacts=[])
        stage["request_params"] = self.resolve(self.steps[ident].get("params", {}))
        stage["request_inputs"] = [self.resolve(value) for value in self.steps[ident].get("inputs", [])]
        stage.pop("block_reason", None)
        stage.pop("error", None)
        self.event("begin", step=ident, request_id=request)
        self.save()
        return True
    def artifacts(self, rows, output_dir):
        if not isinstance(rows, list) or not rows:
            raise ValueError("Successful stages require real nonempty artifacts")
        result, roles, paths = [], set(), set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("role"), str) or not IDENT.fullmatch(row["role"].replace(".", "_")):
                raise ValueError("Every artifact needs a safe role")
            path = absolute(row.get("path"), self.project)
            if not inside(path, output_dir):
                raise ValueError("Stage artifact must be inside this attempt's output directory")
            if row["role"] in roles or str(path) in paths:
                raise ValueError("Artifact roles and source paths must be unique")
            roles.add(row["role"]); paths.add(str(path))
            hashed = digest(path)
            if hashed["type"] == "directory" and path == Path(output_dir).resolve():
                raise ValueError("Return artifact subdirectories, not the engine-owned attempt root")
            if hashed["type"] == "file" and not hashed["bytes"]:
                raise ValueError("A top-level artifact cannot be an empty file")
            result.append({"role": row["role"], "path": str(path), **hashed})
        return result
    def execute_local(self, ident, skills_root):
        stage, step = self.stage(ident), self.steps[ident]
        try:
            module_path = Path(__file__).with_name("pipeline_adapter.py")
            spec = importlib.util.spec_from_file_location("production_pipeline_adapter", module_path)
            if spec is None or spec.loader is None:
                raise ValueError("Local adapter is unavailable")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            result = module.execute(step["action"], self.resolve(step.get("params", {})),
                                    self.project, Path(stage["output_dir"]), Path(skills_root).resolve())
            if not isinstance(result, dict) or result.get("status") not in {"succeeded", "failed", "blocked"}:
                raise ValueError("Adapter returned an invalid result")
            if result.get("metrics", {}).get("machine_gate") == "failed":
                result["status"] = "failed"
            stage["result"] = result
            stage["dynamic_inputs"] = result.get("metrics", {}).get("input_files", [])
            stage["status"] = result["status"]
            if result["status"] == "succeeded":
                for item in result.get("metrics", {}).get("input_files", []):
                    if not isinstance(item, dict) or digest(absolute(item.get("path"), self.project))["sha256"] != item.get("sha256"):
                        raise ValueError("A dynamic input changed during execution")
                stage["artifacts"] = self.artifacts(result.get("artifacts"), stage["output_dir"])
                stage["input_fingerprint"] = self.signature(ident)
                stage["completed"] = now()
            else:
                stage["error"] = result.get("error") or result.get("reason") or "Local action did not succeed"
        except Exception as exc:
            stage.update(status="failed", error=str(exc))
        self.event("local_result", step=ident, status=stage["status"])
        self.save()
    def budget(self):
        active = [t for t in self.state.get("transactions", []) if t.get("phase") != "released"]
        return {"generation_calls": len(active),
                "credits_exposure": sum(t.get("actual_credits") if t.get("actual_credits") is not None else (t.get("estimated_credits") or 0) for t in active),
                "unknown_cost_calls": sum(t.get("estimated_credits") is None and t.get("actual_credits") is None for t in active),
                "limits": self.limits}
    def transaction(self, request):
        return next((t for t in self.state.get("transactions", []) if t["request_id"] == request), None)
    def actions(self, include_local=True):
        actions = []
        for ident in self.order:
            stage, step = self.stage(ident), self.steps[ident]
            existing_provider = (step["kind"] == "provider" and stage.get("request_id")
                                 and (stage.get("external", {}).get("phase") in ACTIVE_PHASES or stage.get("restore_needed")))
            if not self.ready(ident) and not existing_provider:
                continue
            if stage["status"] == "succeeded":
                continue
            if stage["status"] == "pending" and step["kind"] != "local":
                self.begin(ident)
            stage = self.stage(ident)
            if stage.get("block_reason") == "missing_inputs":
                actions.append({"step_id": ident, "kind": step["kind"], "action": step["action"],
                                "instruction": "resolve_missing_inputs", "missing_inputs": stage.get("missing_inputs", [])})
                continue
            if stage["status"] == "pending" and step["kind"] == "local" and include_local:
                actions.append({"step_id": ident, "kind": "local", "action": "run_local",
                                "message": "Run the fixed local adapter through orchestrator run"})
                continue
            if stage["status"] not in {"waiting_external", "blocked"} or not stage.get("request_id"):
                continue
            if step["kind"] == "local":
                continue
            external = stage.get("external", {})
            if step["kind"] == "provider":
                phase = external.get("phase")
                if stage.get("restore_needed"):
                    instruction = "restore_existing_provider_output"
                elif phase == "result_recovered":
                    instruction = "record_recovered_synchronous_result"
                elif external.get("task_id"):
                    instruction = "poll_existing_task"
                elif phase in {"submit_intent", "uncertain"}:
                    instruction = "reconcile_submission_before_any_retry"
                elif stage["status"] == "blocked":
                    instruction = "resolve_blocker"
                else:
                    instruction = "reserve_then_submit"
            else:
                instruction = "perform_review" if step["kind"] == "review" else "execute_skill"
            try:
                params = stage.get("request_params") if stage.get("stale_inputs") else self.resolve(step.get("params", {}))
                inputs = stage.get("request_inputs") if stage.get("stale_inputs") else [self.resolve(value) for value in step.get("inputs", [])]
            except ValueError:
                params, inputs = step.get("params", {}), step.get("inputs", [])
            actions.append({"project_id": self.spec["id"], "step_id": ident, "kind": step["kind"],
                            "action": step["action"], "instruction": instruction, "skill": step.get("skill"),
                            "request_id": stage["request_id"], "input_fingerprint": stage["input_fingerprint"],
                            "output_dir": stage["output_dir"], "params": params, "inputs": inputs,
                            "required_checks": step.get("required_checks", []),
                            "expected_media": step.get("expected_media", step.get("params", {}).get("expected_media")),
                            "provider_mode": step.get("provider_mode", step.get("params", {}).get("provider_mode", "asynchronous")),
                            "external": external, "stale_inputs": stage.get("stale_inputs", False)})
            saved = Path(stage["output_dir"]) / ("review-result.json" if step["kind"] == "review" else "execution-result.json")
            if saved.is_file() and not stage.get("restore_needed") and not stage.get("stale_inputs"):
                try:
                    receipt = read_json(saved)
                    if receipt.get("request_id") == stage["request_id"] and receipt.get("input_fingerprint") == stage["input_fingerprint"]:
                        actions[-1].update(instruction="recover_saved_result", saved_result=str(saved))
                except (ValueError, OSError):
                    pass
        if self.changed:
            self.save()
        return actions
    def summary(self):
        stages = {ident: self.stage(ident)["status"] for ident in self.order}
        complete = all(status == "succeeded" for status in stages.values())
        accepted_package = any(
            self.steps[ident]["action"] == "package_delivery"
            and self.stage(ident).get("result", {}).get("metrics", {}).get("acceptance") == "production_accepted"
            for ident in self.order)
        if complete:
            status = "production_accepted" if accepted_package else "technical_done_pending_review"
        elif any(value in {"pending", "running", "waiting_external"} for value in stages.values()):
            status = "in_progress_or_waiting_host"
        else:
            status = "blocked_or_failed"
        return {"schema": "ai-production-orchestrator.status/v1", "project_id": self.spec["id"],
                "status": status, "steps": stages, "budget": self.budget(),
                "repair_rounds": self.state.get("repair_rounds", 0),
                "host_agent_required_for_creative_and_provider_actions": True}
    def emit(self):
        actions = self.actions()
        result = {**self.summary(), "actions": actions}
        atomic_json(self.project / "actions.json", result)
        return result
    def run(self, skills_root, execute=True):
        self.refresh()
        if execute:
            while True:
                found = False
                for ident in self.order:
                    if self.steps[ident]["kind"] == "local" and self.stage(ident)["status"] == "pending" and self.ready(ident):
                        if self.begin(ident):
                            self.execute_local(ident, skills_root)
                        found = True
                        break
                if not found:
                    break
        return self.emit()
    def reserve(self, ident, provider, estimate, mode=None):
        self.refresh()
        step, stage = self.steps[ident], self.stage(ident)
        if step["kind"] != "provider" or not self.ready(ident):
            raise ValueError("Only ready provider stages may reserve generation")
        if stage["status"] == "pending":
            self.begin(ident)
            stage = self.stage(ident)
        existing = self.transaction(stage.get("request_id"))
        if existing:
            return {"reservation": existing, "new_submission_allowed": False, "message": "Existing intent: poll/reconcile; do not submit twice"}
        if stage["status"] != "waiting_external":
            raise ValueError("Provider stage is not waiting for a new authorized submission")
        if not self.spec.get("permissions", {}).get("generate_media", False):
            raise ValueError("Project does not authorize media generation; infer authorization only from the actual user task")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("provider is required")
        mode = mode or step.get("provider_mode") or step.get("params", {}).get("provider_mode", "asynchronous")
        if mode not in {"synchronous", "asynchronous"}:
            raise ValueError("provider_mode must be synchronous or asynchronous")
        estimate = None if estimate is None else finite(estimate, "estimated credits")
        budget = self.budget()
        if budget["generation_calls"] >= self.limits["max_generation_calls"]:
            raise ValueError("Generation call budget exhausted")
        if budget["credits_exposure"] + (estimate or 0) > self.limits["max_generation_credits"]:
            raise ValueError("Generation credits budget exhausted")
        tx = {"request_id": stage["request_id"], "step_id": ident, "provider": provider,
              "input_fingerprint": stage["input_fingerprint"], "phase": "submit_intent",
              "estimated_credits": estimate, "actual_credits": None, "created": now(),
              "mode": mode, "task_id": None,
              "cost_accounting": "known_estimate" if estimate is not None else "unknown_counted_by_calls"}
        self.state.setdefault("transactions", []).append(tx)
        stage["external"] = copy.deepcopy(tx)
        self.event("reserve_before_submission", step=ident, request_id=tx["request_id"])
        self.save()
        return {"reservation": tx, "new_submission_allowed": True}
    def submitted(self, ident, task_id):
        stage = self.stage(ident)
        tx = self.transaction(stage.get("request_id"))
        if not tx or tx["phase"] not in {"submit_intent", "submitted", "uncertain"}:
            raise ValueError("Submission requires an existing durable reservation")
        if tx.get("mode") == "synchronous":
            raise ValueError("Synchronous tools have no remote task id; record the actual tool receipt")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("A real provider task id is required")
        if tx.get("task_id") and tx["task_id"] != task_id:
            raise ValueError("Cannot replace an existing provider task id")
        tx.update(task_id=task_id, phase="submitted", submitted=now())
        stage["external"] = copy.deepcopy(tx)
        if not stage.get("stale_inputs"):
            stage["status"] = "waiting_external"
        self.event("provider_task_recorded", step=ident, task_id=task_id)
        self.save()
        return tx
    def evidence(self, rows, output_dir=None):
        if not isinstance(rows, list) or not rows:
            raise ValueError("Real evidence files and notes are required")
        result = []
        for item in rows:
            if isinstance(item, str):
                item = {"path": item, "note": "Evidence referenced by checked result"}
            if not isinstance(item, dict) or not isinstance(item.get("note"), str) or not item["note"].strip():
                raise ValueError("Evidence entries need a path and explanatory note")
            path = absolute(item.get("path"), self.project)
            hashed = digest(path)
            if not hashed["bytes"]:
                raise ValueError("Evidence cannot be an empty file")
            if output_dir and hashed["type"] == "directory" and inside(output_dir, path):
                raise ValueError("Evidence directory cannot contain this attempt's future receipt")
            result.append({"path": str(path), "note": item["note"], **hashed})
        return result
    def record(self, ident, result_path):
        self.refresh()
        stage, step = self.stage(ident), self.steps[ident]
        result = read_json(result_path)
        if stage["status"] == "succeeded" and step["kind"] != "local":
            if result.get("request_id") == stage.get("request_id") and result.get("input_fingerprint") == stage.get("input_fingerprint"):
                return {"status": "already_recorded", "step_id": ident, "artifacts": stage["artifacts"]}
        if step["kind"] == "local" or stage["status"] not in {"waiting_external", "blocked"}:
            raise ValueError("This stage is not awaiting a host result")
        if result.get("request_id") != stage.get("request_id") or result.get("input_fingerprint") != stage.get("input_fingerprint"):
            raise ValueError("Result request_id/input_fingerprint mismatch")
        if result.get("status") != "succeeded":
            raise ValueError("Use fail for failed results; record only verified success")
        output_dir = Path(stage["output_dir"])
        rows = result.get("artifacts", [])
        evidence = self.evidence(result.get("evidence", []), output_dir) if result.get("evidence") else []
        checks = {}
        if step["kind"] == "review":
            supplied = result.get("checks", {})
            if not isinstance(supplied, dict):
                raise ValueError("Review checks must be an object")
            for name in step["required_checks"]:
                check = supplied.get(name, {})
                if check.get("state") != "passed" or not isinstance(check.get("notes"), str) or not check["notes"].strip():
                    raise ValueError(f"Required review check is failed/unperformed: {name}")
                checked_evidence = self.evidence(check.get("evidence", []), output_dir)
                checks[name] = {**check, "evidence": [v["path"] for v in checked_evidence]}
                evidence.extend(checked_evidence)
            resolved_params = self.resolve(step.get("params", {}))
            subject_value = next((resolved_params.get(name) for name in ("video", "final_video", "subject")
                                  if isinstance(resolved_params.get(name), str)), None)
            input_paths = []
            for value in step.get("inputs", []):
                candidate = absolute(self.resolve(value), self.project)
                if candidate.is_file():
                    input_paths.append(candidate)
            if subject_value:
                subject = absolute(subject_value, self.project)
            else:
                videos = [p for p in input_paths if p.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".mxf"}]
                if len(videos) == 1:
                    subject = videos[0]
                elif len(input_paths) == 1:
                    subject = input_paths[0]
                else:
                    raise ValueError("Review must bind an exact declared subject/input")
            subject_hash = digest(subject)["sha256"]
            if result.get("subject_sha256") not in (None, subject_hash):
                raise ValueError("Review subject hash differs from actual media")
        else:
            if not evidence:
                raise ValueError("Host/provider success needs real evidence, not just a success flag")
            if step["kind"] == "agent" and (result.get("generation_performed") is not False):
                raise ValueError("Agent stages must declare generation_performed:false; actual media generation needs a reserved provider stage")
        provider = None
        if step["kind"] == "provider":
            provider = self.transaction(stage.get("request_id"))
            if not provider:
                raise ValueError("Provider result requires a durable budget reservation")
            if provider.get("mode") == "synchronous":
                if provider.get("phase") not in {"submit_intent", "result_recovered"}:
                    raise ValueError("Synchronous provider intent must be reconciled before result acceptance")
                receipt_value = result.get("tool_receipt")
                if not isinstance(receipt_value, str):
                    raise ValueError("Synchronous generation requires a saved raw tool receipt")
                tool_receipt = absolute(receipt_value, self.project)
                evidence.extend(self.evidence([{"path": str(tool_receipt), "note": "Actual synchronous tool receipt"}], output_dir))
                read_json(tool_receipt)
            elif provider.get("phase") != "submitted" or not provider.get("task_id"):
                raise ValueError("Asynchronous provider result requires a real durably recorded task id")
            if result.get("task_id") != provider.get("task_id") or result.get("provider_status") != "succeeded":
                raise ValueError("Provider task completion receipt mismatch")
            if result.get("actual_credits") is not None:
                finite(result["actual_credits"], "actual credits")
        artifacts = self.artifacts(rows, output_dir) if rows else []
        if any(row["role"] in {"review_result", "provider_receipt", "agent_receipt"} for row in artifacts):
            raise ValueError("Receipt roles are reserved for the engine")
        if step["kind"] != "review" and not artifacts:
            raise ValueError("Real stage output is required")
        if provider:
            expected = step.get("expected_media", step.get("params", {}).get("expected_media"))
            allowed = {
                "image": {".png", ".jpg", ".jpeg", ".webp"},
                "video": {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"},
                "audio": {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg"},
            }[expected]
            media_files = []
            for artifact in artifacts:
                path = Path(artifact["path"])
                choices = path.rglob("*") if path.is_dir() else [path]
                media_files.extend(p for p in choices if p.is_file() and p.suffix.lower() in allowed)
            if not media_files:
                raise ValueError("No actual media artifact of the declared expected_media type")
            for media_path in media_files:
                process = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-of", "json", str(media_path)],
                                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
                if process.returncode:
                    raise ValueError("Generated media does not probe successfully: " + process.stderr[-600:])
                streams = json.loads(process.stdout).get("streams", [])
                kind = "audio" if expected == "audio" else "video"
                matching = [s for s in streams if s.get("codec_type") == kind]
                if not matching or (kind == "video" and not all(s.get("width", 0) > 0 and s.get("height", 0) > 0 for s in matching)):
                    raise ValueError("Generated artifact stream type differs from expected_media")
                if not any(str(s.get("nb_read_frames", "")).isdigit() and int(s["nb_read_frames"]) > 0 for s in matching):
                    raise ValueError("Generated media has no readable frames/audio samples")
                if expected == "image":
                    decoded = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-nostdin",
                                              "-i", str(media_path), "-map", "0:v:0", "-frames:v", "1",
                                              "-f", "null", "-"], capture_output=True, text=True, timeout=120)
                    if decoded.returncode:
                        raise ValueError("Generated image cannot decode: " + decoded.stderr[-600:])
        normalized = {**result, "step_id": ident, "required_checks": step.get("required_checks", []),
                      "checks": checks if step["kind"] == "review" else result.get("checks", {}),
                      "evidence": evidence, "verified_artifacts": artifacts, "recorded_at": now()}
        unique_evidence = {}
        for item in evidence:
            unique_evidence.setdefault(item["path"], item)
        evidence = list(unique_evidence.values())
        normalized["evidence"] = evidence
        if provider:
            normalized.update(provider=provider["provider"], provider_mode=provider.get("mode", "asynchronous"),
                              task_id=provider.get("task_id"))
        if step["kind"] == "review":
            normalized.update(subject_path=str(subject), subject_sha256=subject_hash)
        receipt = output_dir / ("review-result.json" if step["kind"] == "review" else "execution-result.json")
        if receipt.exists():
            existing = read_json(receipt)
            comparable = lambda obj: {key: value for key, value in obj.items() if key != "recorded_at"}
            if comparable(existing) != comparable(normalized):
                raise ValueError("Existing saved receipt differs; never overwrite or promote mismatched results")
            normalized = existing
        else:
            atomic_json(receipt, normalized)
        role = "review_result" if step["kind"] == "review" else "provider_receipt" if provider else "agent_receipt"
        artifacts.extend(self.artifacts([{"role": role, "path": str(receipt)}], output_dir))
        if provider:
            provider["phase"] = "completed"
            if result.get("actual_credits") is not None:
                provider["actual_credits"] = finite(result["actual_credits"], "actual credits")
            stage["external"] = copy.deepcopy(provider)
        stage.update(status="succeeded", artifacts=artifacts, evidence=evidence,
                     checks=checks, completed=now(), receipt=str(receipt))
        if stage.get("stale_inputs") or stage["input_fingerprint"] != self.signature(ident):
            self.reset(self.closure(ident), "Completed provider result archived; inputs changed and it cannot be promoted")
            status = "stale_result_archived_not_promoted"
        else:
            status = "recorded"
        self.event("host_result", step=ident, status=status)
        self.save()
        return {"status": status, "step_id": ident, "artifacts": artifacts}
    def fail(self, ident, reason, terminal=False):
        stage = self.stage(ident)
        if stage["status"] not in {"waiting_external", "blocked", "running"}:
            raise ValueError("Only active stages can fail")
        tx = self.transaction(stage.get("request_id"))
        if tx:
            if not terminal:
                tx["phase"] = "uncertain"
                stage.update(status="blocked", block_reason="provider_outcome_uncertain")
            else:
                if not tx.get("task_id"):
                    raise ValueError("Unknown submission cannot be declared terminal; reconcile first")
                tx["phase"] = "failed"
                stage["status"] = "failed"
            stage["external"] = copy.deepcopy(tx)
        else:
            stage["status"] = "failed"
        stage["error"] = reason
        self.event("fail", step=ident, reason=reason, terminal=terminal)
        self.save()
    def reconcile(self, ident, outcome, evidence_path, notes, task_id=None, actual=None):
        stage = self.stage(ident)
        tx = self.transaction(stage.get("request_id"))
        if not tx or tx["phase"] not in ACTIVE_PHASES:
            raise ValueError("No unresolved provider transaction")
        proof = self.evidence([{"path": evidence_path, "note": notes}])
        if outcome == "submitted":
            self.submitted(ident, task_id)
            tx = self.transaction(stage["request_id"])
        elif outcome == "completed":
            if tx.get("mode") != "synchronous":
                raise ValueError("Asynchronous tools recover through their real task id")
            tx["phase"] = "result_recovered"
            if not stage.get("stale_inputs"):
                stage["status"] = "waiting_external"
        else:
            if outcome == "not_submitted" and tx.get("task_id"):
                raise ValueError("A known task cannot be reconciled as not submitted")
            tx["phase"] = "released" if outcome == "not_submitted" else "failed"
            if actual is not None:
                tx["actual_credits"] = finite(actual, "actual credits")
            stage["status"] = "failed"
        tx["reconciliation_evidence"] = proof
        tx["reconciled"] = now()
        stage["external"] = copy.deepcopy(tx)
        self.event("reconcile", step=ident, outcome=outcome)
        self.save()
        return tx
    def retry(self, ident, reason):
        stage = self.stage(ident)
        if stage["status"] not in {"failed", "blocked"}:
            raise ValueError("Only failed/blocked stages can retry")
        if stage.get("external", {}).get("phase") in ACTIVE_PHASES:
            raise ValueError("Provider submission is unresolved; poll/reconcile before any retry")
        if stage.get("restore_needed"):
            raise ValueError("Restore the already generated provider output before considering new generation")
        self.reset(self.closure(ident), "retry: " + reason)
        self.save()
    def invalidate(self, ident, reason):
        rounds = self.state.get("repair_rounds", 0)
        if rounds >= self.limits["max_repair_rounds"]:
            raise ValueError("Automatic repair round limit reached")
        self.state["repair_rounds"] = rounds + 1
        self.reset(self.closure(ident), "repair: " + reason)
        self.save()
    def restore(self, ident):
        stage = self.stage(ident)
        if not stage.get("restore_needed") or stage.get("external", {}).get("phase") != "completed":
            raise ValueError("No existing provider output awaiting restoration")
        if stage["input_fingerprint"] != self.signature(ident):
            raise ValueError("Inputs changed; restoration cannot promote an obsolete asset")
        for artifact in stage["artifacts"]:
            if digest(artifact["path"])["sha256"] != artifact["sha256"]:
                raise ValueError("Restored output does not match its original recorded hash")
        for evidence in stage.get("evidence", []):
            if digest(evidence["path"])["sha256"] != evidence["sha256"]:
                raise ValueError("Restored evidence does not match its original recorded hash")
        stage.update(status="succeeded", restore_needed=False)
        stage.pop("block_reason", None)
        self.event("provider_output_restored_without_generation", step=ident)
        self.save()

def initialize(spec_path, project):
    spec = read_json(spec_path)
    validate_spec(spec)
    project = Path(project).resolve()
    project.mkdir(parents=True, exist_ok=True)
    if (project / "project.json").exists() or (project / "state.json").exists():
        raise ValueError("Project already initialized; source/state will not be overwritten")
    with ProjectLock(project):
        with (project / "project.json").open("x", encoding="utf-8") as stream:
            json.dump(spec, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        atomic_json(project / "state.json", {"schema": "ai-production-orchestrator.state/v1",
                    "version": VERSION, "project_id": spec["id"], "created": now(),
                    "steps": {s["id"]: new_stage() for s in spec["steps"]},
                    "transactions": [], "repair_rounds": 0, "events": []})
    return {"project": str(project), "project_id": spec["id"], "status": "initialized"}

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    item = commands.add_parser("init"); item.add_argument("spec"); item.add_argument("--project", required=True)
    for command in ("run", "status", "next"):
        item = commands.add_parser(command); item.add_argument("project")
        if command == "run":
            item.add_argument("--skills-root", default=str(Path(__file__).resolve().parents[2]))
    item = commands.add_parser("reserve"); item.add_argument("project"); item.add_argument("step")
    item.add_argument("--provider", required=True); item.add_argument("--estimate-credits", type=float)
    item.add_argument("--mode", choices=["synchronous", "asynchronous"])
    item = commands.add_parser("submitted"); item.add_argument("project"); item.add_argument("step"); item.add_argument("--task-id", required=True)
    item = commands.add_parser("record"); item.add_argument("project"); item.add_argument("step"); item.add_argument("--result", required=True)
    item = commands.add_parser("fail"); item.add_argument("project"); item.add_argument("step"); item.add_argument("--reason", required=True); item.add_argument("--terminal", action="store_true")
    item = commands.add_parser("reconcile"); item.add_argument("project"); item.add_argument("step")
    item.add_argument("--outcome", choices=["not_submitted", "submitted", "completed", "failed"], required=True)
    item.add_argument("--evidence", required=True); item.add_argument("--notes", required=True)
    item.add_argument("--task-id"); item.add_argument("--actual-credits", type=float)
    for command in ("retry", "invalidate"):
        item = commands.add_parser(command); item.add_argument("project"); item.add_argument("step"); item.add_argument("--reason", required=True)
    item = commands.add_parser("restore"); item.add_argument("project"); item.add_argument("step")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            result = initialize(args.spec, args.project); code = 0
        else:
            project = Path(args.project).resolve()
            with ProjectLock(project):
                workflow = Workflow(project)
                workflow.refresh()
                if args.command == "run":
                    result = workflow.run(args.skills_root)
                    complete = result["status"] in {"production_accepted", "technical_done_pending_review"}
                    code = 0 if complete else 10 if result["actions"] else 20
                elif args.command in {"status", "next"}:
                    result = workflow.emit(); code = 0
                elif args.command == "reserve":
                    result = workflow.reserve(args.step, args.provider, args.estimate_credits, args.mode); workflow.emit(); code = 0
                elif args.command == "submitted":
                    result = workflow.submitted(args.step, args.task_id); workflow.emit(); code = 0
                elif args.command == "record":
                    result = workflow.record(args.step, args.result); workflow.emit(); code = 0
                elif args.command == "fail":
                    workflow.fail(args.step, args.reason, args.terminal); result = workflow.emit(); code = 0
                elif args.command == "reconcile":
                    result = workflow.reconcile(args.step, args.outcome, args.evidence, args.notes, args.task_id, args.actual_credits); workflow.emit(); code = 0
                elif args.command == "retry":
                    workflow.retry(args.step, args.reason); result = workflow.emit(); code = 0
                elif args.command == "restore":
                    workflow.restore(args.step); result = workflow.emit(); code = 0
                else:
                    workflow.invalidate(args.step, args.reason); result = workflow.emit(); code = 0
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return code
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
