#!/usr/bin/env python3
"""Attach measured full-length audio stems to an FCP7 XML copy.

This is a structural interchange adapter, not a claim of native NLE acceptance.
Python standard library + ffprobe. No network or source-file writes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path, PureWindowsPath
import re
import subprocess
import sys
from fractions import Fraction
from xml.etree import ElementTree as ET

ROLES = ("dialogue", "voiceover", "music", "sfx", "ambience")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_timeline(path):
    data = path.read_bytes()
    if len(data) > 20 * 1024 * 1024:
        raise ValueError("XML exceeds the 20 MiB interchange limit")
    text = data.decode("utf-8-sig")
    if re.search(r"<!ENTITY\b", text, re.I):
        raise ValueError("XML entities are unsupported")
    doctypes = re.findall(r"<!DOCTYPE\b", text, re.I)
    without_plain = re.sub(r"<!DOCTYPE\s+xmeml\s*>", "", text, flags=re.I)
    if len(doctypes) > 1 or re.search(r"<!DOCTYPE\b", without_plain, re.I):
        raise ValueError("Only a plain <!DOCTYPE xmeml> is allowed; no external DTD or subset")
    try:
        root = ET.fromstring(without_plain)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid XML: {exc}") from exc
    sequences = root.findall("./sequence")
    if root.tag != "xmeml" or root.get("version") not in ("4", "5") or len(sequences) != 1:
        raise ValueError("Expected FCP7 xmeml version 4/5 with one direct sequence")
    sequence = sequences[0]
    try:
        frames = int(sequence.findtext("./duration", ""))
        timebase = int(sequence.findtext("./rate/timebase", ""))
    except ValueError as exc:
        raise ValueError("Sequence duration/timebase must be integer frame values") from exc
    ntsc_text = sequence.findtext("./rate/ntsc", "FALSE").upper()
    if frames <= 0 or not 1 <= timebase <= 240 or ntsc_text not in ("TRUE", "FALSE"):
        raise ValueError("Invalid sequence duration or frame rate")
    fps = Fraction(timebase * 1000, 1001) if ntsc_text == "TRUE" else Fraction(timebase)
    media = sequence.find("./media")
    if media is None or media.find("./video") is None:
        raise ValueError("Sequence needs a media/video subtree")
    if len(media.findall("./audio")) > 1:
        raise ValueError("Multiple media/audio containers are unsupported")
    return root, sequence, frames, timebase, ntsc_text, fps


def probe_audio(path, expected_seconds):
    if path.suffix.lower() != ".wav" or not path.is_file():
        raise ValueError(f"Expected an existing WAV: {path}")
    try:
        completed = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"ffprobe could not inspect {path.name}: {exc}") from exc
    if completed.returncode:
        raise ValueError(f"ffprobe failed for {path.name}: {completed.stderr[-1000:]}")
    try:
        data = json.loads(completed.stdout)
        streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
        if len(streams) != 1 or any(s.get("codec_type") == "video" for s in data.get("streams", [])):
            raise ValueError("Expected one audio stream and no video")
        stream = streams[0]
        sample_rate = int(stream["sample_rate"])
        channels = int(stream["channels"])
        duration = float(stream.get("duration") or data["format"]["duration"])
        depth = int(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or 0)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unreliable WAV metadata: {path.name}: {exc}") from exc
    if sample_rate != 48000 or channels not in (1, 2):
        raise ValueError(f"{path.name}: requires 48 kHz mono/stereo audio")
    if not str(stream.get("codec_name", "")).startswith("pcm_") or depth not in (16, 24, 32):
        raise ValueError(f"{path.name}: requires 16/24/32-bit PCM WAV")
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"{path.name}: no finite positive duration")
    if abs(duration - expected_seconds) > 2 / sample_rate + 1e-7:
        raise ValueError(
            f"{path.name}: duration {duration:.9f}s differs from sequence "
            f"{expected_seconds:.9f}s; prepare full-length aligned audio first"
        )
    return {"duration": duration, "sample_rate": sample_rate, "channels": channels,
            "depth": depth, "codec": stream["codec_name"], "sha256": sha256(path)}


def media_uri(path):
    """Path.as_uri performs the required Windows drive/Unicode/space escaping."""
    if isinstance(path, PureWindowsPath):
        return path.as_uri()
    return Path(path).resolve().as_uri()


def child(parent, name, text=None, **attrs):
    element = ET.SubElement(parent, name, {key: str(value) for key, value in attrs.items()})
    if text is not None:
        element.text = str(text)
    return element


def add_rate(parent, timebase, ntsc):
    rate = child(parent, "rate")
    child(rate, "timebase", timebase)
    child(rate, "ntsc", ntsc)


def source_description(file, path, info, frames, timebase, ntsc):
    child(file, "name", path.name)
    child(file, "pathurl", media_uri(path))
    add_rate(file, timebase, ntsc)
    child(file, "duration", frames)
    audio = child(child(file, "media"), "audio")
    characteristics = child(audio, "samplecharacteristics")
    child(characteristics, "depth", info["depth"])
    child(characteristics, "samplerate", info["sample_rate"])
    child(audio, "channelcount", info["channels"])


def fresh_id(prefix, seed, used):
    base = prefix + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    value, index = base, 0
    while value in used:
        index += 1
        value = f"{base}-{index}"
    used.add(value)
    return value


def attach(timeline, audio_dir, output, mode):
    if mode not in ("stems", "mix"):
        raise ValueError("mode must be stems or mix")
    if output.exists() or output.is_symlink():
        raise ValueError("Output exists; choose a new XML filename")
    timeline, audio_dir, output = timeline.resolve(), audio_dir.resolve(), output.resolve()
    if not timeline.is_file() or not audio_dir.is_dir():
        raise ValueError("Timeline and audio directory must exist")
    if output.exists() or output.is_symlink() or output == timeline:
        raise ValueError("Output exists or equals the source; choose a new XML filename")
    if output.suffix.lower() != ".xml":
        raise ValueError("Output must have an .xml extension")
    source_hash = sha256(timeline)
    root, sequence, frames, timebase, ntsc, fps = read_timeline(timeline)
    expected_seconds = float(Fraction(frames, 1) / fps)
    report_path = audio_dir / "report.json"
    if report_path.is_file():
        try:
            json.loads(report_path.read_text(encoding="utf-8-sig"))
        except (ValueError, UnicodeError) as exc:
            raise ValueError("Audio report.json is not readable JSON") from exc
    selected = [("mix", audio_dir / "mix.wav")] if mode == "mix" else [
        (role, audio_dir / "stems" / f"{role}.wav") for role in ROLES
        if (audio_dir / "stems" / f"{role}.wav").exists()
    ]
    if not selected:
        raise ValueError("No role stems found in audio-dir/stems; use --mode mix for the master")
    checked, identities = [], set()
    for role, path in selected:
        resolved = path.resolve()
        if not resolved.is_file():
            raise ValueError(f"Missing WAV: {path}")
        stat = resolved.stat()
        identity = (stat.st_dev, stat.st_ino)
        if resolved == output or identity in identities:
            raise ValueError("Duplicate audio sources or source/output collision")
        identities.add(identity)
        checked.append((role, resolved, probe_audio(resolved, expected_seconds)))
    media = sequence.find("./media")
    assert media is not None
    video = media.find("./video")
    assert video is not None
    video_before = ET.tostring(video, encoding="utf-8")
    old_audio = media.find("./audio")
    removed_ids = {item.get("id") for item in old_audio.findall(".//clipitem")} if old_audio is not None else set()
    removed_files = set(old_audio.findall(".//file")) if old_audio is not None else set()
    video_file_ids = {file.get("id") for file in video.findall(".//file") if file.get("id")}
    for file_id in video_file_ids:
        definitions = [file for file in root.iter("file")
                       if file.get("id") == file_id and file.findtext("./pathurl")]
        if any(file in removed_files for file in definitions) and not any(
                file not in removed_files for file in definitions):
            raise ValueError(
                "A video source file is defined only inside the replaced audio tracks. "
                "Export a self-contained basic XML or move its full definition into the "
                "video clip in an independent copy; video nodes are never silently changed."
            )
    for reference in video.findall(".//link/linkclipref"):
        if reference.text in removed_ids:
            raise ValueError(
                "Video has links to the replaced audio clips. Unlink them in an independent "
                "timeline copy or use the basic XML exporter; video links are never silently changed."
            )
    used_ids = {element.get("id") for element in root.iter() if element.get("id")}
    new_audio = ET.Element("audio")
    child(new_audio, "numOutputChannels", 2)
    characteristics = child(child(new_audio, "format"), "samplecharacteristics")
    child(characteristics, "depth", 24)
    child(characteristics, "samplerate", 48000)
    outputs = child(new_audio, "outputs")
    for channel in (1, 2):
        group = child(outputs, "group")
        child(group, "index", channel)
        child(group, "numchannels", 1)
        child(group, "downmix", 0)
        child(child(group, "channel"), "index", channel)
    track_cursor = 0
    for group_index, (role, path, info) in enumerate(checked, 1):
        # A stereo file becomes two linked mono-channel clips, routed left/right.
        # Mono is duplicated to both outputs; either route contains one source channel.
        channels = 2
        file_id = fresh_id("audioprod-file-", str(path), used_ids)
        clip_ids = [fresh_id("audioprod-clip-", f"{path}:{c}", used_ids) for c in range(channels)]
        for index in range(channels):
            track = child(new_audio, "track")
            item = child(track, "clipitem", id=clip_ids[index])
            child(item, "name", f"{role} - {'L' if index == 0 else 'R'}")
            child(item, "enabled", "TRUE")
            child(item, "duration", frames)
            add_rate(item, timebase, ntsc)
            child(item, "start", 0)
            child(item, "end", frames)
            child(item, "in", 0)
            child(item, "out", frames)
            file = child(item, "file", id=file_id)
            if index == 0:
                source_description(file, path, info, frames, timebase, ntsc)
            source_track = child(item, "sourcetrack")
            child(source_track, "mediatype", "audio")
            child(source_track, "trackindex", index + 1 if info["channels"] == 2 else 1)
            for linked_index, linked_id in enumerate(clip_ids):
                link = child(item, "link")
                child(link, "linkclipref", linked_id)
                child(link, "mediatype", "audio")
                child(link, "trackindex", track_cursor + linked_index + 1)
                child(link, "clipindex", 1)
                child(link, "groupindex", group_index)
            child(track, "enabled", "TRUE")
            child(track, "locked", "FALSE")
            child(track, "outputchannelindex", index + 1)
        track_cursor += channels
    if old_audio is None:
        media.append(new_audio)
    else:
        location = list(media).index(old_audio)
        media.remove(old_audio)
        media.insert(location, new_audio)
    if ET.tostring(video, encoding="utf-8") != video_before:
        raise ValueError("Internal error: video subtree changed")
    if sha256(timeline) != source_hash:
        raise ValueError("Source timeline changed during export; no output was written")
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + ET.tostring(root, encoding="utf-8") + b"\n"
    # Exclusive creation protects even an output created after the initial guard.
    with output.open("xb") as stream:
        stream.write(payload)
    return {
        "schema": "audio-production-director.nle/v1", "output": str(output),
        "source_xml_sha256": source_hash, "source_unchanged": sha256(timeline) == source_hash,
        "mode": mode, "sequence_frames": frames, "sequence_fps": str(fps),
        "sequence_seconds": expected_seconds, "new_audio_tracks": track_cursor,
        "replaced_audio_tracks": len(old_audio.findall("./track")) if old_audio is not None else 0,
        "sources": [{"role": role, "path": str(path), **info} for role, path, info in checked],
        "software_ui_verified": False,
        "note": "XML structure and measured WAV duration checked; import in a new NLE timeline and inspect/listen.",
        "master_note": (
            "Pre-master stems are editable; summing them may differ from the two-pass-normalized/limited master."
            if mode == "stems" else "Master mix is baked; its internal voices, music and effects are not separately editable."
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeline", type=Path, required=True)
    parser.add_argument("--audio-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mode", choices=("stems", "mix"), default="stems")
    args = parser.parse_args(argv)
    try:
        report = attach(args.timeline, args.audio_dir, args.out, args.mode)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
