#!/usr/bin/env python3
"""Build and run a zero-generation-cost synthetic production project."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

def run(argv):
    result = subprocess.run([str(x) for x in argv], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {argv[0]}\n{result.stdout[-10000:]}\n{result.stderr[-5000:]}")
    return result.stdout

def dump(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--skills-root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    root = args.out_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    fixtures = root / "fixtures"; fixtures.mkdir()
    for name, fps, frequency in (("a",25,440),("b",30,660)):
        run(["ffmpeg","-hide_banner","-nostdin","-loglevel","error",
             "-f","lavfi","-i",f"testsrc2=size=320x180:rate={fps}:duration=3.2",
             "-f","lavfi","-i",f"sine=frequency={frequency}:sample_rate=48000:duration=3.2",
             "-c:v","libx264","-pix_fmt","yuv420p","-c:a","aac","-shortest",
             fixtures/f"{name}.mp4"])
    run(["ffmpeg","-hide_banner","-nostdin","-loglevel","error","-f","lavfi","-i",
         "sine=frequency=220:sample_rate=48000:duration=4.2","-ac","2","-c:a","pcm_s24le",fixtures/"music.wav"])
    job = {
        "version":1,"name":"Synthetic automation verification","fps":25,"width":320,"height":180,
        "clips":[{"id":"A01","source":str(fixtures/"a.mp4"),"start":0.12,"end":1.896},
                 {"id":"A02","source":str(fixtures/"b.mp4"),"start":0.24,"end":2.364}],
        "broll":[{"source":str(fixtures/"b.mp4"),"at":0.8,"source_start":0,"duration":0.6,"fit":"cover"}],
        "subtitles":[{"start":0.1,"end":1.5,"text":"Synthetic test: no AI media generated"},
                     {"start":1.9,"end":3.4,"text":"Technical checks, pending creative review"}],
        "grade":{"preset":"neutral","lut":None},"audio":{"target_lufs":-16}
    }
    job_path = fixtures/"job.json"; dump(job_path,job)
    def stage(ident,action,needs=(),**params):
        return {"id":ident,"kind":"local","action":action,"needs":list(needs),"params":params}
    steps = [
        stage("preflight","preflight"),
        stage("render","render",["preflight"],job=str(job_path)),
        stage("nle","export_nle",["render"],job=str(job_path)),
        stage("audio_plan","prepare_audio_plan",["render"],job=str(job_path),video="@render:video",
              voice_mode="preserve",tracks=[{"id":"M01","role":"music","source":str(fixtures/"music.wav"),
                                            "at":0,"source_start":0,"duration":3.88,"gain_db":-18,
                                            "fade_in":0.1,"fade_out":0.2,"duck":True}]),
        stage("mix","mix_audio",["audio_plan"],audio_plan="@audio_plan:audio_plan"),
        stage("mux","mux_audio",["render","mix"],video="@render:video",audio="@mix:mix"),
        stage("audio_nle","export_audio_nle",["nle","mix"],timeline="@nle:xml",mix_dir="@mix:mix_dir"),
        stage("qc","qc_scan",["mux","render"],video="@mux:video",srt="@render:srt",extract_frames=True),
        stage("repair","qc_repair",["qc","mux","render"],video="@mux:video",srt="@render:srt",
              normalize_loudness=True,fix_subtitles=True,max_rounds=2,extract_frames=False),
        stage("final_qc","qc_scan",["repair"],video="@repair:video",srt="@repair:srt",extract_frames=True),
        stage("package","package_delivery",["final_qc","audio_nle","mix","repair","nle"],
              files=[{"role":"video","path":"@repair:video"},{"role":"srt","path":"@repair:srt"},
                     {"role":"xml","path":"@audio_nle:xml"},{"role":"audio","path":"@mix:mix_dir"}],
              qc_report="@final_qc:qc_report",acceptance="technical",
              delivery_profile={"width":320,"height":180,"fps":25},
              include_source_media=True,job=str(job_path))
    ]
    spec = {"version":1,"id":"synthetic-production-demo","kind":"talking_head",
            "brief":{"purpose":"Synthetic automation proof; no speech/AI creation or artistic acceptance",
                     "expected_frames":97,"actual_user_media":False},
            "permissions":{"generate_media":False},
            "limits":{"max_generation_calls":0,"max_generation_credits":0,"max_repair_rounds":2},
            "steps":steps}
    spec_path=root/"demo-spec.json";dump(spec_path,spec)
    project=root/"project";engine=Path(__file__).with_name("orchestrator.py")
    run([sys.executable,engine,"init",spec_path,"--project",project])
    first=json.loads(run([sys.executable,engine,"run",project,"--skills-root",args.skills_root]))
    if first["status"]!="technical_done_pending_review":
        raise RuntimeError(f"Unexpected demo acceptance status: {first['status']}")
    state=json.loads((project/"state.json").read_text())
    attempts={k:v["attempt"] for k,v in state["steps"].items()}
    second=json.loads(run([sys.executable,engine,"run",project,"--skills-root",args.skills_root]))
    resumed=json.loads((project/"state.json").read_text())
    if attempts!={k:v["attempt"] for k,v in resumed["steps"].items()}:
        raise RuntimeError("Unchanged resume reran one or more stages")
    package=next(x["path"] for x in resumed["steps"]["package"]["artifacts"] if x["role"]=="delivery_manifest")
    summary={"status":second["status"],"project":str(project),"stages":len(steps),"expected_frames":97,
             "resume_reused_all_stages":True,"generation_calls":0,"creative_review":"not_performed",
             "delivery_manifest":package,"delivery_manifest_sha256":hashlib.sha256(Path(package).read_bytes()).hexdigest()}
    dump(root/"verification.json",summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__=="__main__":
    main()
