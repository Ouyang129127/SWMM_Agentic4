"""Compare saved-resolution facts, real usage and diagnosis event coverage."""
import json
from pathlib import Path
from datetime import datetime
import sys

BATCH=Path(__file__).resolve().parent
PROJECT=BATCH.parents[1]
BASE=PROJECT/'outputs/example1_online_pilot_20261116'
sys.path.insert(0,str(PROJECT))

def read(path):return json.loads(path.read_text(encoding='utf-8-sig'))
def pct(old,new):return 100*(1-new/old)
def first_usage(task):
    calls=sorted([read(p) for p in task.glob('diagnosis_attempt_*.json')],key=lambda a:a['started_at'])
    first=next(a for a in calls if a.get('usage'))
    u=first['usage'];return {'input':u['prompt_tokens'],'output':u['completion_tokens'],'total':u['prompt_tokens']+u['completion_tokens']}
def overlap(a,b):return a['node_id']==b['node_id'] and a['start']<b['end'] and b['start']<a['end']

def main():
    old,new=read(BASE/'summary.json'),read(BATCH/'summary.json')
    checks=read(BATCH/'sampling_verification.json');cases=[]
    for a,b in zip(old['cases'],new['cases']):
        assert a['facts']['rainfall_total_mm']==b['facts']['rainfall_total_mm']
        facts5,facts60=a['facts'],b['facts']
        first5,first60=first_usage(Path(a['task_root'])),first_usage(Path(b['task_root']))
        u5,u60=a['usage'],b['usage'];total5=u5['prompt_tokens']+u5['completion_tokens'];total60=u60['prompt_tokens']+u60['completion_tokens']
        e5={e['event_id']:e for e in a['event_assessments']};e60={e['event_id']:e for e in b['event_assessments']}
        comparisons=[]
        for coarse in facts60['events']:
            fine=[f for f in facts5['events'] if overlap(f,coarse)]
            statuses=[]
            for f in fine:
                s5={m['mechanism_id']:m['status'] for m in e5[f['event_id']]['mechanism_assessments']}
                s60={m['mechanism_id']:m['status'] for m in e60.get(coarse['event_id'],{}).get('mechanism_assessments',[])}
                statuses.append({'fine_event':f['event_id'],'fine_statuses':s5,'coarse_statuses':s60,
                                 'status_changes':{k:{'5s':v,'60s':s60.get(k)} for k,v in s5.items() if k in s60 and v!=s60[k]}})
            comparisons.append({'coarse_event':coarse,'fine_events':fine,'mapping':'one_to_one' if len(fine)==1 else 'merged',
                                'mechanisms':statuses})
        report5=a['report_usage']['prompt_tokens']+a['report_usage']['completion_tokens']
        report60=b['report_usage']['prompt_tokens']+b['report_usage']['completion_tokens']
        result={'rain_mm':facts5['rainfall_total_mm'],'first_5s':first5,'first_60s':first60,
                'first_input_reduction_pct':pct(first5['input'],first60['input']),
                'first_total_reduction_pct':pct(first5['total'],first60['total']),
                'diagnosis_usage_5s':u5,'diagnosis_usage_60s':u60,
                'diagnosis_total_5s':total5,'diagnosis_total_60s':total60,
                'diagnosis_total_reduction_pct':pct(total5,total60),
                'diagnosis_input_reduction_pct':pct(u5['prompt_tokens'],u60['prompt_tokens']),
                'html_tokens_5s':report5,'html_tokens_60s':report60,
                'with_html_total_5s':total5+report5,'with_html_total_60s':total60+report60,
                'facts_5s':facts5,'facts_60s':facts60,
                'volume_relative_change_pct':100*(facts60['overflow_volume_m3']/facts5['overflow_volume_m3']-1),
                'event_comparisons':comparisons,'new_status':b['status']['status'],
                'new_report':b.get('standalone_report',b.get('html_report')),
                'old_task':a['task_root'],'new_task':b['task_root']}
        cases.append(result)
    sums={key:sum(c[key] for c in cases) for key in ['diagnosis_total_5s','diagnosis_total_60s','with_html_total_5s','with_html_total_60s']}
    sums['first_input_5s']=sum(c['first_5s']['input'] for c in cases);sums['first_input_60s']=sum(c['first_60s']['input'] for c in cases)
    sums['first_total_5s']=sum(c['first_5s']['total'] for c in cases);sums['first_total_60s']=sum(c['first_60s']['total'] for c in cases)
    sums['first_input_reduction_pct']=pct(sums['first_input_5s'],sums['first_input_60s'])
    sums['first_total_reduction_pct']=pct(sums['first_total_5s'],sums['first_total_60s'])
    sums['diagnosis_total_reduction_pct']=pct(sums['diagnosis_total_5s'],sums['diagnosis_total_60s'])
    sums['with_html_total_reduction_pct']=pct(sums['with_html_total_5s'],sums['with_html_total_60s'])
    result={'scope':'Actual provider usage, failures included, diagnosis and HTML separately. Event comparisons use node/time overlap, not event ID alone.',
            'sampling_verification':checks,'cases':cases,'totals':sums}
    (BATCH/'sampling_comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# Example1：5秒与1分钟保存的三场在线对照','',
           '5秒组使用刚完成的Agentic4 Example1运行；1分钟组使用Example1Tokens。三场降雨文件一致，生成的SWMM输入只有REPORT_STEP不同，ROUTING_STEP与其他求解设置保持一致。两组共享相同诊断协议及两类输入精简。','',
           '## 保存与采样核验','',
           '每场保存时刻5041→421。三场内部求解步数分别5040、5042、5070，两组逐场完全一致。1分钟结果中全部节点水深/溢流、管段流量/水深与5秒结果在相同时间戳逐值相等。SWMM内部Node Flooding Summary报告也保持一致；以下事件统计差异来自保存后识别、积分及地表耦合输入。','',
           '## 实际诊断tokens','',
           '| 降雨 | 首轮输入 5s→60s | 首轮输入下降 | 完整诊断总tokens 5s→60s | 完整下降 |',
           '|---|---:|---:|---:|---:|']
    for c in cases:lines.append(f"| {c['rain_mm']:.0f} mm | {c['first_5s']['input']:,} → {c['first_60s']['input']:,} | {c['first_input_reduction_pct']:.1f}% | {c['diagnosis_total_5s']:,} → {c['diagnosis_total_60s']:,} | {c['diagnosis_total_reduction_pct']:.1f}% |")
    lines+=['',f"三场首轮输入合计{sums['first_input_5s']:,}→{sums['first_input_60s']:,}，下降{sums['first_input_reduction_pct']:.1f}%；首轮输入+输出下降{sums['first_total_reduction_pct']:.1f}%。完整诊断合计{sums['diagnosis_total_5s']:,}→{sums['diagnosis_total_60s']:,}，下降{sums['diagnosis_total_reduction_pct']:.1f}%。",'',
            '完整诊断计入真实失败重试：5秒组14 mm有一次JSON错误重试；1分钟组40 mm有一次遗漏机制引用的校验失败重试，并做了两轮补证（5秒组为一轮）。补证由模型自主提出，范围与返回长度并非完全相同，输出长度和重试也随调用变化，因此总降幅包含这些执行差异，不能全部解释为采样点减少。','',
            '## HTML写作额外用量','', '| 降雨 | HTML tokens 5s→60s | 诊断+HTML全部tokens 5s→60s |','|---|---:|---:|']
    for c in cases:lines.append(f"| {c['rain_mm']:.0f} mm | {c['html_tokens_5s']:,} → {c['html_tokens_60s']:,} | {c['with_html_total_5s']:,} → {c['with_html_total_60s']:,} |")
    lines+=['',f"两组均生成图文报告。诊断加HTML全部实际消耗合计{sums['with_html_total_5s']:,}→{sums['with_html_total_60s']:,}，下降{sums['with_html_total_reduction_pct']:.1f}%。HTML重试次数的差异亦计入。",'',
            '## 保存样本识别的事件','', '| 降雨 | 事件数 5s→60s | 积分体积m³ 5s→60s | 变化 |','|---|---:|---:|---:|']
    for c in cases:lines.append(f"| {c['rain_mm']:.0f} mm | {c['facts_5s']['overflow_event_count']} → {c['facts_60s']['overflow_event_count']} | {c['facts_5s']['overflow_volume_m3']:.3f} → {c['facts_60s']['overflow_volume_m3']:.3f} | {c['volume_relative_change_pct']:+.2f}% |")
    lines+=['','14 mm的保存峰值57.853→36.698 L/s，低估约36.57%；起溢识别由01:40:25推迟至01:41:00。20 mm峰值151.048→148.814 L/s；起溢识别由01:36:40到01:37:00。','',
            '40 mm节点15在5秒保存中有01:33:45—01:34:05的20秒短事件，以及01:34:30—01:51:20的较长事件，中间25秒无溢流间歇。1分钟保存没有记录这段间歇，两者合并为01:34—01:52的一次事件。事件编号15:E001因此不再表示同一物理片段，不能按同名ID直接逐项对比。','',
            '## 机制判断','']
    for c in cases:
        lines.append(f"### {c['rain_mm']:.0f} mm")
        for event in c['event_comparisons']:
            oldids=', '.join(e['event_id'] for e in event['fine_events'])
            lines.append(f"- 5秒事件 {oldids} → 分钟事件 {event['coarse_event']['event_id']}，映射：{event['mapping']}。")
            for match in event['mechanisms']:
                changes=match['status_changes']
                if changes:lines.append('  - 状态变化：'+'；'.join(k+': '+v['5s']+' → '+v['60s'] for k,v in changes.items()))
                else:lines.append('  - 与'+match['fine_event']+'的五类机制状态一致。')
        if c['new_report']:lines.append(f"- [1分钟组图文报告]({Path(c['new_report']).as_posix()})")
        lines.append('')
    lines+=['总体主解释仍是来水集中与局部排出约束。14 mm五类机制状态一致；20 mm的M3由候选变为不适用；40 mm节点10的M3由不适用变为候选。节点15两段合并后，M2为有支持，原短事件的独立候选判断已不再保留。M3状态的变化同时伴随补证范围及模型判断变化，此次记录不能单独区分这些因素各自的贡献。','',
            '## 解释与使用','',
            '这是有信息损失的保存策略实验，不是无损传输压缩。结果表明1分钟保存能够降低上下文量，但会漏掉短间歇和尖峰，影响事件边界、峰值、体积及可解释的事件数量。已只保存1分钟的原生结果，不能通过补证恢复不存在的5秒样本。','',
            '若需要保留短事件诊断能力，更合适的后续路线是保留高分辨率原始结果，再按调查目的提供粗背景和细事件窗口；这与删除原始高分辨率结果不同。','',
            f"![尖峰与短间歇采样对照]({(BATCH/'sampling_effects.png').as_posix()})",'',
            '原Example1配置保持不变。新模型配置备份、差异及只改变REPORT_STEP的核验见model_change_manifest.json与sampling_verification.json；逐调用源路径和判断见sampling_comparison.json。']
    (BATCH/'COMPARISON.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps({'totals':sums,'cases':[{'rain_mm':c['rain_mm'],'first_input_reduction_pct':c['first_input_reduction_pct'],
             'diagnosis_total_reduction_pct':c['diagnosis_total_reduction_pct'],'status':c['new_status']} for c in cases]},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
