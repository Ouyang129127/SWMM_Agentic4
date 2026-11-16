# 诊断规则v2：SWMM-Agentic4核对记录

核对日期：2026-11-15。后续使用本目录的提示词和运行入口。

当前目录已包含SWMM-Agentic3最新规则，包括两场多峰降雨局部核验后的修正。本轮逐项核对了现有文件，保留Agentic4的过程序列传输优化，无需再覆盖生产代码。

| 项目 | 当前行为 | 入口 |
|---|---|---|
| 成因协议 | joint_mechanisms_v2，仅M1、M2、M3、M5、M6；不重排编号 | workflow_agents/diagnosis/contract.py |
| M4 | 从成因状态与联合成因中移除；冒溢时刻、位置和阈值作为基础事实 | workflow_agents/diagnosis/prompts.py |
| 拓扑/纵断面 | 独立结构核查；几何事实结合同期水头、流向、流量解释相关成因 | workflow_agents/diagnosis/structure_contract.py |
| M6 | 允许模拟初始占水、前一雨峰余水和明确储水单元约束；同轮上升段近满本身不列候选 | workflow_agents/diagnosis/prompts.py |
| 整场雨情 | 保留全部实际输入雨强点；事件定位到雨峰、雨段、间歇及同节点先前冒溢 | workflow_agents/evidence/diagnostic_context.py |
| 时间量 | 零雨间歇时长与距前一雨段结束的经过时间分开，避免把本轮已降雨时间计入雨隙 | workflow_agents/evidence/rainfall_context.py |
| 省token的传输 | 过程证据使用path_table_v1公共表头＋数据行；保留全部采样、数值与原引用绑定 | workflow_agents/diagnosis/series_transport.py |

提示词与Agentic3最新版本逐行比较，仅保留Agentic4专用的表格读取说明这一处差异；成因定义、M6候选边界和雨隙时长说明一致。service.py继续使用process_evidence_view，原始证据与任务快照仍采用原格式。

## 本地验证

全部265项离线测试通过，包括成因协议迁移、整场雨情、结构核查、表格无损还原和实际发送入口的模拟客户端测试。首次运行有2项Windows临时文件替换权限错误，完整重跑265项通过；未修改诊断或引用校验以绕过检查。

另对两场已有EPA多峰降雨输入做只读组包检查：

| 降雨 | 表格保留的过程样本数 | 整场雨强点数 | 请求JSON字符量减少 |
|---|---:|---:|---:|
| 双峰10＋10 mm，峰时相隔120 min | 268 | 45 | 50.04% |
| 双峰15＋5 mm，峰时相隔60 min | 297 | 33 | 51.58% |

表格经过JSON往返再还原后，全部选中记录与原记录相等；原记录未变。第二次起溢前60分钟零雨间歇及本轮已降雨25.083分钟也分别核对通过。这里测量的是完整请求JSON的字符量，实际token与费用由后续调用审计记录。

本轮模型调用0次，新模拟0次。详细哈希和组包检查见outputs/diagnosis_rules_sync_20261115/verification.json。M6候选边界的新条款已就位，其在线语义效果仍沿用小范围验证安排；本轮未重复付费验证。

如果服务进程在这些文件更新前已启动，重启后才能加载当前提示词。历史任务继续保留，新的诊断使用当前协议创建新任务。
