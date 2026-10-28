"""One-attempt imported live validation; review and exact approval are separate actions."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from patchtrail.config import ROOT,Settings,load_local_env
from scripts.verify_live import request

CASES=('textlab-storage-revision','textlab-storage-inventory','patchtrail-pytest-evidence')
BASE='http://127.0.0.1:18818'
EVIDENCE=ROOT/'artifacts/private/imported-live'
IMAGES={'unittest':'docker.io/library/python@sha256:1148dbb014615300fa7e713b2f94f550c0d1c69780c0805f84f093ceee34dd0b','pytest':'sha256:6896b73045bc7adf6dc64c94d6932aff6a14e20e4d6a8b79b392a801bd81d4f7'}


def save(path,value):
    fd,name=tempfile.mkstemp(prefix='.capture-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:json.dump(value,stream,ensure_ascii=False,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.chmod(name,0o600);os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['start','status','review','approve','fetch']);p.add_argument('case',choices=CASES);p.add_argument('--wait',type=int,default=35);p.add_argument('--digest');p.add_argument('--version',type=int);p.add_argument('--review-note');p.add_argument('--attempt',type=int,choices=[1,2],default=1);p.add_argument('--manifest',type=Path);p.add_argument('--retry-reason');p.add_argument('--expected-runtime-hash');args=p.parse_args()
    if not 0<=args.wait<=45:p.error('Wait must be 0..45 seconds')
    load_local_env();settings=Settings()
    if not settings.admin_token:raise RuntimeError('Local operator credential is unavailable')
    EVIDENCE.mkdir(parents=True,exist_ok=True);EVIDENCE.chmod(0o700)
    stem=args.case+('' if args.attempt==1 else '-attempt2')
    pending=EVIDENCE/(stem+'.request.json')
    stored=json.loads(pending.read_text()) if pending.exists() else {}
    manifest_path=args.manifest or Path(stored.get('manifest_path') or ROOT/'artifacts/private/real-cases'/args.case/'manifest.json')
    manifest=json.loads(manifest_path.read_text())
    with httpx.Client(base_url=BASE,headers={'Authorization':'Bearer '+settings.admin_token},timeout=15,trust_env=False,follow_redirects=False) as client:
        health=request(client,'GET','/api/health')
        if not health.get('model_configured') or not health.get('container_available'):
            raise RuntimeError('Live model or container is not ready')
        if args.expected_runtime_hash and health['runtime_code_hash']!=args.expected_runtime_hash:
            raise RuntimeError('The explicitly expected repaired runtime is not serving')
        tasks=request(client,'GET','/api/tasks')['tasks'];found=[t for t in tasks if t.get('private') and t['title']==manifest['title'] and t['description']==manifest['description']]
        if len(found)!=1:raise RuntimeError('Expected one matching imported task')
        task=found[0]
        for row in task['files']:
            content=(ROOT/'artifacts/private/real-cases'/args.case/row['path']).read_bytes()
            if hashlib.sha256(content).hexdigest()!=row['sha256']:raise RuntimeError('Imported and preregistered source differ')
        if args.action=='start':
            if pending.exists():raise RuntimeError('A request identity already exists; use status to reconcile it, never create another attempt')
            previous_run_id=None
            if args.attempt==2:
                if not args.retry_reason:p.error('An explicitly authorized retry reason is required for attempt 2')
                first=EVIDENCE/(args.case+'.request.json')
                if not first.exists():raise RuntimeError('The original attempt record must be retained')
                previous_run_id=json.loads(first.read_text())['run_id']
                previous=request(client,'GET','/api/runs/'+previous_run_id)
                if previous['state'] not in {'failed','cancelled'}:raise RuntimeError('Only a known failed/rejected attempt can have an explicitly authorized iteration')
            runs=request(client,'GET','/api/runs')['runs']
            if any(r['task_id']==task['id'] and r['mode']=='live' and r['id']!=previous_run_id for r in runs):raise RuntimeError('A live attempt already exists for this task; reconcile before proceeding')
            state={'case':args.case,'attempt':args.attempt,'previous_run_id':previous_run_id,'retry_reason':args.retry_reason,'manifest_path':str(manifest_path.resolve()),'task_id':task['id'],'request_key':'imported-live-'+args.case+'-'+uuid.uuid4().hex,'created_at_utc':datetime.now(timezone.utc).isoformat(),'run_id':None,'write_outcome':'pending'}
            with pending.open('x') as stream:json.dump(state,stream,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
            pending.chmod(0o600)
            row=request(client,'POST','/api/runs',json={'task_id':task['id'],'mode':'live'},headers={'Idempotency-Key':state['request_key']})
            state.update(run_id=row['id'],write_outcome='confirmed');save(pending,state)
        else:
            state=json.loads(pending.read_text())
            if not state['run_id']:
                runs=request(client,'GET','/api/runs')['runs'];matches=[r for r in runs if r['task_id']==task['id'] and r['mode']=='live' and r['id']!=state.get('previous_run_id')]
                if len(matches)!=1:raise RuntimeError('Unknown creation outcome: preserve this request key; do not start another paid task')
                state.update(run_id=matches[0]['id'],write_outcome='reconciled by unique task run');save(pending,state)
            row=request(client,'GET','/api/runs/'+state['run_id'])
        print(json.dumps({'case':args.case,'run_id':row['id'],'state':row['state'],'limits':row['limits']},ensure_ascii=False),flush=True)
        if args.action=='approve':
            if not args.digest or args.version is None or not args.review_note:p.error('Exact digest/version and an actual review note are required')
            if row['state']!='awaiting_approval' or row['proposal']['digest']!=args.digest or row['version']!=args.version:raise RuntimeError('Approval snapshot is no longer exact')
            paths=[f['path'] for f in row['proposal'].get('files',[])] or [row['proposal'].get('path')]
            if not set(paths)<=set(task['editable_files']):raise RuntimeError('Proposal exceeds the preregistered editable allowlist')
            save(EVIDENCE/(stem+'.approval-review.json'),{'run_id':row['id'],'reviewer':'Codex internal code review; not user acceptance','note':args.review_note,'digest':args.digest,'expected_version':args.version,'files':paths,'reviewed_at_utc':datetime.now(timezone.utc).isoformat()})
            row=request(client,'POST','/api/runs/'+row['id']+'/approve',json={'proposal_digest':args.digest,'expected_version':args.version})
        deadline=time.monotonic()+args.wait
        while row['state'] in {'queued','diagnosing','verifying'} and time.monotonic()<deadline:
            time.sleep(.6);row=request(client,'GET','/api/runs/'+row['id'])
        report=request(client,'GET','/api/runs/'+row['id']+'/report')
        previous_capture=EVIDENCE/(row['id']+'.json')
        if previous_capture.exists():
            previous=json.loads(previous_capture.read_text())
            for key in ('record_corrections','operator_review_outcome'):
                if key in previous:report[key]=previous[key]
        origin=next((e['payload'] for e in report['events'] if e['kind']=='run_created'),{})
        report['validation_context']={'case':args.case,'attempt':args.attempt,'previous_run_id':state.get('previous_run_id'),'retry_reason':state.get('retry_reason'),'task_metadata':task,'run_origin_runtime_code_hash':origin.get('runtime_code_hash'),'controller_runtime_code_hash_at_capture':health['runtime_code_hash'],'configured_model':health['model'],'container_image':IMAGES[task['check_profile']],'approval_actor':'Codex internal review; not human user acceptance','scope':'Pre-registered owned-repository small feature; all attempts retained, explicitly authorized iterations identified; not a blind benchmark'}
        save(EVIDENCE/(row['id']+'.json'),report)
        print(json.dumps({'run_id':row['id'],'state':row['state'],'stop_reason':row.get('stop_reason'),'calls':row['checkpoint'].get('calls'),'steps':row['checkpoint'].get('steps'),'usage':row['checkpoint'].get('usage'),'digest':(row.get('proposal')or{}).get('digest'),'version':row['version']},ensure_ascii=False),flush=True)
        if args.action=='review' and row.get('proposal'):print(row['proposal']['diff'],flush=True)
        if args.action=='fetch' and row.get('proposal'):
            response=client.get('/api/runs/'+row['id']+'/patch.diff');response.raise_for_status();patch=EVIDENCE/(stem+'.diff');patch.write_bytes(response.content);patch.chmod(0o600)
            if response.text!=row['proposal']['diff']:raise RuntimeError('Downloaded patch differs from reviewed proposal')
            print(json.dumps({'patch_downloaded':patch.name,'sha256':hashlib.sha256(response.content).hexdigest()}),flush=True)
        if row.get('result'):print(json.dumps({'result':row['result']},ensure_ascii=False),flush=True)
        return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except (RuntimeError,httpx.HTTPError) as exc:print(str(exc),file=sys.stderr);raise SystemExit(2)
