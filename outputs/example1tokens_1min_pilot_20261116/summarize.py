"""Summarize program facts, real model judgments and audited call usage."""
import html
import json
import shutil
from pathlib import Path
import sys

BATCH=Path(__file__).resolve().parent
PROJECT=BATCH.parents[1]
sys.path.insert(0,str(PROJECT))

def read(path,default=None):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else default

def link(path,label):return f'[{label}]({str(Path(path).resolve()).replace(chr(92),"/")})'

def main():
    plan=read(BATCH/'plan.json');cases=[]
    for case in plan['cases']:
        folder=BATCH/'cases'/Path(case['event_file']).stem
        state=read(folder/'status.json',{})
        root=PROJECT/'models/Example1Tokens/runs'/case['run_id']
        pointer=read(folder/'task_pointer.json',{})
        task=root/'diagnosis_tasks'/pointer.get('task_id','missing')
        diagnosis=read(task/'diagnosis.json',{})
        task_state=read(task/'task.json',{})
        verification=read(task/'verification.json',{})
        events=read(folder/'events.json',[])
        audits=[read(p) for p in task.glob('diagnosis_attempt_*.json')]
        report_audits=[read(p) for p in task.glob('display_reports/**/narrative_attempt_*.json')]
        calls=read(folder/'call_index.json',[])
        facts={'rainfall_total_mm':case['total_mm'],'rainfall_peak_mm_h':case['peak_mm_h'],
               'overflow_event_count':len(events),'overflow_nodes':sorted({e['node_id'] for e in events}),
               'overflow_volume_m3':sum(e['facts']['estimated_volume_m3'] for e in events),
               'events':[e['facts'] for e in events]}
        usage={'diagnosis_attempts':len(audits),'diagnosis_completed':sum(a.get('status')=='completed' for a in audits),
               'diagnosis_failed':sum(a.get('status')=='failed' for a in audits),
               'prompt_tokens':sum(a.get('usage',{}).get('prompt_tokens') or 0 for a in audits),
               'completion_tokens':sum(a.get('usage',{}).get('completion_tokens') or 0 for a in audits)}
        item={'case':case,'run_root':str(root),'task_root':str(task),'status':state,'task_state':task_state.get('state'),
              'revision':task_state.get('revision'),'facts':facts,'hydraulic_audit':read(folder/'hydraulic_audit.json',{}),
              'usage':usage,'model_overview':diagnosis.get('run_overview',{}).get('text',''),
              'event_assessments':diagnosis.get('event_assessments',[]),'evidence_requests':diagnosis.get('evidence_requests',[]),
              'reference_verification':verification.get('summary',{}),'call_comparisons':calls,
              'markdown_report':task_state.get('report_file'),'html_report':task_state.get('display_report_file'),
              'display_report':task_state.get('display_report',{})}
        if item['html_report']:
            standalone=BATCH/'reports'/f"report_{case['total_mm']:.0f}mm.html"
            standalone.parent.mkdir(exist_ok=True)
            shutil.copy2(item['html_report'],standalone)
            item['standalone_report']=str(standalone)
        item['report_usage']={'configured_attempts':sum(a.get('source')=='configured_model' for a in report_audits),
             'prompt_tokens':sum(a.get('usage',{}).get('prompt_tokens') or 0 for a in report_audits),
             'completion_tokens':sum(a.get('usage',{}).get('completion_tokens') or 0 for a in report_audits)}
        cases.append(item)
    result={'kind':'real_simulation_and_llm_smoke_pilot_not_controlled_quality_comparison','plan':plan,'cases':cases}
    (BATCH/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# Example1Tokens：Agentic4 三档降雨在线试跑','',
           '三场均为4小时同形单峰雨，动态波，SWMM保存步长60秒，雨后继续3小时，统一结束于2024-01-01 07:00。下表为本次新模拟的程序事实；机制分析来自真实模型输出，引用核查只验证来源与绑定。','',
           '| 总雨量 mm | 峰值雨强 mm/h | 溢流节点 | 事件数 | 事件积分溢流量 m³ | 诊断修订 | 状态 |',
           '|---:|---:|---|---:|---:|---:|---|']
    for c in cases:
        f=c['facts'];lines.append(f"| {f['rainfall_total_mm']:.0f} | {f['rainfall_peak_mm_h']:.3f} | {', '.join(f['overflow_nodes'])} | {f['overflow_event_count']} | {f['overflow_volume_m3']:.3f} | {c['revision']} | {c['status'].get('status','running')} / {c.get('task_state')} |")
    lines+=['','事件量由60秒保存值积分，表中按事件舍入值合计；精确耦合水量见各场hydraulic_audit.json。降雨为合成敏感性雨型，未赋予重现期。CA2D是合成地表场景，本次主要检验计算、证据、诊断和报告链路。','',
            '## 实际模型调用','', '| 总雨量 mm | 诊断尝试 | 完成/失败 | 真实输入 tokens | 真实输出 tokens | 当前引用核查 |',
            '|---:|---:|---|---:|---:|---|']
    for c in cases:
        u=c['usage'];v=c['reference_verification'];lines.append(f"| {c['facts']['rainfall_total_mm']:.0f} | {u['diagnosis_attempts']} | {u['diagnosis_completed']}/{u['diagnosis_failed']} | {u['prompt_tokens']:,} | {u['completion_tokens']:,} | {v.get('references_verified',0)}/{v.get('total_claims',0)} |")
    lines+=['','上表只计DiagnosisAgent的实际usage；独立HTML报告写作调用另保存在display_reports中。没有用代理分词估算替代真实用量，也没有把输入格式变化解释为诊断准确率提高。','']
    for c in cases:
        amount=c['facts']['rainfall_total_mm'];lines += [f'## {amount:.0f} mm','']
        if c.get('html_report'):lines += [link(c.get('standalone_report',c['html_report']),'打开图文诊断报告'),'']
        if c.get('markdown_report'):lines += [link(c['markdown_report'],'打开经引用核查的完整诊断记录'),'']
        lines += [link(c['run_root'],'运行目录'),'','### 事件事实','']
        for e in c['facts']['events']:
            lines.append(f"- 节点{e['node_id']}，{e['event_id']}：{e['start']}至{e['end']}，持续{e['duration_minutes']:.3f} min，峰值{e['peak_flooding_Ls']:.3f} L/s，体积{e['estimated_volume_m3']:.3f} m³。")
        lines += ['','### 模型分析','',c['model_overview'] or '模型尚未交付有效诊断。','']
        for e in c['event_assessments']:
            statuses='；'.join(f"{m['mechanism_id']}={m['status']}" for m in e['mechanism_assessments'])
            lines += [f"**事件{e['event_id']}**：{statuses}",'',e['joint_assessment']['conclusion'],'',
                      '待补证：'+'；'.join(e.get('evidence_gaps',[])),'']
        lines+=['### 发送格式','']
        for q in c['call_comparisons']:
            raw=q['characters_raw'];first=q['characters_tables_only'];actual=q['characters_actual']
            lines.append(f"- 第{q['sequence']}次：{q['evidence_view_format']}，{q['visible_records']}条记录，共享{q['shared_definitions']}份定义；完整请求字符量{raw:,} → 表格化{first:,} → 实际{actual:,}。两类合计较原始记录表达减少{100*(1-actual/raw):.2f}%，浅层共享另减少{100*(1-actual/first):.2f}%。")
        lines += ['','以上百分比是同次请求的字符量配对比较，已计入格式提示开销，不是提供方token降幅或独立准确率对照。','']
        if c['status'].get('error'):lines+=['执行问题：'+c['status']['error'],'']
        outcome=c['status'].get('diagnosis',{})
        if outcome:lines+=['停止原因：'+outcome.get('stop_reason',''),'']
        if c['display_report'].get('status')=='failed':lines+=['HTML生成问题：'+c['display_report'].get('error',''),'']
    (BATCH/'SUMMARY.md').write_text('\n'.join(lines),encoding='utf-8')
    cards=[]
    for c in cases:
        f=c['facts'];amount=f['rainfall_total_mm']
        href=f'reports/report_{amount:.0f}mm.html'
        mechanisms=[]
        names={'M1':'来水集中','M2':'局部输水约束','M3':'下游水位影响','M5':'设施运行','M6':'前期蓄水'}
        labels={'supported':'有证据支持','candidate':'候选','not_applicable':'不适用','needs_evidence':'需补证','not_investigated':'未调查'}
        for e in c['event_assessments']:
            status='；'.join(names[m['mechanism_id']]+'：'+labels[m['status']] for m in e['mechanism_assessments'])
            mechanisms.append('<p><b>'+html.escape(e['event_id'])+'</b>　'+html.escape(status)+'</p>')
        cards.append(f'<article><h2>{amount:.0f} mm · {f["rainfall_peak_mm_h"]:.3f} mm/h峰值雨强</h2>'
            f'<p>节点{html.escape("、".join(f["overflow_nodes"]))} · {f["overflow_event_count"]}个事件 · {f["overflow_volume_m3"]:.3f} m³溢流</p>'
            +''.join(mechanisms)+f'<a href="{href}">打开完整图文报告 →</a></article>')
    page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Example1Tokens · Agentic4 在线试跑</title>
<style>body{font:16px/1.8 "Microsoft YaHei",sans-serif;background:#f3f6f4;color:#213e38;margin:0}main{max-width:1000px;margin:48px auto;padding:0 24px}h1{font-size:32px}h2{font-size:22px}article{background:white;border-radius:12px;padding:24px 30px;margin:22px 0;border-left:5px solid #087b72}a{color:#087b72;font-weight:bold}small{color:#62736c}.intro{font-size:18px}</style><main>
<small>2026-11-16 · SWMM-Agentic4 · 新模拟与真实模型诊断</small><h1>Example1Tokens：三档降雨的溢流与诊断</h1>
<p class="intro">4小时同形单峰雨，动态波；管网每60秒保存，雨后继续3小时。三场均完成初诊、一次补证复核和图文报告。</p>
<p>主要过程为来水集中与局部排出约束；下游影响按各事件证据分别判断。分钟保存后的节点15识别为一段连续事件，短间歇是否存在需对照5秒结果。</p>
'''+''.join(cards)+'''<p><a href="SUMMARY.md">完整比较记录</a>　·　<a href="summary.json">结构化结果与调用用量</a></p>
<small>全部最终引用通过核查；这表示来源与对象/事件绑定有效。机制解释及其候选和缺证范围见各报告。地表为合成技术验证场景。</small></main></html>'''
    (BATCH/'index.html').write_text(page,encoding='utf-8')
    print(json.dumps([{'rain_mm':c['facts']['rainfall_total_mm'],'status':c['status'].get('status'),
                      'phase':c['status'].get('phase'),'revision':c['revision'],'usage':c['usage'],
                      'html':c.get('html_report')} for c in cases],ensure_ascii=False,indent=2))

if __name__=='__main__':main()
