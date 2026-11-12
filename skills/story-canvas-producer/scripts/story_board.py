#!/usr/bin/env python3
"""Validate and display a local story asset board; never generate or approve images."""
from __future__ import annotations

import argparse
import base64
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import sys
import zlib

WEIGHTS = {"identity": 0.30, "scene": 0.20, "continuity": 0.30, "anatomy": 0.10, "composition": 0.10}
STATUSES = {"planned", "generated", "reviewed", "selected", "rejected"}
KINDS = {"character", "scene", "storyboard", "shot", "reference", "image"}
MAX_IMAGE = 8 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_DECOMPRESSED = 64 * 1024 * 1024

def png_pixels(header, compressed, palette):
    """Bounded structural scanline validation, not a semantic image review."""
    if len(header) != 13:
        raise ValueError("PNG IHDR must contain exactly 13 bytes")
    width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", header)
    allowed = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    if color not in allowed or depth not in allowed[color] or compression != 0 or filtering != 0 or interlace not in (0, 1):
        raise ValueError("PNG IHDR format is invalid or unsupported")
    if color == 3 and (not palette or len(palette) % 3 or len(palette) > 768 or len(palette) // 3 > 2 ** depth):
        raise ValueError("Indexed PNG requires a valid palette")
    geometry = ((0, 0, 1, 1),) if not interlace else (
        (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
        (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
    passes, expected = [], 0
    for x, y, dx, dy in geometry:
        columns = max(0, (width - x + dx - 1) // dx)
        rows = max(0, (height - y + dy - 1) // dy)
        if not columns or not rows:
            continue
        row_bytes = (columns * channels[color] * depth + 7) // 8
        expected += rows * (row_bytes + 1)
        if expected > MAX_DECOMPRESSED:
            raise ValueError("PNG preview exceeds the 64 MiB decompressed-image limit")
        passes.append((rows, row_bytes))
    try:
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(b"".join(compressed), expected + 1)
    except zlib.error as exc:
        raise ValueError("PNG IDAT is not a valid zlib stream") from exc
    if len(pixels) != expected or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
        raise ValueError("PNG decompressed scanline length or stream termination is invalid")
    position = 0
    for rows, row_bytes in passes:
        for _ in range(rows):
            if pixels[position] > 4:
                raise ValueError("PNG scanline filter byte is invalid")
            position += row_bytes + 1

def integer(value, label, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value

def finite(value):
    return type(value) in (int, float) and math.isfinite(value)

def issue(items, code, message, subject=None, severity="error", **extra):
    items.append({"code": code, "severity": severity, "subject": subject, "message": message, **extra})

def image_data(path):
    if path.is_symlink():
        raise ValueError("Image symlinks are not embedded")
    if not path.is_file():
        raise ValueError("Image file is missing")
    size = path.stat().st_size
    if size > MAX_IMAGE:
        raise ValueError("Image exceeds the 8 MiB per-image preview limit")
    data = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) < 45 or data[12:16] != b"IHDR" or b"IEND" not in data[-16:]:
            raise ValueError("PNG structure is incomplete")
        width, height = struct.unpack(">II", data[16:24])
        if not width or not height or width * height > 100_000_000:
            raise ValueError("PNG preview dimensions are invalid or excessive")
        offset, idat, ended, header, palette = 8, [], False, None, None
        while offset + 12 <= len(data):
            length = struct.unpack(">I", data[offset:offset+4])[0]
            kind = data[offset+4:offset+8]
            stop = offset + 12 + length
            if stop > len(data):
                raise ValueError("PNG chunk extends beyond file")
            payload = data[offset+8:offset+8+length]
            checksum = struct.unpack(">I", data[offset+8+length:stop])[0]
            if zlib.crc32(kind + payload) & 0xffffffff != checksum:
                raise ValueError("PNG chunk checksum failed")
            if kind == b"IHDR":
                if offset != 8 or header is not None:
                    raise ValueError("PNG has an invalid duplicate/misplaced IHDR")
                header = payload
            elif kind == b"PLTE":
                palette = payload
            elif kind == b"IDAT":
                idat.append(payload)
            offset = stop
            if kind == b"IEND":
                if length:
                    raise ValueError("PNG IEND must be empty")
                ended = True
                break
        if not idat or not ended or offset != len(data):
            raise ValueError("PNG image chunks are incomplete")
        png_pixels(header or b"", idat, palette)
        mime = "image/png"
    elif suffix in (".jpg", ".jpeg") and data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"):
        if len(data) < 20:
            raise ValueError("JPEG structure is incomplete")
        offset, dimensions = 2, None
        while offset < len(data) - 2:
            if data[offset] != 0xff:
                raise ValueError("JPEG marker structure is invalid")
            while offset < len(data) and data[offset] == 0xff:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            if marker in (0xd9, 0xda):
                break
            if marker in (0x01, *range(0xd0, 0xd9)):
                continue
            if offset + 2 > len(data):
                break
            length = struct.unpack(">H", data[offset:offset+2])[0]
            if length < 2 or offset + length > len(data):
                raise ValueError("JPEG segment length is invalid")
            if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
                if length < 8:
                    raise ValueError("JPEG frame header is incomplete")
                height, width = struct.unpack(">HH", data[offset+3:offset+7])
                dimensions = width, height
            offset += length
        if not dimensions or min(dimensions) <= 0 or dimensions[0] * dimensions[1] > 100_000_000:
            raise ValueError("JPEG dimensions are missing, invalid or excessive")
        mime = "image/jpeg"
    elif suffix == ".webp" and len(data) >= 20 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if struct.unpack("<I", data[4:8])[0] + 8 != len(data):
            raise ValueError("WebP RIFF length is inconsistent")
        kind, width, height = data[12:16], 0, 0
        if kind == b"VP8X" and len(data) >= 30:
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
        elif kind == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(data[26:28], "little") & 0x3fff
            height = int.from_bytes(data[28:30], "little") & 0x3fff
        elif kind == b"VP8L" and len(data) >= 25 and data[20] == 0x2f:
            packed = int.from_bytes(data[21:25], "little")
            width, height = (packed & 0x3fff) + 1, ((packed >> 14) & 0x3fff) + 1
        if not width or not height or width * height > 100_000_000:
            raise ValueError("WebP dimensions are missing, invalid or excessive")
        mime = "image/webp"
    else:
        raise ValueError("Only signature-matched PNG, JPEG and WebP previews are supported")
    return {"mime": mime, "data": data, "bytes": size, "sha256": hashlib.sha256(data).hexdigest()}

def load(path):
    source = Path(path).resolve()
    raw = source.read_bytes()
    board = json.loads(raw.decode("utf-8-sig"), parse_constant=lambda x: (_ for _ in ()).throw(ValueError("Non-finite JSON number")))
    if not isinstance(board, dict) or type(board.get("version")) is not int or board["version"] != 1:
        raise ValueError("A board object with integer version=1 is required")
    return source, raw, board

def inspect(path):
    source, raw, board = load(path)
    issues, maps, all_ids = [], {}, {}
    for kind in ("characters", "scenes", "assets", "shots", "candidates"):
        rows = board.get(kind, [])
        if not isinstance(rows, list):
            raise ValueError(f"{kind} must be a list")
        maps[kind] = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
                issue(issues, "invalid_id", "Every item needs a nonempty string id", kind)
                continue
            ident = row["id"]
            if ident in all_ids:
                issue(issues, "duplicate_id", "IDs must be globally unique", ident)
            else:
                all_ids[ident] = kind
            maps[kind][ident] = row
    limits = board.get("limits", {})
    if not isinstance(limits, dict):
        raise ValueError("limits must be an object")
    per_round = integer(limits.get("candidates_per_round", 3), "limits.candidates_per_round")
    max_rounds = integer(limits.get("max_rounds", 2), "limits.max_rounds")
    if per_round > 32 or max_rounds > 10:
        raise ValueError("Use an explicit bounded batch: at most 32 candidates/round and 10 rounds")
    previews, total = {}, 0
    for ident, asset in maps["assets"].items():
        status = asset.get("status", "planned")
        if asset.get("kind") not in KINDS:
            issue(issues, "asset_kind", "Asset kind must be character, scene, storyboard/shot, reference or image", ident)
        if status not in STATUSES:
            issue(issues, "asset_status", "Unknown asset status", ident)
        character_ids = asset.get("character_ids", [])
        if not isinstance(character_ids, list):
            issue(issues, "asset_characters", "Asset character_ids must be a list", ident)
            character_ids = []
        for cid in character_ids:
            if cid not in maps["characters"]:
                issue(issues, "missing_character", "Asset references a missing character", ident, reference=cid)
        if asset.get("scene_id") and asset["scene_id"] not in maps["scenes"]:
            issue(issues, "missing_scene", "Asset references a missing scene", ident)
        value = asset.get("path")
        if not value:
            if status != "planned":
                issue(issues, "missing_asset_file", "Non-planned assets need a real image file", ident)
            continue
        if not isinstance(value, str) or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value) and not re.match(r"^[A-Za-z]:[\\/]", value):
            issue(issues, "unsafe_asset_path", "Only local file paths are accepted; URLs and executable schemes are rejected", ident)
            continue
        try:
            local = Path(value).expanduser()
            local = local if local.is_absolute() else source.parent / local
            # Inspect symlinks before resolving so a direct symlink is not hidden.
            data = image_data(local)
            total += data["bytes"]
            if total > MAX_TOTAL:
                raise ValueError("Board exceeds the 64 MiB total embedded-image limit")
            previews[ident] = {"mime": data["mime"], "bytes": data["bytes"], "sha256": data["sha256"],
                              "url": "data:" + data["mime"] + ";base64," + base64.b64encode(data["data"]).decode("ascii")}
        except (OSError, ValueError) as exc:
            issue(issues, "asset_unviewable", str(exc), ident)
    targets = {**maps["characters"], **maps["scenes"], **maps["shots"]}
    counts, eligible = Counter(), {}
    for ident, candidate in maps["candidates"].items():
        target, asset_id = candidate.get("target_id"), candidate.get("asset_id")
        if target not in targets:
            issue(issues, "missing_target", "Candidate target must be an existing character, scene or shot", ident)
        if asset_id not in maps["assets"]:
            issue(issues, "missing_candidate_asset", "Candidate references a missing asset", ident)
        valid_round = True
        try:
            round_no = integer(candidate.get("round", 1), "candidate.round")
            if round_no > max_rounds:
                issue(issues, "round_cap", "Candidate exceeds the configured round limit", ident)
                valid_round = False
            counts[(target, round_no)] += 1
        except ValueError as exc:
            issue(issues, "candidate_round", str(exc), ident)
            valid_round = False
        failures = candidate.get("hard_failures", [])
        evidence = candidate.get("evidence", [])
        if not isinstance(failures, list) or not all(isinstance(v, str) and v.strip() for v in failures):
            issue(issues, "hard_failures_type", "hard_failures must be a list of nonempty strings", ident)
            failures = ["invalid hard_failures"]
        valid_evidence = isinstance(evidence, list) and bool(evidence) and all(isinstance(v, str) and v.strip() for v in evidence)
        review = candidate.get("review_status", "pending")
        if review not in ("pending", "reviewed"):
            issue(issues, "review_status", "review_status must be pending or reviewed", ident)
        if review == "reviewed" and not valid_evidence:
            issue(issues, "review_without_evidence", "Reviewed candidates need actual review evidence", ident)
        scores = candidate.get("scores", {})
        valid_scores = isinstance(scores, dict) and all(finite(scores.get(key)) and 0 <= scores[key] <= 100 for key in WEIGHTS)
        if scores and not valid_scores:
            issue(issues, "candidate_scores", "Ranking requires all five finite scores from 0 to 100", ident, severity="warning")
        asset = maps["assets"].get(asset_id, {})
        kind = asset.get("kind")
        matching_kind = (kind in ("reference", "image") or
                         kind == "character" and target in maps["characters"] or
                         kind == "scene" and target in maps["scenes"] or
                         kind in ("storyboard", "shot") and target in maps["shots"])
        if asset_id in maps["assets"] and target in targets and not matching_kind:
            issue(issues, "candidate_asset_kind", "Candidate asset kind contradicts its target category", ident)
        ok = target in targets and valid_round and matching_kind and review == "reviewed" and valid_evidence and not failures and asset_id in previews and asset.get("status") in ("reviewed", "selected")
        eligible[ident] = {"selection_eligible": ok, "rank_eligible": ok and valid_scores,
                           "weighted_score": round(sum(scores[k] * WEIGHTS[k] for k in WEIGHTS), 3) if ok and valid_scores else None}
    for (target, round_no), count in counts.items():
        if count > per_round:
            issue(issues, "candidate_batch_cap", "Too many candidates for this target/round", target, round=round_no, count=count)
            for cid, candidate in maps["candidates"].items():
                if candidate.get("target_id") == target and candidate.get("round", 1) == round_no:
                    eligible[cid] = {"selection_eligible": False, "rank_eligible": False, "weighted_score": None}
    for target_id, target in targets.items():
        selected = target.get("selected_asset_id")
        if selected:
            if selected not in maps["assets"]:
                issue(issues, "selected_missing", "Selected asset does not exist", target_id)
            elif not any(c.get("target_id") == target_id and c.get("asset_id") == selected and eligible[cid]["selection_eligible"]
                         for cid, c in maps["candidates"].items()):
                issue(issues, "unreviewed_selection", "Selection requires a real reviewed asset, matching reviewed evidence, and no hard failures", target_id, asset_id=selected)
            else:
                asset = maps["assets"][selected]
                declared_chars = asset.get("character_ids", [])
                if target_id in maps["characters"] and declared_chars and target_id not in declared_chars:
                    issue(issues, "selected_character_mismatch", "Selected asset declares a different character", target_id)
                expected_scene = target_id if target_id in maps["scenes"] else target.get("scene_id")
                if expected_scene and asset.get("scene_id") and asset["scene_id"] != expected_scene:
                    issue(issues, "selected_scene_mismatch", "Selected asset declares a different scene", target_id)
                if target_id in maps["shots"] and declared_chars and not set(target.get("character_ids", [])).issubset(set(declared_chars)):
                    issue(issues, "selected_cast_mismatch", "Selected shot asset is missing declared shot characters", target_id)
    for aid, asset in maps["assets"].items():
        if asset.get("status") == "selected" and not any(t.get("selected_asset_id") == aid for t in targets.values()):
            issue(issues, "orphan_selection", "Selected assets need an explicit reviewed target selection", aid)
    groups = defaultdict(list)
    for ident, shot in maps["shots"].items():
        if shot.get("scene_id") not in maps["scenes"]:
            issue(issues, "shot_scene", "Shot scene_id is missing or unknown", ident)
        chars = shot.get("character_ids", [])
        if not isinstance(chars, list):
            issue(issues, "shot_characters", "character_ids must be a list", ident)
            chars = []
        for cid in chars:
            if cid not in maps["characters"]:
                issue(issues, "shot_character", "Shot references a missing character", ident, reference=cid)
        try:
            order = integer(shot.get("order"), "shot.order")
        except ValueError as exc:
            issue(issues, "shot_order", str(exc), ident)
            continue
        group = shot.get("continuity_group", "main")
        if not isinstance(group, str) or not group:
            issue(issues, "continuity_group", "continuity_group must be a nonempty string", ident)
            continue
        for field in ("start_state", "end_state"):
            if not isinstance(shot.get(field, {}), dict):
                issue(issues, "state_type", f"{field} must be an object", ident)
        deps = shot.get("depends_on", [])
        if not isinstance(deps, list):
            issue(issues, "dependencies", "depends_on must be a list", ident)
            deps = []
        for dep in deps:
            if dep not in maps["shots"] or dep == ident:
                issue(issues, "dependency_reference", "Shot dependency is missing or refers to itself", ident, reference=dep)
        groups[group].append((order, ident, shot))
    for group, shots in groups.items():
        seen, state = set(), {}
        for order, ident, shot in sorted(shots, key=lambda r:(r[0], r[1])):
            if order in seen:
                issue(issues, "duplicate_shot_order", "Shot order is duplicated within a continuity group", ident)
            seen.add(order)
            initial, final = shot.get("start_state", {}), shot.get("end_state", {})
            if not isinstance(initial, dict) or not isinstance(final, dict):
                continue
            overrides = {}
            for item in shot.get("continuity_overrides", []):
                if isinstance(item, dict) and isinstance(item.get("key"), str) and isinstance(item.get("reason"), str) and item["reason"].strip():
                    overrides[item["key"]] = item["reason"]
                else:
                    issue(issues, "invalid_override", "Continuity overrides need key and nonempty reason", ident)
            for key, value in initial.items():
                if key in state and state[key] != value:
                    if key in overrides:
                        issue(issues, "declared_state_change", "Explicit continuity change retained for review", ident, severity="warning",
                              key=key, previous=state[key], current=value, reason=overrides[key])
                    else:
                        issue(issues, "continuity_break", "Explicit start state contradicts the previous tracked end state", ident,
                              key=key, previous=state[key], current=value)
            state.update(initial)
            state.update(final)
    visiting, visited = set(), set()
    def visit(ident):
        if ident in visiting:
            issue(issues, "dependency_cycle", "Shot dependency graph contains a cycle", ident)
            return
        if ident in visited:
            return
        visiting.add(ident)
        for dep in maps["shots"][ident].get("depends_on", []):
            if dep in maps["shots"]:
                visit(dep)
        visiting.remove(ident)
        visited.add(ident)
    for ident, shot in maps["shots"].items():
        if isinstance(shot.get("depends_on", []), list):
            visit(ident)
    errors = sum(i["severity"] == "error" for i in issues)
    report = {"schema": "story-canvas.validation/v1", "source_sha256": hashlib.sha256(raw).hexdigest(),
              "title": board.get("title", "Story canvas"), "errors": errors,
              "warnings": sum(i["severity"] == "warning" for i in issues),
              "status": "invalid" if errors else "metadata_checked_pending_visual_review",
              "visual_review": "Not performed by this script; scores/evidence are supplied review records, not image recognition. Preview structure checks do not replace a full decoder or actual visual inspection.",
              "generation_performed": False, "limits": {"candidates_per_round": per_round, "max_rounds": max_rounds},
              "preview_limits": {"per_image_bytes": MAX_IMAGE, "total_bytes": MAX_TOTAL,
                                 "png_decompressed_per_image_bytes": MAX_DECOMPRESSED}, "issues": issues}
    return board, maps, previews, eligible, report

def ranking(maps, eligible):
    by_target = defaultdict(list)
    excluded = []
    for ident, candidate in maps["candidates"].items():
        state = eligible[ident]
        if state["rank_eligible"]:
            by_target[candidate["target_id"]].append({"candidate_id": ident, "asset_id": candidate["asset_id"],
                                                    "score": state["weighted_score"], "round": candidate.get("round", 1)})
        else:
            excluded.append(ident)
    for rows in by_target.values():
        rows.sort(key=lambda row:(-row["score"], row["candidate_id"]))
    return {"schema": "story-canvas.ranking/v1", "weights": WEIGHTS, "recommendations": dict(by_target),
            "excluded_candidates": excluded, "selection_changed": False,
            "note": "Recommendations only. Ranking does not approve an asset or establish visual consistency."}

HTML = r"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>故事制作画布</title>
<style>
:root{font-family:system-ui,sans-serif;color:#e8edf6;background:#0e1420}*{box-sizing:border-box}
body{margin:0;overflow:hidden}header{height:88px;position:relative;z-index:3;padding:12px 18px;background:#182234;border-bottom:1px solid #354157;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:17px;margin:0;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.sub{font-size:11px;color:#a6b7ce}
button,select,input,textarea{font:inherit;border:1px solid #4d5c74;border-radius:6px;background:#233149;color:#edf3ff;padding:7px}
button{cursor:pointer}button:hover{background:#34445f}input{width:160px}#viewport{position:absolute;inset:88px 290px 0 0;overflow:hidden;background-image:radial-gradient(#334055 1px,transparent 1px);background-size:24px 24px;touch-action:none}
#world{position:absolute;transform-origin:0 0;width:6000px;height:4000px}svg{position:absolute;inset:0;width:6000px;height:4000px;overflow:visible;pointer-events:none}
.card{position:absolute;width:260px;min-height:210px;border:1px solid #4a5b77;border-radius:10px;background:#1a2639;box-shadow:0 5px 20px #0004;overflow:hidden}.drag{padding:10px;background:#243751;cursor:grab;user-select:none;display:flex;justify-content:space-between;gap:8px}
.drag span:first-child{overflow-wrap:anywhere}.tag{font-size:10px;color:#adc9ee}.content{padding:10px}.card img{width:238px;height:144px;object-fit:contain;background:#0b1018;border-radius:4px}.card p{font-size:12px;line-height:1.5;white-space:pre-wrap;overflow-wrap:anywhere;margin:8px 0}.card textarea{width:100%;height:65px;font-size:11px}.decision{width:100%;font-size:11px}
.muted{color:#9eb0c9}.danger{color:#ffb4ac}.score{color:#9ee4bd}.hidden{display:none}.lane{position:absolute;color:#91b8eb;font-size:18px;font-weight:650;pointer-events:none}
aside{position:absolute;right:0;top:88px;bottom:0;width:290px;padding:16px;background:#141e2d;overflow:auto;font-size:12px;line-height:1.55}aside h2{font-size:14px}aside ol{padding-left:18px}.notice{padding:9px;border:1px solid #5f708c;border-radius:7px;margin-bottom:14px}.legend{display:flex;gap:10px;font-size:11px}
@media(max-width:850px){#viewport{right:220px}aside{width:220px}header{height:120px}#viewport,aside{top:120px}}
</style>
<header>
<div><h1 id="title"></h1><div class="sub">离线分镜板 · 无生成调用 · 排名不是批准</div></div>
<button data-testid="zoom-in">＋</button><button data-testid="zoom-out">−</button><button data-testid="reset-view">复位</button>
<select data-testid="filter-kind"><option value="all">全部卡片</option><option value="character">角色</option><option value="scene">场景</option><option value="shot">故事镜头</option><option value="candidate">抽卡候选</option></select>
<input data-testid="search" placeholder="搜索角色／镜头／候选">
<button data-testid="save-layout">保存当前布局</button><button data-testid="export-review">导出评审 JSON</button>
<span class="sub" id="zoom-label"></span>
</header>
<div id="viewport"><div id="world"><svg id="links"></svg></div></div>
<aside><h2>状态与连续性</h2><div id="summary" class="notice"></div><div class="notice">图片按原始文件嵌入。脚本仅校验引用、审核记录及显式状态，不自动看图、不证明人物或故事视觉连续。</div><ol id="issues"></ol><h2>操作</h2><p>拖动卡片标题调整位置；拖动画布空白平移，滚轮缩放。筛选只改变视图。保存布局保留在此浏览器，导出产生新文件，不修改源 board。</p><p>候选的“建议采用”仅是本地评审建议；源数据的选定仍需真实审核证据。不要把生成失败用占位图替代。</p></aside>
<script id="board-data" type="application/json">__DATA__</script>
<script>
'use strict';
const data=JSON.parse(document.getElementById('board-data').textContent), board=data.board;
const world=document.getElementById('world'),viewport=document.getElementById('viewport'),links=document.getElementById('links');
const query=s=>document.querySelector('[data-testid="'+s+'"]');
const key='story-canvas-layout:'+data.validation.source_sha256;
let layout={pan:{x:25,y:25},scale:.75,positions:{}},notes=Object.create(null),decisions=Object.create(null);
try{const saved=JSON.parse(localStorage.getItem(key)||'null');if(saved&&saved.positions&&saved.pan&&Number.isFinite(saved.pan.x)&&Number.isFinite(saved.pan.y)&&Number.isFinite(saved.scale)){layout=saved;}}catch(e){}
const nodeMap=new Map(), assetMap=new Map((board.assets||[]).map(a=>[a.id,a])), nodes=[];
function text(parent,tag,value,cls){const el=document.createElement(tag);el.textContent=String(value??'');if(cls)el.className=cls;parent.append(el);return el;}
function compact(value){return JSON.stringify(value??{},null,1);}
document.getElementById('title').textContent=String(board.title||'故事制作画布');
document.getElementById('summary').textContent=data.validation.status+' · '+data.validation.errors+' 项错误 · '+data.validation.warnings+' 项提示';
for(const problem of data.validation.issues){text(document.getElementById('issues'),'li',(problem.subject||'board')+' · '+problem.message,problem.severity==='error'?'danger':'muted');}
function preview(parent,id){const item=data.previews[id];if(item){const img=document.createElement('img');img.src=item.url;img.alt='资产 '+id;parent.append(img);}else{text(parent,'p','尚无有效可预览图片','muted');}}
function add(kind,row,x,y,assetId){
 const id=kind+':'+row.id,node={id,kind,row,x,y};nodes.push(node);
 const saved=layout.positions[id];if(saved&&Number.isFinite(saved.x)&&Number.isFinite(saved.y)){node.x=saved.x;node.y=saved.y;}
 const card=document.createElement('section');card.className='card';card.dataset.nodeId=id;card.dataset.kind=kind;
 card.style.left=node.x+'px';card.style.top=node.y+'px';node.el=card;nodeMap.set(id,node);
 const head=document.createElement('div');head.className='drag';head.dataset.testid='drag-handle';text(head,'span',row.name||row.id);text(head,'span',{character:'角色',scene:'场景',shot:'镜头',candidate:'候选'}[kind],'tag');card.append(head);
 const body=document.createElement('div');body.className='content';card.append(body);if(assetId)preview(body,assetId);
 if(kind==='character')text(body,'p',compact(row.locked_traits));
 if(kind==='scene')text(body,'p',compact(row.locked_facts));
 if(kind==='shot'){text(body,'p',row.action||'尚未描述动作');text(body,'p','起始 '+compact(row.start_state)+'\n结束 '+compact(row.end_state),'muted');}
 if(kind==='candidate'){
  const state=data.eligibility[row.id]||{};text(body,'p','目标 '+row.target_id+' · 第 '+(row.round||1)+' 轮 · '+(row.review_status||'pending'),'muted');
  text(body,'p',state.rank_eligible?'推荐分 '+state.weighted_score:'未进入排名：待审核／证据／评分／硬失败条件未满足',state.rank_eligible?'score':'danger');
  if((row.hard_failures||[]).length)text(body,'p','硬失败：'+row.hard_failures.join('；'),'danger');
  text(body,'p','证据：'+(row.evidence||[]).join('；'),'muted');
  const select=document.createElement('select');select.className='decision';select.dataset.candidateId=row.id;
  for(const [v,t] of [['pending','本地建议：待定'],['recommend','本地建议：采用'],['repair','本地建议：返工'],['reject','本地建议：淘汰']]){const opt=document.createElement('option');opt.value=v;opt.textContent=t;select.append(opt);}
  select.addEventListener('change',()=>{decisions[row.id]=select.value;});body.append(select);
  const note=document.createElement('textarea');note.placeholder='记录实际观察、需修复点；此处不会自动批准';note.dataset.reviewFor=row.id;note.addEventListener('input',()=>{notes[row.id]=note.value;});body.append(note);
 }
 text(body,'p','ID '+row.id+(assetId?' · 资产 '+assetId:''),'muted');world.append(card);
 head.addEventListener('pointerdown',e=>{if(e.button!==0)return;e.stopPropagation();const ox=node.x,oy=node.y,sx=e.clientX,sy=e.clientY;head.setPointerCapture(e.pointerId);
  function move(ev){node.x=ox+(ev.clientX-sx)/layout.scale;node.y=oy+(ev.clientY-sy)/layout.scale;card.style.left=node.x+'px';card.style.top=node.y+'px';layout.positions[id]={x:node.x,y:node.y};draw();}
  function end(){head.removeEventListener('pointermove',move);head.removeEventListener('pointerup',end);head.removeEventListener('pointercancel',end);}
  head.addEventListener('pointermove',move);head.addEventListener('pointerup',end);head.addEventListener('pointercancel',end);
 });
}
const sections=[['character','characters',40,80],['scene','scenes',40,600],['shot','shots',40,1120],['candidate','candidates',40,1740]];
for(const [kind,collection,x,y] of sections){const label=document.createElement('div');label.className='lane';label.style.left=x+'px';label.style.top=(y-38)+'px';label.textContent={character:'角色身份与多视图',scene:'故事场景',shot:'连续故事镜头',candidate:'候选比较与局部返工'}[kind];world.append(label);
 const rows=[...(board[collection]||[])];if(kind==='shot')rows.sort((a,b)=>(a.order||0)-(b.order||0));
 rows.forEach((row,i)=>add(kind,row,x+i*295,y,kind==='candidate'?row.asset_id:row.selected_asset_id));
}
function draw(){links.replaceChildren();for(const shot of board.shots||[]){const to=nodeMap.get('shot:'+shot.id);if(!to||to.el.classList.contains('hidden'))continue;for(const dep of shot.depends_on||[]){const from=nodeMap.get('shot:'+dep);if(!from||from.el.classList.contains('hidden'))continue;const line=document.createElementNS('http://www.w3.org/2000/svg','path');const ax=from.x+260,ay=from.y+40,bx=to.x,by=to.y+40;line.setAttribute('d','M '+ax+' '+ay+' C '+(ax+60)+' '+ay+' '+(bx-60)+' '+by+' '+bx+' '+by);line.setAttribute('stroke','#6f95c4');line.setAttribute('stroke-width','2');line.setAttribute('fill','none');links.append(line);}}}
function apply(){layout.scale=Math.max(.15,Math.min(2.5,layout.scale));world.style.transform='translate('+layout.pan.x+'px,'+layout.pan.y+'px) scale('+layout.scale+')';document.getElementById('zoom-label').textContent=Math.round(layout.scale*100)+'%';draw();}
function zoom(factor,cx=viewport.clientWidth/2,cy=viewport.clientHeight/2){const old=layout.scale,next=Math.max(.15,Math.min(2.5,old*factor));layout.pan.x=cx-(cx-layout.pan.x)*next/old;layout.pan.y=cy-(cy-layout.pan.y)*next/old;layout.scale=next;apply();}
query('zoom-in').onclick=()=>zoom(1.2);query('zoom-out').onclick=()=>zoom(1/1.2);query('reset-view').onclick=()=>{layout.pan={x:25,y:25};layout.scale=.75;apply();};
viewport.addEventListener('wheel',e=>{e.preventDefault();const box=viewport.getBoundingClientRect();zoom(e.deltaY<0?1.1:1/1.1,e.clientX-box.left,e.clientY-box.top);},{passive:false});
viewport.addEventListener('pointerdown',e=>{if(e.button!==0||e.target.closest('.card'))return;const sx=e.clientX,sy=e.clientY,ox=layout.pan.x,oy=layout.pan.y;viewport.setPointerCapture(e.pointerId);function move(ev){layout.pan={x:ox+ev.clientX-sx,y:oy+ev.clientY-sy};apply();}function end(){viewport.removeEventListener('pointermove',move);viewport.removeEventListener('pointerup',end);viewport.removeEventListener('pointercancel',end);}viewport.addEventListener('pointermove',move);viewport.addEventListener('pointerup',end);viewport.addEventListener('pointercancel',end);});
function filter(){const kind=query('filter-kind').value,term=query('search').value.toLowerCase();let first=null;for(const node of nodes){const match=(kind==='all'||kind===node.kind)&&JSON.stringify(node.row).toLowerCase().includes(term);node.el.classList.toggle('hidden',!match);if(match&&!first)first=node;}if(first&&(kind!=='all'||term)){layout.pan={x:25-first.x*layout.scale,y:45-first.y*layout.scale};}apply();}
query('filter-kind').onchange=filter;query('search').oninput=filter;
query('save-layout').onclick=()=>{try{localStorage.setItem(key,JSON.stringify(layout));query('save-layout').textContent='布局已保存';}catch(e){query('save-layout').textContent='无法保存；请导出';}};
query('export-review').onclick=()=>{const report={schema:'story-canvas-review-export/v1',source_board_sha256:data.validation.source_sha256,title:board.title,layout,notes,decisions,selection_changed:false,warning:'Local recommendations only; source board, generation status and approval are unchanged.'};const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));const anchor=document.createElement('a');anchor.href=url;anchor.download='story-board-review.json';document.body.append(anchor);anchor.click();anchor.remove();setTimeout(()=>URL.revokeObjectURL(url),2000);};
apply();
</script></html>"""

def write_new(path, text, source):
    output = Path(path).resolve()
    if output == source or output.exists():
        raise ValueError("Output exists or equals the source board; choose a new filename")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(text)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "rank", "build"):
        command = commands.add_parser(name)
        command.add_argument("board")
        command.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        source = Path(args.board).resolve()
        if Path(args.out).resolve() == source or Path(args.out).exists():
            raise ValueError("Output exists or equals the input board")
        board, maps, previews, eligible, report = inspect(source)
        rank = ranking(maps, eligible)
        if args.command == "build":
            payload = json.dumps({"board": board, "previews": previews, "eligibility": eligible, "validation": report, "ranking": rank},
                                 ensure_ascii=False, allow_nan=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            write_new(args.out, HTML.replace("__DATA__", payload), source)
        else:
            value = report if args.command == "validate" else {"validation": report, **rank}
            write_new(args.out, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", source)
        print(json.dumps({"output": str(Path(args.out).resolve()), "status": report["status"], "errors": report["errors"],
                          "warnings": report["warnings"], "generation_performed": False}, ensure_ascii=False))
        return 10 if report["errors"] else 0
    except (ValueError, OSError, json.JSONDecodeError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
