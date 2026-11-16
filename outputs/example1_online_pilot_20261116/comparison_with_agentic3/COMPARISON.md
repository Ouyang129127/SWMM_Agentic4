# Agentic3 与 Agentic4：三场降雨的实际用量和结果对照

只读对照2026-11-03的Agentic3 exp15运行与2026-11-16的Agentic4在线试跑，没有重跑模拟、没有新增模型调用。

## 首轮诊断：实际tokens

| 雨量 | 旧输入 | 新输入 | 输入下降 | 旧输入+输出 | 新输入+输出 | 合计下降 |
|---|---:|---:|---:|---:|---:|---:|
| 14 mm | 41,962 | 38,390 | 8.5% | 67,471 | 61,793 | 8.4% |
| 20 mm | 81,372 | 55,189 | 32.2% | 101,953 | 75,485 | 26.0% |
| 40 mm | 255,346 | 142,531 | 44.2% | 278,388 | 171,610 | 38.4% |

三场首轮合计：输入下降37.6%，输入加输出下降31.0%。

## 完整诊断：包含失败重试，不含HTML写作

| 雨量 | 旧输入+输出 | 新输入+输出 | 实际变化 | 对齐情况 |
|---|---:|---:|---|---|
| 14 mm | 250,728 | 331,127 | 增加32.1% | R2 → R2 |
| 20 mm | 510,686 | 214,311 | 下降58.0% | R2 → R2 |
| 40 mm | 278,388（仅已返回usage部分） | 543,430 | 不作完整流程同比 | R1 → R2 |

两边均完成补证的14/20 mm合计：761,414 → 545,438，诊断总tokens下降28.4%，输入下降33.4%。

14 mm新版补证后一次JSON语法错误产生了115,086输入和20,081输出tokens，随后成功重试。仅成功调用小计195,960，较旧版下降21.8%，但实际已经消耗的失败重试必须计入，因此完整实际消耗增加32.1%。成功调用小计不是“无重试时本来会花多少”的反事实估计。

40 mm旧版补证请求被1048576上下文上限拒绝，错误提示消息需要4741684 tokens。该次未返回usage，既不计入已记录用量求和，也不能解释为已知零消耗。新版补证输入352,072 tokens并完成R2；补证范围也不同，不将这两个数相除当作纯格式压缩率。

## HTML报告额外用量

| 雨量 | 新增HTML写作输入+输出 | 新版诊断+HTML全部tokens |
|---|---:|---:|
| 14 mm | 49,896 | 381,023 |
| 20 mm | 100,511 | 314,822 |
| 40 mm | 557,173 | 1,100,603 |

旧批次没有执行同类HTML写作，以上工作量单列。若按用户实际完成的14/20 mm全部工作量计算，连新版新增HTML也计入，则761,414 → 695,845，总tokens下降8.6%。该口径的输出工作量不同。

SWMM和CA2D模拟本身不调用LLM，不消耗上述tokens；其计算时间、CPU和文件存储另计。

## 模拟事实与设置

三场降雨文件SHA256均一致，节点和管段输入定义一致，观察终点均为2024-01-01 07:00。旧版使用baseline，新版使用dynamic_wave；两者均为DYNWAVE，但求解选项并不完全相同：HEAD_TOLERANCE为0.0015→0.001，MAX_TRIALS为8→12，MIN_SURFAREA为1.167→0。其他格式/报告/地图配置差异见JSON。

14/20 mm事件事实（除run_id外）完全一致。40 mm仍为节点10、15的3次事件，总事件积分665.235→665.133 m³，减少约0.0153%；节点15第一次短事件持续25→20秒、峰值46.025→41.694 L/s。因此不能说40 mm的每项模拟结果完全相同，也不能把这些数值变化归因于LLM输入简化。

## 诊断结果

14/20 mm最终共同机制M1、M2、M3、M5、M6的状态全部相同：M1与M2有支持；14 mm的M3不适用，20 mm的M3保留候选；M5、M6不适用。M4在新版协议中移除，原来对应的冒溢阈值/位置事实仍在事件与结构分析中。

40 mm旧版只有R1，新版已到R2，以下比较阶段不同：

- 40 mm，10:E001，M6：needs_evidence → not_applicable。
- 40 mm，15:E001，M1：candidate → supported。
- 40 mm，15:E001，M6：needs_evidence → not_applicable。
- 40 mm，15:E002，M3：not_applicable → candidate。
- 40 mm，15:E002，M6：needs_evidence → not_applicable。

40 mm三事件的M6由需补证变为不适用，与新版前期水力背景及规则相关：同一雨峰上升段的近满和短间隔冒溢不自动构成独立前期余水。节点15短事件M1从候选升为支持；其M2仍为候选。节点15较长事件M3由不适用改为候选，新版补证识别出口管顶轻微淹没，保留局部尾水影响；没有把它升级为已确认主因。

以上同时涉及求解设置、诊断协议、前期背景证据和模型自主补证范围的变化；实际用量对照成立，但不是只改变传输格式的严格A/B，不能据此声称诊断准确率提高。

## 来源

- 14 mm Agentic3：[诊断任务](E:/SWMM_Agentic/SWMM-Agentic3/models/Example1/runs/exp15_1103_213612_A02_14mm/diagnosis_tasks/27646de31a1144619300cca572ea7f58)。
- 14 mm Agentic4：[诊断任务](E:/SWMM_Agentic/SWMM-Agentic4/models/Example1/runs/pilot4_20261116_adff44_A02_14mm/diagnosis_tasks/3d0aa008f9b24075a1ed14e7af323b5d)。
- 20 mm Agentic3：[诊断任务](E:/SWMM_Agentic/SWMM-Agentic3/models/Example1/runs/exp15_1103_213612_A03_20mm/diagnosis_tasks/a676d55e22624c9a9665c4367889c110)。
- 20 mm Agentic4：[诊断任务](E:/SWMM_Agentic/SWMM-Agentic4/models/Example1/runs/pilot4_20261116_adff44_A03_20mm/diagnosis_tasks/f97df86c54374755b65701ccdb4469fa)。
- 40 mm Agentic3：[诊断任务](E:/SWMM_Agentic/SWMM-Agentic3/models/Example1/runs/exp15_1103_213612_A05_40mm/diagnosis_tasks/de496e260f924c12b1b8efd7b4f1ad29)。
- 40 mm Agentic4：[诊断任务](E:/SWMM_Agentic/SWMM-Agentic4/models/Example1/runs/pilot4_20261116_adff44_A05_40mm/diagnosis_tasks/3e556d0278fa4cba806b39a74903cced)。