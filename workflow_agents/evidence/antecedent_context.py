"""Shared pre-event hydraulic samples from bound native output, without simulation."""
import copy
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from workflow_agents.common.hashing import sha256_file
from workflow_agents.evidence.event_packages import parse_run_input
from workflow_agents.evidence.evidence_precision import round_evidence_row, uses_decimal3
from workflow_agents.evidence.saved_result_evidence import _native, digest


VERSION = 'antecedent_hydraulics_v1'
METRIC = 'antecedent_hydraulic_context'
BACKGROUND_INTERVAL_SECONDS = 300


def context_times(times, anchors, interval=BACKGROUND_INTERVAL_SECONDS):
    """Use actual samples at coarse cadence plus both sides of each anchor."""
    if not times:
        return []
    selected = {times[0], times[-1]}
    last = datetime.fromisoformat(times[0])
    for time in times[1:]:
        current = datetime.fromisoformat(time)
        if (current-last).total_seconds() >= interval:
            selected.add(time)
            last = current
    for anchor in anchors:
        i = bisect_left(times, anchor)
        if i < len(times):
            selected.add(times[i])
        if i > 0:
            selected.add(times[i-1])
    return sorted(selected)


def _bound_source(root, package, path):
    declared = next((h for name,h in package.get('source_hashes',{}).items()
                     if (Path(name) if Path(name).is_absolute() else root/name).resolve()==path.resolve()), None)
    if not path.is_file() or declared is None:
        return None
    if sha256_file(path) != declared:
        raise ValueError('Antecedent hydraulic source changed: '+str(path))
    return declared


def add_antecedent_context(root, package, rows, manifest, rain, scope=None):
    """One history per selected overflow node, shared across its events.

    A shared row is not bound to any one event. Each event_context supplies its
    cutoff; event-specific claims must also cite their own event evidence.
    """
    scope = scope or {}
    events = [e for e in manifest if (not scope.get('node_id') or e['node_id']==scope['node_id'])
              and (not scope.get('event_id') or e['event_id']==scope['event_id'])]
    by_node = defaultdict(list)
    contexts = {r['event_id']:r for r in rows if r.get('metric_name')=='event_context'}
    for event in events:
        by_node[event['node_id']].append(event)
        contexts[event['event_id']]['value']['antecedent_hydraulics'] = {
            'status':'unavailable', 'method':VERSION,
            'reason':'Missing hash-bound native output or input; previous water is unknown, not zero.',
            'next_investigation':'Request local_process_series or storage_volume_process with explicit pre-event time_start/time_end.'}
    summary = {'version':VERSION, 'background_interval_seconds':BACKGROUND_INTERVAL_SECONDS,
               'selected_events':len(events), 'history_rows':0}
    if not events:
        return summary
    input_path, output_path = root/'swmm/model_with_event.inp', root/'swmm/model.out'
    hashes = {str(path.relative_to(root)): _bound_source(root,package,path) for path in (input_path,output_path)}
    if not all(hashes.values()):
        return summary
    model = parse_run_input(input_path)
    structures = {r['event_id']:r['value'] for r in rows if r.get('metric_name')=='local_structure'}
    scopes = {}
    for node,group in by_node.items():
        nodes,links = {node},set()
        for event in group:
            structure = structures.get(event['event_id'],{})
            nodes.update(structure.get('nodes',{}))
            links.update(structure.get('links',{}))
        nodes &= set(model['nodes'])
        links &= set(model['links'])
        for link in links:
            nodes.update(model['links'][link][key] for key in ('from_node','to_node'))
        scopes[node] = {'nodes':sorted(nodes), 'links':sorted(links)}
    wanted_nodes = sorted({n for s in scopes.values() for n in s['nodes']})
    wanted_links = sorted({n for s in scopes.values() for n in s['links']})
    try:
        native = _native(output_path,wanted_nodes,wanted_links)
    except ValueError as error:
        if not str(error).startswith('Native output is not SI/LPS'):
            raise
        for event in events:
            contexts[event['event_id']]['value']['antecedent_hydraulics']['reason'] = str(error)
        return summary
    for relative,expected in hashes.items():
        if sha256_file(root/relative)!=expected:
            raise ValueError('Antecedent hydraulic source changed during extraction')
    rain_anchors = {p[key] for p in rain.get('rainfall_peaks',[]) for key in ('peak_time','plateau_end')}
    rain_anchors.update(p[key] for p in rain.get('wet_periods',[]) for key in ('start','end'))
    for node,group in by_node.items():
        spatial = scopes[node]
        if not spatial['nodes']:
            continue
        last_start = max(e['facts']['start'] for e in group)
        # Select on a common clock, but expose missing object samples as missing.
        all_times = sorted({t for kind in ('nodes','links') for obj in spatial[kind]
                            for t in native[kind].get(obj,{}) if t<=last_start})
        if not all_times:
            continue
        anchors = set(rain_anchors)
        anchors.update(e['facts'][key] for e in group for key in ('start','peak_time','end'))
        selected = context_times(all_times,anchors)
        series = [{'time':t, **{kind:{obj:copy.deepcopy(native[kind].get(obj,{}).get(t,{}))
                                    for obj in spatial[kind]} for kind in ('nodes','links')}} for t in selected]
        missing = sum(not values or any(v is None for v in values.values())
                      for sample in series for kind in ('nodes','links') for values in sample[kind].values())
        intervals = sorted({(datetime.fromisoformat(b)-datetime.fromisoformat(a)).total_seconds()
                            for a,b in zip(all_times,all_times[1:])})
        row = {'source_model':package['model_name'], 'run_id':package['run_id'],
               'object_type':'node','object_id':node,'event_id':'','metric_name':METRIC,'unit':'structured',
               'time_start':selected[0],'time_end':selected[-1],'source_file':'swmm/model.out',
               'calculation_method':VERSION,'derivation':{'source_hashes':hashes},
               'value':{'status':'partial' if missing else 'complete', 'series':series,
                        'resolved_scope':spatial, 'related_event_ids':[e['event_id'] for e in group],
                        'actual_time_start':selected[0],'actual_time_end':selected[-1],
                        'sampling':{'background_interval_seconds':BACKGROUND_INTERVAL_SECONDS,
                                    'native_intervals_seconds':intervals,'native_samples_in_window':len(all_times),
                                    'returned_samples':len(selected),'anchor_policy':'actual samples on both sides of rain/event boundaries, no interpolation'},
                        'native_metadata':native['metadata'],
                        'interpretation_limits':[
                            '前期水力背景按稀疏原生样本提供，不能据此断言两样本之间无短时变化；关键区段可补取原保存分辨率。',
                            '同一节点共享历史；分析某事件时只取其起溢及之前的样本，不将后续事件状态倒置为前期证据。',
                            '最早保存样本不是模拟初始时刻；结合输入初始设置和可见热启动记录判断，更早历史未提供时保持未知。',
                            '节点和管段的体积是各自实际储量，不相加充当独立调蓄容量；有水状态不自动确立M6。',
                            '空间范围沿用所选事件的已声明局部结构，不代表整个管网已调查。']}}
        if uses_decimal3(package):
            row = round_evidence_row(row)
        row['evidence_id'] = 'HIST_'+digest(row)[:24]
        rows.append(row)
        summary['history_rows'] += 1
        for event in group:
            cutoff = event['facts']['start']
            i = bisect_right(selected,cutoff)
            last = selected[i-1] if i else None
            contexts[event['event_id']]['value']['antecedent_hydraulics'] = {
                'status':'available' if last else 'unavailable','method':VERSION,'evidence_id':row['evidence_id'],
                'history_start':selected[0], 'event_cutoff':cutoff,'last_sample_at_or_before_start':last,
                'background_interval_seconds':BACKGROUND_INTERVAL_SECONDS,
                'history_is_shared':True,'instruction':'Use samples up to event_cutoff for this event; event-local native-resolution series describes its overflow response.'}
    summary['source_hashes'] = hashes
    return summary
