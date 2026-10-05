# jev-bugfix

[![Offline tests](https://github.com/xiaoxiaomugong/jev-bugfix/actions/workflows/offline-tests.yml/badge.svg?branch=main)](https://github.com/xiaoxiaomugong/jev-bugfix/actions/workflows/offline-tests.yml)

让 Codex 用本机 Jev CLI 为候选代码提供相关性排序，再通过复现、调用关系和测试完成 bug 修复。Jev 提供相关性评分，Python 助手生成调查顺序，Codex 负责确认根因、最小修改和验证。

所有候选都会保留，堆栈直接指向的片段优先调查；评分失败时继续本地调查。项目目标是减少无效代码阅读，**当前尚无真实阅读、时间或费用收益结论**。

## 当前功能

| 入口 | 功能 |
| --- | --- |
| [Codex 技能](skills/jev-bugfix/SKILL.md) | 复现问题、收集短片段、审核输入、排序、修复与测试 |
| [候选排序助手](skills/jev-bugfix/scripts/rank_candidates.py) | 校验候选 JSON；可选调用 Jev；输出完整调查顺序、逐项状态和调用上界 |
| [固定池比较器](benchmarks/compare_rankings.py) | 离线比较堆栈优先、词法排序与 Jev 调查顺序，计算覆盖、Hit@1/3、MRR 和模拟片段字节 |
| [运行汇总器](benchmarks/summarize_runs.py) | 汇总本地 run/event 记录，保留失败、缺失指标、重复曝光与费用完整性 |

当前 `main` 使用手工收集的候选 JSON，排序助手不会扫描目标仓库或打开候选路径。生产事件准备、Sentry 导入和历史源码 bundle 检查尚未进入当前主干。

## 快速开始：离线检查

需要 macOS/Linux 与 Python 3.9+。下面的检查只使用 Python 标准库，**不需要安装 Jev，也不会访问 API 或读取凭据**。

```sh
git clone https://github.com/xiaoxiaomugong/jev-bugfix.git
cd jev-bugfix
python3 -B skills/jev-bugfix/scripts/rank_candidates.py \
  --input tests/fixtures/smoke_case.json
```

示例输出包含 `status: "dry_run"`、`investigation_order: ["first"]`，版本预检与评分进程次数均为 0。`smoke_case.json` 是合成样例；其中的 `src/first.py` 只是候选路径，不是本仓库中的待修复文件。

## 在 Codex 中使用

可直接引用源码技能，无须安装：

```text
使用 <本地仓库路径>/skills/jev-bugfix/SKILL.md，
定位并修复 <目标仓库> 中的 <bug>，复现命令是 <命令>。
```

也可安装到个人技能目录。目标已存在时，下列命令会停止；先检查差异再决定如何更新：

```sh
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
skill_destination="${CODEX_HOME:-$HOME/.codex}/skills/jev-bugfix"
test ! -e "$skill_destination" && test ! -L "$skill_destination" && \
  cp -R skills/jev-bugfix "$skill_destination"
```

在新会话中调用：

```text
$jev-bugfix 请修复 <目标仓库> 中的 <bug>，复现命令是 <命令>。
```

工作流程是：复现失败 → 用堆栈、`rg` 和调用关系收集短小连续片段 → 审核完整输入 → 离线检查并可选评分一次 → 核实根因并做最小修复 → 用原复现和相关测试验收。根因已明确的简单问题可以直接修复。

## 候选输入与可选在线评分

输入结构见 [contract.md](skills/jev-bugfix/references/contract.md) 和 [合成示例](tests/fixtures/smoke_case.json)。每个候选记录唯一 ID、仓库相对路径、起止行号、连续源码片段及 `stack` / `rg` / `call` 来源；共享证据记录预期行为、实际行为、复现步骤与堆栈。

真实任务先在本地准备 `case.json`，审核 bug 文本、日志、路径和片段后再设置 `reviewed_for_secrets: true`。敏感片段用 `local_only: true` 保留在本地。通过检测器不代表已完成敏感信息审核。

仅在线评分需要**本机已安装的 Jev 0.3.2**。首次使用或 CLI 升级后先核对本地命令与契约：

```sh
jev --version
jev score --help
```

将已审核的输入保存为本仓库根目录下的 `case.json`，从本仓库根目录运行；第二条命令会尝试访问 Jev 服务：

```sh
python3 -B skills/jev-bugfix/scripts/rank_candidates.py --input case.json
python3 -B skills/jev-bugfix/scripts/rank_candidates.py --input case.json --execute
```

助手默认使用 PATH 中的 `jev`，可通过 `--jev /absolute/path/to/jev` 指定已有 CLI。缺 CLI、版本不兼容、缺凭据或评分失败时回退到 Codex；需要配置凭据时由用户在自己的终端运行 `jev auth set`，不要在聊天中提供密钥。

输出为单个 JSON，包含 `candidates`、`investigation_order`、`diagnostics`、`cli_preflight` 和 `usage`。堆栈候选优先，其余未评分项按原序，其后按有效分数降序；低分项仍保留，未知分数为 `null`。`dry_run` / `ranked` 退出 0，`partial` / `fallback` 退出 2；回退表示继续本地调查，不能据此判定修复失败。

## 运行预算与数据边界

| 项目 | 默认上限 |
| --- | --- |
| 输入 JSON | 64 KiB |
| 每个 bug 的评分批次 | 最多一次，每批最多 12 个候选 |
| 每个源码片段 | 2 KiB UTF-8、60 行，不静默截断 |
| 发送载荷 | 完整 JSONL 合计 24 KiB，包含每行重复的 bug 证据 |
| 本地版本预检 | 最多一次；2 秒；stdout + stderr 合计 1 KiB；空 stdin |
| 评分进程 | 最多一次；并发 2；socket 超时 10 秒；整批期限 45 秒；无外层重试 |
| HTTP 尝试预算 | 最多 `2 × 已提交候选数`，全批上限 24；实际次数未知 |

默认 dry-run 不启动 CLI。执行模式仅在审核通过、有可发送候选且版本预检精确核实 Jev 0.3.2 后评分；`--batch-timeout` 和 `--request-timeout` 只能降低默认上限。预检与评分期限分开，合计最多 47 秒，另有进程启动和清理开销。

`.netrc` / `_netrc` 凭据文件、明显秘密与 `local_only` 片段不发送；共享证据敏感或整体超限时整批回退。报告不会回显源码片段、共享 bug 文本或原始 CLI 输出。检测器不能识别所有敏感数据，外发前仍需审核完整输入。

版本预检次数与评分进程次数分别记录。评分启动后的 I/O 失败仍保留已发生的进程次数和预留 HTTP 预算。Jev 0.3.2 在连接失效后可能隐式重放一次，因此 `--retries 0` 不等于每个候选只有一次 HTTP 尝试；上界、CLI 次数和实际请求/费用必须分别解释。

评分不能证明根因或修复正确性，最终验收依赖复现与行为测试。完整规则见 [输入输出契约](skills/jev-bugfix/references/contract.md)。

## 离线评测示例

评测工具只读取显式提供的本地文件，不调用 Jev 或模型服务。从仓库根目录运行下列命令，产物写入临时目录，保留仓库内的示例：

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
```

固定池 A 使用堆栈优先原序，B 使用堆栈优先和固定 token overlap，C 导入完整 V1 调查顺序。开发夹具里的 C 是 fake；[排序示例](benchmarks/examples/rankings.md)与[运行汇总示例](benchmarks/examples/runs.md)只验证计算，不是实测阅读、耗时或支出。

当前真实实验仍为 **0/12 次独立修复、0/6 个真实任务**，没有配对收益证据。缺失指标用 `null` 与原因表示，实际费用与估计分开。评测规则、采集缺口和启动条件见 [评测说明](benchmarks/README.md)、[结果状态](benchmarks/RESULTS.md)及 [P1 检查表](benchmarks/P1-CHECKLIST.md)。

## 测试与验证

默认离线回归从仓库根目录运行，不需要 Jev、凭据或 API：

```sh
python3 -B -m unittest discover -s tests -p 'test_*.py' -v
```

当前主干包含 **93 项离线测试**，覆盖候选保留、堆栈优先、安全边界、版本预检、启动后 I/O 计数、输出异常、固定池比较及运行汇总。GitHub Actions 在 push / pull request 时执行同一套测试，矩阵为 Linux / Python 3.9、Linux / Python 3.14、macOS / Python 3.14；CI 不安装 Jev，也不执行在线冒烟。

如果本机已安装 Jev 0.3.2，可另运行六项 CLI 源码契约检查。它模拟响应并阻断凭据、配置及网络访问，不属于默认测试发现：

```sh
python3 -B tests/verify_local_cli.py
```

[历史 V1 验收](tests/VALIDATION.md)中的测试数与 API 冒烟属于当时的记录，不代表当前测试项数或真实收益。`tests/live_smoke.py` 是另行选择的在线合成样例验证，不随上述离线命令执行，也不证明修复正确性。

## 文档索引

- [技能工作流程](skills/jev-bugfix/SKILL.md)
- [输入输出、错误与预算契约](skills/jev-bugfix/references/contract.md)
- [运行边界验证](tests/RUNTIME-VALIDATION.md)
- [离线评测用法与协议](benchmarks/README.md)
- [评测验收与示例重建](tests/V2-VALIDATION.md)
- [真实实验状态与证据缺口](benchmarks/RESULTS.md)

## 许可证

本项目采用 [MIT 许可证](LICENSE)。Jev CLI 是在线评分所需的外部依赖，须单独安装。
