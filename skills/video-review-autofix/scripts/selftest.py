#!/usr/bin/env python3
"""Meaningful FFmpeg integration checks; writes synthetic media outside the skill."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


def run(command, allowed=(0,)):
    result = subprocess.run([str(x) for x in command], capture_output=True, text=True)
    if result.returncode not in allowed:
        raise AssertionError(f"Exit {result.returncode}: {command}\n{result.stderr[-3000:]}\n{result.stdout[-1000:]}")
    return result


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="A new test directory")
    args = parser.parse_args()
    root = Path(args.out).resolve()
    root.mkdir(parents=True, exist_ok=False)
    script = Path(__file__).with_name("video_qc.py")
    assertions = []
    def check(condition, name):
        assert condition, name
        assertions.append(name)
    normal, defect = root / "normal.mp4", root / "defects.mp4"
    run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-n",
         "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=4",
         "-f", "lavfi", "-i", "sine=frequency=700:sample_rate=48000:duration=4",
         "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "48000",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", normal])
    v = "testsrc2=size=320x180:rate=25:duration=2[a];color=c=black:s=320x180:r=25:d=1[b];color=c=gray:s=320x180:r=25:d=1[c];testsrc2=size=320x180:rate=25:duration=2[d];[a][b][c][d]concat=n=4:v=1:a=0"
    a = "sine=frequency=600:sample_rate=48000:duration=2[a];anullsrc=r=48000:cl=mono:d=2[b];sine=frequency=600:sample_rate=48000:duration=2[c];[a][b][c]concat=n=3:v=0:a=1"
    run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-n", "-f", "lavfi", "-i", v,
         "-f", "lavfi", "-i", a, "-af", "volume=0.08", "-ar", "48000", "-c:v", "libx264",
         "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", defect])
    srt = root / "bad.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n正常字幕\n\n2\n00:00:00,800 --> 00:00:01,500\n重叠字幕\n\n3\n00:00:02,000 --> 00:00:02,500\n\n4\n00:00:03,000 --> 00:00:03,010\n这是一条明显过快而且需要重新安排阅读时间的字幕\n\n5\n00:00:05,700 --> 00:00:06,150\n结尾\n\n6\n00:00:06,500 --> 00:00:08,000\n整条越界字幕不能压到片尾\n", encoding="utf-8")
    good_srt = root / "good.srt"
    good_srt.write_text("1\n00:00:00,200 --> 00:00:02,200\n正常字幕\n", encoding="utf-8")
    before_media, before_srt = digest(defect), digest(srt)
    run([sys.executable, script, "scan", normal, "--srt", good_srt, "--out", root / "normal-scan", "--extract-frames"])
    good = json.loads((root / "normal-scan/report.json").read_text())
    check(good["machine_status"] == "checks_passed", "normal clip passes machine checks")
    check(len(good["visual_evidence"]) == 8, "real encoded normal frames extracted")
    run([sys.executable, script, "scan", defect, "--srt", srt, "--out", root / "defect-scan"], allowed=(10,))
    bad = json.loads((root / "defect-scan/report.json").read_text())
    codes = {x["code"] for x in bad["findings"]}
    for code in ("black_candidate", "freeze_candidate", "silence_candidate", "loudness_target", "subtitle_bounds", "subtitle_empty", "subtitle_overlap", "subtitle_speed"):
        check(code in codes, "detects " + code)
    job = root / "job.json"
    job.write_text(json.dumps({"version": 1, "name": "QC synthetic", "fps": 25, "width": 320, "height": 180,
        "clips": [{"source": "defects.mp4", "start": 0, "end": 6, "id": "clip01"}], "broll": [],
        "subtitles": [], "grade": {"preset": "neutral", "lut": None}, "audio": {"target_lufs": -16}}), encoding="utf-8")
    run([sys.executable, script, "repair", defect, "--srt", srt, "--out", root / "repair",
         "--normalize-loudness", "--fix-subtitles", "--max-rounds", "2", "--job", job], allowed=(10,))
    fixed = json.loads((root / "repair/repair.json").read_text())
    check(fixed["source_unchanged"] and digest(defect) == before_media and digest(srt) == before_srt, "source video and subtitles unchanged")
    remaining = {x["code"] for x in fixed["remaining_findings"]}
    check("loudness_target" not in remaining and "true_peak" not in remaining, "two-pass loudness repair passes remeasurement")
    check("black_candidate" in remaining and "freeze_candidate" in remaining, "creative black/freeze not blindly removed")
    check("subtitle_empty" not in remaining, "empty subtitle safely removed")
    check("subtitle_overlap" in remaining and "subtitle_speed" in remaining, "ambiguous overlap and speed remain explicit")
    check("subtitle_bounds" in remaining, "entire out-of-range cue remains visible as unresolved")
    check(len(fixed["rounds"]) <= 2, "bounded to at most two repair rounds")
    text = Path(fixed["output_srt"]).read_text()
    check("00:00:05,700 --> 00:00:06,000" in text, "short end overrun clamped")
    check("00:00:06,500 --> 00:00:08,000" in text, "long invalid cue not crushed into end frame")
    handoff = json.loads((root / "repair/rerender-request.json").read_text())
    check(handoff["job_was_modified"] is False and handoff["already_changed_job_fields"] == [], "rerender handoff does not silently modify job")
    check(any(x["mapped_regions"] for x in handoff["requests"] if x["finding"]["code"] == "black_candidate"), "finding time mapped to source clip")
    check(good["coverage"]["lip_sync"]["automated"] is False, "no machine lip-sync success claim")
    # A quiet, otherwise clean clip demonstrates a fully closed technical loop.
    quiet = root / "quiet.mp4"
    run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-n", "-i", normal,
         "-c:v", "copy", "-af", "volume=0.15", "-c:a", "aac", quiet])
    run([sys.executable, script, "repair", quiet, "--srt", good_srt, "--out", root / "closed-loop",
         "--normalize-loudness", "--extract-frames"])
    closed = json.loads((root / "closed-loop/repair.json").read_text())
    check(not closed["remaining_findings"], "quiet normal clip repaired and rescanned clean")
    check(closed["overall_acceptance"] == "pending_visual_and_listening_review", "clean technical scan still awaits real review")
    corrupt = root / "corrupt.mp4"
    corrupt.write_bytes(b"not a media file")
    run([sys.executable, script, "scan", corrupt, "--out", root / "corrupt-scan"], allowed=(10,))
    broken = json.loads((root / "corrupt-scan/report.json").read_text())
    check(broken["machine_status"] == "failed" and broken["findings"][0]["code"] == "probe_failed", "corrupt input fails closed")
    # A readable container with a corrupt key packet must fail full decoding,
    # rather than merely proving ffprobe can read the metadata.
    packets = json.loads(run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_packets",
                              "-show_entries", "packet=pos,size", "-of", "json", normal]).stdout)["packets"]
    payload = bytearray(normal.read_bytes())
    pos, size = int(packets[0]["pos"]), int(packets[0]["size"])
    payload[pos:pos + size] = b"\0" * size
    damaged = root / "damaged-packets.mp4"
    damaged.write_bytes(payload)
    run([sys.executable, script, "scan", damaged, "--out", root / "damaged-scan"], allowed=(10,))
    damaged_report = json.loads((root / "damaged-scan/report.json").read_text())
    check("decode_failed" in {f["code"] for f in damaged_report["findings"]}, "valid container with broken media packets fails decode check")
    # Interface regression: the editor rounds EACH clip duration to output
    # frames; accumulating unrounded seconds can drift by a second here.
    spec = importlib.util.spec_from_file_location("video_qc", script)
    qc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qc)
    touching = "freeze_start: 0\nfreeze_end: 1\nfreeze_start: 1\nfreeze_end: 2\nfreeze_start: 3\nfreeze_end: 3\n"
    spans = qc.segments(touching, r"freeze_start:\s*([0-9.]+)", r"freeze_end:\s*([0-9.]+)", 4)
    check(spans == [(0.0, 1.0), (1.0, 2.0)], "touching detector regions pair by log order and zero-length spans are omitted")
    quantized = {"version": 1, "fps": 25, "clips": [{"source": "normal.mp4", "start": 0, "end": .05, "id": f"q{i}"} for i in range(100)],
                 "broll": [{"source": "normal.mp4", "at": .059, "duration": .06, "source_start": 0}]}
    mapping_dir = root / "quantized-mapping"
    mapping_dir.mkdir()
    qc.make_handoff(job, quantized, [{"code": "black_candidate", "start": 3.99, "end": 4.0}], mapping_dir, None, False)
    mapping = json.loads((mapping_dir / "rerender-request.json").read_text())
    region = mapping["requests"][0]["mapped_regions"][0]
    check(region["id"] == "q99" and region["timeline_end"] == 4.0, "clip handoff follows per-clip frame quantization without cumulative drift")
    fractional_dir = root / "fractional-mapping"
    fractional_dir.mkdir()
    quantized["fps"] = "30000/1001"
    qc.make_handoff(job, quantized, [{"code": "black_candidate", "start": .05, "end": .1}], fractional_dir, None, False)
    fractional = json.loads((fractional_dir / "rerender-request.json").read_text())
    check(any(r["kind"] == "broll" and r["timeline_start_frame"] == 2 for r in fractional["requests"][0]["mapped_regions"]), "fractional FPS and B-roll frame-grid mapping supported")
    summary = {"assertions_passed": len(assertions), "assertions": assertions,
               "normal_findings": len(good["findings"]), "defect_codes": sorted(codes),
               "repair_remaining": sorted(remaining), "closed_loop_status": closed["status"],
               "synthetic_test_only": True, "user_desktop_connected": False}
    (root / "verification.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
