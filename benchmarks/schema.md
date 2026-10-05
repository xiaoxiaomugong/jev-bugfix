# 本地运行记录 v1

这是独立的评测 schema，不扩展 V1 评分输入。固定池 case/C envelope 见 [rankings-schema.md](rankings-schema.md)。完整可解析的运行和事件例子见 `examples/input-runs.jsonl` 与 `examples/input-events.jsonl`；全部明确标为开发夹具。

## 输入与来源

`summarize_runs.py` 接受 JSON 数组或 `.jsonl`（每行一个对象），拒绝重复 JSON key、非有限数、重复 run ID、同一 pair 的重复 arm、同一 run 的重复 event ID。不做网络调用。事件先规范化再导入，不猜测任意原始代理日志格式。

每个标量指标使用以下对象；字段全部必需：

```json
{"value":null,"unit":"tokens","kind":"unavailable","source":null,"evidence":[],"completeness":"unavailable","reason":"当前运行器未提供逐运行 usage"}
```

| 字段 | 规则 |
| --- | --- |
| value | 非负有限数；count/bytes/tokens/lines 必须整数；缺失只能 null |
| unit | seconds、bytes、lines、tokens、count，费用用 currency 中的统一单位 |
| kind | measured / derived / estimated / unavailable；null 只能 unavailable |
| source / evidence | 非空值必须有来源说明及证据路径列表；不能把模型猜测标 measured |
| completeness | complete / lower_bound / unavailable；下界需要 reason |
| reason | 缺失/下界的具体原因；其他情况允许 null |
| price | estimated 值额外必需 `{date: YYYY-MM-DD, source, basis}`；只有带实际 usage 和适用价格的计算才能用作费用估计 |
| billing_basis | derived 的实际费用必需 `invoice_aggregation`；usage×单价属于 estimated |

token 字段只接受供应商/运行器实测 usage 或 unavailable；不接受字符折算或模型估计。保留各字段定义，cached input 是否包含在 input 中必须写清；不擅自相加。订阅百分比不等于本次费用。

## run 字段

| 字段 | 内容 |
| --- | --- |
| schema_version | 整数 1 |
| case_id / run_id / pair_id | case 是冻结任务；run 全局唯一；pair 显式绑定一次 A/C，重复实验使用新 pair/run ID |
| arm / development_fixture | A 或 C；夹具标签必需且必须为布尔值 |
| repository | `{url, revision}`，buggy 起点；同 case 不允许跨版本复用 |
| config | `{model, reasoning, prompt_hash, tools_hash, environment_hash, budget_hash}`；真实实验应保存冻结文件/配置 SHA256；导入器只校验非空身份和配对一致性，原始哈希真实性由评估者审计。基础 prompt 相同，A/C 的策略差异在冻结 arm 指令中登记 |
| candidate_hash | C 构建候选后保存有序候选 SHA256；尚未构建/评分被跳过时允许 null 并在证据说明。A 正常调查，允许 null，不强迫构建候选 JSON。固定池哈希匹配由 comparator 严格校验 |
| timing | `{started_at, finished_at, evidence, phases}`；ISO8601 必须带时区；finished_at 可 null；从收到任务到最终验证，包括候选收集、版本预检、评分/失败等待、调查、修复、测试 |
| timing.phases | collection / preflight / scoring / investigation / repair / tests 六项 seconds 指标；缺失明确 unavailable，不能用总时间均分；并发/重叠阶段须在 source 说明，不能直接相加冒充总时间 |
| capture | `{complete:bool, method, scope, reason, evidence}`；complete 意味着覆盖全部展示给模型的源码（含收集时片段、rg/命令输出、重复上下文、候选 JSON 等）；scope/method 固定且相同才可配对 |
| outcome | null 或 `{status, tests_unmodified, validations, reason}`；status 为 success/failure/timeout/environment_failure/unavailable |
| usage | input_tokens / cached_input_tokens / output_tokens 三项指标及 definitions 字符串 |
| costs | `{currency, main_model, jev, review}`；三个分项均使用上述指标对象，额外归因评审不可漏记；没有评审也要有“未执行评审”记录支持 0 |
| jev | `{version, ranker_revision, status, failure_types, cli_invocations, preflight_invocations, submitted_candidates, payload_bytes, http_attempts_upper_bound}`；数值均指标对象，version 可 null；status 为 not_used/ranked/partial/fallback/unavailable |
| protocol_deviations / evidence | 偏离字符串列表及原始证据索引；空偏离须表示确实核验过协议 |

`validations` 每项为 `{role, command, exit_code, evidence}`。role 为 reproduction_before/reproduction_after/independent/regression。success 必须至少一个原复现失败、修复后原复现全部通过、独立行为测试及相关回归全部通过，而且 tests_unmodified=true；删/跳过/放宽测试不算成功。结果缺失不推断成功。测试命令与日志支持不同但合法的修复。

A 的 Jev status=not_used，不允许非零调用记录。C 的评分进程≤1、预检进程≤1、候选≤12、payload≤24576 bytes、HTTP 上界≤24；超出时必须登记 protocol_deviations，仍保留 run，不参与收益判定。只有 verified 0.3.2 可声明正的 `2×submitted` 上界；0 表示没有启动评分，不代表某次启动后没有 HTTP 请求。上界永远不是实测物理请求或费用。

## event 字段

公共字段：`schema_version=1, event_id, run_id, type, timestamp, source, evidence`。timestamp 带时区且位于该 run 计时范围。event ID 在一个 run 内唯一；重复导入直接拒绝，不静默累计。未知 run/type 被拒绝。

- `source_read`：额外字段 `path, file_version, start_line, end_line, bytes`。首尾行含在内；file_version 是读取时源码版本/内容 SHA256；bytes 是实际展示的源码 UTF-8 字节，不含终端装饰。相同片段重复展示必须产生不同 event ID；累计 bytes 不去重。按 `(path,file_version)` 合并重叠及相邻区间，报告去重行数与全部区间。
- `root_cause`：额外字段 `claim, verified:bool`。代理首次提出的时间须原样保留；评估者事后核验，最早 verified=true 的事件生成首次正确根因耗时。source/evidence 指向提出事件及评估依据；没有事件就是不可测，不能用修复完成时间替代。

无法把工具输出可靠拆成源码时，不制造 source_read。记录 capture.complete=false，报告已观察量及下界。完整工具输出量可另存原始日志，但不作为实际源码量。规范化过程不执行记录中的 command。

## summary 与判定边界

输出保留所有 run 的来源、完整性、验证和事件派生指标；缺少真实 main_model/Jev/review 任一分项时 total_real_cost=null，known_cost_subtotal 保留已知分项。total_estimated_cost 单列（可混合已知账单和带依据估计），不可称实付总费用。开发夹具中的模拟金额同样不构成真实费用。

显式 pair 的 repository/config/fixture 标签/currency 不匹配就拒绝。只有双方成功、无协议偏离的 pair 比较耗时；阅读另要求 capture 全覆盖及相同 method/scope。缺项和不配对在 excluded_reasons 中保留。所有失败/超时/缺结果仍计入全部运行成功率；累计费用及费用/成功数包括失败，零成功或费用不全时费用/成功数不可得。

按 case 汇总配对差值中位数，然后按独立任务报告胜/平/负、范围和中位数；重复 run 不增加独立任务数。差值为 C−A，负数表示下降；reading_reduction=(A−C)/A，A=0 时百分比为 null。描述统计与收益门槛要使用一致的配对集合，不能拼接不同重复的缺失指标。

描述统计可以各自报告不同的已知指标数量；收益门槛则只使用同时有完整阅读和耗时的同一组 pair，`joint_comparable_pairs` 列出它们，不能拼接重复间的缺项。真实运行集合存在未配对 run 时暂不判定门槛。

预设探索门槛为至少六个完整真实配对任务、阅读降幅中位数≥20%、耗时差中位数≤0；费用仅在全部真实运行费用完整时评估，按任务汇总所有成功与失败重复的费用差，再要求差值中位数、总费用及费用/成功数均不增加。缺费用时成本目标为 unavailable，即使阅读/耗时目标达成也不能宣称成本收益。出现 A 成功而 C 实际失败/超时，先暂停扩样；环境失败仍保留并注明性质。结果最多说明本探索样本是否达到预设工程目标，不能证明普遍收益或质量非劣性。夹具永远是 insufficient_evidence。

导入器验证结构和算术，不证明声明的来源真实、盲测隔离成立或采集完整；真实实验必须由评估者核对原始证据、版本与冻结清单。当前未实现自动调度或任意日志适配器。
