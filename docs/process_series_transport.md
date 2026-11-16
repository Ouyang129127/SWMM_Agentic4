# 诊断输入的过程时序表格

发送给 DiagnosisAgent 前，将同构的过程采样记录转换成公共表头和数据行。此转换只处理每个采样点重复的数据结构，不合并跨事件结构、不删除公共元信息、不改变跨轮上下文策略。

随后可以执行第二类[请求内浅层静态共享](shallow_shared_transport.md)，与本页的过程表格化分步处理；事件过程不参与静态共享。

适用指标：`local_process_series`、`fullness_time_series`、`max_fullness`、`storage_volume_process`、`ponding_depth_time_series` 的 `value.series`。首轮、补证后和修订时均经过同一个组包入口。

M6的`antecedent_hydraulic_context.value.series`也使用同一传输格式。该背景记录在构建时明确按300秒及关键边界选取原生样本；表格转换再完整保留背景记录的全部样本。采样策略与无损格式转换是两个步骤，详见[M6前期视窗](m6_antecedent_window.md)。

```json
{
  "format": "path_table_v1",
  "columns": [["time"], ["nodes", "10", "head_m"], ["links", "6", "signed_model_flow_Ls"]],
  "rows": [
    ["2024-01-01 01:00:00", 304.1, -2.0],
    ["2024-01-01 01:00:05", 304.2, 0.0]
  ]
}
```

每列表头是原始字典键组成的路径数组，编号中的点号、斜杠或数字不会产生歧义。每行对应一个原始采样点，时间戳、数值、精度、顺序、单位和方法不变；空字典、数组及 `null` 原值保留。重复的方法字符串仍作为单元格保存，本次不额外提取常量。

所有采样点必须具有相同的字段路径；字段缺失、对象集合变化或字典与标量形态变化时，整个序列保留原记录数组，避免将“字段不存在”填成 `null`。空序列和非记录数组也保留原格式。只有紧凑 JSON 的字符数实际减少时才采用表格。

转换在 `diagnosis/series_transport.py` 实现，由 `build_diagnosis_prompt` 调用。它先深拷贝选中的完整记录，仅改变副本中的过程序列。原始证据包、任务快照、证据 ID、来源绑定、数值策略、事件清单和引用校验仍使用原始记录；不需要重新模拟或重建证据。运行中的服务需要重启才能加载代码和提示词。

离线验证：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p test_series_transport.py
```

测试包括 JSON 往返还原、缺失与空值、负流量与浮点精度、大范围对象、证据副本隔离、补证指标和实际模型发送入口。字符数不等同于 token 数，token 和诊断质量应在实际模型实验中另行比较。
