"""Git-apply an approved download locally, then reverify exact bytes in server OCI."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from patchtrail.config import ROOT
from patchtrail.workspace import tree_hash
from scripts.verify_imported_live import CASES,EVIDENCE,IMAGES,save

REMOTE=r'''
import hashlib,json,os,sys,uuid
from pathlib import Path
os.chdir('/opt/patchtrail/current');sys.path.insert(0,'/opt/patchtrail/current')
from patchtrail.projects import ProjectCatalog
from patchtrail.workspace import prepare_workspace,tree_hash
from patchtrail.sandbox import DockerRunner
payload=json.load(sys.stdin);base=Path('/var/lib/patchtrail/data/independent-imported-validation');base.mkdir(mode=0o700,exist_ok=True)
task=ProjectCatalog('/var/lib/patchtrail/data/projects').get_task(payload['task_id'])
source_before=tree_hash(task.source_dir);assert source_before==task.source_hash==payload['base_source_hash']
assert set(payload['replacements'])<=set(task.editable_files)
workspace=prepare_workspace(base,'check-'+uuid.uuid4().hex,task)
runner=DockerRunner('podman',payload['image'],timeout=20)
before=runner.run(workspace,task.check_command,'independent-before-'+uuid.uuid4().hex).to_dict()
for relative,entry in payload['replacements'].items():
 content=entry['content'].encode();assert len(content)<=128*1024 and hashlib.sha256(content).hexdigest()==entry['sha256']
 (workspace/relative).write_bytes(content)
for path in task.source_dir.rglob('*'):
 if path.is_file() and path.relative_to(task.source_dir).as_posix() not in payload['replacements']:
  assert path.read_bytes()==(workspace/path.relative_to(task.source_dir)).read_bytes(),'Unapproved file changed'
after=runner.run(workspace,task.check_command,'independent-after-'+uuid.uuid4().hex).to_dict()
assert tree_hash(task.source_dir)==source_before,'Original imported snapshot changed'
receipt={'case':payload['case'],'run_id':payload['run_id'],'task_id':task.id,'snapshot_id':task.snapshot_id,'source_hash':source_before,'image':payload['image'],'profile':task.check_profile,'patch_sha256':payload['patch_sha256'],'git_apply_location':'local isolated copy of the same immutable source hash; source was never executed on the host','git_apply':payload['git_apply'],'remote_replacements_match_git_applied_sha256':{k:v['sha256'] for k,v in payload['replacements'].items()},'before':before,'after':after,'original_snapshot_unchanged':True,'immutable_test_files_unchanged':True,'actual_oci':True,'host_source_execution':False,'independent_workspace':str(workspace),'passed':bool(before['complete'] and before['exit_code']==1 and not before['passed'] and after['passed'])}
p=base/(payload['run_id']+'.json');p.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n');p.chmod(0o600)
print(json.dumps(receipt,ensure_ascii=False))
'''

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('case',choices=CASES);p.add_argument('--attempt',type=int,choices=[1,2],default=1);a=p.parse_args();stem=a.case+('' if a.attempt==1 else '-attempt2')
 pending=json.loads((EVIDENCE/(stem+'.request.json')).read_text());report=json.loads((EVIDENCE/(pending['run_id']+'.json')).read_text());patch_path=EVIDENCE/(stem+'.diff');patch=patch_path.read_text();task=report['validation_context']['task_metadata']
 if report['run']['state']!='succeeded' or patch!=report['run']['proposal']['diff']:raise RuntimeError('Only the exact approved successful patch can be rechecked')
 profile=task['check_profile'];proposal=report['run']['proposal'];entries=proposal.get('files')or[proposal];paths={entry['path']:entry for entry in entries};checks=[];replacements={}
 with tempfile.TemporaryDirectory(prefix='patchtrail-exported-reapply-') as tmp:
  workspace=Path(tmp)
  for file in task['files']:
   content=(ROOT/'artifacts/private/real-cases'/a.case/file['path']).read_bytes()
   if hashlib.sha256(content).hexdigest()!=file['sha256']:raise RuntimeError('Original local and server source differ')
   target=workspace/file['path'];target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(content)
  if tree_hash(workspace)!=task['source_hash']:raise RuntimeError('Independent original source hash differs')
  for argv in (['git','apply','--check','-'],['git','apply','-']):
   result=subprocess.run(argv,cwd=workspace,input=patch,text=True,capture_output=True,timeout=15);checks.append({'argv':argv,'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr})
   if result.returncode:raise RuntimeError('Downloaded patch failed independent local Git application')
  for file in task['files']:
   content=(workspace/file['path']).read_bytes();digest=hashlib.sha256(content).hexdigest()
   if file['path'] in paths:
    if digest!=paths[file['path']]['new_sha256']:raise RuntimeError('Git-applied bytes differ from the exact reviewed replacement')
    replacements[file['path']]={'content':content.decode(),'sha256':digest}
   elif digest!=file['sha256']:raise RuntimeError('An unapproved file changed')
 payload={'case':a.case,'run_id':pending['run_id'],'task_id':pending['task_id'],'image':IMAGES[profile],'patch_sha256':hashlib.sha256(patch.encode()).hexdigest(),'base_source_hash':task['source_hash'],'git_apply':checks,'replacements':replacements}
 command='sudo -n -u patchtrail -H env XDG_RUNTIME_DIR=/run/user/1001 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus /opt/patchtrail/current/.venv/bin/python -c '+shlex.quote(REMOTE)
 result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','vps01-madokanun',command],input=json.dumps(payload),text=True,capture_output=True,timeout=90)
 if result.returncode:
  save(EVIDENCE/(stem+'.replay-failure.json'),{'case':a.case,'run_id':pending['run_id'],'git_apply':checks,'error':result.stderr[-3000:],'model_called':False})
  raise RuntimeError('Independent remote replay failed; private evidence retained')
 receipt=json.loads(result.stdout);receipt['attempt']=a.attempt;save(EVIDENCE/(stem+'.independent-replay.json'),receipt)
 print(json.dumps({'case':a.case,'run_id':pending['run_id'],'passed':receipt['passed'],'before_tests':receipt['before']['tests_run'],'before_exit':receipt['before']['exit_code'],'after_tests':receipt['after']['tests_run'],'after_exit':receipt['after']['exit_code'],'runner':receipt['after']['runner'],'immutable_tests_and_original_unchanged':True},ensure_ascii=False))
 if not receipt['passed']:raise SystemExit(1)

if __name__=='__main__':main()
