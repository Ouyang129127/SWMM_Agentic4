"""Three real Example1 simulations and current-protocol diagnosis trials."""
import argparse
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback
from uuid import uuid4

BATCH=Path(__file__).resolve().parent
PROJECT=BATCH.parents[1]
sys.path.insert(0,str(PROJECT))
os.environ['SWMM_MODELS_DIR']=str(PROJECT/'models')
os.environ['SWMM_RUNTIME_DIR']=str(BATCH/'runtime')
os.environ['OPENAI_REQUEST_TIMEOUT_SECONDS']='600'

def stamp():return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')
def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8');temp.replace(path)
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def initialize():
    if (BATCH/'plan.json').exists():return read(BATCH/'plan.json')
    prefix='pilot4_20261116_'+uuid4().hex[:6]
    manifest=read(PROJECT/'models/Example1/events/exp15/manifest.json')
    cases=[]
    for name in ['A02_14mm.txt','A03_20mm.txt','A05_40mm.txt']:
        entry=next(e for e in manifest['events'] if e['event_file']==name)
        cases.append({'event_file':name,'rainfall_file':'events/exp15/'+name,
                      'total_mm':entry['total_mm'],'peak_mm_h':entry['peak_mm_h'],
                      'run_id':prefix+'_'+Path(name).stem,'event_sha256':entry['sha256']})
    files=list((PROJECT/'workflow_agents').rglob('*.py'))
    files += [PROJECT/'models/Example1/simulation_timing.json',PROJECT/'models/Example1/model.yaml',
              PROJECT/'models/Example1/swmm/scenarios/dynamic_wave/model.inp']
    files += [PROJECT/'models/Example1'/c['rainfall_file'] for c in cases]
    plan={'created_at':stamp(),'project':str(PROJECT),'model_name':'Example1','scenario_name':'dynamic_wave',
          'simulation_end_time':'2024-01-01 07:00:00','max_supplement_rounds':2,
          'max_diagnosis_attempts_per_case':8,'cases':cases,
          'purpose':'Real smoke trial of current M6, table and shallow shared-view protocol; not a controlled accuracy comparison.',
          'source_hashes':{str(p):sha(p) for p in files}}
    save(BATCH/'plan.json',plan);return plan

def check_sources(plan):
    changed=[name for name,digest in plan['source_hashes'].items() if sha(name)!=digest]
    if changed:raise ValueError('Pilot source changed: '+', '.join(changed))

async def run_case(index):
    plan=read(BATCH/'plan.json');case=plan['cases'][index]
    folder=BATCH/'cases'/Path(case['event_file']).stem;folder.mkdir(parents=True,exist_ok=True)
    root=PROJECT/'models/Example1/runs'/case['run_id'];started=time.monotonic()
    state={'case':case,'run_root':str(root),'status':'running','phase':'simulation','started_at':stamp()}
    save(folder/'status.json',state)
    def progress(phase,**values):
        state.update(phase=phase,updated_at=stamp(),**values);save(folder/'status.json',state)
        print(stamp(),case['event_file'],phase,flush=True)
    try:
        check_sources(plan)
        from workflow_agents.scenario.requests import prepare_scenario_request
        from workflow_agents.simulation.service import run_simulation_from_scenario_request
        from workflow_agents.orchestration.run_preparation import run_workflow_stage
        from workflow_agents.evidence.evidence_package import load_package
        from workflow_agents.diagnosis.contract import event_manifest
        from workflow_agents.diagnosis import service
        from workflow_agents.diagnosis.series_transport import process_evidence_view
        from workflow_agents.evidence.saved_result_evidence import load_supplements
        from workflow_agents.orchestration.investigation import advance_investigation
        spec=importlib.util.spec_from_file_location('pilot_batch_helpers',PROJECT/'scripts/batch_exp15.py')
        helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
        if not (root/'summary.json').exists():
            if root.exists():raise ValueError('Run folder already exists without a completed simulation; refusing overwrite')
            request=prepare_scenario_request('Example1',case['rainfall_file'],Path(case['event_file']).stem,'dynamic_wave',case['run_id'])
            output=await asyncio.to_thread(run_simulation_from_scenario_request,request['scenario_request'])
            (folder/'simulation_result.txt').write_text(output,encoding='utf-8')
            if not (root/'summary.json').exists():raise RuntimeError(output)
        audit=helper.run_audit(root,plan['simulation_end_time']);save(folder/'hydraulic_audit.json',audit)
        progress('evidence_building')
        built=await asyncio.to_thread(run_workflow_stage,'Example1',case['run_id'],'evidence_building')
        if not built['ok']:raise RuntimeError(built['message'])
        package=load_package(root,verify_sources=True);events=event_manifest(package)
        save(folder/'events.json',events)
        if not events:raise ValueError('Selected rainfall did not generate overflow; choose a replacement case')
        state.update(event_count=len(events),overflow_nodes=sorted({e['node_id'] for e in events}),
                     overflow_volume_m3=sum(e['facts']['estimated_volume_m3'] for e in events))
        original_completion=service.configured_completion
        calls=[]
        async def recorded(prompt,audit=None):
            number=len(calls)+1
            task=read(root/'diagnosis_tasks'/prompt['task']['task_id']/'task.json')
            directory=root/'diagnosis_tasks'/task['task_id']
            rows=load_supplements(root,directory,task,read(directory/'evidence_snapshot.json'))
            ids={r['evidence_id'] for r in prompt['visible_evidence']}
            visible=[r for r in rows if r['evidence_id'] in ids]
            raw=copy.deepcopy(prompt);first=copy.deepcopy(prompt)
            for p in [raw,first]:
                for k in ['record_defaults','shared_static','evidence_view_format']:p.pop(k,None)
            raw['visible_evidence']=visible;first['visible_evidence']=process_evidence_view(visible)
            actual_system=service.diagnosis_system_prompt(prompt)
            item={'sequence':number,'started_at':stamp(),'evidence_view_format':prompt.get('evidence_view_format','inline'),
                  'visible_records':len(visible),'shared_definitions':len(prompt.get('shared_static',{})),
                  'characters_raw':len(service.SYSTEM_PROMPT)+len(service.serialize_prompt(raw)),
                  'characters_tables_only':len(service.SYSTEM_PROMPT)+len(service.serialize_prompt(first)),
                  'characters_actual':len(actual_system)+len(service.serialize_prompt(prompt))}
            calls.append(item)
            save(folder/'calls'/f'request_{number:02}.json',{'system':actual_system,'input':prompt,'comparison':item})
            save(folder/'call_index.json',calls)
            progress('diagnosis_call',request_number=number)
            if audit:audit(pilot_comparison=item)
            try:
                result=await original_completion(prompt,audit)
                item.update(status='response_parsed',completed_at=stamp())
                return result
            except Exception as exc:
                item.update(status='failed',error_type=type(exc).__name__,error=str(exc),completed_at=stamp());raise
            finally:save(folder/'call_index.json',calls)
        service.configured_completion=recorded
        progress('diagnosis')
        outcome=await helper.diagnose_case(plan,case,root,folder)
        save(folder/'diagnosis_result.json',outcome);state['diagnosis']=outcome
        if outcome['current_verified_report']:
            progress('display_report',task_id=outcome['task_id'],revision=outcome['revision'])
            report=await advance_investigation('Example1',case['run_id'],'display_report',outcome['task_id'])
            state['display_report']=report.get('display_report',{})
            if report.get('display_report_file'):state['html_file']=report['display_report_file']
        check_sources(plan)
        state.update(status=outcome['status'],phase='finished',completed_at=stamp(),elapsed_seconds=round(time.monotonic()-started,2))
        save(folder/'status.json',state)
        print(json.dumps(state,ensure_ascii=False),flush=True)
    except Exception as exc:
        traceback.print_exc()
        state.update(status='failed',phase=state.get('phase'),error_type=type(exc).__name__,error=str(exc),
                     completed_at=stamp(),elapsed_seconds=round(time.monotonic()-started,2))
        save(folder/'status.json',state)
        raise

async def launch():
    plan=initialize();workers=[]
    for index,case in enumerate(plan['cases']):
        folder=BATCH/'cases'/Path(case['event_file']).stem;folder.mkdir(parents=True,exist_ok=True)
        log=(folder/'worker.log').open('a',encoding='utf-8')
        process=await asyncio.create_subprocess_exec(sys.executable,'-u',str(__file__),'--worker',str(index),
                    cwd=str(PROJECT),stdout=log,stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        workers.append((process,log,case));print('started',case['event_file'],process.pid,flush=True)
    for process,log,case in workers:
        code=await process.wait();log.close();print('finished',case['event_file'],'exit',code,flush=True)
    statuses=[read(BATCH/'cases'/Path(c['event_file']).stem/'status.json') for c in plan['cases']]
    save(BATCH/'status.json',{'completed_at':stamp(),'cases':statuses})

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--worker',type=int)
    args=parser.parse_args()
    asyncio.run(launch() if args.worker is None else run_case(args.worker))
