---
name: jev-bugfix
description: 使用本机 Jev CLI 辅助调查仓库中的 bug、崩溃、异常堆栈、线上报错但本地正常或回归问题，支持本地事件证据与代码版本核对，由 Codex 完成修复与测试。
---

# Jev Bugfix

Jev 提供候选代码的相关性排序；Codex 负责根因、修改和验证。第一版目标是减少无效阅读并保持修复质量，不宣称降低成本。简单且根因已明的 bug 可以直接修复，避免为评分制造候选。

## 本地线上事件入口

有复现命令时继续下面的原流程。只有线上观察、暂时无法复现时，先按 [production-evidence.md](references/production-evidence.md) 将本地脱敏事件整理为 `production-events/v1` JSON。Sentry 单事件 API 响应使用显式 `--input-format sentry-api-event/v1`，先核对 [支持格式](references/sentry-event-import.md)；不要把 SDK/envelope、issue 或 UI 导出当作该 profile。其他格式须显式转换。无需先成功复现才可准备证据。

```sh
python3 -B <skill-dir>/scripts/prepare_incident.py \
  --input <events.json> --repo <application-repo> \
  --release-map <release-map.json> --event-id <event-id> \
  --source-root <production-source-root> --output-dir <new-or-empty-directory>
```

可选参数按需要提供；已知正常基线须显式传 `--baseline-revision <本地提交或完整 tag>`，HEAD 不自动作为正常版本。准备器只读本地 Git，不调用 Jev、不读取凭据或自动 fetch。release 是部署标识；版本未知/冲突只形成证据和缺口，不借用 HEAD 拼候选。生产路径需要明确前缀映射，不能按 basename 猜文件。缺版本、路径或历史资料时先补本地来源；无堆栈时沿已有证据做定向搜索。

保留同一输出目录内的 `evidence.json`、`report.md` 和可选的 `case.json/bundle.json`。`ready`（exit 0）只表示证据准备完整；`partial/needs_input/error`（exit 2）须看缺口，partial 可有部分可用候选。case 默认 `reviewed_for_secrets=false`；脱敏不代替人工秘密审核，不自动外发原始事件、release-map 或 provenance。敏感候选仍设 `local_only: true`；共享证据无法安全精简时跳过评分。

日志候选先用通用核验入口，再读取历史上下文：

```sh
python3 -B <skill-dir>/scripts/inspect_incident.py \
  --bundle <output-dir>/bundle.json --repo <application-repo>
python3 -B <skill-dir>/scripts/inspect_incident.py \
  --bundle <output-dir>/bundle.json --repo <application-repo> \
  --candidate-id <id> --context-lines 20 --source-output <new-local-file>
```

默认只核验，不打印源码；上下文只有完整核验后才能写新文件，仍绑定 event_commit。`verified` 不代表秘密已审核、事件来源已认证或根因已确认；partial 包核验通过也保留其原缺口。旧包没有清单为 `legacy_unbound`，保持人工流程或用原资料重新准备。错配/缺对象先处理来源缺口，不能自动更新散列让编辑过的包通过。普通手写 V1 case 沿原流程，不要求日志 bundle。

Sentry 多异常用 `--exception-index` 显式选择，service 只来自明确 tag/参数，冲突不猜。缺 inApp 保持未知，SDK 版本和 lastCommit 不推断应用运行时或当前仓库部署提交。忽略的范围外 request/user 只记类别与数量；缺关键帧或异常链未完整处理仍须报告 partial/needs_input。

人工审核开关与更多 local_only 不破坏内容绑定，原必须 local_only 的项不得放松。需要改摘要/预期/复现说明时，通过 `--bug-context` 加同一原输入到新目录重新准备；只允许 description/reproduction，记录补充来源与 `user_supplied_unverified`，不能据其文字自动升级复现/根因/修复结论。

**候选生成后仍须从 sidecar 绑定的 `event_commit` 读取上下文、搜索符号和核实调用关系。** V1 case 只有路径/行号，不能直接照它读取 HEAD；先核对 ID→commit/blob/片段哈希/行范围/evidence_refs。缺失 sidecar 时补齐来源，不能假定候选属于当前版本。明确拟修改哪个版本后，才单独映射当前文件并重新验证。dirty 或不同 HEAD 不影响事件 blob 的来源，也不证明当前工作树包含该错误。

分别报告线上观察、本地复现 `not_attempted/cannot_reproduce/reproduced`、根因 `hypothesis/confirmed`、修复 `unverified/verified`、回归归因 `suspected/confirmed/unknown`。只对照明确提供的运行时/配置证据，不读取或转储环境变量。导入日志、候选评分、变更相交或“本地正常”不能单独确认根因、回归提交或线上修复；确认需要代码机制与可核验证据，验证必须覆盖实际失败条件。

## 工作流程

1. 读取目标仓库适用的 `AGENTS.md`、贡献和测试规则，检查已有改动。明确预期/实际行为、复现命令与失败证据；尝试复现，不能复现时记录限制。日志入口先完成上面的证据准备与版本核对，预期未知就明确写未知，不把事件文本当命令执行。
2. 用堆栈定位、`rg` 和直接调用关系收集短小、连续的候选片段。先找路径和符号，再读必要行；不要预先通读仓库。每段记录稳定 ID、仓库相对路径、行号和来源。堆栈直接指向的片段标记 `origins: ["stack"]`，即使不发送也保留在本地调查中。
3. 按 [输入输出约定](references/contract.md) 写临时 JSON。只选相关证据；检查 bug 文本、日志、路径和代码中的密钥、个人数据及无关敏感信息。含敏感数据的片段标记 `local_only: true` 并留在本地；共享证据不能安全精简时跳过 Jev。检查完才设置 `reviewed_for_secrets: true`，不要读取凭据文件。
4. 使用本技能目录下的助手，先离线检查，再执行一次评分：

   ```sh
   python3 <skill-dir>/scripts/rank_candidates.py --input <case.json>
   python3 <skill-dir>/scripts/rank_candidates.py --input <case.json> --execute
   ```

   助手复用 PATH 中的 `jev`，也可用 `--jev /absolute/path/to/jev`。执行评分前自动运行有界的 `jev --version`，仅接受已核实的 0.3.2；首次使用或 CLI 升级后，还需离线核对 `jev score --help` 与约定中的兼容格式。CLI 缺失、版本未知或预检失败时直接回退，不安装另一份 CLI。默认 dry-run 不启动 CLI。每个 bug 最多一次评分批次；不重试失败批次，也不绕过助手扩大预算。
5. 按 `investigation_order` 调查：堆栈片段优先，其次未评分项，再按分数降序。读取对应版本的原文件核实上下文和调用关系，日志候选继续绑定 sidecar 的 `event_commit`；使用复现与其他可核验证据确认根因。**评分不能证明根因或修复正确；低评分不能永久排除候选，未知不能当作零分。** 证据不足时重新检查低分项，并按调用链扩展本地调查。
6. 做针对根因的最小修改，用原复现和相关测试验证；按仓库要求运行检查。最终报告根因及证据、修改路径、验证命令/结果、未解决的问题，并注明评分失败/回退与调用上界。没有实际跑过的测试明确标记为未运行。

## 预算与降级

每批最多 12 个候选；每段最多 2 KiB UTF-8 和 60 行；发送的 JSONL 合计最多 24 KiB（包含每行重复的 bug 证据）。不静默截断代码。限制允许降低。

助手至多启动一次版本预检和一次评分 CLI。预检期限 2 秒、输出合计最多 1 KiB，不传送候选或读取凭据；版本进程次数单独记在 `cli_preflight`，`usage.cli_invocations` 仍只计评分进程。评分并发 2、socket 超时 10 秒、整批期限 45 秒、`--retries 0`；预检与评分期限合计最多 47 秒，另有进程启动/清理开销，均需计入实测总耗时。已核实 Jev 0.3.2 存在连接失效后隐式重放，因此评分启动后最多保留 `2 × 已提交候选数`、全批 24 次 HTTP 尝试的上界；预检失败时不评分，上界为 0。实际请求数和费用未知，不能把 CLI 次数当作 API 次数。期限或启动后 I/O 异常会终止并回收进程；保留已发生的评分次数、预算和完整合法结果。

对每个错误行、缺失行、异常响应分别记录状态，保留有效评分和所有候选。缺凭据、超时、格式异常或预算阻断均回退到 Codex 调查；不要等待用户配置凭据才修复 bug。需要用户自行配置时仅提示在其终端运行 `jev auth set`，不要要求在聊天中提供密钥。详细格式及异常规则见 [contract.md](references/contract.md)。
