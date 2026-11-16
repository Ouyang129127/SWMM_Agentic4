"""Task-local enrichment from hash-bound saved inputs; archived packages stay intact."""
import copy
from pathlib import Path

from workflow_agents.common.hashing import sha256_file
from workflow_agents.evidence.rainfall_context import build_rainfall_context, event_rainfall_position
from workflow_agents.evidence.event_packages import parse_run_input
from workflow_agents.evidence.structure_audit import AUDIT_VERSION, build_structure_audit, event_interface_summary

VERSION = 'rainfall_history_structure_context_v2'


def prepare_context_rows(root, package, manifest):
    rows = copy.deepcopy(package['evidence_rows'])
    original_rain = next((r for r in rows if r.get('metric_name')=='rainfall_context'), None)
    if original_rain and original_rain['value'].get('timeline_status')=='available':
        rain, rain_id = original_rain['value'], original_rain['evidence_id']
        source, source_sha = original_rain.get('source_file'), None
    else:
        source = original_rain.get('source_file') if original_rain else 'rainfall_event.txt'
        path = Path(source) if source and Path(source).is_absolute() else root/(source or 'rainfall_event.txt')
        expected = next((digest for name,digest in package.get('source_hashes',{}).items()
                         if (Path(name) if Path(name).is_absolute() else root/name).resolve()==path.resolve()), None)
        if path.exists() and expected is None:
            raise ValueError('Rainfall timeline source is outside the bound evidence sources')
        source_sha = sha256_file(path) if path.is_file() else None
        if expected is not None and source_sha != expected:
            raise ValueError('Rainfall source changed before task-local context preparation')
        rain = build_rainfall_context(path, event_name=package.get('event_name',''),
            run_id=package['run_id'], model_name=package['model_name'], scenario_name=package.get('scenario_name',''))
        rain.pop('created_at', None)
        if path.is_file() and sha256_file(path)!=source_sha:
            raise ValueError('Rainfall source changed during context preparation')
        rain_id = 'CTX_RAINFALL_TIMELINE'
        rows.append({'evidence_id':rain_id,'source_model':package['model_name'],'run_id':package['run_id'],
            'event_id':'','object_type':'run','object_id':package['run_id'],'metric_name':'rainfall_timeline',
            'value':rain,'unit':'structured','source_file':str(source or path),
            'calculation_method':VERSION,'derivation':{'source_sha256':source_sha,'missing_is_not_zero':True}})
    facts = {e['event_id']:e['facts'] for e in manifest}
    contexts = {r['event_id']:r for r in rows if r.get('metric_name')=='event_context'}
    for event in manifest:
        timing = event_rainfall_position(rain, event['facts'])
        timing['rainfall_evidence_id'] = rain_id
        previous = [p for p in manifest if p['node_id']==event['node_id'] and p['facts']['end']<=event['facts']['start']
                    and p['event_id']!=event['event_id']]
        preceding = max(previous,key=lambda p:p['facts']['end']) if previous else None
        timing['previous_same_node_overflow'] = ({'event_id':preceding['event_id'],
            'end':preceding['facts']['end'],'interpretation':'Overflow ending does not establish complete drainage.'} if preceding else None)
        contexts[event['event_id']]['value']['rainfall_timing'] = timing
    processes = {r['event_id']:r['value'].get('series',[]) for r in rows if r.get('metric_name')=='local_process_series'}
    model, audits = None, {}
    for row in rows:
        if (row.get('metric_name')!='local_structure' or 'structure_audit' in row['value']
                or row.get('event_id') not in facts):
            continue
        if model is None:
            model = parse_run_input(root/'swmm/model_with_event.inp')
        target=row['object_id']; scope=row['value'].get('topology_scope')
        if target not in audits:
            if scope and model.get('available'):
                audits[target]=build_structure_audit(model,scope)
            else:
                audits[target]=build_structure_audit({'available':False,'nodes':{},'links':{},'options':{}}, {'target_node':target})
                audits[target]['unavailable_checks']=[{'check_id':'structure_scope','check':'input_geometry',
                    'reason':'Missing saved input or declared topology scope; not treated as absence of problems.'}]
        row['value']['structure_audit']={**audits[target],
            'event_interfaces':event_interface_summary(audits[target],processes.get(row['event_id'],[]),facts[row['event_id']])}
    return rows, {'version':VERSION,'rainfall_evidence_id':rain_id,'source_file':source,
                  'source_sha256':source_sha,'event_facts_preserved':True,'structure_audit_version':AUDIT_VERSION}
