# jev-bugfix

源技能在 `skills/jev-bugfix`。Codex 通过复现或本地线上事件证据开始调查，并通过堆栈、rg、调用关系收集片段；Python 标准库助手调用已安装的 Jev 排序；Codex 确认根因、最小修复并跑测试。目标是减少无效代码阅读和保持修复质量，当前没有成本改善结论。

输入是 bug/复现证据和带 ID、路径、行号、来源的候选 JSON；输出是完整调查顺序、逐项评分/错误和调用上界。具体示例及边界见 [contract.md](skills/jev-bugfix/references/contract.md)。默认一批 12 个候选、每段 2 KiB/60 行、发送合计 24 KiB、并发 2、socket 超时 10 秒、整批 45 秒、无外层重试；最多 24 次底层 HTTP 尝试。堆栈候选优先，评分不能永久排除候选或证明修复正确。

## 安装与调用

执行评分需要 Python 3.9+（macOS/Linux）和本机 Jev 0.3.2；本地证据准备及 dry-run 无须启动 Jev，准备器另需本地 Git。核对命令只访问本机：

```sh
jev --version
jev score --help
```

克隆本仓库：

```sh
git clone https://github.com/xiaoxiaomugong/jev-bugfix.git
cd jev-bugfix
```

直接在 Codex 中调用源码，无须安装：

```text
使用 <本地仓库路径>/skills/jev-bugfix/SKILL.md，定位并修复：<bug 描述及复现命令>。
```

安装到个人技能目录（目标已存在时先检查差异，避免覆盖）：

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

助手可单独运行，`--execute` 才调用外部服务，默认只检查输入和预算：

```sh
python3 skills/jev-bugfix/scripts/rank_candidates.py --input tests/fixtures/smoke_case.json
python3 skills/jev-bugfix/scripts/rank_candidates.py --input tests/fixtures/smoke_case.json --execute
```

缺凭据时直接回退到 Codex。若需要配置，用户在自己的终端运行 `jev auth set`；不要在聊天中提供密钥。

## 本地线上事件入口

“线上报错、本地正常”或暂时无法复现时，可先导入明确格式的 `production-events/v1` UTF-8 JSON。准备层仅用 Python 标准库、本地文件和只读 Git，不需要 Jev 或凭据；格式、完整合成样例和限制见 [生产证据契约](skills/jev-bugfix/references/production-evidence.md)。其他日志格式必须先显式转换。

以下为参数模板，须替换成本地输入、仓库、映射和新输出目录。

```sh
python3 -B skills/jev-bugfix/scripts/prepare_incident.py \
  --input /path/events.json --repo /path/application \
  --release-map /path/release-map.json --event-id event-001 \
  --source-root /srv/application --output-dir /path/new-output
python3 -B skills/jev-bugfix/scripts/rank_candidates.py \
  --input /path/new-output/case.json
```

`--release-map`、`--event-id`、`--source-root` 按实际证据提供；已知正常版本可另传 `--baseline-revision <本地提交或完整 tag>`。release 是部署标识，不能默认当作 Git tag 或 HEAD；没有显式基线时不做升级归因。多组事件需要明确选择，缺版本映射、路径映射或历史对象时，按报告补齐本地来源再运行，不自动 fetch。

输出目录须为新目录或空目录。`evidence.json` 保存代表事件、显式运行时信息、版本核对、候选 provenance 和缺口，`report.md` 供本地调查；仅有合格且未超预算的候选时生成严格 V1 `case.json`。`ready` 为 exit 0，`partial/needs_input/error` 为 exit 2；部分成功须继续处理缺口。这些状态只描述证据准备。生成的 case 默认 `reviewed_for_secrets=false`，脱敏后仍须人工审核，才可沿原流程外发评分；原始日志、release-map 与 sidecar 不自动外发。

**V1 case 不带版本信息。** 后续阅读上下文、搜索符号与核对调用关系，必须使用 sidecar 中候选绑定的 `event_commit` 和 blob；不能直接按 case 的路径/行号读 HEAD。HEAD 不同或 dirty 仍可准备事件版本候选。丢失 sidecar 时先补齐来源；明确拟修改哪个版本后，再单独映射到当前源码并重新核验。

报告分别记录线上观察、本地复现 `not_attempted/cannot_reproduce/reproduced`、根因 `hypothesis/confirmed`、修复 `unverified/verified`、回归归因 `suspected/confirmed/unknown`。只比较明确提供的运行时/配置证据，不转储环境变量。线上事件可支持假设与调查顺序；本地未报错不能证明线上已经修复。

首期不连接 Sentry，不支持任意日志、source maps、自动 bisect 或自动修复。两个明确标为 synthetic 的离线演练及可重建产物见 [生产证据验收](tests/PRODUCTION-EVIDENCE-VALIDATION.md)，它们不计入原 P1 benchmark。

## 测试与验收

V1 历史验收：41 项离线回归、5 项本机 CLI 契约检查、独立修复演练和真实 API 冒烟均通过，见 [验收记录](tests/VALIDATION.md)。这些记录不证明定位效率或成本收益。

[运行边界验证说明](tests/RUNTIME-VALIDATION.md)。执行评分前运行 2 秒／1 KiB 的本地版本预检，仅验证为 Jev 0.3.2 时评分。默认 dry-run 不启动 CLI；版本预检和评分进程次数分开记录。启动后的 I/O 失败仍保留评分调用和 HTTP 预算上界。

本地评测工具提供固定候选池 A/B/C 比较和运行／事件汇总，均完全离线。协议与用法见 [benchmarks/README.md](benchmarks/README.md)，验证说明见 [V2-VALIDATION.md](tests/V2-VALIDATION.md)。开发夹具只验证计算；[P1 状态](benchmarks/RESULTS.md)仍为 0/12，真实阅读、时间和成本收益尚无配对证据。

离线测试不读凭据、不访问 API，用真实 subprocess 边界和 fake CLI 验证输出处理：

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

GitHub Actions 会在 push / pull request 时运行上述离线测试，覆盖 Linux、macOS 与 Python 3.9 / 3.14；不安装 Jev，也不运行真实 API 冒烟。

助手和离线单元测试只用 Python 标准库。如果本机有 Codex 自带的 skill-creator 校验器，还可检查技能结构（需要 PyYAML）：

```sh
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" skills/jev-bugfix
```

历史结构校验使用临时目录中的 PyYAML，未修改系统 Python。

另外提供针对**已安装 CLI 源码**的离线契约检查，模拟 API 返回并阻断凭据读取和网络；它不在默认测试发现中，也不要求安装另一份 Jev：

```sh
python3 tests/verify_local_cli.py
```

此检查限定 Jev 0.3.2，验证真实命令解析、成功/错误 JSONL、缺凭据处理及隐式重放上界。未知版本应重新核对契约，期间回退到 Codex 调查。

真实 API 验证单独执行，只发送合成样例，一候选、一次 CLI 调用、底层请求上界两次。网络权限/本机凭据由运行环境决定，失败记录回退；它验证集成契约，不验证修复正确性：

```sh
python3 tests/live_smoke.py
```

| 验收案例 | 预期行为 |
| --- | --- |
| 正常评分且顺序颠倒 | 按回显 ID 核对，稳定降序排列 |
| 堆栈片段得低分 | 仍优先调查；低分候选全部保留 |
| 批次成功/错误混合、exit 2 | 保留有效评分，逐项记录错误 |
| 畸形、缺失、重复或回显不一致 | 标记未知/异常，不能错配或置零 |
| 非法 Unicode 输入、极大数值响应 | 返回结构化错误；异常响应不影响其他有效评分 |
| 服务分数与概率存在舍入差异 | 允许有限舍入余量；明显冲突仍标为异常 |
| 缺 CLI、缺凭据、超时、输出过大 | 有界终止并回退，记录事实 |
| 超候选数、超发送量、敏感共享证据 | 不发请求；超限/敏感片段留本地 |
| 隔离仓库修复演练 | 复现失败 → 确认根因 → 最小修复 → 测试通过，评分异常仍能完成 |

无法自动识别所有敏感信息，外发前仍需由 Codex 核对完整输入。尚未证明能减少实际阅读量或成本，需要后续真实任务的对照证据。

## 许可证

本项目采用 [MIT 许可证](LICENSE)。Jev CLI 是需单独安装的外部依赖。
