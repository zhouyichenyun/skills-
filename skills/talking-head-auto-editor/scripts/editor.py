#!/usr/bin/env python3
"""Local talking-head editing: measured plans, FFmpeg renders and NLE handoffs.

Only Python's standard library is required. Optional ASR loads a pre-downloaded
faster-whisper model, with local_files_only=True. No network clients are used.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as ET

IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}
VIDEO_SUFFIXES = {'.mp4', '.mov', '.mkv', '.m4v', '.avi', '.webm', '.mxf'}
PRESETS = {
    'neutral': [],
    'warm': ['colorbalance=rs=0.025:bs=-0.020', 'eq=saturation=1.025'],
    'cool': ['colorbalance=rs=-0.015:bs=0.025', 'eq=saturation=0.985'],
    'contrast': ['eq=contrast=1.035:brightness=-0.003:saturation=1.015'],
}


def run(args, *, cwd=None, check=True):
    p = subprocess.run([str(a) for a in args], cwd=cwd, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    if check and p.returncode:
        raise ValueError(f'Command failed ({p.returncode}): {args[0]}\n{p.stderr[-7000:]}')
    return p


def ff(*args):
    return ['ffmpeg', '-hide_banner', '-nostdin', '-filter_threads', '2',
            '-filter_complex_threads', '2', *args]


def write_json(path, obj, *, overwrite=False):
    path = Path(path)
    if path.exists() and not overwrite:
        raise ValueError(f'Output already exists; choose a new path: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def sha256_file(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda:handle.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def local_path(value, base):
    if not isinstance(value, str) or not value:
        raise ValueError('A nonempty local media path is required')
    if re.match(r'^\w+://', value):
        raise ValueError('Network media URLs are unsupported; use an authorized local file')
    p = Path(value).expanduser()
    return (base / p if not p.is_absolute() else p).resolve()


def number(value, label, minimum=None):
    try:
        n = float(value)
    except (ValueError, TypeError):
        raise ValueError(f'{label}: expected a finite number')
    if not math.isfinite(n) or (minimum is not None and n < minimum):
        raise ValueError(f'{label}: invalid value {value}')
    return n


def frame_rate(value):
    try:
        f = Fraction(str(value)).limit_denominator(100000)
    except (ValueError, ZeroDivisionError):
        raise ValueError('fps must be a positive number or fraction, such as 30000/1001')
    if not 1 <= float(f) <= 240:
        raise ValueError('fps must be between 1 and 240')
    return f


def probe(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError(f'Media is missing: {path}')
    p = run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', path])
    data = json.loads(p.stdout)
    video = next((s for s in data.get('streams', []) if s['codec_type'] == 'video'), None)
    audio = next((s for s in data.get('streams', []) if s['codec_type'] == 'audio'), None)
    if not video:
        raise ValueError(f'No video/image stream: {path}')
    raw_dur = data.get('format', {}).get('duration') or video.get('duration')
    duration = float(raw_dur) if raw_dur not in (None, 'N/A') else None
    raw_rate = video.get('avg_frame_rate', '0/0')
    if raw_rate in ('0/0', '0/1'):
        raw_rate = video.get('r_frame_rate', '30/1')
    fps = float(frame_rate(raw_rate))
    return {'path': str(path), 'width': int(video['width']), 'height': int(video['height']),
            'fps': fps, 'duration': duration, 'has_audio': audio is not None,
            'audio_channels':int(audio.get('channels',1)) if audio else 0,
            'audio_sample_rate':int(audio.get('sample_rate',48000)) if audio else 0,
            'color_transfer': video.get('color_transfer', 'unknown'),
            'color_primaries': video.get('color_primaries', 'unknown'),
            'image': path.suffix.lower() in IMAGE_SUFFIXES}


def load_job(path):
    path = Path(path).resolve()
    job = json.loads(path.read_text(encoding='utf-8-sig'))
    if job.get('version') != 1:
        raise ValueError('Only job version 1 is supported')
    fps = frame_rate(job.get('fps', 30))
    width, height = int(job.get('width', 1920)), int(job.get('height', 1080))
    if width < 16 or height < 16 or width % 2 or height % 2:
        raise ValueError('width and height must be positive even numbers >=16')
    if width > 7680 or height > 7680:
        raise ValueError('Canvas exceeds this local renderer\'s 7680-pixel limit')
    if not job.get('clips'):
        raise ValueError('job.clips cannot be empty')
    cache, clips, ids, cursor = {}, [], set(), 0
    for i, c in enumerate(job['clips']):
        source = local_path(c.get('source'), path.parent)
        cache.setdefault(str(source), probe(source))
        info = cache[str(source)]
        if info['image'] or info['duration'] is None:
            raise ValueError('Main clips need a finite-duration video; use images as B-roll')
        start = number(c.get('start', 0), 'clip.start', 0)
        end = number(c.get('end'), 'clip.end', 0)
        if end <= start or end > info['duration'] + 1 / info['fps']:
            raise ValueError(f'Invalid source range for {source.name}: {start}..{end}')
        ident = str(c.get('id', f'clip-{i+1:03d}'))
        if ident in ids:
            raise ValueError(f'Duplicate clip id: {ident}')
        ids.add(ident)
        frames = round((end-start)*float(fps))
        if frames < 1:
            raise ValueError(f'Clip {ident} is shorter than one output frame')
        clips.append({**c, 'id': ident, 'source': str(source), 'start': start, 'end': end,
                      'output_start_frame': cursor, 'frames': frames, 'info': info})
        cursor += frames
    duration = cursor / float(fps)
    broll = []
    for i, b in enumerate(job.get('broll', [])):
        source = local_path(b.get('source'), path.parent)
        cache.setdefault(str(source), probe(source))
        info = cache[str(source)]
        at = number(b.get('at'), 'broll.at', 0)
        dur = number(b.get('duration'), 'broll.duration', 0)
        ss = number(b.get('source_start', 0), 'broll.source_start', 0)
        at_fr, nfr = round(at*float(fps)), round(dur*float(fps))
        if nfr < 1 or at_fr+nfr > cursor:
            raise ValueError('B-roll must fit inside the assembled output timeline')
        if b.get('fit', 'cover') != 'cover':
            raise ValueError('B-roll currently supports fit="cover" only')
        if info['image'] and ss:
            raise ValueError('An image B-roll source_start must be zero')
        if not info['image'] and (info['duration'] is None or ss+nfr/float(fps) > info['duration']+1/info['fps']):
            raise ValueError(f'B-roll source is too short: {source.name}')
        broll.append({**b, 'source': str(source), 'at': at_fr/float(fps),
                      'duration': nfr/float(fps), 'source_start': ss, 'frames': nfr,
                      'output_start_frame': at_fr, 'info': info})
    ordered = sorted(broll, key=lambda b:b['output_start_frame'])
    if any(a['output_start_frame']+a['frames'] > b['output_start_frame'] for a,b in zip(ordered,ordered[1:])):
        raise ValueError('Overlapping B-roll overlays are unsupported; choose one per interval')
    captions = []
    for cap in job.get('subtitles', []):
        a, b = number(cap.get('start'), 'subtitle.start', 0), number(cap.get('end'), 'subtitle.end', 0)
        text = str(cap.get('text', '')).strip()
        if b <= a or b > duration+1/float(fps) or not text:
            raise ValueError('Subtitle has empty text or invalid output-time boundaries')
        if '-->' in text:
            raise ValueError('Subtitle text cannot contain an SRT time arrow')
        captions.append({'start':a,'end':min(b,duration),'text':text})
    captions.sort(key=lambda c:c['start'])
    if any(a['end'] > b['start']+1e-6 for a,b in zip(captions,captions[1:])):
        raise ValueError('Overlapping subtitles need explicit editorial resolution')
    grade = copy.deepcopy(job.get('grade', {'preset':'neutral','lut':None}))
    if grade.get('preset', 'neutral') not in PRESETS:
        raise ValueError('Unknown grade.preset; use neutral, warm, cool or contrast')
    if grade.get('lut'):
        lut = local_path(grade['lut'], path.parent)
        if not lut.is_file() or lut.suffix.lower() != '.cube':
            raise ValueError('grade.lut must name an existing local .cube file')
        grade['lut'] = str(lut)
    target = number(job.get('audio', {}).get('target_lufs', -16), 'audio.target_lufs')
    if not -30 <= target <= -9:
        raise ValueError('audio.target_lufs must lie between -30 and -9')
    return {**job, 'fps':fps,'width':width,'height':height,'clips':clips,'broll':broll,
            'subtitles':captions,'grade':grade,'audio':{'target_lufs':target},
            '_path':str(path),'_frames':cursor,'_duration':duration,'_media':cache}


def srt_time(value):
    ms = max(0, round(value*1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem,60000)
    s, ms = divmod(rem,1000)
    return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'


def write_srt(path, captions):
    body = '\n\n'.join(f'{i}\n{srt_time(c["start"])} --> {srt_time(c["end"])}\n{c["text"]}'
                        for i,c in enumerate(captions,1))
    Path(path).write_text(body+('\n' if body else ''),encoding='utf-8')


def read_words(path):
    obj = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    raw = obj.get('words') or [w for s in obj.get('segments',[]) for w in s.get('words',[])]
    words = []
    for w in raw:
        if w.get('type', 'word') not in ('word', 'speech'):
            continue
        start,end = number(w.get('start'),'word.start',0),number(w.get('end'),'word.end',0)
        if end <= start:
            raise ValueError('Word timing must have end > start')
        text = str(w.get('text',w.get('word',''))).strip()
        if text:
            words.append({'start':start,'end':end,'text':text})
    words.sort(key=lambda w:(w['start'],w['end']))
    if not words:
        raise ValueError('Transcript has no actual word timestamps; segment timings are insufficient')
    return obj,words


def cmd_plan(a):
    if Path(a.out).exists():
        raise ValueError('Plan output already exists; source and existing jobs are never overwritten')
    source=Path(a.source).resolve()
    info=probe(source)
    if not info['duration'] or not info['has_audio']:
        raise ValueError('Pause planning requires a video with a finite duration and audio')
    duration=info['duration']
    removed=[]
    words=[]
    suggestions=[]
    # A missing ASR word is not proof of silence. Only measured quiet intervals
    # may be shortened, and word evidence further protects actual word ranges.
    silence=run(ff('-v','info','-threads','2','-i',source,'-af',f'silencedetect=noise={a.noise_db}dB:d={a.max_pause}',
                   '-f','null','-'))
    silence_starts=[float(x) for x in re.findall(r'silence_start: ([\d.]+)',silence.stderr)]
    silence_ends=[float(x) for x in re.findall(r'silence_end: ([\d.]+)',silence.stderr)]
    quiet=[(start,silence_ends[i] if i<len(silence_ends) else duration) for i,start in enumerate(silence_starts)]
    if a.transcript:
        raw,words=read_words(a.transcript)
        declared=raw.get('source')
        if declared and local_path(declared,Path(a.transcript).resolve().parent)!=source:
            raise ValueError('Transcript belongs to a different source')
        if words[-1]['end'] > duration+0.1:
            raise ValueError('Transcript word extends beyond source duration')
        for left,right in zip(words,words[1:]):
            gap=right['start']-left['end']
            if gap>a.max_pause:
                for silence_start,silence_end in quiet:
                    lo=max(left['end']+a.keep_pause/2,silence_start+a.keep_pause/2)
                    hi=min(right['start']-a.keep_pause/2,silence_end-a.keep_pause/2)
                    if hi>lo:
                        removed.append((lo,hi))
        mode='word_guarded_measured_silence'
        warning='Only measured quiet intervals inside inter-word gaps are shortened. Transcript accuracy and semantic pauses still require listening review; no words or false starts were semantically deleted.'
    else:
        for start,end in quiet:
            if end-start>a.max_pause:
                suggestions.append({'start':start,'end':end,'keep_seconds':a.keep_pause})
                if a.trim_measured_silence:
                    removed.append((start+a.keep_pause/2,end-a.keep_pause/2))
        mode='measured_silence_trim' if a.trim_measured_silence else 'silence_suggestions_only'
        warning='No word evidence is available. Silence is not proof of a mistake or dispensable semantic pause. By default this plan retains the entire source; --trim-measured-silence is an explicit choice to shorten measured quiet intervals.'
    cursor=0.0
    ranges=[]
    for lo,hi in sorted(removed):
        if lo>cursor+0.02:
            ranges.append((cursor,lo))
        cursor=max(cursor,hi)
    if duration>cursor+0.02:
        ranges.append((cursor,duration))
    if not ranges:
        ranges=[(0,duration)]
    out=Path(a.out).resolve()
    clips=[{'id':f'clip-{i:03d}','source':str(source),'start':round(lo,6),'end':round(hi,6)}
           for i,(lo,hi) in enumerate(ranges,1)]
    fps=frame_rate(a.fps or str(info['fps']))
    captions=[]
    output_cursor=0
    for clip in clips:
        group=[]
        for w in words:
            if w['start']>=clip['start']-1e-6 and w['end']<=clip['end']+1e-6:
                group.append(w)
                if len(group)>=a.caption_words or w['end']-group[0]['start']>=3.5 or re.search(r'[。！？.!?]$',w['text']):
                    captions.append({'start':round(output_cursor+group[0]['start']-clip['start'],6),
                                     'end':round(output_cursor+group[-1]['end']-clip['start'],6),
                                     'text':join_words(group)})
                    group=[]
        if group:
            captions.append({'start':round(output_cursor+group[0]['start']-clip['start'],6),
                             'end':round(output_cursor+group[-1]['end']-clip['start'],6),'text':join_words(group)})
        output_cursor+=round((clip['end']-clip['start'])*float(fps))/float(fps)
    job={'version':1,'name':a.name or source.stem,'fps':str(fps),'width':a.width or info['width'],
         'height':a.height or info['height'],'clips':clips,'broll':[],'subtitles':captions,
         'grade':{'preset':'neutral','lut':None},'audio':{'target_lufs':-16},
         'planning':{'mode':mode,'review_required':True,'warning':warning,
                     'removed_intervals':[{'start':x,'end':y} for x,y in removed],
                     'silence_candidates':suggestions,'word_timestamps_supplied':bool(words)}}
    write_json(out,job)
    print(json.dumps({'job':str(out),'clips':len(clips),'subtitles':len(captions),
                      'removed_seconds':round(sum(y-x for x,y in removed),3),'warning':warning},ensure_ascii=False))


def join_words(words):
    text=''
    for w in words:
        s=w['text']
        if text and not re.search(r'[\u3400-\u9fff]$',text) and not re.match(r'[\u3400-\u9fff，。！？、,.!?;:]',s):
            text+=' '
        text+=s
    return text


def cmd_transcribe(a):
    model=Path(a.model).resolve()
    if not model.is_dir():
        raise ValueError('--model must be an existing, already downloaded CTranslate2/faster-whisper model directory. This skill never downloads a model.')
    source=Path(a.source).resolve()
    if not source.is_file():
        raise ValueError('ASR source file is missing')
    out=Path(a.out).resolve()
    if out.exists():
        raise ValueError('ASR output already exists; choose a new file')
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise ValueError('Optional faster-whisper is not installed. See requirements-optional.txt; supplied word-timed transcripts work without it.')
    engine=WhisperModel(str(model),device=a.device,compute_type=a.compute_type,local_files_only=True)
    segments,info=engine.transcribe(str(source),language=a.language,word_timestamps=True,vad_filter=True)
    result=[]
    words=[]
    for s in segments:
        ws=[{'start':w.start,'end':w.end,'text':w.word.strip()} for w in (s.words or [])]
        result.append({'start':s.start,'end':s.end,'text':s.text,'words':ws})
        words.extend(ws)
    write_json(out,{'version':1,'source':str(source),'language':info.language,'model_path':str(model),
                    'segments':result,'words':words,'verification':'machine transcript; text and cuts need review'})
    print(json.dumps({'out':str(out),'words':len(words),'network_used':False}))


def loudness_filter(base_args,target,cwd):
    p=run(ff('-v','info',*base_args,'-vn','-af',f'loudnorm=I={target}:TP=-1.5:LRA=11:print_format=json',
             '-f','null','-'),cwd=cwd)
    matches=re.findall(r'\{\s*"input_i".*?\}',p.stderr,re.S)
    if not matches:
        raise ValueError('FFmpeg loudness measurement returned no data')
    measured=json.loads(matches[-1])
    numeric=('input_i','input_tp','input_lra','input_thresh','target_offset')
    if not all(math.isfinite(float(measured[k])) for k in numeric):
        return 'anull',{'status':'unmeasurable_or_silent','measurement':measured}
    filt=(f'loudnorm=I={target}:TP=-1.5:LRA=11:measured_I={measured["input_i"]}'
          f':measured_TP={measured["input_tp"]}:measured_LRA={measured["input_lra"]}'
          f':measured_thresh={measured["input_thresh"]}:offset={measured["target_offset"]}'
          ':linear=true:print_format=summary')
    return filt,{'status':'two_pass_measured','measurement':measured}


def cmd_render(a):
    job=load_job(a.job)
    out=Path(a.out).resolve()
    outputs=[out,out.with_suffix('.srt'),out.with_suffix('.render.json')]
    if any(p.exists() or str(p) in job['_media'] for p in outputs):
        raise ValueError('A render output/sidecar already exists or equals a source; choose a new output basename')
    for info in job['_media'].values():
        if info['color_transfer'] in ('smpte2084','arib-std-b67'):
            raise ValueError('PQ/HLG source detected. Supply a deliberately converted SDR copy first; this renderer does not guess a color transform.')
    out.parent.mkdir(parents=True,exist_ok=True)
    work=Path(tempfile.mkdtemp(prefix='editor-render-',dir=out.parent))
    fps=float(job['fps']); rate=str(job['fps'])
    w,h=job['width'],job['height']
    manifest={'job':job['_path'],'expected_frames':job['_frames'],'fps':rate,
              'work_directory':str(work),'software_ui_verified':False,
              'color_note':'No implicit Log/HDR-to-Rec.709 transform was applied. Unknown/log material needs an explicit source-aware workflow.'}
    try:
        for i,c in enumerate(job['clips']):
            dur=c['frames']/fps
            inputs=['-threads','2','-ss',f'{c["start"]:.9f}','-i',c['source']]
            if not c['info']['has_audio']:
                inputs+=['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
            vf=f'scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={rate},trim=end_frame={c["frames"]},setpts=PTS-STARTPTS'
            af=f'aresample=48000,apad,atrim=duration={dur:.9f},asetpts=PTS-STARTPTS'
            cmd=ff('-v','error','-y',*inputs,'-map','0:v:0','-map','0:a:0' if c['info']['has_audio'] else '1:a:0',
                   '-vf',vf,'-af',af,'-t',f'{dur:.9f}',
                   '-threads','2','-c:v','ffv1','-level','3','-pix_fmt','yuv420p','-c:a','pcm_s16le','-ar','48000','-ac','2',
                   f'segment-{i:04d}.mkv')
            run(cmd,cwd=work)
        (work/'concat.txt').write_text(''.join(f"file 'segment-{i:04d}.mkv'\nduration {c['frames']/fps:.9f}\n" for i,c in enumerate(job['clips'])),encoding='utf-8')
        base=['-f','concat','-safe','0','-i','concat.txt']
        af,loudness=loudness_filter(base,job['audio']['target_lufs'],work)
        manifest['audio']=loudness
        inputs=list(base)
        filters=['[0:v]setpts=PTS-STARTPTS[base0]']
        for i,b in enumerate(job['broll'],1):
            if b['info']['image']:
                inputs+=['-loop','1','-framerate',rate,'-threads','2','-i',b['source']]
            else:
                inputs+=['-threads','2','-ss',f'{b["source_start"]:.9f}','-i',b['source']]
            end=b['at']+b['duration']
            filters.append(f'[{i}:v]trim=duration={b["duration"]:.9f},setpts=PTS-STARTPTS+{b["at"]:.9f}/TB,fps={rate},scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1[br{i}]')
            filters.append(f'[base{i-1}][br{i}]overlay=eof_action=pass:repeatlast=0:enable=\'gte(t,{b["at"]:.9f})*lt(t,{end:.9f})\'[base{i}]')
        grade=list(PRESETS[job['grade'].get('preset','neutral')])
        if job['grade'].get('lut'):
            shutil.copyfile(job['grade']['lut'],work/'look.cube')
            grade.append('lut3d=file=look.cube')
        write_srt(work/'captions.srt',job['subtitles'])
        if job['subtitles'] and not a.no_burn_subtitles:
            grade.append("subtitles=filename=captions.srt:charenc=UTF-8:force_style='FontName=Noto Sans CJK SC,FontSize=22,Outline=1.3,Shadow=0.5,MarginV=24'")
        grade.append('format=yuv420p')
        grade.extend([f'fps={rate}',f'trim=end_frame={job["_frames"]}'])
        filters.append(f'[base{len(job["broll"])}]'+','.join(grade)+'[vout]')
        filters.append(f'[0:a]{af},aresample=48000,apad,atrim=duration={job["_duration"]:.9f},asetpts=PTS-STARTPTS[aout]')
        run(ff('-v','error','-y',*inputs,'-filter_complex',';'.join(filters),'-map','[vout]','-map','[aout]',
               '-t',f'{job["_duration"]:.9f}','-threads','2','-c:v','libx264',
               '-preset','fast','-crf',str(a.crf),'-pix_fmt','yuv420p','-c:a','aac','-b:a','192k','-ar','48000','-ac','2',
               '-movflags','+faststart','render.mp4'),cwd=work)
        output_info=probe(work/'render.mp4')
        n=run(['ffprobe','-v','error','-select_streams','v:0','-count_frames','-show_entries','stream=nb_read_frames',
               '-of','default=nw=1:nk=1',work/'render.mp4']).stdout.strip()
        if int(n)!=job['_frames']:
            raise ValueError(f'Render frame count {n} != expected {job["_frames"]}')
        if abs(output_info['duration']-job['_duration'])>1/fps+0.03 or not output_info['has_audio']:
            raise ValueError('Render duration/audio verification failed')
        run(ff('-v','error','-threads','2','-i',work/'render.mp4','-f','null','-'))
        shutil.copyfile(work/'render.mp4',out)
        write_srt(out.with_suffix('.srt'),job['subtitles'])
        manifest.update({'output':str(out),'verified_frames':int(n),'duration':output_info['duration'],
                         'decode_check':'passed','broll_count':len(job['broll']),
                         'subtitles_burned':bool(job['subtitles']) and not a.no_burn_subtitles,
                         'grade_preset':job['grade'].get('preset','neutral'),'lut_applied':bool(job['grade'].get('lut')),
                         'sha256':sha256_file(out),
                         'review':'Technical checks only. Watch/listen to the actual output before aesthetic or semantic acceptance.'})
        write_json(out.with_suffix('.render.json'),manifest)
        print(json.dumps(manifest,ensure_ascii=False))
        shutil.rmtree(work)
    except Exception:
        write_json(work/'failure-context.json',manifest)
        raise


def elem(parent,name,text=None,**attrs):
    node=ET.SubElement(parent,name,{k:str(v) for k,v in attrs.items()})
    if text is not None: node.text=str(text)
    return node


def xml_rate(parent,fps):
    node=elem(parent,'rate')
    value=float(fps)
    nominal=round(value)
    ntsc=abs(value-nominal*1000/1001)<0.002
    elem(node,'timebase',nominal)
    elem(node,'ntsc','TRUE' if ntsc else 'FALSE')


def write_xml(job,path):
    root=ET.Element('xmeml',version='5')
    seq=elem(root,'sequence',id='talking-head-sequence')
    elem(seq,'name',job.get('name','Talking-head edit'))
    elem(seq,'duration',job['_frames']);xml_rate(seq,job['fps'])
    media=elem(seq,'media'); video=elem(media,'video')
    fmt=elem(elem(video,'format'),'samplecharacteristics')
    elem(fmt,'width',job['width']);elem(fmt,'height',job['height']);elem(fmt,'pixelaspectratio','square')
    elem(fmt,'fielddominance','none');xml_rate(fmt,job['fps'])
    tracks=[elem(video,'track'),elem(video,'track')]
    audio=elem(media,'audio');elem(audio,'numOutputChannels',2)
    atrack=elem(audio,'track')
    file_ids={p:f'file-{i+1}' for i,p in enumerate(job['_media'])}
    declared=set()
    def add_clip(parent,c,start_frame,nframes,source_start,kind,index):
        info=c['info'];source=c['source']
        item=elem(parent,'clipitem',id=f'{kind}-{index}')
        elem(item,'name',Path(source).name);elem(item,'enabled','TRUE')
        srcfps=info['fps']
        xml_rate(item,frame_rate(str(srcfps)));elem(item,'duration',round((info['duration'] or job['_duration'])*srcfps))
        elem(item,'start',start_frame);elem(item,'end',start_frame+nframes)
        # FCP7 source in/out use the source media frame grid, not output fps.
        srcin=round(source_start*srcfps)
        elem(item,'in',srcin);elem(item,'out',srcin+round(nframes/float(job['fps'])*srcfps))
        file=elem(item,'file',id=file_ids[source])
        if source not in declared:
            declared.add(source)
            elem(file,'name',Path(source).name);elem(file,'pathurl',Path(source).as_uri())
            xml_rate(file,frame_rate(str(srcfps)))
            elem(file,'duration',round((info['duration'] or job['_duration'])*srcfps))
            fm=elem(file,'media');fv=elem(elem(fm,'video'),'samplecharacteristics')
            elem(fv,'width',info['width']);elem(fv,'height',info['height']);xml_rate(fv,frame_rate(str(srcfps)))
            elem(fv,'pixelaspectratio','square');elem(fv,'fielddominance','none')
            if info['has_audio']:
                fa=elem(fm,'audio');elem(fa,'channelcount',info['audio_channels'])
                ac=elem(fa,'samplecharacteristics');elem(ac,'samplerate',info['audio_sample_rate'])
        sourcetrack=elem(item,'sourcetrack');elem(sourcetrack,'mediatype','audio' if kind=='audio' else 'video');elem(sourcetrack,'trackindex',1)
    for i,c in enumerate(job['clips'],1):
        add_clip(tracks[0],c,c['output_start_frame'],c['frames'],c['start'],'video',i)
        if c['info']['has_audio']:
            add_clip(atrack,c,c['output_start_frame'],c['frames'],c['start'],'audio',i)
    for i,b in enumerate(job['broll'],1):
        add_clip(tracks[1],b,b['output_start_frame'],b['frames'],b['source_start'],'broll',i)
    ET.indent(root,space='  ')
    Path(path).write_bytes(b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n'+ET.tostring(root,encoding='utf-8'))


def tc(frame,fps):
    nominal=round(float(fps));h,rem=divmod(frame,nominal*3600);m,rem=divmod(rem,nominal*60);s,f=divmod(rem,nominal)
    if h>=24: raise ValueError('CMX3600 timecode cannot represent this duration')
    return f'{h:02d}:{m:02d}:{s:02d}:{f:02d}'


def write_edl(job,path):
    rows=[f'TITLE: {job.get("name","Talking head")}', 'FCM: NON-DROP FRAME',
          '* Basic V1 cuts only; import XML for B-roll tracks and use captions.srt separately.']
    for i,c in enumerate(job['clips'],1):
        rec=c['output_start_frame'];si=round(c['start']*float(job['fps']))
        rows += [f'{i:03d}  R{i:07d} V     C        {tc(si,job["fps"])} {tc(si+c["frames"],job["fps"])} {tc(rec,job["fps"])} {tc(rec+c["frames"],job["fps"])}',
                 f'* FROM CLIP NAME: {Path(c["source"]).name}',f'* SOURCE FILE: {c["source"]}']
    Path(path).write_text('\n'.join(rows)+'\n',encoding='utf-8')


def jianying_data(job,template=None):
    draft=copy.deepcopy(template) if template else {'version':360000,'materials':{},'tracks':[]}
    if not isinstance(draft,dict) or not isinstance(draft.get('materials',{}),dict):
        raise ValueError('Template is not readable draft-content style JSON')
    old_tracks=draft.get('tracks',[])
    old_video=next((t for t in old_tracks if t.get('type')=='video' and t.get('segments')),None)
    old_text=next((t for t in old_tracks if t.get('type')=='text' and t.get('segments')),None)
    video_skeleton=old_video['segments'][0] if old_video else {}
    text_skeleton=old_text['segments'][0] if old_text else {}
    materials=copy.deepcopy(draft.get('materials',{}))
    materials['videos']=[];materials['texts']=[]
    material_ids={}
    for source,info in job['_media'].items():
        ident=str(uuid.uuid4()).upper();material_ids[source]=ident
        materials['videos'].append({'id':ident,'path':source,'material_name':Path(source).name,
                                    'type':'photo' if info['image'] else 'video','duration':round((info['duration'] or job['_duration'])*1e6),
                                    'width':info['width'],'height':info['height']})
    tracks=[]
    for kind,events in [('video',job['clips']),('video',job['broll'])]:
        if not events: continue
        segs=[]
        for c in events:
            ss=c.get('start',c.get('source_start',0));at=c['output_start_frame']/float(job['fps']);dur=c['frames']/float(job['fps'])
            seg=copy.deepcopy(video_skeleton)
            # Do not retain effect/material references from the template's old clip.
            seg.update({'id':str(uuid.uuid4()).upper(),'material_id':material_ids[c['source']],
                        'source_timerange':{'start':round(ss*1e6),'duration':round(dur*1e6)},
                        'target_timerange':{'start':round(at*1e6),'duration':round(dur*1e6)},
                        'speed':1.0,'volume':1.0 if events is job['clips'] else 0.0,'extra_material_refs':[]})
            segs.append(seg)
        track=copy.deepcopy(old_video) if old_video else {}
        track.update({'id':str(uuid.uuid4()).upper(),'type':kind,'segments':segs});tracks.append(track)
    if job['subtitles']:
        segs=[]
        for c in job['subtitles']:
            ident=str(uuid.uuid4()).upper()
            materials['texts'].append({'id':ident,'type':'text','content':json.dumps({'text':c['text']},ensure_ascii=False)})
            seg=copy.deepcopy(text_skeleton)
            seg.update({'id':str(uuid.uuid4()).upper(),'material_id':ident,
                        'target_timerange':{'start':round(c['start']*1e6),'duration':round((c['end']-c['start'])*1e6)},
                        'source_timerange':None,'extra_material_refs':[]})
            segs.append(seg)
        track=copy.deepcopy(old_text) if old_text else {}
        track.update({'id':str(uuid.uuid4()).upper(),'type':'text','segments':segs});tracks.append(track)
    draft.update({'id':str(uuid.uuid4()).upper(),'name':job.get('name','Talking head'),
                  'duration':round(job['_duration']*1e6),'fps':float(job['fps']),
                  'materials':materials,'tracks':tracks})
    canvas=copy.deepcopy(draft.get('canvas_config',{}));canvas.update({'width':job['width'],'height':job['height']})
    draft['canvas_config']=canvas
    return draft


def cmd_export(a):
    job=load_job(a.job)
    out=Path(a.out_dir).resolve()
    if out.exists() and any(out.iterdir()):
        raise ValueError('Export directory must be new or empty; existing exports are not overwritten')
    out.mkdir(parents=True,exist_ok=True)
    write_xml(job,out/'resolve-premiere.xml')
    write_edl(job,out/'resolve-v1.edl')
    write_srt(out/'captions.srt',job['subtitles'])
    # A clean canonical job remains portable if media paths are relinked on transfer.
    clean=copy.deepcopy(job)
    for k in list(clean):
        if k.startswith('_'):clean.pop(k)
    clean['fps']=str(job['fps'])
    for key in ('clips','broll'):
        for e in clean.get(key,[]):
            for k in ('info','frames','output_start_frame'):e.pop(k,None)
    write_json(out/'job.json',clean)
    draft=jianying_data(job)
    write_json(out/'jianying-best-effort-draft_content.json',draft)
    template_info=None
    if a.jianying_template:
        source=Path(a.jianying_template).resolve()
        payload=source/'draft_content.json' if source.is_dir() else source
        if not payload.is_file():raise ValueError('No draft_content.json in supplied template')
        original=payload.read_bytes()
        try: parsed=json.loads(original.decode('utf-8-sig'))
        except (ValueError,UnicodeError):raise ValueError('Template is encrypted/opaque or not JSON; this skill does not bypass application encryption')
        dest=out/'jianying-template-adapted'
        if source.is_dir():
            # Do not follow template symlinks or copy an ancestor of this output.
            if source==out or source in out.parents:raise ValueError('Template cannot contain the export directory')
            if any(p.is_symlink() for p in source.rglob('*')):raise ValueError('Template symlinks require manual review')
            shutil.copytree(source,dest)
        else:dest.mkdir()
        write_json(dest/'draft_content.json',jianying_data(job,parsed),overwrite=True)
        if payload.read_bytes()!=original:raise ValueError('Template source unexpectedly changed')
        template_info={'source':str(source),'source_sha256':hashlib.sha256(original).hexdigest(),
                       'adapted_copy':str(dest),'source_unchanged':True,'native_ui_verified':False}
    if job['grade'].get('lut'):
        shutil.copyfile(job['grade']['lut'],out/'selected-look.cube')
    notes=[
      '# 剪辑交付与兼容性',
      '',
      f'片名：{job.get("name","Talking head")}；{job["_frames"]} 帧；{job["_duration"]:.6f} 秒；帧率 {job["fps"]}。',
      '',
      '- resolve-premiere.xml：FCP7-style XML，含主剪辑、源音频和独立 B-roll 视频轨；可作为 Resolve/Premiere 导入候选。',
      '- resolve-v1.edl：CMX3600 NDF 基础 V1 切点；不包含 B-roll、字幕、调色或混音。非整数帧率采用名义帧号，源/时间线帧率不同时请用 XML 并核对切点。',
      '- captions.srt：在目标软件单独导入字幕轨。',
      '- jianying-best-effort-draft_content.json：非官方格式试验交付，不能声称适配所有剪映/CapCut版本。',
      '- jianying-template-adapted（如提供模板）：在独立副本中替换轨道与素材，保留模板其他状态；原模板不修改。必须同版本打开复核，模板不等于已验证原生兼容。',
      '- XML/EDL 不携带本地 FFmpeg 烧录字幕、B-roll cover 裁切、调色/LUT及响度处理。需要外观一致时使用实际渲染的 MP4；需要继续编辑时在目标软件复核并重新应用这些设置。',
      '- 当前未在 Resolve/Premiere/剪映 UI 中实际导入验证；XML 可解析不等于软件导入成功。',
      '- 媒体路径指向当前机器；跨机器传输请带齐原素材并重链接，或在目标 Windows 机器重新运行 export。',
      '- 不猜测 Log/HDR 输入色彩空间；LUT 必须与真实输入相匹配。',
      '- 尚需核对：首尾/每个切口、字词与字幕、B-roll语义/权利/裁切、肤色、音画同步和实际声音。',
    ]
    (out/'compatibility.md').write_text('\n'.join(notes)+'\n',encoding='utf-8')
    report={'output_directory':str(out),'frames':job['_frames'],'duration':job['_duration'],
            'xml_parse':'passed','software_ui_verified':False,'template':template_info,
            'capabilities':{'xml_main_cuts':True,'xml_source_audio':True,'xml_broll_second_track':True,'srt_sidecar':True,
                            'edl_v1_cuts':True,'jianying_best_effort':True,'same_version_template_copy':bool(template_info)},
            'losses':['burned subtitle appearance','FFmpeg grade and LUT','B-roll cover crop','two-pass loudness mix',
                      'proprietary app effects and state','cross-machine absolute media paths require relinking'],
            'exports':['resolve-premiere.xml','resolve-v1.edl','captions.srt','job.json','jianying-best-effort-draft_content.json','compatibility.md']}
    ET.parse(out/'resolve-premiere.xml')
    write_json(out/'export-report.json',report)
    print(json.dumps(report,ensure_ascii=False))


def cmd_inventory(a):
    root=Path(a.directory).resolve();out=Path(a.out).resolve()
    if out.exists():raise ValueError('Inventory output already exists; choose a new path')
    if not root.is_dir():raise ValueError('Media library is not a directory')
    items=[]
    evidence=Path(a.frames_dir).resolve() if a.frames_dir else None
    if evidence:
        if evidence.exists() and any(evidence.iterdir()):raise ValueError('Frame evidence directory must be new or empty')
        evidence.mkdir(parents=True,exist_ok=True)
    for p in sorted(root.rglob('*')):
        if not p.is_file() or p.suffix.lower() not in IMAGE_SUFFIXES|VIDEO_SUFFIXES:continue
        try:
            info=probe(p);ident=hashlib.sha256(str(p).encode()).hexdigest()[:12]
            entry={'id':ident,**info,'observed_subjects':[],'semantic_tags':[],
                   'rights':{'status':'unknown','basis':''},'visual_review':'not_performed','frames':[]}
            if evidence:
                times=[0] if info['image'] else [info['duration']*f for f in (.15,.5,.85)]
                for n,t in enumerate(times,1):
                    dst=evidence/f'{ident}-{n}.jpg'
                    run(ff('-v','error','-y','-ss',str(t),'-threads','2','-i',p,'-frames:v','1','-vf','scale=640:-2',dst))
                    entry['frames'].append(str(dst))
            items.append(entry)
        except ValueError as e:items.append({'path':str(p),'error':str(e)})
    write_json(out,{'version':1,'library':str(root),'assets':items,
                    'note':'Metadata and frame extraction only. A visual model/person must inspect evidence and annotate subjects/rights before semantic selection.'})
    print(json.dumps({'inventory':str(out),'assets':len(items),'visual_review_performed':False}))


def cmd_doctor(a):
    bins={n:shutil.which(n) for n in ('ffmpeg','ffprobe')}
    caps={'binaries':bins,'python':sys.version.split()[0],'network_required':False,
          'optional_asr_installed':__import__('importlib.util',fromlist=['find_spec']).find_spec('faster_whisper') is not None,
          'resolve_connected':False,'nle_ui_validated':False}
    if bins['ffmpeg']:
        filters=run(['ffmpeg','-hide_banner','-filters']).stdout
        caps['ffmpeg_filters']={f:bool(re.search(r'\b'+f+r'\b',filters)) for f in ('subtitles','lut3d','loudnorm','overlay','silencedetect')}
    print(json.dumps(caps,ensure_ascii=False,indent=2))
    if not all(bins.values()):raise ValueError('FFmpeg and ffprobe must be installed and on PATH')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('doctor');p.set_defaults(func=cmd_doctor)
    p=sub.add_parser('transcribe');p.add_argument('source');p.add_argument('--model',required=True)
    p.add_argument('--out',required=True);p.add_argument('--language',default=None);p.add_argument('--device',default='cpu')
    p.add_argument('--compute-type',default='int8');p.set_defaults(func=cmd_transcribe)
    p=sub.add_parser('plan');p.add_argument('source');p.add_argument('--transcript');p.add_argument('--out',required=True)
    p.add_argument('--name');p.add_argument('--fps');p.add_argument('--width',type=int);p.add_argument('--height',type=int)
    p.add_argument('--max-pause',type=float,default=1.2);p.add_argument('--keep-pause',type=float,default=.4)
    p.add_argument('--caption-words',type=int,default=10);p.add_argument('--noise-db',type=float,default=-38)
    p.add_argument('--trim-measured-silence',action='store_true');p.set_defaults(func=cmd_plan)
    p=sub.add_parser('render');p.add_argument('job');p.add_argument('--out',required=True)
    p.add_argument('--no-burn-subtitles',action='store_true');p.add_argument('--crf',type=int,default=18);p.set_defaults(func=cmd_render)
    p=sub.add_parser('export');p.add_argument('job');p.add_argument('--out-dir',required=True)
    p.add_argument('--jianying-template');p.set_defaults(func=cmd_export)
    p=sub.add_parser('inventory');p.add_argument('directory');p.add_argument('--out',required=True)
    p.add_argument('--frames-dir');p.set_defaults(func=cmd_inventory)
    a=parser.parse_args(argv)
    if a.command=='plan' and (a.keep_pause<0.1 or a.max_pause<=a.keep_pause or a.caption_words<1):
        parser.error('--max-pause must exceed --keep-pause >=0.1; caption words must be positive')
    try:a.func(a)
    except (ValueError,OSError,KeyError,json.JSONDecodeError) as e:
        print(f'ERROR: {e}',file=sys.stderr);return 2
    return 0


if __name__=='__main__':
    sys.exit(main())
