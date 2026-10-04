# 评分输入与输出约定

适用于 Python 3.9+、macOS/Linux 和本机 Jev CLI 0.3.2；不依赖另一个 skill，也不直接实现 Jev API 客户端。CLI 升级后重新核对 `--version`、`score --help` 和下述格式。无法核实时使用 Codex 本地调查。

## 输入

助手读取 `--input` 指定的 UTF-8 JSON 文件，最多 64 KiB。候选的 `path` 是目标仓库相对路径，助手不会打开候选文件。必须人工检查片段、路径、行号及敏感信息。

```json
{
  "schema_version": 1,
  "reviewed_for_secrets": true,
  "bug": {
    "description": "空列表调用 first() 时抛出 IndexError",
    "reproduction": {
      "steps": ["python3 -m unittest tests.test_first"],
      "expected": "空列表返回 None",
      "actual": "IndexError: list index out of range"
    },
    "stack_trace": ["src/first.py:1 in first"]
  },
  "candidates": [
    {
      "id": "first", "path": "src/first.py",
      "start_line": 1, "end_line": 2,
      "snippet": "def first(items):\n    return items[0]",
      "origins": ["stack", "rg"]
    }
  ]
}
```

- 顶层、bug、reproduction、候选均只接受示例中的字段；候选另允许布尔 `local_only`。拒绝未知字段，避免意外附带整份配置或日志。
- `id` 是唯一的 ASCII 标识（字母、数字、下划线、连字符，1–64 字符）。`path` 使用 `/`，不得绝对路径、`..`、反斜杠或控制字符。行号是正整数、首尾含在内，并与片段连续行数一致。来源是 `stack`、`rg`、`call` 的非空无重复列表。
- `description` 最多 1024 UTF-8 字节；`expected`、`actual` 各最多 1024；复现步骤最多 8 条、每条最多 512；堆栈最多 8 条、每条最多 512。空堆栈允许；不能复现时在 actual 中说明事实，不编造证据。
- 超过 12 个候选时整批回退，保留候选元数据；单段超 2048 字节或 60 行则仅在本地调查。发送总量为完整 JSONL 的 UTF-8 字节数，超过 24576 时整批回退，绝不静默截断。
- `local_only: true` 不会发送片段。明显密钥模式或凭据文件路径也会阻断发送；其中任意目录下名为 `.netrc` / `_netrc` 的文件均只在本地调查，不依赖片段是否包含完整的 `login` / `password` 字段。检测器只是补充，无法识别所有秘密或业务敏感信息。先人工删除无关数据，不能因通过检测就认为安全。bug 的共享证据含明显秘密时整批回退；安全审核为 false 时执行模式回退。报告不回显片段、bug 文本、原始 CLI 输出或原始错误信息。

## 调用与原始 CLI 契约

每个可发送候选组成一个完整 JSONL state：

```json
{"schema_version":1,"bug":{"description":"...","reproduction":{"steps":["..."],"expected":"...","actual":"..."},"stack_trace":[]},"candidate":{"id":"first","path":"src/first.py","start_line":1,"end_line":2,"snippet":"...","origins":["stack"]}}
```

不发送 `reviewed_for_secrets` 和 `local_only`；无整库扫描、文件上传、日志上传或环境变量转储。标准输入传送 JSONL；subprocess 使用 argv，禁用 shell。准确命令为：

```sh
jev score '<固定的相关性问题与 0–4 刻度说明>' --range 0-4 \
  --lines --json --jobs 2 --retries 0 --timeout 10
```

保留本机 provider/model 选择，不改动用户配置。API key 由 Jev 自行读取；助手不读取 key 文件、不输出 stderr 原文。凭据错误记录为 `credentials_missing` / `authentication_error`，然后本地继续。

Jev 0.3.2 的成功 JSONL 行是 envelope（不是直接的 answer）：

```json
{
  "input": {"schema_version":1,"bug":{},"candidate":{}},
  "answer": {
    "type":"score", "score":2.7, "value":2.7, "label":"3",
    "legend":{"0":"0","1":"1","2":"2","3":"3","4":"4"},
    "probabilities":{"0":0.05,"1":0.1,"2":0.15,"3":0.5,"4":0.2},
    "confidence":0.8
  }
}
```

此示例只说明形状，并非真实响应。API 的 score 是从零开始的期望索引；`--range 0-4` 的 value 与 score 相同。助手验证有限数、范围、legend、概率及一致性，输出 `score = answer.score / 4`，范围 0–1。label 为最大概率等级，confidence 保留原值，均不作为正确性证明。

真实服务曾返回 `score=value=3.99`、概率 `{"0":0,"1":0,"2":0,"3":0,"4":1}` 和 `confidence=0.99`。因此不能用展示的概率精确重算 score。第一版按各字段独立舍入到百分位预留兼容余量：五项概率和误差最多 0.025、期望分数误差最多 0.055（均加 1e-9 浮点余量）；score/value 差异仍最多 0.001。此容差是基于观测的兼容选择，不是后端精度保证。保留服务返回的 score，不自行重新归一化概率或改写分数；明显冲突仍回退。

错误行是 `{"input":"原 JSONL 行字符串","error":"错误信息"}`。逐行模式中成功结果 input 是对象，失败结果 input 是字符串。存在任一行失败则 exit 2，仍会输出其他行；全成功 exit 0。致命配置/响应异常也可能 exit 2 且没有结果。空输入行被跳过，不能用物理行号作为结果身份。

助手解析回显 input 的 candidate.id，并核对完整 state 等于原提交内容；错误回显先解析成 JSON。不要使用 `--field` 丢失身份。重复身份作废该候选；未知身份、畸形行或回显不一致记录诊断。每个未返回的候选标记 `missing_result`（超时或输出过大时记相应原因），不会当作零分。完整合法行可在其他行失败后保留。

默认一次 CLI 评分调用、12 次逻辑评分、至多 24 次物理 HTTP 尝试，CLI 不暴露实际物理尝试数。`--retries 0` 关闭外层重试，但源码 `http_post` 会在复用连接失效后重新 POST 一次。该预算基于核实的 0.3.2，不适用于未知版本。`--timeout` 是 socket 超时，因此额外用 45 秒进程期限；stdout+stderr 收集上限合计 64 KiB，异常时终止进程组。`--batch-timeout` / `--request-timeout` 只能降低默认上限。

## 助手输出

stdout 单个 JSON 对象，`schema_version: 1`：

- `status`：`dry_run`（未执行）、`ranked`（全部评分成功）、`partial`（有有效评分且有错误或本地项）、`fallback`（无可用评分）。
- `candidates`：每个输入候选的元数据、`must_inspect`（来源包含 stack）、`score`（数值或 null）、`label`（等级字符串或 null）、`confidence`（原始数值或 null）、`status`（`pending/scored/local_only/error`）、`error`（null 或 `{code,message}`）。未评分或结果失效时三个评分字段均为 null；低分项仍在列表中。
- `investigation_order`：所有 ID，先 stack，随后未评分项按原序，再按分数降序；同分保持原序。全部回退时保留原序，但 stack 提前。
- `diagnostics`：批次/解析问题的固定 code/message，原始错误内容被抑制。消息是助手事实，不是 Jev 的自由文本解释。
- `usage`：`cli_invocations`（评分进程次数）、`submitted_candidates`（交给该进程的候选数）、`http_attempts_upper_bound`（预算预留，非实测）、`payload_bytes`（准备好的载荷大小）、`batch_timeout_seconds`、`request_timeout_seconds`。未启动 CLI 时前三者为 0；CLI 因缺凭据或提前终止而没有发出请求时，也会保留进程次数与预留预算，不能将其视为真实远程调用数。不报告未经测量的费用或收益。

dry_run / ranked exit 0；partial / fallback exit 2。无效命令行参数由 argparse 返回 exit 2。回退不是修复失败，继续用 Codex 的复现与调用链调查。无效输入可能无法恢复元数据，此时候选为空，仍保留本地收集的原候选继续调查。

最终修复报告另由 Codex 输出：根因和支持证据；最小修改及路径；实际验证命令、结果和未运行项；未解决的问题；评分/回退情况及调用上界。API 评分不构成修复验收证据。
