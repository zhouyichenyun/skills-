#!/usr/bin/env python3
"""Optional local Resolve bridge. --dry-run inspects inputs without connecting.

Requires a locally installed/running Resolve and the Python scripting capability
provided by that exact edition/version. No Resolve installation is bundled.
"""
from __future__ import annotations
import argparse
import importlib
import json
import os
from pathlib import Path
import sys
import uuid
from xml.etree import ElementTree as ET


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--timeline-xml',required=True)
    p.add_argument('--project-name',default='Talking-head import')
    p.add_argument('--timeline-name',default='Talking-head imported timeline')
    p.add_argument('--drx',help='Explicitly selected local DRX grade to apply after import')
    p.add_argument('--drx-track',type=int,default=1)
    p.add_argument('--use-current-project',action='store_true',help='Explicitly permit saving/importing into the currently open project')
    p.add_argument('--dry-run',action='store_true')
    a=p.parse_args()
    timeline=Path(a.timeline_xml).resolve()
    if not timeline.is_file():p.error('Timeline XML is missing')
    ET.parse(timeline)
    drx=Path(a.drx).resolve() if a.drx else None
    if drx and (not drx.is_file() or drx.suffix.lower()!='.drx'):p.error('DRX must be an existing local .drx')
    if a.drx_track<1:p.error('--drx-track must be positive')
    description={'timeline_xml':str(timeline),'project_name':a.project_name,
                 'timeline_name':a.timeline_name,'selected_drx':str(drx) if drx else None,
                 'drx_track':a.drx_track,'dry_run':a.dry_run,'connected':False,
                 'software_ui_verified':False,'project_strategy':'current_project_explicit' if a.use_current_project else 'create_new_unique_project'}
    if a.dry_run:
        description['status']='inputs_validated_only; no Resolve connection attempted'
        print(json.dumps(description,ensure_ascii=False,indent=2));return 0
    candidates=[]
    if os.environ.get('RESOLVE_SCRIPT_API'):
        candidates.append(Path(os.environ['RESOLVE_SCRIPT_API'])/'Modules')
    if sys.platform=='win32':
        candidates.append(Path(os.environ.get('PROGRAMDATA',r'C:\ProgramData'))/'Blackmagic Design/DaVinci Resolve/Support/Developer/Scripting/Modules')
    elif sys.platform=='darwin':
        candidates.append(Path('/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules'))
    else:
        candidates.append(Path('/opt/resolve/Developer/Scripting/Modules'))
    for candidate in candidates:
        if candidate.is_dir():sys.path.insert(0,str(candidate))
    try:
        script=importlib.import_module('DaVinciResolveScript')
    except ImportError:
        raise ValueError('Resolve Python module unavailable. Use the installed app\'s Developer/Scripting documentation and RESOLVE_SCRIPT_API; importing XML manually remains supported.')
    resolve=script.scriptapp('Resolve')
    if not resolve:
        raise ValueError('Resolve did not connect. Open the app and check External scripting Local and the exact version/edition\'s scripting support. Dry-run never establishes this connection.')
    pm=resolve.GetProjectManager()
    if a.use_current_project:
        project=pm.GetCurrentProject()
        if not project:raise ValueError('--use-current-project was requested but no project is open')
    else:
        unique_name=a.project_name+'-'+uuid.uuid4().hex[:8]
        project=pm.CreateProject(unique_name)
    if not project:raise ValueError('Could not obtain/create the requested project; no timeline imported')
    if not pm.SaveProject():raise ValueError('SaveProject failed before import')
    media_pool=project.GetMediaPool()
    imported=media_pool.ImportTimelineFromFile(str(timeline),{'timelineName':a.timeline_name,'importSourceClips':True})
    if not imported:raise ValueError('Resolve ImportTimelineFromFile failed; verify source paths and importer compatibility')
    if not project.SetCurrentTimeline(imported):raise ValueError('Imported timeline could not be made current')
    description.update({'connected':True,'version':resolve.GetVersionString(),
                        'project':project.GetName(),'timeline':imported.GetName(),
                        'status':'timeline_imported_by_api','drx_applied':False})
    if drx:
        items=imported.GetItemListInTrack('video',a.drx_track) or []
        if not items:raise ValueError('Requested DRX track contains no video items')
        applied=imported.ApplyGradeFromDRX(str(drx),0,items)
        if not applied:raise ValueError('ApplyGradeFromDRX failed; do not report the grade as applied')
        description.update({'drx_applied':True,'graded_item_count':len(items)})
    if not pm.SaveProject():raise ValueError('SaveProject failed after import; inspect the still-open timeline')
    description['review_required']='Inspect media linking, first/last frames, every cut, B-roll framing, subtitles, actual sound and source-aware color in Resolve. API success is not visual approval.'
    print(json.dumps(description,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':
    try:sys.exit(main())
    except (ValueError,OSError,ET.ParseError) as e:
        print(f'ERROR: {e}',file=sys.stderr);sys.exit(2)
