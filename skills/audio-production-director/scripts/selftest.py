#!/usr/bin/env python3
"""Real, synthetic-media regression tests; no network or paid generation."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import struct
import subprocess
import sys

SCRIPT = Path(__file__).with_name("audio.py")

def invoke(*args, ok=True):
    result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True)
    if ok and result.returncode:
        raise AssertionError(result.stderr)
    if not ok and not result.returncode:
        raise AssertionError("Expected rejection: " + " ".join(map(str, args)))
    return result

def ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-filter_threads", "2", "-filter_complex_threads", "2", *map(str,args)], check=True)

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def rms(path, start, duration):
    process = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start), "-i", str(path),
                              "-t", str(duration), "-map", "0:a:0", "-ac", "1",
                              "-f", "f32le", "-"], check=True, capture_output=True)
    values = struct.unpack("<" + "f" * (len(process.stdout)//4), process.stdout)
    return math.sqrt(sum(value*value for value in values)/len(values))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    root = args.out_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    assertions = []
    def check(condition, name):
        assert condition, name
        assertions.append(name)

    voice, music, effect = root/"speech tone.wav", root/"music tone.wav", root/"effect.wav"
    ff("-f","lavfi","-i","sine=frequency=880:duration=2:sample_rate=48000",
       "-af","volume=3","-c:a","pcm_s24le",voice)
    ff("-f","lavfi","-i","sine=frequency=220:duration=6:sample_rate=48000",
       "-c:a","pcm_s24le",music)
    ff("-f","lavfi","-i","sine=frequency=1700:duration=0.4:sample_rate=48000",
       "-c:a","pcm_s24le",effect)
    hashes={str(path):sha(path) for path in (voice,music,effect)}
    plan={"version":1,"name":"Synthetic cue mix","duration":6,"sample_rate":48000,
          "channels":2,"target_lufs":-16,"true_peak_db":-1.5,
          "ducking":{"enabled":True,"threshold_db":-30,"ratio":8,"attack_ms":15,"release_ms":250},
          "tracks":[
              {"id":"VO1","role":"voiceover","source":voice.name,"at":2,"source_start":0,"duration":2,"gain_db":0,"fade_in":0.01,"fade_out":0.01},
              {"id":"MU1","role":"music","source":music.name,"at":0,"source_start":0,"duration":6,"gain_db":-3,"fade_in":0.1,"fade_out":0.15},
              {"id":"FX1","role":"sfx","source":effect.name,"at":4.5,"source_start":0,"duration":0.4,"gain_db":-8,"fade_in":0.01,"fade_out":0.04}
          ]}
    plan_file=root/"audio-plan.json";save(plan_file,plan)
    out=root/"mix output"
    result=invoke("mix",plan_file,"--out-dir",out)
    report=json.loads(result.stdout)
    check(report["status"]=="technical_checks_passed_pending_listening_review","master loudness and true peak measured within targets")
    check(abs(report["duration"]-6)<1/48000,"sample-grid duration preserved")
    check(report["sample_rate"]==48000 and report["channels"]==2,"48 kHz stereo output")
    check({s["role"] for s in report["stems"]}=={"voiceover","music","sfx"},"independent role stems created")
    check(all((out/s["path"]).is_file() for s in report["stems"]),"stem report paths resolve")
    music_stem=out/"stems/music.wav"
    ratio=rms(music_stem,2.7,0.6)/rms(music_stem,0.5,0.6)
    check(ratio<0.45,"music measurably ducked during speech")
    voice_stem=out/"stems/voiceover.wav"
    check(rms(voice_stem,0.5,0.5)<1e-6 and rms(voice_stem,2.5,0.5)>0.03,"voice cue aligned at requested output time")
    check(not report["tracks"][2]["ducking_applied"],"SFX not automatically ducked")
    check(all(sha(path)==digest for path,digest in hashes.items()),"source WAVs unchanged")
    check((out/"cue-sheet.csv").is_file() and (out/"report.json").is_file(),"cue sheet and provenance report exist")
    original_mix=sha(out/"mix.wav")
    invoke("mix",plan_file,"--out-dir",out,ok=False)
    check(sha(out/"mix.wav")==original_mix,"existing output directory rejected without overwrite")

    bad=copy.deepcopy(plan);bad["tracks"][0]["loop"]=True
    save(root/"bad-loop.json",bad)
    invoke("mix",root/"bad-loop.json","--out-dir",root/"bad-loop",ok=False)
    check((root/"bad-loop/failed.json").exists(),"speech loop rejected with failure evidence")
    bad=copy.deepcopy(plan);bad["tracks"][0]["duration"]=3
    save(root/"bad-range.json",bad)
    invoke("mix",root/"bad-range.json","--out-dir",root/"bad-range",ok=False)
    check((root/"bad-range/failed.json").exists(),"source overrun rejected rather than padding speech")
    bad=copy.deepcopy(plan);bad["tracks"][0]["at"]=float("nan")
    save(root/"bad-nan.json",bad)
    invoke("mix",root/"bad-nan.json","--out-dir",root/"bad-nan",ok=False)
    check((root/"bad-nan/failed.json").exists(),"NaN rejected")
    bad=copy.deepcopy(plan);bad["tracks"][0]["duration"]=True
    save(root/"bad-boolean.json",bad)
    invoke("mix",root/"bad-boolean.json","--out-dir",root/"bad-boolean",ok=False)
    check((root/"bad-boolean/failed.json").exists(),"boolean is not accepted as a cue duration")
    corrupt=root/"corrupt.wav";corrupt.write_bytes(b"not an audio container")
    bad=copy.deepcopy(plan);bad["tracks"][0]["source"]=corrupt.name
    save(root/"bad-media.json",bad)
    invoke("mix",root/"bad-media.json","--out-dir",root/"bad-media",ok=False)
    check((root/"bad-media/failed.json").exists(),"bad media fails closed")

    loop_plan=copy.deepcopy(plan)
    loop_plan["tracks"]=[{"id":"AM1","role":"ambience","source":effect.name,
                          "at":0,"source_start":0,"duration":6,"loop":True,"gain_db":-12}]
    save(root/"loop-plan.json",loop_plan)
    loop_report=json.loads(invoke("mix",root/"loop-plan.json","--out-dir",root/"loop-mix").stdout)
    check(loop_report["loop_join_review"] and loop_report["duration"]==6,"explicit ambience looping fills requested duration")

    silent=root/"silence.wav"
    ff("-f","lavfi","-i","anullsrc=r=48000:cl=stereo","-t","6","-c:a","pcm_s24le",silent)
    silence_plan=copy.deepcopy(plan)
    silence_plan["tracks"]=[{"id":"SI1","role":"dialogue","source":silent.name,"at":0,"duration":6}]
    save(root/"silence-plan.json",silence_plan)
    silent_report=json.loads(invoke("mix",root/"silence-plan.json","--out-dir",root/"silent-mix").stdout)
    check(not silent_report["after"]["measurable"] and silent_report["status"]=="technical_review_required","pure silence not mislabeled loudness success")

    video=root/"original video.mp4"
    ff("-f","lavfi","-i","testsrc2=size=320x180:rate=25:duration=6","-i",music,
       "-map","0:v:0","-map","1:a:0","-c:v","libx264","-threads","2",
       "-pix_fmt","yuv420p","-c:a","aac","-t","6",video)
    video_hash=sha(video);video_out=root/"with mix.mp4"
    mux_report=json.loads(invoke("mux",video,"--audio",out/"mix.wav","--out",video_out).stdout)
    check(mux_report["video_frames"]==150 and mux_report["video_stream_copy"],"mux preserves 150 video frames and encoded packet payload")
    check(sha(video)==video_hash and sha(out/"mix.wav")==original_mix,"mux preserves input video and mix")
    check(mux_report["audio_mode"].startswith("replace"),"mux replaces source soundtrack instead of duplicating dialogue")
    invoke("mux",video,"--audio",out/"mix.wav","--out",video_out,ok=False)
    check(sha(video)==video_hash,"existing mux output rejected")
    invoke("mux",video,"--audio",voice,"--out",root/"bad-short-mux.mp4",ok=False)
    check(not (root/"bad-short-mux.mp4").exists(),"duration-mismatched audio rejected before mux")
    (root/"reserved.audio.json").write_text("keep this log")
    invoke("mux",video,"--audio",out/"mix.wav","--out",root/"reserved.mp4",ok=False)
    check(not (root/"reserved.mp4").exists() and (root/"reserved.audio.json").read_text()=="keep this log","existing mux sidecar protected")
    extracted=copy.deepcopy(plan)
    extracted["tracks"]=[{"id":"VID1","role":"dialogue","source":video.name,"at":0,"duration":6}]
    save(root/"video-audio-plan.json",extracted)
    video_audio_report=json.loads(invoke("mix",root/"video-audio-plan.json","--out-dir",root/"video-audio-mix").stdout)
    check(video_audio_report["sources_unchanged"],"audio track can be read from MP4 without altering video")
    short_audio_video=root/"video long audio short.mkv"
    ff("-f","lavfi","-i","testsrc2=size=160x90:rate=25:duration=6","-i",voice,
       "-map","0:v:0","-map","1:a:0","-c:v","libx264","-threads","2",
       "-pix_fmt","yuv420p","-c:a","pcm_s16le",short_audio_video)
    bad_source_plan=copy.deepcopy(silence_plan)
    bad_source_plan["tracks"]=[{"id":"SHORT1","role":"voiceover","source":short_audio_video.name,"at":0,"duration":6}]
    save(root/"short-stream-plan.json",bad_source_plan)
    invoke("mix",root/"short-stream-plan.json","--out-dir",root/"short-stream-mix",ok=False)
    short_failure=json.loads((root/"short-stream-mix/failed.json").read_text())
    check("exceeds measured audio duration" in short_failure["error"],"audio stream DURATION tag overrides longer video/container duration")
    forged=root/"forged audio duration.mkv"
    raw=short_audio_video.read_bytes()
    assert b"00:00:02.000000000" in raw
    forged.write_bytes(raw.replace(b"00:00:02.000000000",b"00:00:06.000000000"))
    bad_source_plan["tracks"][0]["source"]=forged.name
    save(root/"forged-stream-plan.json",bad_source_plan)
    invoke("mix",root/"forged-stream-plan.json","--out-dir",root/"forged-stream-mix",ok=False)
    forged_failure=json.loads((root/"forged-stream-mix/failed.json").read_text())
    check("Actual decoded cue is shorter" in forged_failure["error"],"decoded samples are checked before padding even when duration metadata lies")
    unknown=root/"unknown audio duration.mkv"
    unknown.write_bytes(raw.replace(b"DURATION",b"XURATION"))
    bad_source_plan["tracks"][0]["source"]=unknown.name
    save(root/"unknown-stream-plan.json",bad_source_plan)
    invoke("mix",root/"unknown-stream-plan.json","--out-dir",root/"unknown-stream-mix",ok=False)
    unknown_failure=json.loads((root/"unknown-stream-mix/failed.json").read_text())
    check("no reliable finite audio stream" in unknown_failure["error"],"unknown audio stream duration does not inherit container duration")
    results={"assertions_passed":len(assertions),"assertions":assertions,"ducked_music_rms_ratio":ratio,
             "generation_tools_called":False,"listening_or_artistic_acceptance":False}
    save(root/"verification.json",results)
    print(json.dumps(results,ensure_ascii=False,indent=2))

if __name__=="__main__":
    main()
