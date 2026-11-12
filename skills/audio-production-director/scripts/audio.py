#!/usr/bin/env python3
"""Local, non-destructive cue mixing and video muxing. Python stdlib + FFmpeg."""
from __future__ import annotations

import argparse
import csv
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROLES = ("dialogue", "voiceover", "music", "sfx", "ambience")


def number(value, label, low=None, high=None):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number, not a boolean")
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"{label} must be a finite number")
    if not math.isfinite(result) or (low is not None and result < low) or (high is not None and result > high):
        raise ValueError(f"Invalid {label}: {value}")
    return result


def boolean(value, label):
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be true or false")
    return value


def run(argv):
    command = [str(value) for value in argv]
    process = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, encoding="utf-8", errors="replace")
    if process.returncode:
        raise ValueError(f"Command failed ({process.returncode}): {command[0]}\n{process.stderr[-5000:]}")
    return process


def ff(*args):
    return ["ffmpeg", "-hide_banner", "-nostdin", "-filter_threads", "2",
            "-filter_complex_threads", "2", *args]


def dump(path, value):
    with Path(path).open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write("\n")


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        while block := source.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def media(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError(f"Missing local media: {path}")
    raw = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                          "-of", "json", path]).stdout)
    audio = next((item for item in raw.get("streams", []) if item.get("codec_type") == "audio"), None)
    video = next((item for item in raw.get("streams", []) if item.get("codec_type") == "video"), None)
    def duration(stream):
        if not stream:
            return None, "no_stream"
        # A container can outlive its audio stream. Never pad speech on the
        # strength of format.duration alone.
        try:
            timestamp_duration = int(stream["duration_ts"]) * Fraction(stream["time_base"])
            if timestamp_duration > 0:
                return float(timestamp_duration), "stream_duration_ts"
        except (KeyError, ValueError, TypeError, ZeroDivisionError):
            pass
        candidates = [(stream.get("duration"), "stream_duration")]
        tag = next((value for key, value in stream.get("tags", {}).items()
                    if key.upper() == "DURATION"), None)
        if isinstance(tag, str):
            match = re.fullmatch(r"(\d+):([0-5]\d):([0-5]\d(?:\.\d+)?)", tag.strip())
            if match:
                candidates.append((int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3]), "stream_duration_tag"))
        for candidate, origin in candidates:
            try:
                value = float(candidate)
                if math.isfinite(value) and value > 0:
                    return value, origin
            except (ValueError, TypeError):
                pass
        return None, "unknown_stream_duration"
    audio_duration, audio_origin = duration(audio)
    video_duration, video_origin = duration(video)
    if video is not None and video_duration is None:
        try:
            fallback = float(raw.get("format", {}).get("duration"))
            if math.isfinite(fallback) and fallback > 0:
                video_duration, video_origin = fallback, "container_fallback_requires_packet_validation"
        except (ValueError, TypeError):
            pass
    return {"path": str(path), "audio": audio, "video": video,
            "audio_duration": audio_duration, "audio_duration_source": audio_origin,
            "video_duration": video_duration, "video_duration_source": video_origin,
            "format": raw.get("format", {})}


def local(value, base):
    if not isinstance(value, str) or not value or re.match(r"^[A-Za-z]+://", value):
        raise ValueError("A local source path is required, not a URL")
    path = Path(value).expanduser()
    return (base / path if not path.is_absolute() else path).resolve()


def load_plan(path):
    path = Path(path).resolve()
    plan = json.loads(path.read_text(encoding="utf-8-sig"))
    if type(plan.get("version")) is not int or plan["version"] != 1:
        raise ValueError("Only audio-plan version 1 is supported")
    duration = number(plan.get("duration"), "duration", 1 / 48000, 86400)
    if plan.get("sample_rate", 48000) != 48000 or plan.get("channels", 2) != 2:
        raise ValueError("This interchange renderer outputs 48000 Hz stereo only")
    samples = round(duration * 48000)
    duration = samples / 48000
    target = number(plan.get("target_lufs", -16), "target_lufs", -30, -9)
    peak = number(plan.get("true_peak_db", -1.5), "true_peak_db", -9, -0.1)
    duck = plan.get("ducking", {})
    if not isinstance(duck, dict):
        raise ValueError("ducking must be an object")
    duck = {"enabled": boolean(duck.get("enabled", True), "ducking.enabled"),
            "threshold_db": number(duck.get("threshold_db", -30), "duck threshold_db", -60, 0),
            "ratio": number(duck.get("ratio", 8), "duck ratio", 1, 20),
            "attack_ms": number(duck.get("attack_ms", 15), "duck attack_ms", 0.01, 2000),
            "release_ms": number(duck.get("release_ms", 250), "duck release_ms", 0.01, 9000)}
    if not isinstance(plan.get("tracks"), list) or not plan["tracks"]:
        raise ValueError("At least one real local track is required")
    tracks, cache, identifiers = [], {}, set()
    for index, original in enumerate(plan["tracks"]):
        if not isinstance(original, dict):
            raise ValueError("Each track must be an object")
        ident = str(original.get("id", f"cue-{index+1:03d}"))
        if not ident or ident in identifiers:
            raise ValueError("Track IDs must be nonempty and unique")
        identifiers.add(ident)
        role = original.get("role")
        if role not in ROLES:
            raise ValueError(f"Unsupported role: {role}")
        source = local(original.get("source"), path.parent)
        if str(source) not in cache:
            cache[str(source)] = media(source)
        info = cache[str(source)]
        if not info["audio"] or not info["audio_duration"]:
            raise ValueError(f"Source has no reliable finite audio stream: {source}")
        start = number(original.get("source_start", 0), "source_start", 0)
        at = number(original.get("at", 0), "at", 0)
        length = number(original.get("duration"), "track.duration", 1 / 48000)
        at_samples, length_samples = round(at * 48000), round(length * 48000)
        if length_samples <= 0 or at_samples + length_samples > samples:
            raise ValueError(f"Track {ident} is outside the output timeline")
        loop = boolean(original.get("loop", False), "loop")
        if loop and role not in ("music", "ambience"):
            raise ValueError("Only music and ambience may explicitly loop; speech and effects never loop")
        if start >= info["audio_duration"] or (not loop and start + length > info["audio_duration"] + 0.001):
            raise ValueError(f"Requested source range exceeds measured audio duration: {ident}")
        fade_in = number(original.get("fade_in", 0), "fade_in", 0, length)
        fade_out = number(original.get("fade_out", 0), "fade_out", 0, length)
        if fade_in + fade_out > length + 1 / 48000:
            raise ValueError("Fades cannot consume more than the selected cue duration")
        gain = number(original.get("gain_db", 0), "gain_db", -90, 24)
        should_duck = boolean(original.get("duck", role in ("music", "ambience")), "duck")
        if should_duck and role not in ("music", "ambience"):
            raise ValueError("Ducking is only applied to music/ambience; speech is the control")
        tracks.append({**original, "id": ident, "role": role, "source": str(source),
                       "source_start": start, "at": at_samples / 48000,
                       "duration": length_samples / 48000, "at_samples": at_samples,
                       "length_samples": length_samples, "gain_db": gain, "fade_in": fade_in,
                       "fade_out": fade_out, "loop": loop, "duck": should_duck})
    return {**plan, "duration": duration, "sample_rate": 48000, "channels": 2,
            "target_lufs": target, "true_peak_db": peak, "ducking": duck, "tracks": tracks,
            "_samples": samples, "_sources": cache, "_path": str(path)}


def decode_check(path):
    run(ff("-v", "error", "-xerror", "-i", path, "-map", "0:a:0", "-vn", "-f", "null", "-"))


def measure(path, target, peak):
    process = run(ff("-v", "info", "-nostats", "-i", path, "-map", "0:a:0", "-vn",
                     "-af", f"loudnorm=I={target}:TP={peak}:LRA=11:print_format=json", "-f", "null", "-"))
    found = re.findall(r'\{\s*"input_i".*?\}', process.stderr, re.S)
    if not found:
        raise ValueError("Loudness detector returned no parseable result")
    raw = json.loads(found[-1])
    values = {}
    for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        try:
            value = float(raw[key])
            values[key] = value if math.isfinite(value) else None
        except (ValueError, TypeError, KeyError):
            values[key] = None
    return {"integrated_lufs": values["input_i"], "true_peak_dbtp": values["input_tp"],
            "loudness_range_lu": values["input_lra"],
            "measurable": all(value is not None for value in values.values()),
            "raw": raw}


def combine(paths, destination, samples):
    inputs = []
    for path in paths:
        inputs += ["-i", str(path)]
    if len(paths) == 1:
        filters = f"[0:a]apad,atrim=end_sample={samples},asetpts=PTS-STARTPTS[mix]"
    else:
        filters = "".join(f"[{index}:a]" for index in range(len(paths)))
        filters += f"amix=inputs={len(paths)}:normalize=0:duration=longest:dropout_transition=0,apad,atrim=end_sample={samples},asetpts=PTS-STARTPTS[mix]"
    run(ff("-v", "error", "-y", *inputs, "-filter_complex", filters, "-map", "[mix]",
           "-c:a", "pcm_f32le", "-ar", "48000", "-ac", "2", destination))


def exact_audio(path, samples):
    info = media(path)
    if not info["audio"] or int(info["audio"].get("sample_rate", 0)) != 48000 or int(info["audio"].get("channels", 0)) != 2:
        raise ValueError("Output is not 48 kHz stereo")
    if abs(info["audio_duration"] - samples / 48000) > 2 / 48000:
        raise ValueError("Output duration differs from the sample-grid plan")
    decode_check(path)
    return info


def mix(plan_path, output_dir):
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=False)
    work = out / "_work"
    work.mkdir()
    report = {"schema": "audio-production-director.mix/v1", "status": "in_progress",
              "listening_review": "not_performed_by_script",
              "units": {"loudness": "LUFS", "true_peak": "dBTP", "gain": "dB", "time": "seconds"}}
    try:
        plan = load_plan(plan_path)
        samples = plan["_samples"]
        originals = {source: sha256(source) for source in plan["_sources"]}
        report.update({"name": plan.get("name", "Audio production"), "duration": plan["duration"],
                       "sample_rate": 48000, "channels": 2, "target_lufs": plan["target_lufs"],
                       "true_peak_ceiling_dbtp": plan["true_peak_db"], "source_sha256": originals,
                       "plan_path": plan["_path"], "tracks": [], "stems": []})
        files = []
        for index, track in enumerate(plan["tracks"]):
            extracted = work / f"extracted-{index:04d}.wav"
            destination = work / f"cue-{index:04d}.wav"
            inputs = ["-stream_loop", "-1"] if track["loop"] else []
            inputs += ["-ss", f'{track["source_start"]:.9f}', "-i", track["source"]]
            filters = [f'atrim=duration={track["duration"]:.9f}', "asetpts=PTS-STARTPTS",
                       "aresample=48000", "aformat=sample_fmts=flt:sample_rates=48000:channel_layouts=stereo",
                       f'volume={track["gain_db"]:.9f}dB']
            if track["fade_in"]:
                filters.append(f'afade=t=in:st=0:d={track["fade_in"]:.9f}')
            if track["fade_out"]:
                filters.append(f'afade=t=out:st={track["duration"]-track["fade_out"]:.9f}:d={track["fade_out"]:.9f}')
            filters.append(f'atrim=end_sample={track["length_samples"]}')
            run(ff("-v", "error", "-xerror", "-y", *inputs, "-map", "0:a:0", "-vn",
                   "-af", ",".join(filters), "-c:a", "pcm_f32le", "-ar", "48000", "-ac", "2", extracted))
            # Validate actual decoded samples BEFORE any alignment padding.
            # Incorrect/missing source tags must not turn a short speech clip
            # into a supposedly valid full-length cue.
            extracted_info = media(extracted)
            actual_samples = round((extracted_info["audio_duration"] or 0) * 48000)
            if actual_samples < track["length_samples"] - 2:
                raise ValueError(f'Actual decoded cue is shorter than its requested range: {track["id"]}; '
                                 f'{actual_samples} samples < {track["length_samples"]}. No speech padding was accepted.')
            track["decoded_samples_before_padding"] = actual_samples
            alignment = [f'adelay=delays={track["at_samples"]}S:all=1',
                         "apad", f"atrim=end_sample={samples}", "asetpts=PTS-STARTPTS"]
            run(ff("-v", "error", "-xerror", "-y", "-i", extracted,
                   "-af", ",".join(alignment), "-c:a", "pcm_f32le", "-ar", "48000", "-ac", "2", destination))
            exact_audio(destination, samples)
            files.append(destination)
        controls = [files[index] for index, track in enumerate(plan["tracks"]) if track["role"] in ("dialogue", "voiceover")]
        control = work / "speech-sidechain.wav"
        if controls:
            combine(controls, control, samples)
        duck = plan["ducking"]
        processed = []
        for index, (path, track) in enumerate(zip(files, plan["tracks"])):
            applied = bool(duck["enabled"] and track["duck"] and controls)
            if applied:
                destination = work / f"ducked-{index:04d}.wav"
                threshold = 10 ** (duck["threshold_db"] / 20)
                graph = (f'[0:a][1:a]sidechaincompress=threshold={threshold:.9f}'
                         f':ratio={duck["ratio"]}:attack={duck["attack_ms"]}:release={duck["release_ms"]}'
                         f':detection=rms:link=average,apad,atrim=end_sample={samples}[ducked]')
                run(ff("-v", "error", "-y", "-i", path, "-i", control,
                       "-filter_complex", graph, "-map", "[ducked]",
                       "-c:a", "pcm_f32le", "-ar", "48000", "-ac", "2", destination))
                path = destination
            processed.append(path)
            report["tracks"].append({key: value for key, value in track.items() if key not in ("at_samples", "length_samples")})
            report["tracks"][-1]["ducking_applied"] = applied
        (out / "stems").mkdir()
        role_paths = []
        for role in ROLES:
            selected = [processed[index] for index, track in enumerate(plan["tracks"]) if track["role"] == role]
            if not selected:
                continue
            raw_stem, stem = work / f"stem-{role}.wav", out / "stems" / f"{role}.wav"
            combine(selected, raw_stem, samples)
            role_paths.append(raw_stem)
            # Export finite 24-bit stems with a fixed sample-peak safety limiter.
            # The master mix below uses the unclipped float buses, then its own mastering.
            run(ff("-v", "error", "-y", "-i", raw_stem,
                   "-af", f"alimiter=limit=0.988553:level=false:latency=true,apad,atrim=end_sample={samples}",
                   "-c:a", "pcm_s24le", "-ar", "48000", "-ac", "2", stem))
            exact_audio(stem, samples)
            report["stems"].append({"role": role, "path": stem.relative_to(out).as_posix(),
                                    "stage": "pre-master; cue gain/fades/ducking and sample-peak safety limiter applied",
                                    "master_note": "Sum of stems is not guaranteed to equal mastered mix.wav"})
        raw_mix = work / "raw-mix.wav"
        combine(role_paths, raw_mix, samples)
        before = measure(raw_mix, plan["target_lufs"], plan["true_peak_db"])
        final = out / "mix.wav"
        if before["measurable"]:
            raw = before["raw"]
            filters = (f'loudnorm=I={plan["target_lufs"]}:TP={plan["true_peak_db"]}:LRA=11'
                       f':measured_I={raw["input_i"]}:measured_TP={raw["input_tp"]}'
                       f':measured_LRA={raw["input_lra"]}:measured_thresh={raw["input_thresh"]}'
                       f':offset={raw["target_offset"]}:linear=true:print_format=summary,aresample=48000')
        else:
            filters = "anull"
        peak_linear = 10 ** (plan["true_peak_db"] / 20)
        filters += f",alimiter=limit={peak_linear:.9f}:level=false:latency=true,apad,atrim=end_sample={samples}"
        candidate = work / "master.wav"
        run(ff("-v", "error", "-y", "-i", raw_mix, "-af", filters,
               "-c:a", "pcm_s24le", "-ar", "48000", "-ac", "2", candidate))
        after = measure(candidate, plan["target_lufs"], plan["true_peak_db"])
        attenuation = 0.0
        if after["true_peak_dbtp"] is not None and after["true_peak_dbtp"] > plan["true_peak_db"] + 0.05:
            attenuation = plan["true_peak_db"] - after["true_peak_dbtp"] - 0.1
            limited = work / "master-attenuated.wav"
            run(ff("-v", "error", "-y", "-i", candidate, "-af", f"volume={attenuation:.9f}dB",
                   "-c:a", "pcm_s24le", "-ar", "48000", "-ac", "2", limited))
            candidate = limited
            after = measure(candidate, plan["target_lufs"], plan["true_peak_db"])
        shutil.copyfile(candidate, final)
        exact_audio(final, samples)
        unchanged = all(sha256(source) == digest for source, digest in originals.items())
        if not unchanged:
            raise ValueError("A source changed during mixing; do not accept this output")
        issues = []
        if not after["measurable"]:
            issues.append("Loudness is unmeasurable/silent; do not claim a LUFS pass")
        else:
            if abs(after["integrated_lufs"] - plan["target_lufs"]) > 1:
                issues.append("Measured integrated loudness misses target by more than 1 LU")
            if after["true_peak_dbtp"] > plan["true_peak_db"] + 0.05:
                issues.append("Measured true peak exceeds configured ceiling")
        with (out / "cue-sheet.csv").open("x", encoding="utf-8-sig", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=["id", "role", "source", "at", "source_start", "duration", "gain_db", "fade_in", "fade_out", "loop", "duck"])
            writer.writeheader()
            for track in plan["tracks"]:
                writer.writerow({key: track[key] for key in writer.fieldnames})
        report.update({"status": "technical_checks_passed_pending_listening_review" if not issues else "technical_review_required",
                       "mix_path": "mix.wav", "before": before, "after": after,
                       "final_extra_attenuation_db": attenuation, "sources_unchanged": unchanged,
                       "issues": issues, "mix_sha256": sha256(final),
                       "ducking": {**duck, "speech_control_available": bool(controls)},
                       "loop_join_review": any(track["loop"] for track in plan["tracks"]),
                       "creative_review": "Check spoken text, pronunciation, music phrasing, loop joins, sync, masking and listening quality on actual audio."})
        dump(out / "report.json", report)
        shutil.rmtree(work)
        return report
    except Exception as exc:
        report.update({"status": "failed", "error": str(exc)})
        dump(out / "failed.json", report)
        raise


def video_hash(path):
    text = run(ff("-v", "error", "-i", path, "-map", "0:v:0", "-c:v", "copy",
                  "-f", "hash", "-hash", "sha256", "-")).stdout.strip()
    if not text.startswith("SHA256="):
        raise ValueError("No video packet hash was returned")
    return text.split("=", 1)[1]


def video_frames(path):
    value = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                 "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1:nk=1", path]).stdout.strip()
    try:
        return int(value)
    except ValueError:
        raise ValueError("Cannot establish video frame count")


def mux(video_path, audio_path, output_path):
    source, audio, out = Path(video_path).resolve(), Path(audio_path).resolve(), Path(output_path).resolve()
    sidecar = out.with_suffix(".audio.json")
    if out.exists() or sidecar.exists() or out in (source, audio) or sidecar in (source, audio):
        raise ValueError("Output/sidecar exists or equals an input; select a new basename")
    if out.suffix.lower() != ".mp4":
        raise ValueError("Mux output must be .mp4")
    vi, ai = media(source), media(audio)
    if not vi["video"] or not vi["video_duration"] or not ai["audio"] or not ai["audio_duration"]:
        raise ValueError("Reliable video duration and audio stream are required")
    duration = vi["video_duration"]
    if abs(ai["audio_duration"] - duration) > 0.025:
        raise ValueError("Mix duration must match video within 25 ms; rebuild plan rather than stretch speech")
    out.parent.mkdir(parents=True, exist_ok=True)
    originals = {str(source): sha256(source), str(audio): sha256(audio)}
    before_frames, packet_hash = video_frames(source), video_hash(source)
    try:
        run(ff("-v", "error", "-xerror", "-n", "-i", source, "-i", audio,
               "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
               "-af", f"aresample=48000,apad,atrim=duration={duration:.9f},asetpts=PTS-STARTPTS",
               "-ar", "48000", "-ac", "2", "-movflags", "+faststart", out))
        after = media(out)
        frames = video_frames(out)
        if frames != before_frames or video_hash(out) != packet_hash:
            raise ValueError("Video frame count or encoded packet payload changed")
        if abs(after["video_duration"] - duration) > 0.002:
            raise ValueError("Video duration changed")
        run(ff("-v", "error", "-xerror", "-i", out, "-f", "null", "-"))
        if not all(sha256(path) == digest for path, digest in originals.items()):
            raise ValueError("An input changed during mux")
        report = {"status": "technical_mux_passed_pending_listening_review", "output": str(out),
                  "video_duration": duration, "video_frames": frames, "video_stream_copy": True,
                  "video_packet_sha256": packet_hash, "sources_unchanged": True,
                  "audio_source": str(audio), "audio_mode": "replace existing soundtrack; no duplicate original dialogue",
                  "note": "AAC encoding may slightly alter true peak; rescan the actual muxed file before final delivery."}
        dump(sidecar, report)
        return report
    except Exception as exc:
        if not sidecar.exists():
            dump(sidecar, {"status": "failed", "error": str(exc), "output": str(out)})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    item = commands.add_parser("probe")
    item.add_argument("input")
    item = commands.add_parser("mix")
    item.add_argument("plan")
    item.add_argument("--out-dir", required=True)
    item = commands.add_parser("mux")
    item.add_argument("video")
    item.add_argument("--audio", required=True)
    item.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        if args.command == "probe":
            result = media(args.input)
        elif args.command == "mix":
            result = mix(args.plan, args.out_dir)
        else:
            result = mux(args.video, args.audio, args.out)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
