#!/usr/bin/env python3
"""Real local asset-ledger regressions; no provider calls or real creative review."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import zlib

ENTRY = Path(__file__).with_name("asset_registry.py")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def png(path, color):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    header = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    pixels = (b"\0" + bytes(color) * 2) * 2
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cli(*args, ok=True):
    result = subprocess.run([sys.executable, str(ENTRY), *map(str, args)], capture_output=True, text=True)
    if ok and result.returncode not in (0, 10):
        raise AssertionError(result.stderr + result.stdout)
    if not ok and result.returncode == 0:
        raise AssertionError("Expected failure: " + " ".join(map(str, args)))
    text = result.stdout if result.returncode in (0, 10) else result.stderr
    return result, json.loads(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    root = args.out_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    checks = []
    registry = root / "assets.json"
    first, second = root / "角色 基准.png", root / "角色 新版本.png"
    png(first, (20, 50, 80)); png(second, (30, 60, 90))
    originals = {str(p): sha(p) for p in (first, second)}
    cli("init", registry); checks.append("new registry initialized")
    initial = sha(registry)
    cli("init", registry, ok=False)
    assert sha(registry) == initial; checks.append("init never overwrites existing ledger")
    def add(ident, logical, file, kind="image", role="character", *extra, ok=True):
        return cli("add", registry, "--id", ident, "--logical-id", logical, "--file", file,
                   "--kind", kind, "--role", role, *extra, ok=ok)
    add("CHAR_V1", "CHAR_MAIN", first)
    assert json.loads(registry.read_text())["assets"][0]["status"] == "verified"
    checks.append("real PNG technically verified and hashed")
    before = sha(registry)
    cli("select", registry, "CHAR_V1", ok=False)
    assert sha(registry) == before; checks.append("unreviewed asset cannot be selected")
    bad_review = root / "bad-review.json"; dump(bad_review, {"status": "reviewed", "observations": True, "evidence": True})
    cli("review", registry, "CHAR_V1", "--evidence", bad_review, ok=False)
    assert sha(registry) == before; checks.append("boolean approval is not accepted as evidence")
    proof = root / "proof.txt"; proof.write_text("Synthetic fixture structure inspection; not real identity approval.", encoding="utf-8")
    good = root / "review-v1.json"
    dump(good, {"status": "reviewed", "observations": ["Synthetic pixel fixture reviewed for ledger regression only."],
                "evidence": [{"path": "proof.txt", "note": "Fixture-only recorded inspection"}], "hard_failures": []})
    cli("review", registry, "CHAR_V1", "--evidence", good)
    cli("select", registry, "CHAR_V1")
    checks.append("reviewed evidence-backed asset can be selected")
    before = sha(registry); cli("select", registry, "CHAR_V1")
    assert sha(registry) == before; checks.append("same selected version is an idempotent no-op")
    add("CHAR_V2", "CHAR_MAIN", second)
    assert json.loads(registry.read_text())["assets"][-1]["version"] == 2
    checks.append("new immutable ID receives next logical version")
    failed_review = root / "review-rejected.json"
    dump(failed_review, {"status": "reviewed", "observations": ["Deliberate rejection fixture."],
                        "evidence": [{"path": "proof.txt", "note": "Fixture evidence"}], "hard_failures": ["Deliberate test hard failure"]})
    cli("review", registry, "CHAR_V2", "--evidence", failed_review)
    cli("select", registry, "CHAR_V2", ok=False); checks.append("hard failure prevents selection")
    cli("review", registry, "CHAR_V2", "--evidence", good)
    cli("select", registry, "CHAR_V2")
    data = json.loads(registry.read_text())
    assert [row["id"] for row in data["assets"] if row["status"] == "selected"] == ["CHAR_V2"]
    assert data["assets"][-1]["review_history"]; checks.append("selection retires old version and retains review history")
    before = sha(registry); add("CHAR_V2", "OTHER", first, ok=False)
    assert sha(registry) == before; checks.append("asset IDs cannot be overwritten")
    add("BAD_REF", "BAD_REF", first, "image", "keyframe", "--reference", "DOES_NOT_EXIST", ok=False)
    assert sha(registry) == before; checks.append("missing reference blocks registration")
    add("BAD_KIND", "BAD_KIND", first, "image", "shot_video", ok=False)
    checks.append("media kind cannot contradict production role")
    video, audio = root / "真实 测试.mp4", root / "audio.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=25:d=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1", "-shortest",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video)], check=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=330:sample_rate=48000:duration=1", "-c:a", "pcm_s24le", str(audio)], check=True)
    originals.update({str(p): sha(p) for p in (video, audio)})
    add("VIDEO_V1", "SHOT_01_VIDEO", video, "video", "shot_video", "--shot-id", "SHOT_01", "--reference", "CHAR_V2")
    cli("review", registry, "VIDEO_V1", "--evidence", good); cli("select", registry, "VIDEO_V1")
    checks.append("real video fully decoded and selected with reference IDs")
    add("AUDIO_V1", "VOICE_01", audio, "audio", "voiceover")
    checks.append("real audio stream fully decoded")
    bad_media = root / "bad.mp4"; bad_media.write_bytes(b"not a video")
    add("BAD_VIDEO", "BAD_VIDEO", bad_media, "video", "shot_video", ok=False)
    checks.append("corrupt video fails closed")
    add("BAD_AUDIO", "BAD_AUDIO", bad_media, "audio", "music", ok=False)
    checks.append("corrupt audio fails closed")
    receipt = root / "receipt.json"
    dump(receipt, {"task_id": "fixture-task-1", "provider_status": "succeeded", "provider": "synthetic-test",
                   "artifact_sha256": sha(video), "note": "Synthetic receipt fixture; no external generation called"})
    add("PROVIDER_VIDEO", "PROVIDER_VIDEO", video, "video", "broll", "--receipt", str(receipt), "--task-id", "fixture-task-1")
    checks.append("structured completion receipt bound to task and real asset hash")
    cli("review", registry, "PROVIDER_VIDEO", "--evidence", good)
    cli("select", registry, "PROVIDER_VIDEO")
    add("PROVIDER_DUP", "PROVIDER_DUP", video, "video", "broll", "--receipt", str(receipt), "--task-id", "fixture-task-1", ok=False)
    checks.append("same provider task and bytes cannot be duplicated")
    add("WRONG_RECEIPT", "WRONG_RECEIPT", audio, "audio", "music", "--receipt", str(receipt), "--task-id", "other-task", ok=False)
    checks.append("wrong task receipt rejected")
    add("NO_RECEIPT", "NO_RECEIPT", audio, "audio", "music", "--task-id", "claimed-task", ok=False)
    checks.append("provider claims require a saved receipt")
    original_video = video.read_bytes(); video.write_bytes(original_video + b"tamper")
    cli("select", registry, "VIDEO_V1", ok=False)
    result, report = cli("validate", registry)
    assert result.returncode == 10 and any("Asset bytes changed" in value for value in report["errors"])
    video.write_bytes(original_video); checks.append("asset mutation invalidates selection and ledger validation")
    original_proof = proof.read_bytes(); proof.write_bytes(original_proof + b"changed")
    cli("select", registry, "CHAR_V2", ok=False)
    proof.write_bytes(original_proof); checks.append("changed review evidence cannot approve selection")
    original_receipt = receipt.read_bytes(); receipt.write_bytes(original_receipt + b" ")
    cli("select", registry, "PROVIDER_VIDEO", ok=False)
    _, report = cli("validate", registry)
    assert any("provider receipt changed" in value.lower() for value in report["errors"])
    receipt.write_bytes(original_receipt); checks.append("receipt drift is reported")
    checks.append("already-selected asset cannot bypass changed provider receipt")
    data = json.loads(registry.read_text())
    cyclic = root / "cyclic.json"
    data["assets"][0]["reference_asset_ids"] = ["CHAR_V2"]
    data["assets"][1]["reference_asset_ids"] = ["CHAR_V1"]
    dump(cyclic, data); cli("validate", cyclic, ok=False); checks.append("reference cycles rejected")
    data = json.loads(registry.read_text()); data["assets"][0]["status"] = "selected"
    conflicting = root / "conflicting.json"; dump(conflicting, data)
    cli("validate", conflicting, ok=False); checks.append("multiple selected logical versions rejected")
    held = registry.with_name("." + registry.name + ".lock")
    module_spec = importlib.util.spec_from_file_location("registry_lock_fixture", ENTRY)
    module = importlib.util.module_from_spec(module_spec); module_spec.loader.exec_module(module)
    with module.locked(registry):
        cli("verify", registry, "CHAR_V2", ok=False)
        assert held.exists()
    checks.append("held kernel registry lock cannot be stolen or removed")
    crash_code = (
        "import importlib.util,sys,os; "
        "s=importlib.util.spec_from_file_location('ledger',sys.argv[1]);"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        "lock=m.locked(sys.argv[2]);lock.__enter__();os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", crash_code, str(ENTRY), str(registry)], check=True)
    cli("verify", registry)
    checks.append("kernel registry lock automatically releases after abrupt process exit")
    assert json.loads(registry.read_text())["assets"][1]["status"] == "selected"
    checks.append("reverification preserves selected state and checks all real sources")
    _, report = cli("validate", registry, "--full-media")
    assert not report["errors"]; checks.append("full registry validates media, hashes and evidence")
    assert not list(root.glob(".*.tmp")) and held.is_file(); checks.append("atomic writes leave no partial files and persistent lock is released")
    assert all(sha(path) == value for path, value in originals.items()); checks.append("all media source bytes remain unchanged")
    state = json.loads(registry.read_text())
    assert state["revision"] == len(state["events"])
    checks.append("monotonic audit revisions match recorded mutations")
    raw_tool = root / "sync-tool.json"
    dump(raw_tool, {"kind": "synthetic-tool-fixture", "fixture_only": True, "no_generation_called": True})
    sync_receipt = root / "sync-execution.json"
    dump(sync_receipt, {"status": "succeeded", "provider_mode": "synchronous", "request_id": "fixture-sync-1",
                        "task_id": None, "provider": "image_gen", "tool_receipt": str(raw_tool),
                        "verified_artifacts": [{"role": "image", "path": str(first), "sha256": sha(first)}]})
    add("SYNC_IMAGE", "SYNC_IMAGE", first, "image", "keyframe", "--receipt", str(sync_receipt), "--request-id", "fixture-sync-1")
    sync = json.loads(registry.read_text())["assets"][-1]
    assert sync["provenance"]["task_id"] is None and sync["provenance"]["mode"] == "synchronous"
    checks.append("canonical synchronous receipt binds request and verified artifact without a remote task ID")
    cli("review", registry, "SYNC_IMAGE", "--evidence", good); cli("select", registry, "SYNC_IMAGE")
    original_tool = raw_tool.read_bytes(); raw_tool.write_bytes(original_tool + b" ")
    cli("select", registry, "SYNC_IMAGE", ok=False)
    raw_tool.write_bytes(original_tool); checks.append("raw synchronous tool receipt drift blocks selection")
    add("SYNC_DUP", "SYNC_DUP", first, "image", "keyframe", "--receipt", str(sync_receipt), "--request-id", "fixture-sync-1", ok=False)
    checks.append("synchronous provider request and bytes cannot be double-registered")
    add("SYNC_FAKE_TASK", "SYNC_FAKE_TASK", first, "image", "keyframe", "--receipt", str(sync_receipt),
        "--request-id", "fixture-sync-1", "--task-id", "invented-task", ok=False)
    checks.append("synchronous receipt rejects invented remote task IDs")
    add("BAD_RANGE", "BAD_RANGE", video, "video", "shot_video", "--source-start", ".8", "--duration", ".4", ok=False)
    checks.append("explicit video range cannot exceed verified primary-stream duration")
    add("IMAGE_BROLL_NO_DURATION", "IMAGE_BROLL_NO_DURATION", first, "image", "broll", ok=False)
    checks.append("image B-roll requires explicit programme duration")
    # True CLI ledger -> review -> select -> compiler integration, not handwritten registry.
    # All board image reviews below describe synthetic fixtures only.
    add("SCENE_V1", "SCENE_MAIN", first, "image", "scene")
    cli("review", registry, "SCENE_V1", "--evidence", good); cli("select", registry, "SCENE_V1")
    add("VIDEO_V2", "SHOT_01_VIDEO", video, "video", "shot_video", "--shot-id", "SHOT_01",
        "--source-start", ".2", "--duration", ".6", "--reference", "CHAR_V2", "SCENE_V1")
    cli("review", registry, "VIDEO_V2", "--evidence", good); cli("select", registry, "VIDEO_V2")
    default = next(row for row in json.loads(registry.read_text())["assets"] if row["id"] == "VIDEO_V1")
    assert default["source_start"] == 0 and abs(default["duration"] - 1) < 1e-6
    checks.append("default video range is the reliably decoded full primary stream")
    # Retire the previously selected fixture B-roll that is longer than this short story.
    add("BROLL_SHORT", "PROVIDER_VIDEO", video, "video", "broll", "--source-start", ".1", "--duration", ".2", "--at", ".3")
    cli("review", registry, "BROLL_SHORT", "--evidence", good); cli("select", registry, "BROLL_SHORT")
    board = root / "board.json"
    dump(board, {"version": 1, "title": "Synthetic CLI ledger integration; no creative approval",
        "characters": [{"id": "CHARACTER_MAIN", "name": "Synthetic character fixture", "locked_traits": {},
                        "selected_asset_id": "CHAR_V2"}],
        "scenes": [{"id": "SCENE_MAIN", "name": "Synthetic scene fixture", "locked_facts": {},
                    "selected_asset_id": "SCENE_V1"}],
        "assets": [{"id": "CHAR_V2", "kind": "character", "status": "selected", "path": str(second),
                    "character_ids": ["CHARACTER_MAIN"]},
                   {"id": "SCENE_V1", "kind": "scene", "status": "selected", "path": str(first), "scene_id": "SCENE_MAIN"}],
        "shots": [{"id": "SHOT_01", "order": 1, "scene_id": "SCENE_MAIN", "character_ids": ["CHARACTER_MAIN"],
                   "continuity_group": "main", "start_state": {}, "action": "Synthetic video range fixture.",
                   "end_state": {}, "depends_on": []}],
        "candidates": [{"id": "CAND_CHAR", "target_id": "CHARACTER_MAIN", "asset_id": "CHAR_V2", "round": 1,
                        "review_status": "reviewed", "evidence": ["Synthetic fixture only."], "hard_failures": []},
                       {"id": "CAND_SCENE", "target_id": "SCENE_MAIN", "asset_id": "SCENE_V1", "round": 1,
                        "review_status": "reviewed", "evidence": ["Synthetic fixture only."], "hard_failures": []}],
        "limits": {"candidates_per_round": 3, "max_rounds": 2}})
    compiled_dir = root / "compiled"
    result = subprocess.run([sys.executable, str(ENTRY.with_name("compiler.py")), "compile-story", str(board), str(registry),
                             "--out-dir", str(compiled_dir), "--fps", "25", "--width", "160", "--height", "90"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    job = json.loads((compiled_dir / "job.json").read_text())
    assert job["clips"][0]["start"] == .2 and abs(job["clips"][0]["end"] - .8) < 1e-8
    timing = json.loads((compiled_dir / "time-map.json").read_text())
    assert timing["total_frames"] == 15
    assert job["broll"][0]["at"] == .3 and job["broll"][0]["duration"] == .2
    checks.append("real registry CLI register/review/select compiles declared source range into a frame-quantized story job")
    checks.append("registered B-roll at/duration/source-start survives true compiler handoff")
    assert all(sha(path) == value for path, value in originals.items())
    checks.append("end-to-end ledger/compiler integration preserves every source media file")
    verification = {"assertions_passed": len(checks), "assertions": checks,
                    "external_generation_called": False, "actual_creative_review_claimed": False}
    dump(root / "verification.json", verification)
    print(json.dumps(verification, ensure_ascii=False))


if __name__ == "__main__":
    main()
