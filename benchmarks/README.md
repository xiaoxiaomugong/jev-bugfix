# V2 轻量评测

目标是测量 Jev 排序的增量价值与端到端净收益，允许负收益或证据不足。工具使用 Python 3.9+ 标准库、本地 JSON/JSONL 与 Markdown。当前真实实验状态见 [RESULTS.md](RESULTS.md)，输入、指标与采集完整性规则见 [schema.md](schema.md)。从仓库根目录运行下列命令；默认工具不会启动模型、Jev 服务或其他 API。

## 第一层：冻结候选池

收集者只获得 buggy revision 与 bug/复现证据，不能看到根因标签/参考补丁。先冻结输入、原序、路径、行号和版本哈希，再由评估者标注一个或多个相关证据候选。缺席以空标签记录，不能事后补入答案。无法盲收集则明确标人工构造池，只支持构造池范围的结论；开发夹具不能充当真实样本。

A 把堆栈候选提前，其余原序；B 把堆栈候选提前，其余按固定本地 token overlap 排序，同分原序；C 直接导入 V1 完整 investigation_order，包括未知优先、低分保留和回退，不在评测中重写 C。具体规则、哈希与 envelope 见 [rankings-schema.md](rankings-schema.md)。指标为候选覆盖、整体及入池子集 Hit@1/3、MRR、首个相关证据前累计模拟片段字节、顺序改变和改善机会数。

完全离线重算六类开发夹具（七个 case，部分/全部回退各一例）：

```sh
python3 -B benchmarks/compare_rankings.py \
  --cases benchmarks/fixtures/ranking-cases.jsonl \
  --c-reports benchmarks/fixtures/ranking-c-reports.jsonl \
  --json-output benchmarks/examples/rankings.json \
  --markdown-output benchmarks/examples/rankings.md
```

[示例排序报告](examples/rankings.md)只验证计算和导入，不证明真实阅读/时间/成本收益。夹具 C 是 fake，真实 Jev 输出须另存生成命令、冻结输入、输出及 revision，并使用 `source=jev`。哈希 envelope 是需审计的出处声明，不能自行证明 CLI 实际评分了该输入。

## 第二层：完整修复对照

首轮六个真实任务、A/C 各一次、十二个全新会话。A 正常复现/局部搜索/最小修复/测试，无评分 JSON 要求；C 使用修正边界后的完整 V1 skill，最多一次评分批次。冻结主模型、推理、基础 prompt、工具、环境和总预算；预排运行顺序，记录缓存与并发。参考答案、其他组日志、修复后代码与裁判测试只能在评估端，完成补丁后再跑独立行为验收。工作树本身不构成隔离。

每批候选≤12、每段≤2 KiB/60 行、JSONL≤24 KiB、并发2、socket10秒、评分整批45秒、无外层重试。版本预检另有2秒/1KiB限制，时间和进程次数分开记录并计入 C 总开销。全局预算必须包括所有失败与独立重复 run；首轮最多六次 C 批次、72次逻辑评分、144次 HTTP 尝试上界，实际请求/账单另记。

准入来源、冻结字段、12次运行顺序、预算输入和停止条件见 [P1-CHECKLIST.md](P1-CHECKLIST.md) 与 [manifest 模板](p1-manifest.template.json)。六个任务槽位仍需真实来源和 revision；本轮未启动这些实验。第一层 C 不及词法 B 时先分析；开发调试集与以后未用于调规则的验证集分开，小样本不证明普遍收益。

## 导入与汇总

[schema.md](schema.md)定义 run/event/summary。当前只支持明确格式的本地记录；没有假设 Codex 具备某个已验证 usage 导出器，也不以字符数猜 token。每项指标带来源、证据、单位与完整性，缺失用 null；实际费用与估计分开。源码累计暴露包含候选收集和重复展示，去重行按文件版本独立合并。无法覆盖全部读取时只能报告已观察下界，不进入阅读收益判定。

```sh
python3 -B benchmarks/summarize_runs.py \
  --runs benchmarks/examples/input-runs.jsonl \
  --events benchmarks/examples/input-events.jsonl \
  --json benchmarks/examples/runs.json \
  --markdown benchmarks/examples/runs.md
python3 -B -m unittest discover -s tests -p 'test_*.py' -v
python3 -B tests/verify_local_cli.py
```

[示例运行汇总](examples/runs.md)是合成事件，展示失败、费用缺失、已观察下界及重复曝光。不能列入真实收益样本。主模型、Jev 或归因评审真实费用任一未知时总真实费用未知。汇总以任务为独立单位，保留失败/缺项/偏离；success 需原复现失败转成功、独立验收及回归通过且测试未放宽。准备/标注/工具开发成本另列，不混入工作流 run 成本。

## 采集与运行要求

比较器和汇总器只读取显式提供的本地文件，使用 Python 3.9+ 标准库。示例的 `synthetic.log` 只是夹具来源声明，没有发生真实修复或支出。真实 run 必须保存对应原始证据并审计出处；汇总器的 schema 校验不会证明声明中的命令实际执行过。

当前没有已验证的主模型 usage/账单导出器或完整源码采集器。调用次数、预检计数和 HTTP 上界来自 ranker 的本地记录；上界不能替代实际请求、token 或费用。未知采集量继续用 `null` 与原因表示，不以字符数估算实测 token。不同 CLI 版本须重新验证契约；当前外部评分契约限定 Jev 0.3.2，离线工具本身不要求安装该 CLI。

回归与示例重建说明见 [V2-VALIDATION.md](../tests/V2-VALIDATION.md)。
