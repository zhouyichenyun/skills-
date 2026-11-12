#!/usr/bin/env python3
"""Explicitly download a pinned public faster-whisper model, verifying every file."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request

MODELS = {
    "tiny": ("Systran/faster-whisper-tiny", "d90ca5fe260221311c53c58e660288d3deb8d356"),
    "small": ("Systran/faster-whisper-small", "536b0662742c02347bc0e980a01041f333bce120"),
}
FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")

def request(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "talking-head-model-setup/1"}), timeout=90)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=MODELS, default="small")
    parser.add_argument("--dest", type=Path, required=True)
    args = parser.parse_args()
    repo, revision = MODELS[args.model]
    dest = args.dest.expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    with request(f"https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true") as response:
        metadata = json.load(response)
    records = []
    for name in FILES:
        row = next(f for f in metadata["siblings"] if f["rfilename"] == name)
        path = dest / name
        expected_sha256 = row.get("lfs", {}).get("sha256")
        def valid(candidate):
            if not candidate.is_file():
                return False
            digest = hashlib.sha256() if expected_sha256 else hashlib.sha1()
            if not expected_sha256:
                digest.update(b"blob " + str(candidate.stat().st_size).encode() + b"\0")
            with candidate.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    digest.update(block)
            return digest.hexdigest() == (expected_sha256 or row["blobId"])
        if not valid(path):
            temporary = dest / (name + ".download")
            if temporary.exists():
                raise FileExistsError(f"Existing unfinished download: {temporary}; inspect/remove it before retry")
            try:
                with request(f"https://huggingface.co/{repo}/resolve/{revision}/{name}") as response, temporary.open("xb") as stream:
                    while block := response.read(1024 * 1024):
                        stream.write(block)
                if not valid(temporary):
                    raise ValueError(f"Checksum verification failed: {name}")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        checksum = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                checksum.update(block)
        records.append({"file": name, "bytes": path.stat().st_size, "sha256": checksum.hexdigest()})
        print(f"VERIFIED {name} ({path.stat().st_size} bytes)", flush=True)
    (dest / "download-manifest.json").write_text(json.dumps({
        "repository": repo, "revision": revision, "files": records,
        "license_source": f"https://huggingface.co/{repo}/blob/{revision}/README.md",
    }, indent=2) + "\n", encoding="utf-8")
    print(f"MODEL READY: {dest}")

if __name__ == "__main__":
    main()
