# 固定候选池排序记录

`compare_rankings.py` 完全离线，不调用 Jev、模型或网络。它使用单独的 benchmark schema（`schema_version: 1`），其中 `ranker_input` 保持严格的 V1 输入。这里的 schema 版本与嵌套 V1 版本分别校验。

输入支持 UTF-8 JSON 单对象、对象数组，或扩展名为 `.jsonl` 的逐行对象。重复 JSON key、NaN/Infinity、重复 case ID、缺失或多余配对均拒绝。每个 case 必须恰好有一个 C report；重复 run 不能充当新的排序任务。

## Case

| 字段 | 类型与含义 |
| --- | --- |
| `schema_version` | 整数 `1`；布尔值不接受 |
| `case_id` | 非空、唯一任务标识，不暗示答案 |
| `development_fixture` | 布尔值；人工构造池或 fake C 必须为 `true` |
| `pool_provenance` | `{kind, collector_blind_to_answer, annotation_source}` |
| `repo_revision` | 冻结的 buggy revision；开发夹具使用明确的合成标识 |
| `ranker_revision` | 冻结的排序器 revision，必须与 C envelope 完全相同 |
| `ranker_input` | 完整 V1 输入：`schema_version, reviewed_for_secrets, bug, candidates`；最多 12 候选 |
| `evidence_hash` | 下述规则对 `ranker_input.bug` 的 SHA-256 |
| `candidate_hash` | 下述规则对整个有序 `ranker_input.candidates` 数组的 SHA-256 |
| `frozen_hash` | 下述规则对整个 `ranker_input` 的 SHA-256，包含审核标志与 `local_only` |
| `relevant_candidate_ids` | 评估者事先标注的无重复 ID 数组；只能引用池内 ID。`[]` 表示关键证据未入池 |
| `fixture_category` | 可选非空字符串，仅开发夹具可使用 |

`pool_provenance.kind` 为 `blind_collected` 或 `constructed`。前者要求 `collector_blind_to_answer: true`，并记录答案标注证据位置；后者必须标为开发夹具。先由未见答案的收集者冻结输入与哈希，再由评估者标注相关候选。不能事后补入根因，或修改 ID、片段和注释提示答案。无法满足盲采集条件时，只能报告构造池结果。

多个 ID 可标记共同解释根因的候选。本版统计第一次遇到相关证据，并不计算集齐全部证据的时间，也不证明已完成根因诊断。覆盖率表示评估者标注的相关证据是否在池中；不独立验证标签是否正确。标注不确定时，应先补齐人工评价，不能用空数组掩盖未知状态。

哈希规则为 Python 标准库序列化：

```python
hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
```

仅对象 key 排序；数组顺序保留。片段、行号、来源、候选原序和 bug 证据任何改变都会使对应哈希不匹配。无 Unicode 归一化。`freeze_hashes(ranker_input)` 提供三个哈希，但生成哈希不等同于完成冻结或盲采集。

## C report envelope

必须字段为 `schema_version, case_id, repo_revision, ranker_revision, evidence_hash, candidate_hash, frozen_hash, source, jev_version, report`。前七项与 case 精确匹配；`source` 为 `fake` 或 `jev`。`jev_version` 是版本字符串或 `null`：真实 `ranked/partial` 报告只接受已验证的 `0.3.2`，真实 `fallback` 可保留未知版本。`dry_run` 不能标为真实 Jev 调用。

`report` 是现有 `rank_candidates.py` stdout 中的完整 JSON 对象，原样保存。它的 `schema_version`、状态、每个候选元数据、堆栈标志、评分字段和全部候选的 ID 排列会校验；候选列表必须保留输入原序。兼容的 V1 扩展字段允许保留。比较器直接使用 `report.investigation_order`，不根据分数重建 C，不复制 C 的未知项优先或回退策略。

哈希绑定由导入者完成。V1 报告本身没有输入内容哈希，因此 envelope、版本字符串和标签是来源声明；它们能拒绝不一致记录，不能证明来源声明真实。正式试验还应保留生成报告的命令、原始 V1 输出、revision 与冻结证据索引。`fake` 不能被重标成真实收益；即使开发 case 导入 `source: jev`，输出仍携带开发夹具标签。

## 冻结的 A/B 规则

- A：按候选原序取出 `origins` 包含 `stack` 的候选放在前面，其余保留原序。
- B：同样保留 stack 前缀原序；其余按固定词法分数降序排列，同分保留原序。规则名为 `unicode-token-overlap-v1`。
- B 的查询字段：bug description、expected、actual、全部复现 steps 和 stack_trace。候选字段仅为 path 与 snippet，排除 ID、标签和来源说明。
- 先在 ASCII 小写字母/数字与其后大写字母之间拆分 camelCase，再做 Unicode `casefold()`；用 Unicode 正则 `[^\W_]+` 取 token。标点和下划线是分隔符；中文连续字符作为一个 token。无词干化、停用词、模型、频次或字段权重。
- 分数是查询 token 集与候选 token 集的交集大小。重复出现同一词不会增加分数。

修改字段、分词或计分规则后，须使用新规则版本和未用于调规则的新验证任务。

## 输出与解释

JSON 包含逐 case 的 A/B/C 顺序、首个相关排名、Hit@1/3、MRR、模拟字节数，以及覆盖率、整体指标和入池子集指标。MRR 为首个相关排名的倒数；未入池在整体指标中计零。入池子集为空时均值为 `null`，不能写成零或省略失败。

`simulated_bytes_to_first_relevant` 累加输入片段的 UTF-8 字节，包含第一个相关候选；未入池时读取整个池，排名为 `null`、`read_stopped_at_relevant: false`。不含路径和元数据开销，不去重重叠片段。这是固定顺序模拟，不能充当模型实际阅读、token、费用或修复时间。

`improvement_opportunity` 表示候选有相关证据且 A 的首个相关候选不在第一位；`order_changed` 分别记录 B/C 是否与 A 不同。仅顺序变化不会被表述为收益。汇总中只要包含开发夹具，`development_fixture` 就为 `true`，并保留每项来源及 fixture 数量。

## 离线开发夹具与示例

`fixtures/ranking-cases.jsonl` 的七个合成 case 覆盖六类：改善、首位根因、根因缺席、错误排序、回退、多相关证据。回退类分别包含 partial 和全部 fallback。`ranking-c-reports.jsonl` 的 C 是把人工分数交给 V1 `finish()` 后保存的 fake JSON；没有真实 CLI/API 调用。revision 使用合成标识，不能冒充实际仓库冻结版本。

四个片段的 UTF-8 字节分别为 4、12、14、21，总量 51。`ranking-expected.json` 记录独立手工核算的排名、Hit/MRR 与字节预期；测试逐项核对，不使用比较器计算预期。

```sh
python3 -B benchmarks/compare_rankings.py \
  --cases benchmarks/fixtures/ranking-cases.jsonl \
  --c-reports benchmarks/fixtures/ranking-c-reports.jsonl \
  --json-output benchmarks/examples/rankings.json \
  --markdown-output benchmarks/examples/rankings.md
python3 -B -m unittest discover -s tests -p 'test_rankings_benchmark.py' -v
```

CLI 成功 exit 0；记录错配、无效输入或输出写入失败 exit 2。报错只显示异常类别，不回显原始输入或文件错误详情。样例仅证明计算器行为，不属于 P1 实验或产品收益证据。
