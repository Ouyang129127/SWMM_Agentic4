# Example1：SWMM + CA2D 模拟与诊断算例

已补齐标准模型目录，完成 SWMM→溢流边界→CA2D→统一证据包的实际运行验收。当前根目录 `Example1.inp` 及两个工作情景均采用 DYNWAVE 动态波；原始输入归档在 `raw/Example1.inp`。

2026-10-24 已统一更新动态波声明、baseline 输入标题及准备脚本。新运行会读取更新后的模型上下文；已有 runs/ 和 validation/ 中的模型副本、证据及报告保留生成时的历史记录，不会自动更新。

2026-10-16 追加了 20 组接近 4 h / 20 mm 的降雨，现有事件库共 32 场。新增雨型、9 组优先尝试清单及水力预筛见 [低雨量诊断事件说明](E:/SWMM_Agentic/SWMM-Agentic2/models/Example1/events/LOWRAIN_GUIDE.md)。本次预筛遵循当前雨后 3 h 的时间配置，每场共 7 h；下文 12 h 恢复、16 h 总时长和三个验收运行记录的是初始配套时的历史配置。本次没有修改时间策略、管网或地表模型。

**地表是合成的技术验证场景。** 原 INP 的地图单位为 None，没有 DEM、建筑调查或道路底图。这里依据原有节点、管线及子汇水区示意位置构造局部米制场景，不能据此解释真实地点的淹没范围、房屋风险或退水时间。

## 原模型及工作情景

| 项目 | 配置 |
|---|---|
| 子汇水区 | 8 个，总面积 28.73268 ha（原 71 acre） |
| 节点 | 13 个汇合节点、1 个自由出流口 18 |
| 管道 | 13 段圆管，直径 0.3048–0.6096 m；没有 STREET 或原生雨水口 |
| 水文 | Horton 入渗，原始坡度 0.01% 保留 |
| 水质 | 保留 TSS、Lead、土地利用、累积及冲刷设置；仅在 SWMM 内计算 |
| 特殊连通关系 | 子汇水区 8 直接排向出流口 18，绕过管道系统，保留原设定 |
| 原始保存 | raw/Example1.inp 为历史原件；preparation_manifest.json 保留初始来源 SHA256 |

`swmm/Model.inp` 当前已采用 DYNWAVE，保留原降雨及原 36 小时时窗。初始 SI 单位换算验收使用的是当时的运动波参考，相关结果保存在 validation/ 中。

| scenario_name | 路由 | 用途 |
|---|---|---|
| baseline | DYNWAVE 动态波 | 已修改为用于顶托、压力流和管内流向变化的诊断 |
| dynamic_wave | DYNWAVE 动态波 | 建议用于顶托、压力流和管内流向变化的诊断 |

两个工作情景保留相同的管径、长度、糙率、节点标高、汇水区及水质参数；报告步长均为 5 s，最大路由步长 5 s，降雨期水文步长 1 min。动态波可自动使用更小步长。默认情景仍为 baseline；两个情景均可用于管网内部顶托、压力流和倒流诊断，是否实际发生须依据水力证据判断。

单位转换覆盖 acre→ha、ft→m、in→mm、CFS→LPS。水质中 SAT 面积累积量由 lb/acre→kg/ha；EXP 冲刷系数按 `C1_SI = C1_US / 25.4**exponent` 转换，浓度单位保持不变。依据 [EPA SWMM 5.2 手册](https://nepis.epa.gov/Exe/ZyPURL.cgi?Dockey=P10145M6.TXT) 和 [EPA landuse.c](https://github.com/USEPA/Stormwater-Management-Model/blob/develop/src/solver/landuse.c) 的降雨强度、面积换算定义。

## CA2D 地表

| 项目 | 配置 |
|---|---|
| 网格 | 5 m，129 行 × 122 列；范围 610 m × 645 m |
| 有效单元 | 14,192，其中可流动单元 12,872 |
| 建筑 | 160 栋假设建筑，占 1,320 个网格；拔高 15 m，并从流动掩膜中排除 |
| 道路 | 沿原管线示意位置构造 13 条假设走廊，宽 8 m、降低 0.10 m；占 370 个单元 |
| 地形 | 节点井底 + 最大深度作为推定地面高程控制点，IDW 插值，局部修正使映射网格与控制高程吻合 |
| 出流口附近地面 | 井底 + 平均节点覆深的假设值；不是尾水位 |
| 节点映射 | 全部 14 个节点，最大映射距离约 2.974 m，权重均为 1 |
| 输水系数 | 普通地面 0.62、道路 0.90；沿用 CA2D 的经验传输系数含义，不是 Manning n |

原坐标没有长度单位，采用同一个比例尺，使子汇水区多边形并集的近似面积与声明总面积一致；再平移到局部场景。这个尺度是明确假设，不是地理配准。**实际水力管长仍使用原 INP 长度换算值**，不以示意图量距代替。两者差异在 `mapping/coordinate_scale_audit.csv` 中逐管记录。

住宅地块比例来自原 COVERAGES；原 INP 没有实际房屋轮廓。混合用地中的住宅部分暂分配到地块西半部，再以确定规则生成建筑。道路也是假设的地表走廊，没有新增 SWMM STREET 或 INLET 元素。

![CA2D 合成场景布局](E:/SWMM_Agentic/SWMM-Agentic2/models/Example1/static/ca2d_layout_preview.png)

## 耦合边界与解释范围

计算链为：子汇水区降雨径流 → SWMM 节点及管道 → 节点外溢 → CA2D 单向扩散。没有原生街道回流需要扣除，14 个节点映射权重均为 1；出流口映射用于完整性，本次没有把自由出流量作为地表注水。

- CA2D 只接收 SWMM 节点溢流，没有再次叠加网格降雨，避免把同一场雨重复计入。
- 地表没有下渗、蒸发、开放出流边界或回灌管网；结束时仍会保留累计溢流水量。局部水深下降可来自重新分布，不等于真实退水完成。
- 地表水头不反馈到 SWMM，不能模拟地面积水压制井口外溢、二维积水回灌雨水口等双向作用。
- CA2D 使用现有元胞扩散算法；本项目验收的是数据、单位、水量及工作流完整性，没有进行实测洪水或完整浅水方程精度验证。合成地形与点源注水可产生很深的局部网格水深，不能直接当作真实住宅淹没深度。
- TSS 和 Lead 在 SWMM 原生输出中保留，CA2D 没有二维污染物输运。

以上解释已经写入 `mapping/coupling_policy.json`，实际运行会复制到 `model_context.json`、`summary.json` 和统一证据包，供后续诊断读取。

## 降雨与时间设置

初始配套为 12 个降雨文件，后续新增 20 个，当前共 32 个。格式为 `timestamp,value`，强度单位 mm/h，时间戳表示区间起点，末尾补零。所有文件及来源哈希见 `events/manifest.json`。

- 原 TS1 两段降雨序列：30 小时时间轴、1 h 间隔、总量 67.31 mm；显式展开中间零雨时段。
- 对照事件：4 h、5 min 间隔，单峰 60/80/120/160 mm，双峰和三峰各 80/120/160 mm。
- 较低总量对照：单峰 20 mm，由单峰 80 mm 按 0.25 缩放。

这些 Chicago-like 雨型沿用已有示例文件，不对应已标定的当地 IDF 曲线，**没有赋予重现期**。20 mm 情景在本模型中也有短时局部溢流，不能写成完全不溢流。

`simulation_timing.json` 统一规定：SWMM 原生输出每 5 s 保存；以区间平均流量形成 5 min CA2D 入流边界；CA2D 内部步长 10 s、每 5 min 保存；降雨结束后继续 12 h。4 h 事件实际算到 16 h。较长恢复时段考虑原汇水区极小坡度，但并未证明所有过程已完全消退。

## 已完成的验收

全部结果见 `validation/preparation_checks.json` 和 `validation/final_acceptance.json`。

1. 原始与 SI 参考情景按相同 36 h 降雨运行，逐小时比较节点水深、节点溢流、管道流量、水深及 TSS、Lead 浓度；所有最大差异相对于各指标峰值均小于 0.1%。
2. 校验网格、建筑、道路、节点坐标、控制高程及单元 ID；14 节点各注入 10 L/s 持续 10 min，84 m³ 注水的相对水量误差约 1.66×10⁻⁸，建筑网格无积水。
3. 三个实际降雨情景均完成 SWMM、CA2D 和确定性统一证据构建，状态为 EVIDENCE_READY。未调用 LLM，也未生成自动诊断结论。

| 已保存 run_id | 情景 / 4 h 总雨量 | 溢流节点数 | 耦合溢流量 m³ | 管网水量连续性误差 |
|---|---|---:|---:|---:|
| accept_dw_20mm_20261016 | dynamic_wave / 20 mm | 1（节点 10） | 82.86 | −0.003% |
| accept_dw_160mm_20261016 | dynamic_wave / 160 mm | 10 | 15,223.17 | −0.010% |
| accept_kw_160mm_20261016 | baseline / 160 mm | 10 | 16,531.97 | −0.071% |

5 s 溢流积分与 SWMM 原生报告总量的差异均小于报告取整的 1 m³；5 min 边界积分与 CA2D 实际注入量差异小于 10⁻⁵ m³。CA2D 内部结束水量与注水量的相对差异均小于 0.006%。动态波 160 mm 情景有 0.07% 路由步未收敛，其余两场为 0%；记录保留在原生报告与 verification.json 中。

SI 模型的节点 21、22 会出现 `WARNING 02: maximum depth increased`。已读取 SWMM 有效深度核对：输入 0.9144 m，连接管顶经英尺内部换算后为 0.9144000000000001 m，仅差约 1.11×10⁻¹⁶ m。保留原始尺寸并将这两条警告列为已解释项，不接受其他警告。

地表 TSV 沿用现有工作流的 **0.001 m 舍入精度**。`max_saved_depth.npy`、`final_saved_depth.npy` 从每 5 min 保存值导出，因此不同于绘图所用的内部每 10 s 极值；水量守恒应优先查看 summary.json 内部水量，不能把舍入后的 TSV 当作高精度体积基准。所有 579 个保存时刻、建筑干区、证据的 7 个源文件哈希均已复核。

## 文件索引

```text
Example1/
  Example1.inp                      当前动态波模型
  raw/Example1.inp                  历史原件归档
  raw/references/rainfall/          冻结的对照降雨来源
  model.yaml                       标准模型入口
  simulation_timing.json            统一时间策略
  preparation_manifest.json        44 个关键输入文件哈希
  swmm/Model.inp                   当前动态波 SI 参考
  swmm/scenarios/{baseline,dynamic_wave}/model.inp
  static/                          CA2D 高程、掩膜、输水系数、ID 网格及 cells.csv
    synthetic_buildings.csv        假设建筑轮廓、网格范围及拔高
    synthetic_streets.csv          假设道路走廊
    node_to_cell_mapping.csv        SWMM→CA2D 映射
    ca2d_layout_preview.png         二维布局
    ca2d_elevation_3d.png           建筑拔高三维图
    ca2d_smoke_test.png             人工注水验收图，非降雨结果
  mapping/
    network_nodes.csv              节点属性
    network_links.csv              管段拓扑、管长、直径及糙率
    catchment_to_node.csv           汇水区—出流节点关联，含直接出流标记
    schematic_geometry_local.json   局部示意坐标及多边形
    coordinate_transform.json       明确记录比例尺假设
    coordinate_scale_audit.csv      示意距离与水力管长逐段对照
    coupling_policy.json           诊断解释范围
    node_to_cell_mapping.csv        映射副本
  events/                          12 场降雨 + manifest.json
  runs/<run_id>/
    swmm/                          INP、RPT、OUT、节点/管道/溢流时序
    ca2d/                          入流边界、水深 TSV、PNG、GIF、保存水深 NPY
    evidence/evidence_package.json  统一证据包
    workflow_state.json            EVIDENCE_READY
    summary.json                   数值结果和解释上下文
    verification.json              逐场验收记录
  validation/                      单位等价原生结果及最终审计
  scripts/                         可复现的准备、验证、运行与审计脚本
```

当前环境没有可用的栅格 GIS 写出依赖，运行结果未生成 GeoTIFF；可用数据为网格 NPY、cells.csv、水深 TSV 和明确记录局部尺度的配置，且本场景本来没有地理 CRS。

## 使用

在项目根目录 `E:\SWMM_Agentic\SWMM-Agentic2` 执行新计算：

```powershell
.venv\Scripts\python.exe models\Example1\scripts\run_example1_check.py --scenario dynamic_wave --event chicago_like_single_4h_120mm_5min.txt
```

脚本自动生成新 run_id，完成模拟和统一证据；显式指定已存在的 run_id 会拒绝覆盖。亦可直接在当前工作流中选择 `model_name="Example1"`、`scenario_name="dynamic_wave"` 和其 events 中的降雨文件。

重新准备静态文件及验证：

```powershell
.venv\Scripts\python.exe models\Example1\scripts\prepare_example1.py
.venv\Scripts\python.exe models\Example1\scripts\verify_example1.py
.venv\Scripts\python.exe models\Example1\scripts\audit_saved_checks.py
```

准备脚本按原 INP 生成派生输入，适合复现此初始配置；如果已手工修改工作情景或静态文件，应先另存独立情景。它不会覆盖已有模拟目录。审计脚本只复核三个已保存验收算例并刷新附属保存深度数组，不重新计算。
