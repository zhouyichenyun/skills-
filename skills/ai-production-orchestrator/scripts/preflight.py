#!/usr/bin/env python3
"""Read-only production capability probe; no uploads, installs or project changes."""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys


SKILL_FILES = {
    "video-shots": ("SKILL.md", "scripts/video-shots.mjs"),
    "cinematic-video-prompt-engineer": ("SKILL.md",),
    "talking-head-writer": ("SKILL.md",),
    "cinematic-director": ("SKILL.md",),
    "talking-head-auto-editor": ("SKILL.md", "scripts/editor.py"),
    "video-review-autofix": ("SKILL.md", "scripts/video_qc.py"),
    "audio-production-director": ("SKILL.md", "scripts/audio.py", "scripts/audio_nle.py"),
    "story-canvas-producer": ("SKILL.md", "scripts/story_board.py"),
    "ai-production-orchestrator": (
        "SKILL.md", "scripts/orchestrator.py", "scripts/pipeline_adapter.py",
        "scripts/compiler.py", "scripts/preflight.py",
    ),
}
FILTERS = (
    "subtitles", "lut3d", "loudnorm", "overlay", "silencedetect",
    "blackdetect", "freezedetect", "amix", "sidechaincompress", "alimiter",
    "adelay", "atrim",
)
MODEL_FILES = ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt")


def run(argv, timeout=25):
    try:
        result = subprocess.run(
            [str(value) for value in argv], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, "", str(exc)


def binary(name):
    executable = shutil.which(name)
    if not executable:
        return {"state": "missing", "reason": f"{name} is not on PATH"}
    arguments = [executable, "--version" if name == "node" else "-version"]
    code, stdout, stderr = run(arguments)
    if code:
        return {"state": "missing", "reason": f"{name} version probe failed", "returncode": code}
    first_line = (stdout or stderr).splitlines()
    return {"state": "ready", "executable": executable,
            "version": first_line[0] if first_line else "unknown"}


def ffmpeg_filters(ffmpeg):
    if ffmpeg["state"] != "ready":
        return {"state": "missing", "reason": "FFmpeg unavailable", "missing": list(FILTERS)}
    code, stdout, _ = run([ffmpeg["executable"], "-hide_banner", "-filters"])
    if code:
        return {"state": "missing", "reason": "FFmpeg filter enumeration failed", "missing": list(FILTERS)}
    available = set()
    for line in stdout.splitlines():
        match = re.match(r"\s*[TSC.]{3}\s+([A-Za-z0-9_]+)\s+", line)
        if match:
            available.add(match[1])
    missing = [name for name in FILTERS if name not in available]
    return {"state": "missing" if missing else "ready",
            "required": {name: name in available for name in FILTERS}, "missing": missing}


def python_capability(executable):
    code = (
        "import sys,json,importlib.util; "
        "print(json.dumps({'version':list(sys.version_info[:3]),"
        "'executable':sys.executable,'faster_whisper':bool(importlib.util.find_spec('faster_whisper'))}))"
    )
    status, stdout, _ = run([executable, "-I", "-c", code])
    if status:
        return {"state": "missing", "reason": "Selected Python could not run capability probe"}
    try:
        data = json.loads(stdout)
        version = data["version"]
        if not isinstance(version, list) or len(version) < 2:
            raise ValueError("Invalid Python version response")
        ready = tuple(version[:2]) >= (3, 10)
        return {
            "state": "ready" if ready else "missing", "executable": data["executable"],
            "version": ".".join(str(v) for v in version),
            "faster_whisper_available": bool(data.get("faster_whisper")),
            "reason": None if ready else "Python 3.10+ required",
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return {"state": "missing", "reason": "Python did not return a valid capability record"}


def model_capability(directory):
    if directory is None:
        return {"state": "not_checked", "reason": "No local ASR model requested; existing transcripts can bypass ASR"}
    path = Path(directory).expanduser().resolve()
    missing = [name for name in MODEL_FILES if not (path / name).is_file() or (path / name).stat().st_size == 0]
    return {
        "state": "missing" if missing else "ready", "path": str(path), "missing_files": missing,
        "scope": "Files and nonzero sizes only; model load/accuracy is checked when transcribing",
    }


def skill_capability(root):
    paths, missing = {}, []
    for name, required in SKILL_FILES.items():
        absent = [relative for relative in required if not (root / name / relative).is_file()]
        paths[name] = {"state": "missing" if absent else "ready", "missing_files": absent}
        if absent:
            missing.append(name)
    return {"state": "missing" if missing else "ready", "root": str(root),
            "expected_count": len(SKILL_FILES), "skills": paths, "missing": missing,
            "scope": "Required entrypoints present; provenance/hash verification belongs to the installer"}


def resolve_candidates():
    paths = []
    configured = os.environ.get("RESOLVE_SCRIPT_API")
    if configured:
        base = Path(configured).expanduser()
        paths.extend((base / "Modules", base))
    if sys.platform == "win32":
        programdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        paths.append(Path(programdata) / "Blackmagic Design/DaVinci Resolve/Support/Developer/Scripting/Modules")
    elif sys.platform == "darwin":
        paths.append(Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"))
    else:
        paths.append(Path("/opt/resolve/Developer/Scripting/Modules"))
    # De-duplicate paths without opening credential stores or enumerating environment values.
    return list(dict.fromkeys(str(p) for p in paths))


def resolve_capability(probe):
    candidates = resolve_candidates()
    modules = [path for path in candidates if (Path(path) / "DaVinciResolveScript.py").is_file()]
    configured_names = [name for name in ("RESOLVE_SCRIPT_API", "RESOLVE_SCRIPT_LIB", "PYTHONPATH") if os.environ.get(name)]
    result = {
        "state": "not_checked", "connected": False, "module_found": bool(modules),
        "module_directories": modules, "configured_setting_names": configured_names,
        "connection_scope": "This execution host only; not a remote connection to the user's Windows computer",
        "software_ui_verified": False, "project_modified": False,
    }
    if not probe:
        result["reason"] = "No connection attempted; use --probe-resolve for a read-only API check"
        return result
    # Run in an isolated child so Resolve native imports cannot take down the prober.
    # The only Resolve calls are scriptapp, GetVersionString and GetProjectManager.
    code = (
        "import sys,json,importlib.util; "
        f"sys.path[:0]={modules!r}; "
        "spec=importlib.util.find_spec('DaVinciResolveScript'); "
        "assert spec is not None, 'Resolve Python module unavailable'; "
        "import DaVinciResolveScript as dvr; "
        "app=dvr.scriptapp('Resolve'); "
        "assert app is not None, 'Resolve is not running or scripting is unavailable'; "
        "manager=app.GetProjectManager(); "
        "assert manager is not None, 'Project manager unavailable'; "
        "print(json.dumps({'connected':True,'version':str(app.GetVersionString())}))"
    )
    status, stdout, _ = run([sys.executable, "-I", "-c", code], timeout=12)
    if status == 0:
        try:
            data = json.loads(stdout)
            if data.get("connected") is True:
                result.update(state="ready", connected=True, version=data.get("version"),
                              reason="Read-only local API connection established; no project opened, saved or modified")
                return result
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    result.update(state="not_connected", reason="Read-only local API probe failed or timed out; check this host's official Resolve scripting setup")
    return result


def inspect(skills_root, asr_python=None, model=None, probe_resolve=False):
    root = Path(skills_root).expanduser().resolve()
    python = python_capability(sys.executable)
    ffmpeg, ffprobe = binary("ffmpeg"), binary("ffprobe")
    filters, skills, node = ffmpeg_filters(ffmpeg), skill_capability(root), binary("node")
    if node["state"] == "ready":
        match = re.search(r"\bv?(\d+)\.", node.get("version", ""))
        if not match or int(match[1]) < 18:
            node.update(state="missing", reason="Node.js 18+ required for video-shots")
    asr_interpreter = python_capability(str(asr_python or sys.executable))
    if asr_interpreter["state"] == "ready":
        asr = {"state": "ready" if asr_interpreter.get("faster_whisper_available") else "missing",
               "python": asr_interpreter["executable"],
               "scope": "Module discovery only; model loading and transcription are verified during the real stage",
               "reason": "Local module found" if asr_interpreter.get("faster_whisper_available") else "Use existing transcript to bypass missing optional local ASR"}
    else:
        asr = {"state": "missing", "reason": "Requested ASR interpreter unavailable"}
    model_status = model_capability(model)
    native = resolve_capability(probe_resolve)
    capabilities = {
        "python": python, "ffmpeg": ffmpeg, "ffprobe": ffprobe,
        "ffmpeg_filters": filters, "skills": skills, "node": node,
        "asr": asr, "asr_model": model_status, "resolve": native,
        "generation_services": {
            "state": "not_checked",
            "reason": "Host agent must check connected MCP/image/video/audio services and actual account permissions",
        },
        "fonts_and_color": {
            "state": "not_checked",
            "reason": "Validate rendered Chinese glyphs and source color interpretation on actual media",
        },
    }
    required = ("python", "ffmpeg", "ffprobe", "ffmpeg_filters", "skills")
    missing = [name for name in required if capabilities[name]["state"] != "ready"]
    optional = []
    if node["state"] != "ready":
        optional.append({"capability": "node", "affected_action": "video-shots", "reason": "FFmpeg-only production remains possible"})
    if asr["state"] != "ready" or model_status["state"] != "ready":
        optional.append({"capability": "local_asr", "affected_action": "transcribe", "reason": "Provide aligned transcript or prepare local ASR interpreter/model"})
    if not native["connected"]:
        optional.append({"capability": "resolve", "affected_action": "native_resolve", "reason": "Use MP4/SRT/XML delivery; native local control remains unverified"})
    return {
        "schema": "ai-production-orchestrator.preflight/v1",
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "platform": {"system": platform.system(), "machine": platform.machine(),
                     "target": "Windows Jianying/CapCut and DaVinci Resolve",
                     "running_on_windows": sys.platform == "win32",
                     "windows_target_verified": False,
                     "remote_user_desktop_connected": False},
        "local_gate": {"ready": not missing, "missing": missing,
                       "scope": "Full installed local workflow; optional features are evaluated per selected project"},
        "capabilities": capabilities, "blocking": missing, "optional": optional,
        "recommended_route": {
            "picture": "FFmpeg render and verified MP4/SRT",
            "editable": "Measured XML/EDL/WAV interchange; software UI import remains separate",
            "generation": "Host agent consumes persisted provider actions; this CLI cannot call host MCP tools",
            "review": "Machine checks plus real visual/listening evidence; neither implies native UI verification",
        },
        "read_only": True, "uploads_performed": False, "software_installed": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-root", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--python", dest="asr_python")
    parser.add_argument("--model", "--asr-model", dest="model")
    parser.add_argument("--probe-resolve", action="store_true")
    args = parser.parse_args()
    try:
        out = Path(args.out).expanduser().resolve()
        if out.exists():
            raise ValueError("Output already exists; choose a new preflight report")
        report = inspect(args.skills_root, args.asr_python, args.model, args.probe_resolve)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        print(json.dumps({"report": str(out), "local_gate": report["local_gate"],
                          "resolve_connected": report["capabilities"]["resolve"]["connected"],
                          "read_only": True}, ensure_ascii=False))
        # A report with missing features is a successful probe, not a false readiness claim.
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
