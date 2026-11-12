#!/usr/bin/env python3
"""Install the repository's skill directories, keeping backups on explicit update."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SKILLS = (
    "video-shots", "cinematic-video-prompt-engineer", "talking-head-writer",
    "cinematic-director", "talking-head-auto-editor", "video-review-autofix",
    "audio-production-director", "story-canvas-producer",
)

def verify_source(name: str) -> Path:
    source = ROOT / "skills" / name
    text = (source / "SKILL.md").read_text(encoding="utf-8")
    if not re.search(r"(?m)^name:\s*" + re.escape(name) + r"\s*$", text):
        raise ValueError(f"Invalid skill name: {name}")
    for file in source.rglob("*"):
        if file.is_symlink():
            raise ValueError(f"Symbolic links are not installable: {file}")
    lock_path = ROOT / "sources.lock.json"
    if lock_path.exists():
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        record = next((s for s in lock["skills"] if s["name"] == name), None)
        if record is None:
            raise ValueError(f"No source record for {name}")
        declared = {f["path"] for f in record["files"]}
        actual = {p.relative_to(ROOT).as_posix() for p in source.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
        if declared != actual:
            raise ValueError(f"Manifest file list differs for {name}")
        for entry in record["files"]:
            relative = Path(entry["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unsafe manifest path")
            path = ROOT / relative
            if not path.resolve().is_relative_to(source.resolve()):
                raise ValueError("Manifest entry outside selected skill")
            data = path.read_bytes()
            if "sha256" in entry:
                valid = hashlib.sha256(data).hexdigest() == entry["sha256"]
            else:
                valid = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() == entry["git_blob_sha"]
            if not valid:
                raise ValueError(f"Checksum differs: {entry['path']}")
    return source

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home()/".codex")))/"skills")
    parser.add_argument("--skills", nargs="+", choices=SKILLS, default=list(SKILLS))
    parser.add_argument("--update", action="store_true", help="Back up existing selected skills, then replace")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    selected = [(name, verify_source(name)) for name in dict.fromkeys(args.skills)]
    if args.check_only:
        print(json.dumps({"verified": [n for n, _ in selected]}, ensure_ascii=False))
        return 0
    target = args.dest.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    for name, source in selected:
        dest = target / name
        if dest.exists() and not args.update:
            print(f"SKIP {name}: already installed; --update creates a backup")
            continue
        with tempfile.TemporaryDirectory(prefix=".skill-stage-", dir=target.parent) as staging:
            staged = Path(staging) / name
            shutil.copytree(source, staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            backup = None
            if dest.exists():
                stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                backup = target.parent / "skill-backups" / stamp / name
                backup.parent.mkdir(parents=True)
                shutil.move(str(dest), str(backup))
            try:
                staged.rename(dest)
            except Exception:
                if backup is not None and not dest.exists():
                    shutil.move(str(backup), str(dest))
                raise
        print(f"INSTALLED {name}" + (f"; backup: {backup}" if backup else ""))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
