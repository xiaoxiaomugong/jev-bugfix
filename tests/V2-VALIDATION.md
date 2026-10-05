# 离线评测验收与重建

固定池比较器与运行汇总器使用 Python 3.9+ 标准库，默认不启动模型、Jev CLI、网络请求或凭据读取。工具验收与真实收益实验分别记录：P1 仍为 **0/12 次修复、0/6 个真实任务**，当前没有定位效率、阅读、时间或费用收益结论。

## 行为覆盖

- [test_rankings_benchmark.py](test_rankings_benchmark.py) 通过独立手算预期验证 A 的堆栈优先原序、B 的固定 token overlap、C 的完整 V1 调查顺序，以及覆盖、Hit@1/3、MRR 和模拟字节。缺候选、未知分、部分及全部回退均保留，不把开发夹具提升为真实样本。
- [test_benchmark.py](test_benchmark.py) 验证严格 run/event 输入、来源与单位、null 缺项、重复曝光与去重区间、失败与未配对 run、费用完整性、共同配对集合及收益门槛。不同重复 run 的不完整指标不能拼成完整收益证据，失败重复的真实费用必须进入汇总。
- 继承的 [test_rank.py](test_rank.py) 继续验证严格 V1 schema、堆栈优先、候选保留、安全边界、版本预检及评分进程计数；评测层不重写排序或放宽外发边界。

固定池静态输入只需要 [ranking-cases.jsonl](../benchmarks/fixtures/ranking-cases.jsonl)、[ranking-c-reports.jsonl](../benchmarks/fixtures/ranking-c-reports.jsonl) 与独立 [ranking-expected.json](../benchmarks/fixtures/ranking-expected.json)。七个 case 覆盖六类场景，部分与全部回退各一例。汇总器单元测试自行构造输入；公开 [input-runs.jsonl](../benchmarks/examples/input-runs.jsonl)、[input-events.jsonl](../benchmarks/examples/input-events.jsonl) 与 [synthetic.log](../benchmarks/examples/synthetic.log) 是可重算示例。

## 从干净 checkout 运行

以下命令从本仓库根目录执行。临时目录由系统选择；生成产物保留在该目录，避免覆盖仓库示例。

```sh
benchmark_demo=$(mktemp -d "${TMPDIR:-/tmp}/jev-benchmark-demo.XXXXXX") &&
python3 -B benchmarks/compare_rankings.py \
  --cases benchmarks/fixtures/ranking-cases.jsonl \
  --c-reports benchmarks/fixtures/ranking-c-reports.jsonl \
  --json-output "$benchmark_demo/rankings.json" \
  --markdown-output "$benchmark_demo/rankings.md" &&
python3 -B benchmarks/summarize_runs.py \
  --runs benchmarks/examples/input-runs.jsonl \
  --events benchmarks/examples/input-events.jsonl \
  --json "$benchmark_demo/runs.json" \
  --markdown "$benchmark_demo/runs.md"
python3 -B -m unittest discover -s tests -p 'test_*.py' -v
git diff --check
```

比较与汇总均应退出 0，结果保持 `development_fixture=true`。模拟金额、时间与源码字节只是计算器输入，不是实际支出、修复耗时或阅读量。排序 envelope 的哈希仅绑定声明中的输入，不能自行证明 Jev 实际评分。

如果已有兼容的 Jev 0.3.2，可另运行 `python3 -B tests/verify_local_cli.py`。该检查模拟响应并阻断凭据与网络，不在默认测试发现中，不要求安装另一份 CLI。默认回归通过不能替代该可选检查。

CI 保留 Linux/Python 3.9、Linux/Python 3.14、macOS/Python 3.14。提交审阅前应记录各提交的干净 checkout 实测结果及环境；本机测试或 AST 语法检查不能写成其他平台或远端 CI 通过。

本验收不运行在线 Jev、真实 API 冒烟、P1 修复会话、usage/账单采集或盲测隔离。真实实验启动条件、预算和停止规则见 [P1-CHECKLIST.md](../benchmarks/P1-CHECKLIST.md)，现状见 [RESULTS.md](../benchmarks/RESULTS.md)。
