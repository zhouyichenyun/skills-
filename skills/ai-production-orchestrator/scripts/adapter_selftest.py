#!/usr/bin/env python3
"""Real, synthetic FFmpeg integration checks for the allowlisted local adapter."""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import struct
import zlib
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_adapter as adapter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--skills-root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    project, skills = args.out.resolve(), args.skills_root.resolve()
    project.mkdir(parents=True, exist_ok=False)
    checks = []

    def check(condition, message):
        if not condition:
            raise AssertionError(message)
        checks.append(message)

    def command(argv):
        result = subprocess.run([str(value) for value in argv], capture_output=True, text=True)
        if result.returncode:
            raise AssertionError(result.stderr[-3000:])
        return result

    def write(path, payload):
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def run(action, params, name, expected="succeeded"):
        result = adapter.execute(action, params, project, project / name, skills)
        write(project / (name + "-result.json"), result)
        check(result["status"] == expected, name + ": " + expected)
        if expected == "succeeded":
            check(len({item["role"] for item in result["artifacts"]}) == len(result["artifacts"]), name + ": unique output roles")
            for item in result["artifacts"]:
                path = Path(item["path"])
                check(path.exists() and path.is_relative_to(project / name), name + ": artifact is real and stage-owned " + item["role"])
        return result, {item["role"]: item["path"] for item in result["artifacts"]}

    source = project / "source with 空格.mp4"
    broll = project / "broll.mp4"
    music = project / "music.wav"
    command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=4",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", source])
    command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=50:d=4",
             "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=4",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", broll])
    command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             "sine=frequency=180:sample_rate=48000:duration=4", "-c:a", "pcm_s24le", "-ac", "2", music])
    original_hashes = {str(path): adapter.sha256(path) for path in (source, broll, music)}
    transcript = project / "words.json"
    write(transcript, {"version": 1, "source": str(source), "words": [
        {"start": 0.1, "end": 0.5, "text": "第一句"},
        {"start": 2.5, "end": 3.2, "text": "第二句"},
    ], "note": "Synthetic timing fixture; not a speech-recognition accuracy claim"})
    _, planned = run("plan_edit", {"video": str(source), "transcript": str(transcript)}, "plan")
    timing = adapter.read_json(planned["time_map"])
    check(timing["total_frames"] == 100, "Non-silent gap is not silently removed from synthetic word draft")
    job = project / "job.json"
    write(job, {"version": 1, "name": "Adapter integration", "fps": 25, "width": 320, "height": 180,
        "clips": [{"id": "C1", "source": str(source), "start": .1, "end": 1.74},
                  {"id": "C2", "source": str(broll), "start": .2, "end": 1.76}],
        "broll": [{"source": str(broll), "at": .6, "source_start": .3, "duration": .6, "fit": "cover"}],
        "subtitles": [{"start": .1, "end": 1.4, "text": "示例第一句"}, {"start": 1.8, "end": 3.0, "text": "示例第二句"}],
        "grade": {"preset": "neutral", "lut": None}, "audio": {"target_lufs": -16}})
    rendered, picture = run("render", {"job": str(job)}, "render")
    check(rendered["metrics"]["frame_count"] == 80, "Per-clip frame quantization produces 80 actual video frames")
    check(any(item["path"] == str(source) for item in rendered["metrics"]["input_files"]),
          "Nested job source hash is tracked for resume invalidation")
    _, exported = run("export_nle", {"job": str(job)}, "export")
    prepared, sound = run("prepare_audio_plan", {"job": str(job), "video": picture["video"],
        "voice_mode": "preserve", "tracks": [{"id": "M1", "role": "music", "source": str(music),
        "at": 0, "source_start": 0, "duration": 3.2, "gain_db": -18, "fade_in": .1, "fade_out": .2, "duck": True}]}, "prepare")
    audio_plan = adapter.read_json(sound["audio_plan"])
    check(audio_plan["duration"] == 3.2 and audio_plan["timeline"]["total_frames"] == 80,
          "Audio programme length derives from cumulative integer frames")
    check(len([item for item in audio_plan["tracks"] if item["role"] == "dialogue"]) == 1,
          "Preserve mode includes original dialogue exactly once")
    check(audio_plan["tracks"][0]["duration"] <= 3.2, "Dialogue cue uses real decoded sample length")
    _, mix = run("mix_audio", {"audio_plan": sound["audio_plan"]}, "mix")
    _, mux = run("mux_audio", {"video": picture["video"], "audio": mix["mix"]}, "mux")
    _, audio_nle = run("export_audio_nle", {"timeline": exported["xml"], "mix_dir": mix["mix_dir"]}, "audio-nle")
    qc, scanned = run("qc_scan", {"video": mux["video"], "srt": picture["srt"], "extract_frames": False}, "qc")
    check(not qc["metrics"]["technical_failures"] and not qc["metrics"]["technical_warnings"], "Final encoded video has no technical fail/warning")
    _, repaired = run("qc_repair", {"video": mux["video"], "srt": picture["srt"],
        "normalize_loudness": True, "fix_subtitles": True, "max_rounds": 1, "extract_frames": False}, "repair-noop")
    check(adapter.sha256(repaired["video"]) == adapter.sha256(mux["video"]),
          "No-op QC repair still returns a stage-owned copy without altering source bytes")
    check(adapter.read_json(repaired["qc_report"])["schema"] != "video-review-autofix.scan/v1",
          "Repair evidence is not misrepresented as a fresh final scan")
    files = [{"role": "video", "path": mux["video"]}, {"role": "srt", "path": picture["srt"]},
             {"role": "xml", "path": audio_nle["xml"]}, {"role": "audio", "path": mix["mix_dir"]}]
    delivery, packed = run("package_delivery", {"files": files, "qc_report": scanned["qc_report"], "acceptance": "technical",
        "delivery_profile": {"width": 320, "height": 180, "fps": 25}}, "package")
    manifest = adapter.read_json(packed["delivery_manifest"])
    check(manifest["acceptance"] == "technical_ready_pending_creative_review", "Technical delivery does not claim creative or native UI acceptance")
    check(all((Path(packed["delivery_dir"]) / item["path"]).is_file() for item in manifest["files"]),
          "Delivery manifest only names real copied files")
    for item in manifest["files"]:
        check(adapter.sha256(Path(packed["delivery_dir"]) / item["path"]) == item["sha256"], "Copied delivery checksum: " + item["path"])
    run("package_delivery", {"files": files, "qc_report": scanned["qc_report"], "acceptance": "production"}, "unreviewed", "blocked")
    reviews = project / "review-result.json"
    findings = adapter.read_json(scanned["qc_report"])["findings"]
    fake_review = {"required_checks": ["visual", "listening", "continuity"], "subject_sha256": adapter.sha256(source),
                  "checks": {key: {"state": "passed", "evidence": [mux["video"]], "notes": "Synthetic contract fixture, not a real artistic review"}
                             for key in ("visual", "listening", "continuity")},
                  "finding_dispositions": [{"finding_index": index, "decision": "intentional", "evidence": "Synthetic fixture only"}
                                           for index, finding in enumerate(findings) if finding["severity"] == "review"]}
    write(reviews, fake_review)
    run("package_delivery", {"files": files, "qc_report": scanned["qc_report"], "acceptance": "production",
                            "review_summary": str(reviews)}, "stale-review", "blocked")
    fake_review["subject_sha256"] = adapter.sha256(mux["video"])
    fake_review["checks"]["listening"]["state"] = "not_checked"
    write(reviews, fake_review)
    run("package_delivery", {"files": files, "qc_report": scanned["qc_report"], "acceptance": "production",
                            "review_summary": str(reviews)}, "incomplete-review", "blocked")
    fake_review["required_checks"] = ["visual"]
    fake_review["checks"] = {"visual": fake_review["checks"]["visual"]}
    write(reviews, fake_review)
    run("package_delivery", {"files": files, "qc_report": scanned["qc_report"], "acceptance": "production",
        "required_checks": ["visual", "listening", "continuity"], "review_summary": str(reviews)}, "weakened-review", "blocked")
    run("package_delivery", {"files": files, "qc_report": scanned["qc_report"],
        "delivery_profile": {"duration_seconds": 99}}, "wrong-duration", "blocked")
    run("package_delivery", {"files": files, "qc_report": scanned["qc_report"],
        "delivery_profile": {"audio_codec": "pcm_s24le"}}, "wrong-audio-codec", "blocked")
    run("package_delivery", {"files": files, "qc_report": scanned["qc_report"],
        "delivery_profile": {"unsupported_quality_claim": True}}, "unknown-profile", "failed")
    portable, portable_files = run("package_delivery", {"files": files, "qc_report": scanned["qc_report"],
        "acceptance": "technical", "include_source_media": True, "job": str(job), "max_source_bytes": 20 * 1024 ** 2}, "portable")
    directory = Path(portable_files["delivery_dir"])
    source_map = adapter.read_json(directory / "source-map.json")
    check(len(source_map["sources"]) >= 4, "Portable map includes real picture sources and copied audio stems")
    check(portable["metrics"]["source_bundle_bytes"] > 0, "Portable source-byte budget is measured")
    xml_copy = directory / "xml" / Path(audio_nle["xml"]).name
    original_xml_hash = adapter.sha256(xml_copy)
    command([sys.executable, directory / "relink_xml.py"])
    relinked = xml_copy.with_name(xml_copy.stem + ".relinked.xml")
    check(relinked.is_file() and adapter.sha256(xml_copy) == original_xml_hash,
          "Destination relinker creates a new XML without changing the original")
    urls = ET.parse(relinked).getroot().findall(".//pathurl")
    check(bool(urls) and all(Path(unquote(urlparse(node.text).path)).is_relative_to(directory) and
          Path(unquote(urlparse(node.text).path)).is_file() for node in urls),
          "Relinked media paths point to actual files in the portable delivery")
    run("package_delivery", {"files": files, "qc_report": scanned["qc_report"], "include_source_media": True,
                            "job": str(job), "max_source_bytes": 1}, "source-budget", "blocked")
    run("package_delivery", {"files": [{"role": "video", "path": str(source)}], "qc_report": scanned["qc_report"]}, "stale-qc", "blocked")
    run("prepare_audio_plan", {"job": str(job), "voice_mode": "replace", "tracks": []}, "missing-voice", "blocked")
    run("prepare_audio_plan", {"job": str(job), "video": picture["video"], "voice_mode": "preserve",
        "tracks": [{"role": "voiceover", "source": str(music), "duration": 1}]}, "duplicate-voice", "failed")
    run("render", {"job": "missing.json"}, "missing-job", "blocked")
    run("not_a_shell", {"command": "echo unsafe"}, "unsupported", "failed")
    run("render", {"job": str(job), "python": "python -c print(1)"}, "interpreter-injection", "failed")
    # An empty/silent mix may exit zero; the adapter must keep the review gate.
    silent = project / "silent.wav"
    command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             "anullsrc=r=48000:cl=stereo", "-t", "1", "-c:a", "pcm_s24le", silent])
    silent_plan = project / "silent-plan.json"
    write(silent_plan, {"version": 1, "duration": 1, "tracks": [{"id": "S", "role": "ambience", "source": str(silent), "duration": 1}]})
    silence_result, _ = run("mix_audio", {"audio_plan": str(silent_plan)}, "silent-mix")
    check(silence_result["metrics"]["machine_gate"] == "review_required", "Mixer exit-zero with unmeasurable silence is not a quality pass")
    check(all(adapter.sha256(path) == digest for path, digest in original_hashes.items()), "All synthetic source media remain byte-identical")
    old = adapter.sha256(Path(picture["video"]))
    duplicate = adapter.execute("render", {"job": str(job)}, project, project / "render", skills)
    check(duplicate["status"] == "failed" and adapter.sha256(Path(picture["video"])) == old,
          "Repeated output directory is rejected without replacing an accepted render")
    # Exercise the actual asset-registry CLI -> strict story compiler interface,
    # rather than a hand-written registry that accidentally omits producer fields.
    def png(path, rgb):
        def chunk(kind, payload):
            return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)) +
                         chunk(b"IDAT", zlib.compress((b"\0" + bytes(rgb) * 2) * 2)) + chunk(b"IEND", b""))
    char_image, scene_image = project / "character-fixture.png", project / "scene-fixture.png"
    png(char_image, (25, 70, 110)); png(scene_image, (90, 60, 20))
    registry_script = Path(__file__).with_name("asset_registry.py")
    registry = project / "assets.json"
    command([sys.executable, registry_script, "init", registry])
    receipt = project / "provider-fixture.json"
    receipt_payload = {"task_id": "SYNTHETIC_TASK_01", "status": "succeeded", "artifact_sha256": adapter.sha256(source),
                       "note": "Synthetic interface fixture; no provider task was submitted"}
    write(receipt, receipt_payload)
    for ident, path, kind, role, shot in (
        ("REFCHAR", char_image, "image", "character", None),
        ("REFSCENE", scene_image, "image", "scene", None),
        ("VID1", source, "video", "shot_video", "SHOT1"),
        ("VID2", broll, "video", "shot_video", "SHOT2"),
    ):
        args = [sys.executable, registry_script, "add", registry, "--id", ident, "--logical-id", ident,
                "--file", path, "--kind", kind, "--role", role]
        if shot:
            args += ["--shot-id", shot, "--reference", "REFCHAR", "REFSCENE", "--source-start", "0.1",
                     "--duration", "1.64" if ident == "VID1" else "1.56"]
        if ident == "VID1":
            args += ["--receipt", receipt, "--task-id", "SYNTHETIC_TASK_01", "--provider", "synthetic-selftest"]
        command(args)
        evidence = project / ("review-" + ident + ".json")
        write(evidence, {"status": "reviewed", "observations": ["Synthetic contract observation only; no visual-quality assertion"],
                         "evidence": [{"path": str(path), "note": "Real fixture bytes"}], "hard_failures": []})
        command([sys.executable, registry_script, "review", registry, ident, "--evidence", evidence])
        command([sys.executable, registry_script, "select", registry, ident])
    board = project / "board.json"
    scores = {name: 80 for name in ("identity", "scene", "continuity", "anatomy", "composition")}
    board_payload = {"version": 1, "title": "Synthetic story ledger integration",
        "characters": [{"id": "CHAR1", "name": "Test", "selected_asset_id": "REFCHAR", "locked_traits": {}}],
        "scenes": [{"id": "SCENE1", "name": "Test room", "selected_asset_id": "REFSCENE", "locked_facts": {}}],
        "assets": [{"id": "REFCHAR", "kind": "character", "path": str(char_image), "status": "selected", "character_ids": ["CHAR1"]},
                   {"id": "REFSCENE", "kind": "scene", "path": str(scene_image), "status": "selected", "scene_id": "SCENE1"}],
        "shots": [{"id": "SHOT1", "order": 1, "scene_id": "SCENE1", "character_ids": ["CHAR1"], "start_state": {"cup": "table"},
                   "action": "First synthetic shot", "end_state": {"cup": "hand"}, "depends_on": []},
                  {"id": "SHOT2", "order": 2, "scene_id": "SCENE1", "character_ids": ["CHAR1"], "start_state": {"cup": "hand"},
                   "action": "Second synthetic shot", "end_state": {"cup": "hand"}, "depends_on": ["SHOT1"]}],
        "candidates": [{"id": "CANDCHAR", "target_id": "CHAR1", "asset_id": "REFCHAR", "round": 1, "review_status": "reviewed",
                        "scores": scores, "hard_failures": [], "evidence": ["Synthetic fixture only"]},
                       {"id": "CANDSCENE", "target_id": "SCENE1", "asset_id": "REFSCENE", "round": 1, "review_status": "reviewed",
                        "scores": scores, "hard_failures": [], "evidence": ["Synthetic fixture only"]}],
        "limits": {"candidates_per_round": 3, "max_rounds": 2}}
    write(board, board_payload)
    compiled, compiled_paths = run("compile_story", {"board": str(board), "registry": str(registry),
        "fps": 25, "width": 320, "height": 180}, "strict-story")
    check(compiled["metrics"]["registry_mode"] == "versioned_ledger_verified", "Real registry CLI feeds strict compiler without handwritten range metadata")
    check(adapter.read_json(compiled_paths["time_map"])["total_frames"] == 80, "Registered source ranges compile to the same integer frame grid")
    receipt_bytes = receipt.read_bytes();write(receipt, {**receipt_payload, "note": "Changed after asset selection"})
    run("compile_story", {"board": str(board), "registry": str(registry), "width": 320, "height": 180}, "receipt-tampered", "blocked")
    receipt.write_bytes(receipt_bytes)
    review_path = project / "review-VID1.json";review_bytes = review_path.read_bytes()
    write(review_path, {"status": "reviewed", "observations": ["tampered"]})
    run("compile_story", {"board": str(board), "registry": str(registry), "width": 320, "height": 180}, "review-tampered", "blocked")
    review_path.write_bytes(review_bytes)
    data = adapter.read_json(registry)
    parent = next(row for row in data["assets"] if row["id"] == "REFCHAR");parent["status"] = "rejected"
    rejected_registry = project / "rejected-registry.json";write(rejected_registry, data)
    run("compile_story", {"board": str(board), "registry": str(rejected_registry), "width": 320, "height": 180}, "parent-rejected", "failed")
    minimal = project / "minimal-registry.json"
    write(minimal, {"version": 1, "assets": [row for row in data["assets"] if row.get("role") == "shot_video"]})
    run("compile_story", {"board": str(board), "registry": str(minimal), "width": 320, "height": 180}, "legacy-default", "blocked")
    report = {"checks_passed": len(checks), "checks": checks, "synthetic_only": True,
              "generation_tools_called": False, "native_ui_verified": False, "creative_review_performed": False,
              "final_video": mux["video"], "delivery_manifest": packed["delivery_manifest"]}
    write(project / "verification.json", report)
    print(json.dumps({"checks_passed": len(checks), "verification": str(project / "verification.json")}))


if __name__ == "__main__":
    main()
