"""Read-only paired comparison of recorded Agentic3/4 executions."""
import hashlib
import json
from pathlib import Path
import sys

OUT=Path(__file__).resolve().parent
BATCH=OUT.parent
PROJECT=BATCH.parents[1]
sys.path.insert(0,str(PROJECT))
from workflow_agents.evidence.event_packages import parse_run_input

def read(path):return json.loads(path.read_text(encoding='utf-8-sig'))
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def total(records):
    used=[r for r in records if isinstance(r.get('usage'),dict)]
    i=sum(r['usage'].get('prompt_tokens') or 0 for r in used)
    o=sum(r['usage'].get('completion_tokens') or 0 for r in used)
    return {'input':i,'output':o,'total':i+o,'attempts':len(records),'usage_records':len(used),
            'completed':sum(r['status']=='completed' for r in records),
            'failed':sum(r['status']=='failed' for r in records),
            'missing_usage':sum(not isinstance(r.get('usage'),dict) for r in records)}
def reduction(a,b):return 100*(1-b/a)
def inspect(task):
    state=read(task/'task.json')
    attempts=sorted([read(p) for p in task.glob('diagnosis_attempt_*.json')],key=lambda a:a['started_at'])
    first=next(a for a in attempts if isinstance(a.get('usage'),dict))
    revisions=[read(p) for p in sorted(task.glob('diagnosis_r*.json'))]
    return {'task_root':str(task),'state':state['state'],'revision':state['revision'],
            'contract':revisions[-1].get('diagnosis_contract_version'),
            'first_usage':total([first]),'all_usage':total(attempts),'attempts':attempts,
            'revisions':revisions,'manifest':state['event_manifest'],
            'final':revisions[-1],'complete':state['state']=='preliminary_delivered' and not state.get('pending_evidence_requests')}

def main():
    current=read(BATCH/'summary.json');cases=[];sources={}
    for c in current['cases']:
        oldroot=PROJECT.parent/'SWMM-Agentic3/models/Example1/runs'/('exp15_1103_213612_'+Path(c['case']['event_file']).stem)
        oldtask=next(oldroot.glob('diagnosis_tasks/*/task.json')).parent
        newroot,newtask=Path(c['run_root']),Path(c['task_root'])
        old,new=inspect(oldtask),inspect(newtask)
        a,b=parse_run_input(oldroot/'swmm/model_with_event.inp'),parse_run_input(newroot/'swmm/model_with_event.inp')
        options=[{'key':key,'old':a['options'].get(key),'new':b['options'].get(key)}
                 for key in sorted(set(a['options'])|set(b['options'])) if a['options'].get(key)!=b['options'].get(key)]
        ea={e['event_id']:e['facts'] for e in old['manifest']};eb={e['event_id']:e['facts'] for e in new['manifest']}
        fact_changes=[]
        for eid in ea:
            for k,v in ea[eid].items():
                if k not in {'run_id','model_name'} and v!=eb[eid].get(k):
                    fact_changes.append({'event_id':eid,'field':k,'old':v,'new':eb[eid].get(k)})
        oa={e['event_id']:{m['mechanism_id']:m for m in e['mechanism_assessments']} for e in old['final']['event_assessments']}
        nb={e['event_id']:{m['mechanism_id']:m for m in e['mechanism_assessments']} for e in new['final']['event_assessments']}
        changes=[]
        for eid in oa:
            for mid in ['M1','M2','M3','M5','M6']:
                if oa[eid][mid]['status']!=nb[eid][mid]['status']:
                    changes.append({'event_id':eid,'mechanism':mid,'old':oa[eid][mid],'new':nb[eid][mid]})
        paired=old['complete'] and new['complete']
        report_total=c['report_usage']['prompt_tokens']+c['report_usage']['completion_tokens']
        oldvolume=sum(e['estimated_volume_m3'] for e in ea.values());newvolume=sum(e['estimated_volume_m3'] for e in eb.values())
        result={'rain_mm':c['facts']['rainfall_total_mm'],'old_run':str(oldroot),'new_run':str(newroot),'old':old,'new':new,
                'first_input_reduction_pct':reduction(old['first_usage']['input'],new['first_usage']['input']),
                'first_total_reduction_pct':reduction(old['first_usage']['total'],new['first_usage']['total']),
                'full_diagnosis_comparable':paired,
                'full_input_reduction_pct':reduction(old['all_usage']['input'],new['all_usage']['input']) if paired else None,
                'full_total_reduction_pct':reduction(old['all_usage']['total'],new['all_usage']['total']) if paired else None,
                'new_html_report_usage':c['report_usage'],'new_html_report_total':report_total,
                'new_diagnosis_plus_html_total':new['all_usage']['total']+report_total,
                'rainfall_sha_equal':sha(oldroot/'rainfall_event.txt')==sha(newroot/'rainfall_event.txt'),
                'nodes_equal':a['nodes']==b['nodes'],'links_equal':a['links']==b['links'],
                'solver_option_differences':options,'event_fact_changes':fact_changes,
                'old_event_volume_m3':oldvolume,'new_event_volume_m3':newvolume,
                'event_volume_change_pct':100*(newvolume/oldvolume-1),
                'common_mechanism_status_changes':changes,
                'old_final_revision':old['revision'],'new_final_revision':new['revision']}
        cases.append(result)
        for directory in [oldroot,newroot]:
            for p in [directory/'summary.json',directory/'rainfall_event.txt',directory/'swmm/model_with_event.inp']:
                sources[str(p)]=sha(p)
        for directory in [oldtask,newtask]:
            for p in [directory/'task.json',*directory.glob('diagnosis_attempt_*.json'),*directory.glob('diagnosis_r*.json')]:sources[str(p)]=sha(p)
    def aggregate(items,field):
        ai=sum(c['old'][field]['input'] for c in items);bi=sum(c['new'][field]['input'] for c in items)
        at=sum(c['old'][field]['total'] for c in items);bt=sum(c['new'][field]['total'] for c in items)
        return {'old_input':ai,'new_input':bi,'input_reduction_pct':reduction(ai,bi),'old_total':at,'new_total':bt,'total_reduction_pct':reduction(at,bt)}
    completed=[c for c in cases if c['full_diagnosis_comparable']]
    paired=aggregate(completed,'all_usage');paired['new_with_html_total']=paired['new_total']+sum(c['new_html_report_total'] for c in completed)
    paired['total_reduction_including_new_html_pct']=reduction(paired['old_total'],paired['new_with_html_total'])
    result={'scope':'Actual DiagnosisAgent usage includes paid failed retries. ReportAgent HTML usage is listed separately; historic batch deferred HTML. Missing usage is unknown, not zero.',
            'all_first_calls':aggregate(cases,'first_usage'),'completed_14_20':paired,
            'cases':cases,'source_hashes':sources,
            'causal_comparison_limits':['Solver options differ between historical baseline and current dynamic_wave.',
               'M4 removed and M6 rules/evidence upgraded; supplementation scopes selected by the model also differ.',
               '40mm Agentic3 did not complete the supplemented diagnosis.',
               'The model name is an alias and calls were on different dates; this is recorded execution comparison, not an isolated format A/B.']}
    assert all(sha(Path(p))==h for p,h in sources.items())
    (OUT/'comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# Agentic3 与 Agentic4：三场降雨的实际用量和结果对照','',
           '只读对照2026-11-03的Agentic3 exp15运行与2026-11-16的Agentic4在线试跑，没有重跑模拟、没有新增模型调用。','',
           '## 首轮诊断：实际tokens','',
           '| 雨量 | 旧输入 | 新输入 | 输入下降 | 旧输入+输出 | 新输入+输出 | 合计下降 |',
           '|---|---:|---:|---:|---:|---:|---:|']
    for c in cases:
        a,b=c['old']['first_usage'],c['new']['first_usage']
        lines.append(f"| {c['rain_mm']:.0f} mm | {a['input']:,} | {b['input']:,} | {c['first_input_reduction_pct']:.1f}% | {a['total']:,} | {b['total']:,} | {c['first_total_reduction_pct']:.1f}% |")
    first=result['all_first_calls']
    lines+=['',f"三场首轮合计：输入下降{first['input_reduction_pct']:.1f}%，输入加输出下降{first['total_reduction_pct']:.1f}%。",'',
            '## 完整诊断：包含失败重试，不含HTML写作','',
            '| 雨量 | 旧输入+输出 | 新输入+输出 | 实际变化 | 对齐情况 |',
            '|---|---:|---:|---|---|']
    for c in cases:
        a,b=c['old']['all_usage'],c['new']['all_usage']
        change=(f"下降{c['full_total_reduction_pct']:.1f}%" if c['full_total_reduction_pct']>=0 else f"增加{-c['full_total_reduction_pct']:.1f}%") if c['full_diagnosis_comparable'] else '不作完整流程同比'
        lines.append(f"| {c['rain_mm']:.0f} mm | {a['total']:,}{'' if c['full_diagnosis_comparable'] else '（仅已返回usage部分）'} | {b['total']:,} | {change} | R{c['old']['revision']} → R{c['new']['revision']} |")
    lines+=['',f"两边均完成补证的14/20 mm合计：{paired['old_total']:,} → {paired['new_total']:,}，诊断总tokens下降{paired['total_reduction_pct']:.1f}%，输入下降{paired['input_reduction_pct']:.1f}%。",'',
            '14 mm新版补证后一次JSON语法错误产生了115,086输入和20,081输出tokens，随后成功重试。仅成功调用小计195,960，较旧版下降21.8%，但实际已经消耗的失败重试必须计入，因此完整实际消耗增加32.1%。成功调用小计不是“无重试时本来会花多少”的反事实估计。','',
            '40 mm旧版补证请求被1048576上下文上限拒绝，错误提示消息需要4741684 tokens。该次未返回usage，既不计入已记录用量求和，也不能解释为已知零消耗。新版补证输入352,072 tokens并完成R2；补证范围也不同，不将这两个数相除当作纯格式压缩率。','',
            '## HTML报告额外用量','',
            '| 雨量 | 新增HTML写作输入+输出 | 新版诊断+HTML全部tokens |',
            '|---|---:|---:|']
    for c in cases:lines.append(f"| {c['rain_mm']:.0f} mm | {c['new_html_report_total']:,} | {c['new_diagnosis_plus_html_total']:,} |")
    lines+=['',f"旧批次没有执行同类HTML写作，以上工作量单列。若按用户实际完成的14/20 mm全部工作量计算，连新版新增HTML也计入，则{paired['old_total']:,} → {paired['new_with_html_total']:,}，总tokens下降{paired['total_reduction_including_new_html_pct']:.1f}%。该口径的输出工作量不同。",'',
            'SWMM和CA2D模拟本身不调用LLM，不消耗上述tokens；其计算时间、CPU和文件存储另计。','',
            '## 模拟事实与设置','',
            '三场降雨文件SHA256均一致，节点和管段输入定义一致，观察终点均为2024-01-01 07:00。旧版使用baseline，新版使用dynamic_wave；两者均为DYNWAVE，但求解选项并不完全相同：HEAD_TOLERANCE为0.0015→0.001，MAX_TRIALS为8→12，MIN_SURFAREA为1.167→0。其他格式/报告/地图配置差异见JSON。','',
            '14/20 mm事件事实（除run_id外）完全一致。40 mm仍为节点10、15的3次事件，总事件积分665.235→665.133 m³，减少约0.0153%；节点15第一次短事件持续25→20秒、峰值46.025→41.694 L/s。因此不能说40 mm的每项模拟结果完全相同，也不能把这些数值变化归因于LLM输入简化。','',
            '## 诊断结果','',
            '14/20 mm最终共同机制M1、M2、M3、M5、M6的状态全部相同：M1与M2有支持；14 mm的M3不适用，20 mm的M3保留候选；M5、M6不适用。M4在新版协议中移除，原来对应的冒溢阈值/位置事实仍在事件与结构分析中。','',
            '40 mm旧版只有R1，新版已到R2，以下比较阶段不同：','']
    for c in cases:
        for change in c['common_mechanism_status_changes']:
            lines.append(f"- {c['rain_mm']:.0f} mm，{change['event_id']}，{change['mechanism']}：{change['old']['status']} → {change['new']['status']}。")
    lines+=['','40 mm三事件的M6由需补证变为不适用，与新版前期水力背景及规则相关：同一雨峰上升段的近满和短间隔冒溢不自动构成独立前期余水。节点15短事件M1从候选升为支持；其M2仍为候选。节点15较长事件M3由不适用改为候选，新版补证识别出口管顶轻微淹没，保留局部尾水影响；没有把它升级为已确认主因。','',
            '以上同时涉及求解设置、诊断协议、前期背景证据和模型自主补证范围的变化；实际用量对照成立，但不是只改变传输格式的严格A/B，不能据此声称诊断准确率提高。','',
            '## 来源','']
    for c in cases:
        for label,key in [('Agentic3','old'),('Agentic4','new')]:
            path=Path(c[key]['task_root'])
            lines.append(f"- {c['rain_mm']:.0f} mm {label}：[诊断任务]({path.as_posix()})。")
    (OUT/'COMPARISON.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps({'all_first_calls':result['all_first_calls'],'completed_14_20':paired,
          'cases':[{'rain_mm':c['rain_mm'],'first_input_reduction_pct':c['first_input_reduction_pct'],
                    'full_total_reduction_pct':c['full_total_reduction_pct'],'html_extra':c['new_html_report_total'],
                    'fact_changes':c['event_fact_changes']} for c in cases]},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
