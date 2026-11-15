"""Recover one saved report with audited, source-bound category-token edits.

No API/LLM calls, simulation or diagnosis changes. Old attempts remain intact.
"""
import asyncio
import copy
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from workflow_agents.common.hashing import sha256_file
from workflow_agents.reporting.materials import prepare_materials
from workflow_agents.reporting.narrative import make_prompt,validate,assemble_fixed_sections
from workflow_agents.orchestration.investigation import advance_investigation

MODEL='RedChicoSur'
RUN='diagnostic_single_4h_20mm_5min__baseline__20261028_110806_17f8f882'
TASK='7b0e19a074f441729ec212ec3bd0825c'
RUN_ROOT=ROOT/'models'/MODEL/'runs'/RUN
DIRECTORY=RUN_ROOT/'diagnosis_tasks'/TASK
OLD=DIRECTORY/'display_reports/r1/20261028_111432_1e30a564'
OUTPUT=Path(__file__).resolve().parent

def save(path,data):path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

def prepare():
    state=json.loads((DIRECTORY/'task.json').read_text(encoding='utf-8'))
    immutable={str(p):sha256_file(p) for p in [DIRECTORY/'diagnosis.json',DIRECTORY/'verification.json',
        DIRECTORY/'evidence_snapshot.json',RUN_ROOT/'summary.json',RUN_ROOT/'evidence/evidence_package.json']}
    save(OUTPUT/'task_before_recovery.json',state)
    materials,data=prepare_materials(RUN_ROOT,DIRECTORY,state)
    prompt,facts=make_prompt(materials)
    previous_facts=json.loads((OLD/'fact_text.json').read_text(encoding='utf-8'))
    for key,value in previous_facts.items():
        assert facts[key]==value,('Source fact changed',key)
    record=json.loads((OLD/'narrative_attempt_3.json').read_text(encoding='utf-8'))
    original=record['raw_response'];original=json.loads(original) if isinstance(original,str) else original
    edits=[]
    def replace(value,path='response'):
        if isinstance(value,dict):return {k:replace(v,path+'.'+k) for k,v in value.items()}
        if isinstance(value,list):return [replace(v,path+f'[{i}]') for i,v in enumerate(value)]
        if not isinstance(value,str):return value
        before=value
        for literal,label in [('达到15厘米的积水','attention'),('达到15厘米的区域','attention'),
                              ('达到1厘米的积水','general'),('达到1厘米的区域','general')]:
            if literal in value:value=value.replace(literal,prompt['materials']['surface']['threshold_labels'][label])
        # The fixed surface opening already defines this attention threshold.
        # Preserve the supplied observation and its F0049 reference, spelling the
        # category by name instead of repeating a bare literal depth.
        value=value.replace('注入网格自早段起水深达到15厘米以上','注入网格自早段起达到重点关注阈值')
        if value!=before:edits.append({'field':path,'before':before,'after':value,'kind':'source_bound_depth_category_label'})
        return value
    repaired=replace(copy.deepcopy(original))
    assert edits,'Expected original category-literal failure was not found'
    validate(assemble_fixed_sections(repaired,prompt),materials,facts,data['known_ids'])
    save(OUTPUT/'recovery_audit.json',{'source_attempt':str(OLD/'narrative_attempt_3.json'),
        'source_attempt_sha256':sha256_file(OLD/'narrative_attempt_3.json'),'immutable_sources_before':immutable,
        'repairs':edits,'existing_fact_ids_unchanged':True,'model_api_calls':0,'simulation_rerun':False,
        'diagnosis_rewritten':False,'preflight_validation':'passed'})
    save(OUTPUT/'repaired_response.json',repaired)
    return repaired,immutable,facts

async def main():
    repaired,immutable,facts=prepare()
    async def replay(prompt):
        for key,value in facts.items():assert prompt['fact_text'][key]==value
        return copy.deepcopy(repaired)
    result=await advance_investigation(MODEL,RUN,'display_report',TASK,report_complete=replay)
    for name,digest in immutable.items():assert sha256_file(Path(name))==digest,('Immutable source modified',name)
    display=result['display_report']
    assert display['status']=='completed',display
    audit=json.loads((OUTPUT/'recovery_audit.json').read_text(encoding='utf-8'))
    audit.update(result='completed',immutable_sources_unchanged=True,display_report=display)
    save(OUTPUT/'recovery_audit.json',audit)
    print(json.dumps({'status':display['status'],'html_file':display['html_file'],
        'quality_checks':display['quality_checks'],'model_api_calls':0},ensure_ascii=False,indent=2),flush=True)

if __name__=='__main__':asyncio.run(main())
