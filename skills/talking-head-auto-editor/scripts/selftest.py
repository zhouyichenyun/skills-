#!/usr/bin/env python3
"""Meaningful synthetic-media integration checks. No model/API/network needed.

Run: python selftest.py --work <new-or-empty-directory>
Writes only synthetic fixtures into that directory, never real user media.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from xml.etree import ElementTree as ET


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--work',required=True);a=p.parse_args()
    root=Path(a.work).resolve()
    if root.exists() and any(root.iterdir()):p.error('Test work directory must be new or empty')
    root.mkdir(parents=True,exist_ok=True)
    editor=Path(__file__).with_name('editor.py')
    bridge=Path(__file__).with_name('resolve_bridge.py')
    passed=[]
    def run(args,code=0):
        r=subprocess.run([str(x) for x in args],capture_output=True,text=True,encoding='utf-8')
        if r.returncode!=code:raise AssertionError(f'{args}\nexit {r.returncode}, expected {code}\n{r.stdout}\n{r.stderr}')
        return r
    def cli(*args,code=0):return run([sys.executable,editor,*args],code=code)
    def save(name,obj):
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8');return path
    def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
    run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=320x180:rate=30:duration=6',
         '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=6',
         '-af',"volume=0:enable='between(t,1.2,3.3)'",'-threads','2','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',root/'source.mp4'])
    run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=blue:size=240x240:rate=30:duration=3',
         '-threads','2','-c:v','libx264','-pix_fmt','yuv420p',root/'broll.mp4'])
    source_hash=digest(root/'source.mp4')
    words=save('words.json',{'version':1,'source':str(root/'source.mp4'),'words':[
        {'start':.3,'end':.7,'text':'Hello'},{'start':.8,'end':1.2,'text':'there.'},
        {'start':3.3,'end':3.8,'text':'Second'},{'start':4.0,'end':4.5,'text':'sentence.'}]})
    cli('plan',root/'source.mp4','--transcript',words,'--out',root/'planned.json')
    planned=json.loads((root/'planned.json').read_text())
    assert len(planned['clips'])==2 and len(planned['subtitles'])==2
    assert 1.6<sum(x['end']-x['start'] for x in planned['planning']['removed_intervals'])<1.8
    passed.append('word_guarded_measured_pause_plan_and_subtitle_mapping')
    cli('plan',root/'source.mp4','--out',root/'silence-suggestions.json')
    suggestions=json.loads((root/'silence-suggestions.json').read_text())
    assert len(suggestions['clips'])==1 and not suggestions['planning']['removed_intervals']
    assert suggestions['planning']['silence_candidates']
    passed.append('no_transcript_silence_is_suggestion_only')
    job={'version':1,'name':'Integration test','fps':30,'width':320,'height':180,
         'clips':[{'id':'a','source':'source.mp4','start':.133333333,'end':2.466666667},
                  {'id':'b','source':'source.mp4','start':3.1,'end':5.333333333}],
         'broll':[{'source':'broll.mp4','at':1.1,'duration':1.2,'source_start':.25,'fit':'cover'}],
         'subtitles':[{'start':.2,'end':1.0,'text':'Hello synthetic subtitle'},
                      {'start':2.4,'end':3.8,'text':'实际渲染字幕测试'}],
         'grade':{'preset':'warm','lut':None},'audio':{'target_lufs':-16}}
    jobpath=save('job.json',job)
    cli('render',jobpath,'--out',root/'actual.mp4')
    actual=json.loads((root/'actual.render.json').read_text())
    assert actual['verified_frames']==137 and abs(actual['duration']-137/30)<.002
    assert actual['broll_count']==1 and actual['subtitles_burned'] and actual['decode_check']=='passed'
    # Read actual rendered pixels: the overlay must be blue during its interval,
    # while a frame outside that interval must retain varying source colors.
    # Binary output has to stay binary, independently of CLI text diagnostics.
    def rgb(t):
        r=subprocess.run(['ffmpeg','-v','error','-ss',str(t),'-i',str(root/'actual.mp4'),'-frames:v','1',
                          '-vf','crop=2:2:20:20,format=rgb24','-f','rawvideo','-'],capture_output=True)
        assert r.returncode==0 and len(r.stdout)==12
        return tuple(r.stdout[:3])
    blue=rgb(1.5);outside=rgb(.1)
    assert blue[2]>blue[0]+80 and blue[2]>blue[1]+80
    assert outside!=blue
    passed.append('actual_mp4_audio_broll_pixels_bilingual_subtitles_grade_137_frames_decode')
    cli('export',jobpath,'--out-dir',root/'export')
    xml=ET.parse(root/'export/resolve-premiere.xml')
    tracks=xml.findall('./sequence/media/video/track')
    assert len(tracks)==2 and len(tracks[0].findall('clipitem'))==2 and len(tracks[1].findall('clipitem'))==1
    assert xml.findall('./sequence/media/audio/track/clipitem')
    assert (root/'export/captions.srt').read_text().count('-->')==2
    assert (root/'export/resolve-v1.edl').read_text().count('* FROM CLIP NAME:')==2
    passed.append('xml_two_video_tracks_audio_srt_and_edl_static_validation_only')
    template=root/'template';template.mkdir()
    template_payload=save('template/draft_content.json',{'version':999,'custom_unknown':{'preserve':True},
        'materials':{'videos':[],'texts':[]},'tracks':[]})
    (template/'draft_meta_info.json').write_text('{"keep":"metadata"}',encoding='utf-8')
    before=digest(template_payload)
    cli('export',jobpath,'--out-dir',root/'template-export','--jianying-template',template)
    assert digest(template_payload)==before
    adapted=json.loads((root/'template-export/jianying-template-adapted/draft_content.json').read_text())
    assert adapted['version']==999 and adapted['custom_unknown']['preserve'] is True and len(adapted['tracks'])==3
    assert (root/'template-export/jianying-template-adapted/draft_meta_info.json').read_bytes()==(template/'draft_meta_info.json').read_bytes()
    passed.append('jianying_template_source_unchanged_and_copy_preserves_unknown_state')
    # Output safety regression: neither source media nor prior sidecars may be overwritten.
    cli('plan',root/'source.mp4','--out',root/'source.mp4',code=2)
    cli('inventory',root,'--out',root/'source.mp4',code=2)
    cli('transcribe',root/'source.mp4','--model',root,'--out',root/'source.mp4',code=2)
    (root/'protected.srt').write_text('PREEXISTING',encoding='utf-8')
    cli('render',jobpath,'--out',root/'protected.mp4',code=2)
    assert not (root/'protected.mp4').exists() and (root/'protected.srt').read_text()=='PREEXISTING'
    (root/'protected-log.render.json').write_text('PREEXISTING',encoding='utf-8')
    cli('render',jobpath,'--out',root/'protected-log.mp4',code=2)
    assert not (root/'protected-log.mp4').exists()
    assert digest(root/'source.mp4')==source_hash
    passed.append('source_plan_inventory_transcribe_and_render_sidecar_overwrite_protection')
    # Explicit rational output fps and noninteger source trim; LUT filter is actually executed.
    lut=root/'identity.cube'
    lut.write_text('TITLE "Identity"\nLUT_3D_SIZE 2\n'+''.join(f'{r} {g} {b}\n' for b in (0,1) for g in (0,1) for r in (0,1)))
    rational={**job,'fps':'30000/1001','clips':[{'id':'rat','source':'source.mp4','start':.123,'end':1.987}],
              'broll':[],'subtitles':[],'grade':{'preset':'neutral','lut':'identity.cube'}}
    rpath=save('rational.json',rational)
    cli('render',rpath,'--out',root/'rational.mp4')
    rmeta=json.loads((root/'rational.render.json').read_text())
    assert rmeta['verified_frames']==56 and rmeta['lut_applied']
    passed.append('rational_30000_1001_fps_noninteger_trim_actual_cube_lut_render')
    # Source frame rate remains distinct from sequence frame rate in XML in/out.
    rational['fps']=25
    save('mixed.json',rational)
    cli('export',root/'mixed.json','--out-dir',root/'mixed-export')
    item=ET.parse(root/'mixed-export/resolve-premiere.xml').find('./sequence/media/video/track/clipitem')
    assert item.findtext('rate/timebase')=='30'
    assert int(item.findtext('duration'))>=int(item.findtext('out'))
    passed.append('mixed_fps_xml_source_rate_duration_and_in_out_grid')
    dry=run([sys.executable,bridge,'--timeline-xml',root/'export/resolve-premiere.xml','--dry-run'])
    dry=json.loads(dry.stdout)
    assert dry['connected'] is False and dry['project_strategy']=='create_new_unique_project'
    passed.append('resolve_dry_run_reports_no_connection_and_new_project_default')
    report={'tests_passed':passed,'count':len(passed),'software_ui_verified':False,
            'asr_model_executed':False,'visual_pixel_check':True,
            'not_verified':['actual Windows execution','Resolve/Premiere/Jianying UI import','human audio/semantic acceptance','local ASR model accuracy']}
    save('verification.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
