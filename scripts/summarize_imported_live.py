"""Summarize every retained logical-case attempt without publishing source or credentials."""
from datetime import datetime,timezone
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from patchtrail.config import ROOT
from scripts.verify_imported_live import EVIDENCE,CASES


RAW_FIELDS = {'output', 'stdout', 'stderr', 'diff', 'content', 'text', 'csv'}
LOCAL_PATH_FIELDS = {'independent_workspace', 'source_dir', 'data_dir', 'directory',
                     'evidence_path', 'manifest_path'}
PRIVATE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/(?:Users|private|var|tmp|home|opt|srv|etc|root|run|workspace)/)[^\s\"'<>]+")


def digest_only(value):
 payload=value.encode('utf-8') if isinstance(value,str) else json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
 return {'sha256':hashlib.sha256(payload).hexdigest(),'bytes':len(payload)}


def public_metadata(value):
 """Never copy operator stdout, source, test names, or absolute evidence paths."""
 if isinstance(value,dict):
  result={}
  for key,child in value.items():
   if key in RAW_FIELDS:
    result[key+'_evidence']=digest_only(child)
   elif key in LOCAL_PATH_FIELDS:
    result[key+'_evidence']=digest_only(child)
   elif key=='test_outcomes':
    result['reported_outcome_entry_count']=len(child)
    result['reported_outcome_states']=dict(Counter(row.get('state','unknown') for row in child))
    result['test_outcomes_evidence']=digest_only(child)
   else:result[key]=public_metadata(child)
  return result
 if isinstance(value,list):return [public_metadata(child) for child in value]
 if isinstance(value,str):
  if value.startswith(('/', '~/')) or re.match(r'^[A-Za-z]:[\\/]',value):return {'private_path_evidence':digest_only(value)}
  return PRIVATE_PATH.sub(lambda match:'[private path sha256:'+digest_only(match.group())['sha256']+']',value)
 return value


def main():
 attempts=[]
 for request_file in sorted(EVIDENCE.glob('*.request.json')):
  request=json.loads(request_file.read_text());run_id=request.get('run_id')
  if not run_id or not (EVIDENCE/(run_id+'.json')).is_file():continue
  report=json.loads((EVIDENCE/(run_id+'.json')).read_text());run=report['run'];context=report['validation_context'];task=context['task_metadata'];stem=request_file.name.removesuffix('.request.json');replay_path=EVIDENCE/(stem+'.independent-replay.json');review_path=EVIDENCE/(stem+'.approval-review.json');rejected_path=EVIDENCE/(stem+'.review-rejected.json')
  checks=[event['payload'].get('result',{}) if event['kind']=='tool_result' else event['payload'] for event in report['events'] if (event['kind']=='tool_result' and event['payload'].get('name')=='run_check') or event['kind']=='check_unconfirmed']
  replay=json.loads(replay_path.read_text()) if replay_path.exists() else None
  row={'case':request['case'],'attempt':request.get('attempt',1),'previous_run_id':request.get('previous_run_id'),'iteration_reason':request.get('retry_reason'),'run_id':run_id,'task_id':run['task_id'],'project_id':task['project_id'],'task_kind':'pre-registered small feature on owned repository source','snapshot_id':task['snapshot_id'],'source_hash':task['source_hash'],'profile':task['check_profile'],'container_image':context['container_image'],'state':run['state'],'stop_reason':run.get('stop_reason'),'model_calls':run['checkpoint'].get('calls',0),'tool_calls':run['checkpoint'].get('steps',0),'usage':run['checkpoint'].get('usage',{}),'configured_model':run.get('configured_model'),'reported_models':run.get('observed_models',[]),'run_origin_runtime_code_hash':context['run_origin_runtime_code_hash'],'initial_check':checks[0] if checks else None,'approval':json.loads(review_path.read_text()) if review_path.exists() else None,'review_rejection':json.loads(rejected_path.read_text()) if rejected_path.exists() else None,'verification':run.get('result'),'independent_replay':({k:v for k,v in replay.items() if k!='independent_workspace'} if replay else None)}
  if report.get('record_corrections'):row['record_corrections']=report['record_corrections']
  # Keep all attempts; the final publication pass retains metadata and hashes only.
  attempts.append(row)
 attempts.sort(key=lambda row:(CASES.index(row['case']),row['attempt']))
 latest={}
 for case in CASES:
  rows=[r for r in attempts if r['case']==case]
  if rows:latest[case]=max(rows,key=lambda r:r['attempt'])
 complete=[r for r in latest.values() if r['state']=='succeeded' and r['independent_replay'] and r['independent_replay']['passed']]
 terminal=len(latest)==len(CASES) and all(row['state'] in {'succeeded','failed','cancelled'} for row in latest.values())
 status='completed' if len(complete)==len(CASES) else 'completed_with_recorded_rejections' if terminal else 'in_progress'
 result={'recorded_at_utc':datetime.now(timezone.utc).isoformat(),'status':status,'scope':'Three pre-registered owned-source small features across two repositories; not a blind benchmark or general repair rate. All attempts, internal review rejections and infrastructure failures retained.','approval_actor':'Codex internal code review and operator endpoint confirmation; not human user acceptance','logical_case_count':len(CASES),'attempt_count':len(attempts),'registered_source_projects':sorted({r['project_id'] for r in attempts}),'complete_latest_cases':len(complete),'complete_latest_source_projects':sorted({r['project_id'] for r in complete}),'review_rejected_attempt_count':sum(bool(r['review_rejection']) for r in attempts),'recorded_failed_attempt_count':sum(r['state']=='failed' for r in attempts),'additional_model_attempts_planned':False if terminal else None,'total_model_calls':sum(r['model_calls'] for r in attempts),'total_tool_calls':sum(r['tool_calls'] for r in attempts),'total_model_tokens':sum(r['usage'].get('total_tokens',0) for r in attempts),'attempts':attempts,'notes':['The first revision case initially stopped because legitimate unittest subTest error counts exceeded top-level test count; original failure evidence is retained. A separately authorized engineering retry after the runtime correction completed.','Both pytest-evidence proposals were rejected before approval: the first removed an existing exception and accepted all-skipped outcomes; the feedback iteration still changed legacy unittest behavior when earlier nested pytest output was present. Neither model-proposed patch was applied or executed.','The additional engineering retry and review-feedback iteration were separately authorized by the human user; original attempts and usage remain included. No further model attempts are planned.','The server has no Git; independent downloaded-diff application used local Git on an exact source-hash copy, then matching bytes were verified by actual server Podman. No imported source was executed on the host.','Check counts from API verification and independent replay refer to the same tests and must not be added as unique tests.','DeepSeek reported its configured model alias; this does not identify an immutable model-weight snapshot.']}
 archive=EVIDENCE/'full-summary.json'
 if not archive.is_file():raise RuntimeError('Preserve the full combined summary privately before publication')
 public=public_metadata(result)
 public['private_evidence_archive']={'id':'imported-live/full-summary.json',**digest_only(archive.read_text())}
 public['publication_policy']='Operator-only raw stdout, source/proposals and filesystem paths remain private; this summary exposes outcomes, counts, review findings and evidence hashes only.'
 for row in public['attempts']:
  report_path=EVIDENCE/(row['run_id']+'.json')
  row['private_report_evidence']={'id':'imported-live/'+report_path.name,**digest_only(report_path.read_text())}
 output=ROOT/'artifacts/imported-live-validation.json';output.write_text(json.dumps(public,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({k:result[k] for k in ('status','logical_case_count','attempt_count','complete_latest_cases','complete_latest_source_projects','total_model_calls','total_tool_calls','total_model_tokens')},ensure_ascii=False))

if __name__=='__main__':main()
