#!/usr/bin/env python3
"""Synthetic scheduler tests; no real provider submission or artistic acceptance."""
from __future__ import annotations
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import wave
import zlib

STUB = r'''
from pathlib import Path
import hashlib,json
def execute(action,params,project_dir,output_dir,skills_root):
    output_dir=Path(output_dir)
    if action=="fail":
        return {"status":"failed","error":"synthetic failure","artifacts":[]}
    if action=="directory":
        folder=output_dir/"exports";folder.mkdir()
        (folder/"empty.srt").write_text("")
        (folder/"manifest.json").write_text('{"synthetic":true}')
        return {"status":"succeeded","artifacts":[{"role":"directory","path":str(folder)}],"metrics":{}}
    source=Path(params["source"])
    if not source.is_absolute():source=Path(project_dir)/source
    target=output_dir/("package.json" if action=="package_delivery" else "result.txt")
    if action=="package_delivery":
        target.write_text('{"synthetic_package":true}')
    else:
        target.write_bytes(source.read_bytes())
    return {"status":"succeeded","artifacts":[{"role":"file","path":str(target)}],
            "metrics":{"input_files":[{"path":str(source),"sha256":hashlib.sha256(source.read_bytes()).hexdigest()}],
                       "acceptance":"production_accepted" if action=="package_delivery" and params.get("acceptance")=="production" else "technical_ready_pending_creative_review"}}
'''

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out",required=True)
    args=parser.parse_args()
    root=Path(args.out).resolve();root.mkdir(parents=True,exist_ok=False)
    runner=root/"engine";runner.mkdir()
    shutil.copyfile(Path(__file__).with_name("orchestrator.py"),runner/"orchestrator.py")
    (runner/"pipeline_adapter.py").write_text(STUB)
    engine=runner/"orchestrator.py"
    checks=[]
    def check(value,label):
        assert value,label
        checks.append(label)
    def write(path,value):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n")
    def call(*argv,code=0):
        process=subprocess.run([sys.executable,str(engine),*map(str,argv)],capture_output=True,text=True)
        assert process.returncode==code,(argv,process.returncode,process.stdout[-2000:],process.stderr[-2000:])
        return json.loads(process.stdout) if process.stdout.strip() else json.loads(process.stderr)
    def spec(steps,**kw):
        return {"version":1,"id":"fixture","kind":"talking_head","brief":{"purpose":"synthetic tests"},
                "permissions":{"generate_media":True},
                "limits":{"max_generation_calls":8,"max_generation_credits":80,"max_repair_rounds":2},
                "steps":steps,**kw}
    def init(name,definition,inputs=None):
        folder=root/name;folder.mkdir()
        for file,text in (inputs or {}).items():(folder/file).write_text(text)
        source=root/(name+"-spec.json");write(source,definition)
        call("init",source,"--project",folder)
        return folder
    def state(folder):
        return json.loads((folder/"state.json").read_text())
    def step(folder,ident):
        return state(folder)["steps"][ident]
    local={"id":"first","kind":"local","action":"copy","params":{"source":"input.txt"},"inputs":["input.txt"]}
    downstream={"id":"second","kind":"local","action":"copy","needs":["first"],"params":{"source":"@first:file"},"inputs":["@first:file"]}
    p=init("cache",spec([local,downstream]),{"input.txt":"v1"})
    r=call("run",p,"--skills-root",runner)
    check(r["status"]=="technical_done_pending_review","local DAG runs in topological order without claiming creative acceptance")
    first=copy.deepcopy(step(p,"first"));old_file=Path(first["artifacts"][0]["path"])
    call("run",p,"--skills-root",runner)
    check(step(p,"first")["attempt"]==1 and step(p,"second")["attempt"]==1,"unchanged hashes are cached")
    (p/"input.txt").write_text("v2")
    call("run",p,"--skills-root",runner)
    check(step(p,"first")["attempt"]==2 and step(p,"second")["attempt"]==2,"changed source invalidates only downstream closure")
    check(old_file.read_text()=="v1","old successful artifacts are preserved")
    output=Path(step(p,"second")["artifacts"][0]["path"]);output.write_text("tampered")
    call("run",p,"--skills-root",runner)
    check(step(p,"first")["attempt"]==2 and step(p,"second")["attempt"]==3,"changed output invalidates its own subtree")
    definition=json.loads((p/"project.json").read_text());definition["steps"][1]["params"]["marker"]="new"
    write(p/"project.json",definition);call("run",p,"--skills-root",runner)
    check(step(p,"first")["attempt"]==2 and step(p,"second")["attempt"]==4,"parameter change reuses unaffected parent")
    check(not list(p.glob(".state.json.*.tmp")),"atomic state replacement leaves no staging file")
    hidden=init("transitive",spec([{"id":"render","kind":"local","action":"copy",
                "params":{"source":"job.json"},"inputs":["job.json"]}]),{"hidden.bin":"one"})
    write(hidden/"job.json",{"version":1,"clips":[{"source":"hidden.bin","start":0,"end":1}]})
    call("run",hidden,"--skills-root",runner)
    (hidden/"hidden.bin").write_text("two")
    call("run",hidden,"--skills-root",runner)
    check(step(hidden,"render")["attempt"]==2,"job source media changes invalidate cache even when JSON is unchanged")
    directory=init("directory",spec([{"id":"dir","kind":"local","action":"directory"}]))
    call("run",directory,"--skills-root",runner)
    check(step(directory,"dir")["status"]=="succeeded","directory artifacts may contain legitimate empty SRT sidecars")
    bad=spec([{"id":"a","kind":"agent","action":"x","needs":["b"]},
              {"id":"b","kind":"agent","action":"x","needs":["a"]}])
    write(root/"bad.json",bad);call("init",root/"bad.json","--project",root/"cycle",code=2)
    check(not (root/"cycle"/"project.json").exists(),"cyclic DAG rejected before project mutation")
    bad=spec([local,{"id":"second","kind":"local","action":"copy","params":{"source":"@first:file"}}])
    write(root/"bad-ref.json",bad);call("init",root/"bad-ref.json","--project",root/"bad-ref",code=2)
    check(not (root/"bad-ref"/"state.json").exists(),"artifact references require declared dependency ancestry")
    holder_script=root/"hold-lock.py"
    holder_script.write_text("import importlib.util,sys\ns=importlib.util.spec_from_file_location('held_engine',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\nwith m.ProjectLock(sys.argv[2]):\n print('locked',flush=True)\n sys.stdin.read()\n")
    holder=subprocess.Popen([sys.executable,str(holder_script),str(engine),str(p)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    assert holder.stdout.readline().strip()=="locked"
    call("status",p,code=2)
    check(True,"live project lock prevents concurrent mutation")
    holder.kill();holder.wait()
    call("status",p)
    lock=p/".production.lock"
    check(lock.exists() and json.loads(lock.read_text()).get("released"),"OS lock releases after process death without stale-file rename races")
    a=init("agent",spec([{"id":"write","kind":"agent","action":"write_script","inputs":["input.txt"]}]),{"input.txt":"brief"})
    queued=call("run",a,code=10)["actions"][0]
    again=call("run",a,code=10)["actions"][0]
    check(queued["request_id"]==again["request_id"],"host action request identity survives resume")
    out=Path(queued["output_dir"]);(out/"script.md").write_text("Synthetic script")
    (out/"notes.md").write_text("Synthetic test evidence, no visual claim")
    result={"status":"succeeded","request_id":"wrong","input_fingerprint":queued["input_fingerprint"],
            "generation_performed":False,"artifacts":[{"role":"script","path":str(out/"script.md")}],
            "evidence":[{"path":str(out/"notes.md"),"note":"fixture"}]}
    write(a/"result.json",result);call("record",a,"write","--result",a/"result.json",code=2)
    check(step(a,"write")["status"]=="waiting_external","stale or foreign host receipt rejected")
    result["request_id"]=queued["request_id"];result.pop("generation_performed")
    write(a/"result.json",result);call("record",a,"write","--result",a/"result.json",code=2)
    check(True,"agent result cannot hide unreserved generation")
    result["generation_performed"]=False;write(a/"result.json",result)
    call("record",a,"write","--result",a/"result.json")
    check(step(a,"write")["status"]=="succeeded","real host artifact and evidence receipt are recorded")
    check(call("record",a,"write","--result",a/"result.json")["status"]=="already_recorded","repeating an acknowledged receipt is idempotent")
    crash=init("receipt-crash",spec([{"id":"write","kind":"agent","action":"write_script","inputs":["input.txt"]}]),{"input.txt":"brief"})
    cq=call("run",crash,code=10)["actions"][0]
    co=Path(cq["output_dir"]);(co/"script.md").write_text("Synthetic crash-window artifact")
    (co/"proof.txt").write_text("Synthetic evidence")
    cr={"status":"succeeded","request_id":cq["request_id"],"input_fingerprint":cq["input_fingerprint"],
        "generation_performed":False,"artifacts":[{"role":"script","path":str(co/"script.md")}],
        "evidence":[{"path":str(co/"proof.txt"),"note":"fixture"}]}
    write(crash/"result.json",cr)
    imported=importlib.util.spec_from_file_location("engine_fixture",engine)
    module=importlib.util.module_from_spec(imported);imported.loader.exec_module(module)
    with module.ProjectLock(crash):
        wf=module.Workflow(crash)
        save=wf.save
        def interrupted_save():
            if wf.stage("write")["status"]=="succeeded":
                raise OSError("Synthetic crash after receipt, before state")
            save()
        wf.save=interrupted_save
        try:
            wf.record("write",crash/"result.json")
            raise AssertionError("Expected injected save failure")
        except OSError:
            pass
    saved=co/"execution-result.json"
    check(saved.exists() and step(crash,"write")["status"]=="waiting_external","receipt survives simulated crash before state commit")
    check(call("next",crash)["actions"][0]["instruction"]=="recover_saved_result","resume discovers durably written result receipt")
    call("record",crash,"write","--result",saved)
    check(step(crash,"write")["status"]=="succeeded","saved identical receipt repairs state without repeating provider or host work")
    provider={"id":"gen","kind":"provider","action":"generate_audio","params":{"expected_media":"audio"}}
    g=init("provider",spec([provider]))
    q=call("run",g,code=10)["actions"][0]
    reservation=call("reserve",g,"gen","--provider","synthetic","--estimate-credits","1")
    check(reservation["new_submission_allowed"],"budget reservation is durable before outbound generation")
    repeat=call("reserve",g,"gen","--provider","synthetic","--estimate-credits","1")
    check(not repeat["new_submission_allowed"] and len(state(g)["transactions"])==1,"same request cannot double-reserve or double-charge locally")
    check(call("run",g,code=10)["actions"][0]["instruction"]=="reconcile_submission_before_any_retry","unknown submission does not trigger a second order")
    call("retry",g,"gen","--reason","timeout",code=2)
    check(True,"unresolved intent blocks retry")
    call("submitted",g,"gen","--task-id","synthetic-task-001")
    check(call("run",g,code=10)["actions"][0]["instruction"]=="poll_existing_task","known task id resumes polling without submission")
    out=Path(q["output_dir"]);audio=out/"tone.wav"
    with wave.open(str(audio),"wb") as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(48000);wav.writeframes(b"\0\0"*4800)
    original=audio.read_bytes();write(out/"tool.json",{"synthetic":True,"task_id":"synthetic-task-001","status":"succeeded"})
    receipt={"status":"succeeded","request_id":q["request_id"],"input_fingerprint":q["input_fingerprint"],
             "provider_status":"succeeded","task_id":"synthetic-task-001","actual_credits":1.25,
             "artifacts":[{"role":"audio","path":str(audio)}],
             "evidence":[{"path":str(out/"tool.json"),"note":"Synthetic API receipt; no real generation"}]}
    write(g/"receipt.json",receipt);call("record",g,"gen","--result",g/"receipt.json")
    check(step(g,"gen")["status"]=="succeeded" and call("status",g)["budget"]["credits_exposure"]==1.25,"asynchronous completion requires actual media and matching task receipt")
    audio.unlink()
    action=call("run",g,code=10)["actions"][0]
    check(action["instruction"]=="restore_existing_provider_output","lost paid output requests restoration, not new paid generation")
    call("retry",g,"gen","--reason","lost",code=2)
    audio.write_bytes(original);call("restore",g,"gen")
    check(step(g,"gen")["status"]=="succeeded" and len(state(g)["transactions"])==1,"restored original bytes keep original transaction")
    u=init("uncertain",spec([provider],limits={"max_generation_calls":1,"max_generation_credits":1,"max_repair_rounds":2}))
    call("run",u,code=10);call("reserve",u,"gen","--provider","synthetic")
    call("fail",u,"gen","--reason","Synthetic timeout")
    call("retry",u,"gen","--reason","guess",code=2)
    (u/"proof.json").write_text('{"synthetic":"verified no request was sent"}')
    call("reconcile",u,"gen","--outcome","not_submitted","--evidence",u/"proof.json","--notes","Synthetic non-submission proof")
    call("retry",u,"gen","--reason","verified no submission")
    call("reserve",u,"gen","--provider","synthetic")
    check(len([t for t in state(u)["transactions"] if t["phase"]!="released"])==1,"verified non-submission releases reservation before safe retry")
    concurrent=init("concurrent",spec([provider,{**provider,"id":"gen2"}],
                    limits={"max_generation_calls":1,"max_generation_credits":1,"max_repair_rounds":2}))
    call("run",concurrent,code=10)
    processes=[subprocess.Popen([sys.executable,str(engine),"reserve",str(concurrent),ident,"--provider","synthetic","--estimate-credits","1"],
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for ident in ("gen","gen2")]
    results=[proc.communicate() for proc in processes]
    check(sorted(proc.returncode for proc in processes)==[0,2] and len(state(concurrent)["transactions"])==1,
          "concurrent reservation cannot exceed call budget")
    sync={"id":"image","kind":"provider","action":"generate_image","params":{"expected_media":"image","provider_mode":"synchronous"}}
    s=init("sync",spec([sync]));q=call("run",s,code=10)["actions"][0]
    call("reserve",s,"image","--provider","synthetic","--mode","synchronous")
    call("submitted",s,"image","--task-id","fake-task",code=2)
    out=Path(q["output_dir"])
    def chunk(kind,data):
        return struct.pack(">I",len(data))+kind+data+struct.pack(">I",zlib.crc32(kind+data)&0xffffffff)
    png=b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",struct.pack(">IIBBBBB",1,1,8,2,0,0,0))+chunk(b"IDAT",zlib.compress(b"\0\x22\x44\x66"))+chunk(b"IEND",b"")
    (out/"image.png").write_bytes(png);write(out/"tool.json",{"synthetic":True,"status":"succeeded"})
    receipt={"status":"succeeded","request_id":q["request_id"],"input_fingerprint":q["input_fingerprint"],
             "provider_status":"succeeded","tool_receipt":str(out/"tool.json"),
             "artifacts":[{"role":"image","path":str(out/"image.png")}],
             "evidence":[{"path":str(out/"tool.json"),"note":"Synthetic synchronous fixture"}]}
    write(s/"result.json",receipt);call("record",s,"image","--result",s/"result.json")
    check(step(s,"image")["status"]=="succeeded" and step(s,"image")["external"]["task_id"] is None,
          "synchronous image results do not require a fictitious remote task id")
    check(call("status",s)["budget"]["unknown_cost_calls"]==1,"unknown price is explicitly counted by calls rather than called free")
    missing=init("missing-input",spec([{**provider,"inputs":["absent.wav"]}]))
    action=call("run",missing,code=10)["actions"][0]
    call("reserve",missing,"gen","--provider","synthetic",code=2)
    check(action["instruction"]=="resolve_missing_inputs" and not state(missing)["transactions"],
          "missing required media cannot reserve a paid generation")
    inherited=init("stale-provider",spec([{"id":"source","kind":"agent","action":"write","inputs":["input.txt"]},
                  {**provider,"needs":["source"],"inputs":["@source:note"]}]),{"input.txt":"initial"})
    iq=call("run",inherited,code=10)["actions"][0];io=Path(iq["output_dir"])
    (io/"note.txt").write_text("Reference fixture");(io/"proof.txt").write_text("Observed")
    write(inherited/"result.json",{"status":"succeeded","request_id":iq["request_id"],"input_fingerprint":iq["input_fingerprint"],"generation_performed":False,
          "artifacts":[{"role":"note","path":str(io/"note.txt")}],"evidence":[{"path":str(io/"proof.txt"),"note":"fixture"}]})
    call("record",inherited,"source","--result",inherited/"result.json");call("run",inherited,code=10)
    call("reserve",inherited,"gen","--provider","synthetic");call("submitted",inherited,"gen","--task-id","old-live-task")
    call("invalidate",inherited,"source","--reason","Reference changed")
    actions=call("next",inherited)["actions"]
    check(any(x["step_id"]=="gen" and x["instruction"]=="poll_existing_task" and x["stale_inputs"] for x in actions),
          "in-flight provider task remains visible while its new upstream revision is unready")
    b=init("budget",spec([provider],limits={"max_generation_calls":1,"max_generation_credits":1}))
    call("run",b,code=10);call("reserve",b,"gen","--provider","synthetic","--estimate-credits","2",code=2)
    check(not state(b)["transactions"],"credit limit rejects reservation before submission")
    denied=init("denied",spec([provider],permissions={"generate_media":False}))
    call("run",denied,code=10);call("reserve",denied,"gen","--provider","synthetic",code=2)
    check(not state(denied)["transactions"],"configuration alone does not authorize media generation")
    r=init("review",spec([local,{"id":"review","kind":"review","action":"review_final","needs":["first"],
            "params":{"subject":"@first:file"},"inputs":["@first:file"],"required_checks":["visual","listening"]},
            {"id":"deliver","kind":"local","action":"package_delivery","needs":["review"],
             "params":{"source":"@review:review_result","acceptance":"production"},"inputs":["@review:review_result"]}]),{"input.txt":"Synthetic subject"})
    q=call("run",r,code=10)["actions"][0]
    out=Path(q["output_dir"]);(out/"observations.md").write_text("Synthetic receipt validation only; no artistic judgement.")
    proof=str(out/"observations.md")
    receipt={"status":"succeeded","request_id":q["request_id"],"input_fingerprint":q["input_fingerprint"],
             "checks":{"visual":{"state":"passed","notes":"Synthetic check","evidence":[proof]}}}
    write(r/"result.json",receipt);call("record",r,"review","--result",r/"result.json",code=2)
    check(step(r,"review")["status"]=="waiting_external","missing listening/visual review cannot become accepted")
    receipt["checks"]["listening"]={"state":"not_checked","notes":"Synthetic","evidence":[proof]}
    write(r/"result.json",receipt);call("record",r,"review","--result",r/"result.json",code=2)
    check(True,"not_checked cannot be relabeled as passed")
    receipt["checks"]["listening"]["state"]="passed"
    receipt["checks"]["visual"]["evidence"]=[str(out)]
    write(r/"result.json",receipt);call("record",r,"review","--result",r/"result.json",code=2)
    check(True,"evidence cannot contain the receipt being created and invalidate itself")
    receipt["checks"]["visual"]["evidence"]=[proof]
    receipt["checks"]["listening"]["state"]="passed"
    receipt["subject_sha256"]="0"*64;write(r/"result.json",receipt)
    call("record",r,"review","--result",r/"result.json",code=2)
    check(True,"review receipt cannot bind an obsolete subject hash")
    receipt.pop("subject_sha256");write(r/"result.json",receipt);call("record",r,"review","--result",r/"result.json")
    artifact=next(a for a in step(r,"review")["artifacts"] if a["role"]=="review_result")
    canonical=json.loads(Path(artifact["path"]).read_text())
    check(canonical["subject_sha256"]==hashlib.sha256(b"Synthetic subject").hexdigest(),"canonical review result binds the actual subject hash")
    check(call("run",r,"--skills-root",runner)["status"]=="production_accepted","only checked production packaging reports accepted")
    Path(proof).write_text("Changed evidence")
    call("status",r)
    check(step(r,"first")["status"]=="succeeded" and step(r,"review")["status"]=="waiting_external"
          and step(r,"deliver")["status"]=="pending","modified review evidence invalidates review and delivery")
    call("invalidate",r,"review","--reason","synthetic repair 1")
    call("invalidate",r,"review","--reason","synthetic repair 2")
    call("invalidate",r,"review","--reason","synthetic repair 3",code=2)
    check(state(r)["repair_rounds"]==2,"automatic repair wave budget is bounded")
    self_input=init("self-input",spec([{"id":"bad","kind":"agent","action":"inspect","inputs":["."]}]))
    answer=call("run",self_input,code=10)
    check(answer["actions"][0]["instruction"]=="resolve_missing_inputs","project state/output tree cannot hash itself as a required input")
    failure=init("failure",spec([{"id":"bad","kind":"local","action":"fail"}],
                       limits={"max_attempts_per_input":2}))
    call("run",failure,"--skills-root",runner,code=20)
    call("retry",failure,"bad","--reason","synthetic retry")
    call("run",failure,"--skills-root",runner,code=20)
    call("retry",failure,"bad","--reason","synthetic third attempt")
    call("run",failure,"--skills-root",runner,code=20)
    check(step(failure,"bad")["block_reason"]=="attempt_limit","unchanged failing local inputs have a finite attempt limit")
    report={"assertions_passed":len(checks),"assertions":checks,"providers_are_synthetic_fixtures":True,
            "paid_generation_calls":0,"artistic_acceptance_tested":False}
    write(root/"verification.json",report)
    print(json.dumps(report,ensure_ascii=False))

if __name__=="__main__":
    main()
