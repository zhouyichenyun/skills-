#!/usr/bin/env python3
"""Synthetic, no-generation tests for story_board.py. Never use user media."""
from __future__ import annotations
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import zlib

SCRIPT = Path(__file__).with_name("story_board.py")
spec = importlib.util.spec_from_file_location("story_board_under_test", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()
    root = Path(args.out_dir).resolve()
    root.mkdir(parents=True, exist_ok=False)
    passed = []
    def check(ok, name):
        if not ok:
            raise AssertionError(name)
        passed.append(name)
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
    image = root / "角色 preview.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 32, 8, 2, 0, 0, 0)) +
                      chunk(b"IDAT", zlib.compress((b"\0" + bytes([30, 70, 120]) * 32) * 32)) + chunk(b"IEND", b""))
    original_image = digest(image)
    board = {
        "version": 1, "title": "角色与故事测试",
        "characters": [{"id": "CHAR01", "name": "测试角色", "locked_traits": {"coat": "blue"}, "selected_asset_id": "ASSET01"}],
        "scenes": [{"id": "SCENE01", "name": "室内", "locked_facts": {"door": "left"}}],
        "assets": [{"id": "ASSET01", "kind": "character", "status": "reviewed", "path": image.name, "character_ids": ["CHAR01"], "view": "front"}],
        "shots": [
            {"id": "SHOT01", "order": 1, "scene_id": "SCENE01", "character_ids": ["CHAR01"],
             "start_state": {"cup.holder": "table"}, "action": "拿起杯子", "end_state": {"cup.holder": "CHAR01"}, "depends_on": []},
            {"id": "SHOT02", "order": 2, "scene_id": "SCENE01", "character_ids": ["CHAR01"],
             "start_state": {"cup.holder": "CHAR01"}, "action": "看向门", "end_state": {"cup.holder": "CHAR01"}, "depends_on": ["SHOT01"]},
        ],
        "candidates": [{"id": "CAND01", "target_id": "CHAR01", "asset_id": "ASSET01", "round": 1,
                        "scores": {key: 80 for key in module.WEIGHTS}, "hard_failures": [],
                        "review_status": "reviewed", "evidence": ["Synthetic fixture metadata; no real person or image quality claim."]}],
        "limits": {"candidates_per_round": 3, "max_rounds": 2},
    }
    counter = 0
    def inspect(value):
        nonlocal counter
        counter += 1
        path = root / f"case-{counter}.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path, module.inspect(path)
    source, result = inspect(board)
    _, maps, previews, eligible, report = result
    check(report["errors"] == 0, "valid real-image board passes metadata checks")
    check("ASSET01" in previews and previews["ASSET01"]["url"].startswith("data:image/png;base64,"), "real PNG embedded without external URL")
    ranked = module.ranking(maps, eligible)
    check(ranked["recommendations"]["CHAR01"][0]["score"] == 80, "reviewed evidence-backed scores use declared weights")
    check(ranked["selection_changed"] is False, "ranking never changes selection")
    check(report["generation_performed"] is False and "Not performed" in report["visual_review"], "no generation or visual recognition success claim")
    value = copy.deepcopy(board); value["candidates"][0]["hard_failures"] = ["identity mismatch"]
    _, result = inspect(value)
    check(any(i["code"] == "unreviewed_selection" for i in result[-1]["issues"]), "hard-failed candidate cannot be selected")
    check(not module.ranking(result[1], result[3])["recommendations"], "hard-failed candidate cannot rank")
    value = copy.deepcopy(board); value["candidates"][0]["review_status"] = "pending"
    _, result = inspect(value)
    check(result[-1]["errors"] > 0 and not result[3]["CAND01"]["rank_eligible"], "pending candidate cannot be selected or ranked")
    value = copy.deepcopy(board); value["candidates"][0]["evidence"] = []
    _, result = inspect(value)
    check(any(i["code"] == "review_without_evidence" for i in result[-1]["issues"]), "review requires nonempty evidence")
    value = copy.deepcopy(board); value["candidates"][0]["scores"] = {}
    _, result = inspect(value)
    check(not result[3]["CAND01"]["rank_eligible"], "missing scores may display but do not rank")
    value = copy.deepcopy(board); value["candidates"][0]["round"] = 3
    _, result = inspect(value)
    check(any(i["code"] == "round_cap" for i in result[-1]["issues"]) and not result[3]["CAND01"]["rank_eligible"], "round limit prevents further ranking")
    value = copy.deepcopy(board)
    for n in range(2, 5):
        row = copy.deepcopy(value["candidates"][0]); row["id"] = f"CAND0{n}";value["candidates"].append(row)
    _, result = inspect(value)
    check(any(i["code"] == "candidate_batch_cap" for i in result[-1]["issues"]) and not any(x["rank_eligible"] for x in result[3].values()), "per-target per-round batch cap enforced")
    value = copy.deepcopy(board);value["shots"][1]["start_state"]["cup.holder"] = "table"
    _, result = inspect(value)
    check(any(i["code"] == "continuity_break" for i in result[-1]["issues"]), "explicit state discontinuity found")
    value["shots"][1]["continuity_overrides"] = [{"key": "cup.holder", "reason": "Intentional time jump; review required"}]
    _, result = inspect(value)
    check(result[-1]["errors"] == 0 and any(i["code"] == "declared_state_change" for i in result[-1]["issues"]), "explained state override retained as review warning")
    value = copy.deepcopy(board);value["shots"][0]["depends_on"] = ["SHOT02"]
    _, result = inspect(value)
    check(any(i["code"] == "dependency_cycle" for i in result[-1]["issues"]), "cyclic shot dependencies rejected")
    value = copy.deepcopy(board);value["assets"][0]["path"] = "missing.png"
    _, result = inspect(value)
    check(any(i["code"] == "asset_unviewable" for i in result[-1]["issues"]), "missing actual asset rejected")
    damaged = root / "damaged.png"
    damaged.write_bytes(image.read_bytes()[:-5] + b"broken")
    value = copy.deepcopy(board);value["assets"][0]["path"] = damaged.name
    _, result = inspect(value)
    check(any(i["code"] == "asset_unviewable" for i in result[-1]["issues"]), "damaged PNG chunk structure rejected")
    def png_fixture(name, payload, width=1, height=1, depth=8, color=2, interlace=0, palette=None):
        target = root / name
        contents = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, interlace))
        if palette is not None:
            contents += chunk(b"PLTE", palette)
        target.write_bytes(contents + chunk(b"IDAT", payload) + chunk(b"IEND", b""))
        return target
    def preview_error(target):
        value = copy.deepcopy(board);value["assets"][0]["path"] = target.name
        _, result = inspect(value)
        return any(i["code"] == "asset_unviewable" for i in result[-1]["issues"])
    check(preview_error(png_fixture("bad-zlib.png", b"not-a-zlib-stream")), "CRC-valid bad PNG zlib stream rejected")
    check(preview_error(png_fixture("empty-pixels.png", zlib.compress(b""))), "empty PNG pixel stream rejected")
    check(preview_error(png_fixture("wrong-length.png", zlib.compress(b"\0\0"))), "PNG scanline length checked")
    check(preview_error(png_fixture("bad-filter.png", zlib.compress(b"\5\x10\x20\x30"))), "PNG invalid scanline filter rejected")
    oversized = png_fixture("oversized-pixels.png", zlib.compress(b""), width=5000, height=5000, color=6)
    check(preview_error(oversized), "PNG expected decompressed data bounded before inflation")
    bomb = png_fixture("bounded-inflate.png", zlib.compress(b"\0" * 200000))
    check(preview_error(bomb), "PNG actual inflation bounded to expected scanline size")
    sixteen = png_fixture("valid-16bit.png", zlib.compress(b"\0" + b"\x20\0" * 4), depth=16, color=6)
    check(module.image_data(sixteen)["mime"] == "image/png", "legal 16-bit RGBA PNG accepted")
    indexed = png_fixture("valid-indexed.png", zlib.compress(b"\0\x40"), width=2, depth=1, color=3, palette=b"\xff\0\0\0\0\xff")
    check(module.image_data(indexed)["mime"] == "image/png", "legal packed indexed PNG accepted")
    interlaced = png_fixture("valid-adam7.png", zlib.compress(b"\0\x80\0\x80\0\x80\x80"), width=2, height=2, color=0, interlace=1)
    check(module.image_data(interlaced)["mime"] == "image/png", "legal Adam7 PNG scanline passes accepted")
    value = copy.deepcopy(board)
    value["characters"].append({"id": "CHAR02", "name": "另一角色"})
    value["assets"][0]["character_ids"] = ["CHAR02"]
    _, result = inspect(value)
    check(any(i["code"] == "selected_character_mismatch" for i in result[-1]["issues"]), "selected identity metadata mismatch is explicit")
    value = copy.deepcopy(board)
    value["assets"][0]["kind"] = "scene"
    _, result = inspect(value)
    check(any(i["code"] == "candidate_asset_kind" for i in result[-1]["issues"]) and not result[3]["CAND01"]["rank_eligible"], "explicit asset kind mismatch cannot select or rank")
    value = copy.deepcopy(board)
    value["scenes"].append({"id": "SCENE02", "name": "另一场景"})
    value["assets"][0]["kind"] = "reference"
    value["assets"][0]["scene_id"] = "SCENE02"
    value["shots"][0]["selected_asset_id"] = "ASSET01"
    row = copy.deepcopy(value["candidates"][0]);row["id"]="CAND02";row["target_id"]="SHOT01";value["candidates"].append(row)
    _, result = inspect(value)
    check(any(i["code"] == "selected_scene_mismatch" for i in result[-1]["issues"]), "selected shot scene metadata mismatch is explicit")
    value = copy.deepcopy(board);value["assets"][0]["path"] = "javascript:alert(1)"
    _, result = inspect(value)
    check(any(i["code"] == "unsafe_asset_path" for i in result[-1]["issues"]), "executable URL scheme rejected")
    svg = root / "bad.svg";svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    value = copy.deepcopy(board);value["assets"][0]["path"] = svg.name
    _, result = inspect(value)
    check(any(i["code"] == "asset_unviewable" for i in result[-1]["issues"]), "active SVG not embedded")
    value = copy.deepcopy(board);value["assets"][0]["status"] = "planned";value["assets"][0].pop("path");value["characters"][0].pop("selected_asset_id");value["candidates"] = []
    _, result = inspect(value)
    check(result[-1]["errors"] == 0, "planned assets can remain explicit without fabricated files")
    def cli(command, path, out):
        return subprocess.run([sys.executable, str(SCRIPT), command, str(path), "--out", str(out)], capture_output=True, text=True)
    html_path = root / "canvas.html"
    result = cli("build", source, html_path)
    check(result.returncode == 0 and html_path.is_file(), "HTML board actually built")
    html = html_path.read_text()
    check(all(f'data-testid="{name}"' in html for name in ("zoom-in","zoom-out","filter-kind","search","save-layout","export-review")), "interactive canvas controls exposed for browser tests")
    check("fetch(" not in html and "XMLHttpRequest" not in html and '<script src=' not in html, "offline board has no external script or network client")
    value = copy.deepcopy(board);value["title"] = '</script><img src=x onerror=alert(1)>';value["characters"][0]["name"] = '<svg onload=alert(1)>'
    malicious, _ = inspect(value)
    attacked = root / "escaped.html"
    result = cli("build", malicious, attacked)
    output = attacked.read_text()
    check(result.returncode == 0 and value["title"] not in output and value["characters"][0]["name"] not in output and "\\u003c/script\\u003e" in output, "untrusted text cannot break out of JSON script data")
    source_hash = digest(source)
    result = cli("build", source, source)
    check(result.returncode == 2 and digest(source) == source_hash, "source board overwrite rejected")
    existing = root / "existing.json";existing.write_text("keep me")
    result = cli("validate", source, existing)
    check(result.returncode == 2 and existing.read_text() == "keep me", "existing output protected")
    check(digest(image) == original_image, "source image bytes unchanged")
    if shutil.which("node"):
        script = html.split("<script>\n", 1)[1].split("</script>", 1)[0]
        js = root / "canvas.js";js.write_text(script)
        process = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
        check(process.returncode == 0, "interactive JavaScript parses with Node")
    output = {"assertions_passed": len(passed), "assertions": passed, "generation_tools_called": False,
              "visual_identity_verified": False, "browser_interactions_tested": False,
              "board": str(source), "html": str(html_path)}
    (root / "verification.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
