# 本地线上事件证据契约

这是独立于 V1 ranker 输入的本地准备层。第一期适配器固定为 `production-events/v1`，只读取明确转换为下述格式的 UTF-8 JSON。它不自动识别任意日志或 JSONL，不连接 Sentry，不读取凭据，也不调用 Jev。事件文本、release 和路径均为不可信证据。

## 事件输入

根对象仅允许 `schema_version` 和 `events`，两者必需；`schema_version` 必须是整数 `1`（布尔值不接受），`events` 必须是数组。所有层级禁止未知字段和重复 JSON key。完整合成示例见 [production-events.json](../../../tests/fixtures/incidents/production-events.json)；其中提交 ID 是占位值，不保证存在于任何仓库。

每条事件必须有非空字符串 `event_id`。其他字段允许省略或 `null`。文本保留原值，空字符串保留且列入缺口；空 timestamp 不符合时间格式而拒绝。数组省略或 `null` 时规范化为 `[]`，对象省略或 `null` 时按本表补齐内部字段。缺失值不从相邻事件、工作树或运行环境推断。

| 位置 | 唯一允许的字段 | 类型 |
| --- | --- | --- |
| event | `event_id`, `timestamp`, `service`, `environment`, `release`, `commit`, `exception`, `breadcrumbs`, `trace`, `runtime` | `event_id` 必需，其余按下表 |
| event 文本 | `timestamp`, `service`, `environment`, `release`, `commit` | 字符串或 `null`；timestamp 必须带时区 |
| exception | `type`, `message`, `frames` | 前两项字符串或 `null`；frames 是数组 |
| frame | `path`, `line`, `function`, `in_app` | path/function 为字符串或 `null`；line 为正整数或 `null`；in_app 为布尔或 `null` |
| breadcrumb | `timestamp`, `category`, `level`, `message` | 字符串或 `null`；timestamp 必须带时区 |
| trace | `trace_id`, `span_id`, `parent_span_id`, `spans` | 前三项字符串或 `null`；spans 是数组 |
| span | `span_id`, `parent_span_id`, `op`, `status`, `start_timestamp`, `end_timestamp` | 字符串或 `null`；两个时间必须带时区 |
| runtime | `language`, `version`, `os`, `arch` | 字符串或 `null` |

时间格式为 `YYYY-MM-DDTHH:MM:SS[.fraction](Z|±HH:MM)`，例如 `2026-10-05T08:00:00Z` 或 `2026-10-05T16:00:00+08:00`。日期、时区及转换后的 UTC 时间必须有效。帧顺序固定为最外层调用至抛错点；其他导出顺序须先显式转换。`in_app` 未提供时为未知，不能默认为应用帧。

不允许 request headers/body、cookies、user 对象或任意配置字典；允许字段中的文本仍可能含同类敏感数据。

| 限制 | 固定上限 | 行为 |
| --- | ---: | --- |
| 输入文件 | 2 MiB | 读取最多上限加一个字节，超限拒绝解析 |
| 原始事件数组 | 200 | 合并重复事件前计数，超限拒绝 |
| 单事件 frames | 64 | 遍历帧文本前检查，不截断 |
| 单事件 breadcrumbs | 100 | 遍历条目文本前检查，不截断 |
| 单事件 spans | 100 | 遍历条目文本前检查，不截断 |
| 单文本字段 | 4 KiB UTF-8 | 按字节计数，超限拒绝 |
| JSON 容器深度 | 32 | 根容器深度为 1，字符串内括号不计入，解析前检查 |

拒绝非法 UTF-8、孤立 Unicode surrogate、NaN/Infinity（包括溢出的浮点数）、错误类型及无效 JSON。有效 Unicode surrogate pair 转换为其 Unicode 字符后接受。输入失败通过 `EvidenceError` 返回固定 `code`；`str(error)` 只有该代码，不包含文件路径、异常原文或底层错误。原文件保持原位。

## 规范化接口

`load_events(input_path) -> dict` 返回：

```json
{
  "schema_version": 1,
  "adapter": "production-events/v1",
  "input_sha256": "输入原始文件字节的 SHA-256",
  "events": [
    {
      "event_id": "synthetic-event-001",
      "timestamp": null,
      "service": null,
      "environment": null,
      "release": null,
      "commit": null,
      "exception": {"type": null, "message": null, "frames": []},
      "breadcrumbs": [],
      "trace": {"trace_id": null, "span_id": null, "parent_span_id": null, "spans": []},
      "runtime": {"language": null, "version": null, "os": null, "arch": null},
      "provenance": {
        "indices": [0],
        "duplicate_count": 0,
        "missing_fields": ["timestamp", "service", "environment", "release", "commit", "exception.type", "exception.message", "exception.frames", "breadcrumbs", "trace.trace_id", "trace.span_id", "trace.parent_span_id", "trace.spans", "runtime.language", "runtime.version", "runtime.os", "runtime.arch"]
      }
    }
  ],
  "diagnostics": []
}
```

来源索引从 `0` 开始。`missing_fields` 按字段处理顺序列出 null、空文本和空数组；嵌套条目用 `exception.frames[0].line`、`breadcrumbs[0].timestamp`、`trace.spans[0].op` 等路径表示。空数组列为证据缺口，不表示输入语法失败。

内容完全一致的重复 `event_id` 合并为首个事件：`indices` 保留每个原始数组位置，`duplicate_count` 是首条之外的额外重复数量，诊断包含 `identical_events_merged`。比较的是解析后的原始对象（忽略 JSON key 顺序），省略字段与显式 null 不视为完全一致；同 ID 不同内容拒绝为 `conflicting_event_id`。超过 200 条的原始输入不会因可去重而放行。

规范化事件保留未经脱敏的原值，仅在本地内存中用于精确映射。不要直接序列化整个返回值为用户报告或评分载荷。字段白名单不等于秘密审核。

## 分组与选择接口

`select_incident(normalized, event_id=None) -> dict` 返回：

```json
{
  "selected": null,
  "groups": [
    {
      "group_id": "group-001",
      "key": {"service": null, "environment": null, "release": null, "exception_type": null, "frame_path": null, "frame_function": null},
      "event_ids": ["synthetic-event-001"],
      "event_count": 1,
      "missing_fields": ["service", "environment", "release", "exception_type", "frame_path", "frame_function"]
    }
  ],
  "related_events": [],
  "diagnostics": [],
  "status": "needs_input"
}
```

组以 `service/environment/release/exception.type/最内层应用帧 path/function` 六元组精确匹配。最内层应用帧是输入顺序中最后一个 `in_app=true` 的帧；缺应用帧时 path/function 为 null。未知值不作通配符，空字符串不等同于 null；不同组不混合。组顺序及 `group-001` 等标识按首次出现，`event_count` 是去重后的事件数。

多组未指定 `event_id` 时返回 `needs_input`，只列组与缺口，不猜代表事件。显式 ID 精确匹配，找不到则返回 `event_id_not_found`。单组先从有异常 type/message/frames 证据的事件选择；按 UTC 时间从早到晚、同时间按原输入序、无时间排在有时间之后。组内完全没有异常内容时保留最早可用事件，附 `exception_evidence_missing`；后续候选阶段需保留无堆栈缺口。所选对象保留自身全部帧及 provenance。

`related_events` 是同组且与所选事件有相同非空 `trace_id` 的其他事件候选，按同一时间规则排序。空/null trace ID 返回空列表及 `selected_trace_missing`，不得按时间邻近拼接因果。这里不判断 commit 一致，也不宣称完整 trace 或因果链。CLI 必须逐条执行版本解析，将相同 release/trace 但不同提交的项分开展示，未知提交标版本缺口；所选事件的源码候选始终绑定它自己的已解析 commit。

选择诊断码为 `identical_events_merged`、`multiple_incident_groups`、`event_id_not_found`、`no_events`、`exception_evidence_missing`、`selected_timestamp_missing`、`selected_trace_missing`。这些只是固定代码列表，不混入原文。

## 本地派生报告的文本安全

`sanitize_text(text) -> str` 保守脱敏常见 token、API key、password/secret 赋值、授权与 cookie/header 文本、私钥块、URL 密码、邮箱、电话号码、有效 IPv4/IPv6、home/Users 用户目录及凭据路径片段，并屏蔽终端控制及方向覆盖字符。赋值或头部文本可能整行省略；因此报告不是原始日志副本。

`sanitize_value(value)` 递归生成安全副本，包含 dict key、数组中的字符串，不修改原对象。报告所有事件 ID、release、路径、分组键和来源字段都应通过该函数。脱敏不可能识别所有个人信息或业务秘密，不自动设置 `reviewed_for_secrets=true`，也不改变既有 `local_only` 或共享敏感证据阻断。

`escape_markdown(text)` 先 HTML escape，再转义 Markdown 控制字符，禁用事件文本中的 HTML、链接及资源嵌入。Markdown 必须对脱敏后的事件文本调用该函数。两者只改变显示，不执行日志里的命令或指令，也不能把原始事件自动发送给外部服务。候选源码的外发安全沿用 V1 ranker 审核，不能通过改写源码偷偷绕过阻断。

## 输入错误代码

输入异常仅暴露：`input_io_error`、`input_file_over_limit`、`input_encoding_invalid`、`input_depth_over_limit`、`input_duplicate_key`、`input_non_finite`、`input_json_invalid`、`input_schema_invalid`、`input_unknown_field`、`input_type_invalid`、`event_id_invalid`、`text_over_limit`、`timestamp_invalid`、`events_over_limit`、`frames_over_limit`、`breadcrumbs_over_limit`、`spans_over_limit`、`conflicting_event_id`。有多种无效内容时先遇到的检查返回其代码，不承诺一次枚举所有错误。

## Release 映射与版本接口

release 是部署标识。普通版本号不当作 branch/tag/HEAD；事件 `commit` 和恰好符合提交 ID 语法的 release 只接受本地唯一的 4–64 位十六进制提交 ID。对象必须是 commit，多个相同前缀对象也不猜测。明确映射与可选 baseline 的 revision 接受提交 ID 或完整 `refs/tags/...`，不接受任意 Git revision 表达式。tag 当次冻结为完整 commit；不存在的对象、浅克隆缺历史、歧义或非法 revision 都保留固定原因，不自动 fetch。

独立 release-map 格式：

```json
{
  "schema_version": 1,
  "entries": [
    {"service": "billing", "environment": "production", "release": "deploy-2026-10-05",
     "revision": "refs/tags/deploy-2026-10-05"}
  ]
}
```

根与 entry 禁止未知字段和重复 key。`service/environment/release` 必需且允许 null，按原始值的精确三元组匹配；不作通配符。`revision` 必须为非空文本。加载上限 2 MiB、深度 32、单文本 4 KiB，拒绝非法编码及非有限值。重复三元组不静默覆盖，所有匹配 entry 都参与版本核对：不同提交为 `ambiguous`，提供但无法解析的线索也阻断 `resolved`。

`load_release_map(path)` 返回 `{ok, release_map, diagnostics}`。`resolve_incident_version(repo, selected_event, release_map=None, baseline_revision=None, git=None)` 返回：

```json
{
  "resolution": {
    "status": "unknown",
    "event_commit": null,
    "clues": [{"source": "event_commit", "status": "unknown", "commit": null, "diagnostic": "commit_missing"}],
    "diagnostics": ["commit_missing"]
  },
  "checkout": {"head": null, "staged": null, "unstaged": null, "untracked": null,
               "comparison": "unknown", "relation": "unknown", "shallow": null},
  "baseline": null,
  "diagnostics": []
}
```

`resolution.status` 是 `resolved/unknown/ambiguous`，与 checkout 分开。`comparison` 为 `same/different/unknown`；`relation` 从事件提交看 HEAD，分别是 `same/ancestor/descendant/diverged/unknown`。工作区状态失败时布尔值为 null，不能当 clean；detached HEAD 仍可正常核对。源码候选始终来自 event commit，因此 HEAD 不同或 dirty 本身不阻断候选，也不代表候选属于当前工作树。

只有显式 baseline 参数才生成 `{status, commit, changes:[{status,path}], diagnostics}`。它表示用户提供的基线与事件提交间的文件变化，不证明该基线正常或变化引入问题；当前 HEAD 不默认为基线。缺对象、差异命令超时、输出超限或不完整关系分别保留诊断。

`GitSession(repo)` 初始化不启动 Git；同一次 CLI 准备的代表解析、关联事件解析、baseline 和 blob 读取共用一个 session。每个进程 5 秒、stdout+stderr 合计 1 MiB，整个 Git 阶段 20 秒；超时/预算/输出超限不能改写为文件不存在。所有进程 argv 执行受限只读命令，禁 fsmonitor、外部 diff/textconv、partial-clone lazy fetch、replace refs、legacy grafts 与网络协议，不切换分支或写索引/工作树。每次调用前仅查询 filter 配置名，禁用所有 clean/process/required 驱动；查询也计入同一预算，危险名称安全失败，不执行配置中的外部程序。源码 tree 命令固定 `--full-tree`，即使 repo 参数是仓库子目录，也只解释仓库根相对路径。不要执行日志提供的命令。

同组同非空 trace 的关联事件逐条解析：`same_event_commit` 才可进入有限的版本证据顺序；`different_event_commit` 独立展示并标 `related_version_conflict`；`unknown_commit` 标缺口。每条仍保留其 `resolution`，不能把相同 release 当作同一 commit。没有 trace 不按时间接近关联。

## 候选与 sidecar 来源

`collect_candidates(repo, selected, version_report, source_root=None, git=None)` 返回 `{candidates, provenance, frames, unresolved_frames, diagnostics}`。候选仅来自明确 `in_app=true` 的异常帧。所有帧都有处理结果；未知/外部依赖/缺路径行号、缺文件、不支持的构建产物进入 `unresolved_frames`，无堆栈标 `no_application_stack`。版本 unknown/ambiguous 时完全不借用 HEAD 读取源码。

相对路径遵循 V1 规则。Unix 绝对路径与 Windows drive/UNC 路径必须通过显式 source-root 按路径段边界映射；Windows 的 drive/前缀按 Windows 语义核对。拒绝 `..`、`.`、空段、控制字符和 basename/后缀猜测。包含可识别个人信息/秘密、会被脱敏改变的源码路径标 `sensitive_frame_metadata` 缺口，不能进入 case/payload 或伪装成可用于读取的脱敏路径。只从 Git tree 普通 blob 读取，每层检查 symlink/submodule，源码最大 1 MiB、严格 UTF-8 且无 NUL。非 LF/CRLF 的行分隔标 `source_line_separators_unsupported`，避免 V1 `splitlines()` 与异常行号不一致。`dist/build/.next/.nuxt/webpack` 中的帧以及 `.min.*`、`.map` 暂标 `build_artifact_unmapped`，需要显式还原源码后重新导入，首期没有 source maps。

每个片段至多 60 行/2048 UTF-8 字节，连续整行且必须保留异常行；该行单独超限则留下本地缺口，绝不截半行。初始上下文最多异常行前 29 行、后 30 行，按距离收缩以满足字节预算。相同路径与行区间去重并合并 frame 引用。稳定 ID 绑定 commit/blob/path/区间，origin 为 `stack`。V1 中不新增来源字段，独立记录：

```json
{
  "id": "stack_<stable-hash>",
  "event_commit": "完整 commit ID",
  "blob_oid": "完整 blob OID",
  "snippet_sha256": "精确片段 UTF-8 的 SHA-256",
  "path": "src/application.py", "start_line": 1, "end_line": 2,
  "evidence_refs": [{"event_id": "event-001", "event_ref": "event_000", "event_indices": [0], "frame_index": 0}]
}
```

候选源码不进入 provenance/report；敏感源码在本地 case 保留原始连续片段并标 `local_only=true`，旧 ranker 不将其放入发送 payload。无法安全精简的共享摘要阻断 case/评分；脱敏不能替代人工审核。超过 12 个候选、序列化 V1 输入 64 KiB 或实际 JSONL payload 24 KiB 时不生成 case，诊断 `candidates_over_budget` 并保留完整位置清单，要求缩小所选事件或调查范围，不能挑前 12 个伪装成完整池。

后续阅读完整上下文、搜索符号、核实直接调用关系也使用 sidecar 绑定的 event_commit。V1 路径/行号没有版本语义，不能直接用来读 HEAD。缺失 sidecar 必须补齐来源；只有明确拟修改版本后才单独映射当前源码并验证，不能把旧行号套在新版本。

## CLI、产物与状态

```sh
python3 -B skills/jev-bugfix/scripts/prepare_incident.py \
  --input /path/events.json --repo /path/application \
  --release-map /path/release-map.json --event-id event-001 \
  --source-root /srv/application --baseline-revision refs/tags/known-baseline \
  --output-dir /path/new-output
```

四个可选参数按实际证据提供。输出目录只接受新目录或空目录，拒绝 symlink；非空旧目录保持原样，返回 `output_not_empty` 且 `artifacts=null`，明确本轮没有新产物。新产物先在临时 staging 写入，再用不覆盖现有文件的方式安装，case 最后安装；写入失败清理本轮已安装产物，不留下看似成功的旧 case。

| 状态 | 含义 | case | exit |
| --- | --- | --- | ---: |
| ready | 已核实版本，候选无缺口且预算合规 | 有 | 0 |
| partial | 有合格候选，但存在帧、关联版本、checkout 或显式 baseline 的缺口 | 有 | 2 |
| needs_input | 多组需选择、未知/冲突版本、无可用候选或预算需缩小 | 无 | 2 |
| error | 输入、映射或输出失败 | 无 | 2 |

`evidence.json` 顶层为 `schema_version/status/input/events/selected_event/selected_event_ref/selection/version/related_events/timeline/timeline_scope/candidate_provenance/frames/unresolved_frames/summary_omissions/budgets/diagnostics/conclusions`。`input` 记录原文件 hash 与 adapter；`events` 为脱敏规范化白名单值加稳定 `event_ref`，其值以原数组首个索引生成，例如 `event_000`；`selected_event` 仅为脱敏展示 ID，`selected_event_ref` 用于关联代表事件。selection 的 group 同时保存 `event_ids/event_refs`。帧、timeline、关联事件与候选来源均使用 event_ref，不能以可能碰撞的脱敏 ID 关联。所有原始字符串只用于内存中的精确匹配，持久派生字符串经过脱敏。报告不是原日志副本，原文件不改动。

`timeline` 只整理代表事件、其 breadcrumbs/已有 spans 和同版本关联事件；按 UTC 与完整小数精度稳定排列，缺时间在后，不承诺链路完整或因果。其他版本/未知提交在 `related_events` 分开。`report.md` 展示环境、版本、顺序、来源和全部缺口，动态文本先脱敏再转义，不嵌入可执行 HTML、链接或外部资源。终端只输出固定诊断、计数及产物路径，不输出事件原文、原始 Git stderr 或请求信息。

`case.json` 严格调用旧 `validate_case()`，`reviewed_for_secrets=false`。无本地复现时 steps 为“尚无本地复现步骤；仅有导入的线上事件”，actual 明确仅有线上观察，expected 未知；不生成命令、测试或业务预期。bug 摘要遵循 description/actual/expected 各 1024 字节、steps/stack 各最多 8 项且每项 512 字节；摘要省略记录在 `summary_omissions`，候选源码不截断。准备器从不启动 Jev；人工审核后沿原流程评分，原日志/map/provenance 不自动外发。

`conclusions` 默认线上观察 `imported_only`、本地复现 `not_attempted`、根因 `hypothesis`、修复 `unverified`、回归归因 `unknown`。后续人工调查明确区分 `cannot_reproduce/reproduced`、`confirmed`、`verified` 与 `suspected/confirmed/unknown`，必须有各自可核验的证据，不能从 ready、评分或本地正常升级结论。显式 baseline 的变化也不是回归因果证明；运行时只对比明确提供的信息，不转储环境变量。

验收、可再运行的两个 synthetic 演练及未运行范围见 [PRODUCTION-EVIDENCE-VALIDATION.md](../../../tests/PRODUCTION-EVIDENCE-VALIDATION.md)。本期不接 Sentry，不改现有 benchmark schema、success 门槛、P1 0/12 或收益结论。
