# Sentry 单事件 API 离线导入契约

Profile：`sentry-api-event/v1`；adapter version：`1`；mapping version：`1`。
只导入用户在本地保存的 **Retrieve an Event for a Project** 单个响应对象。必须显式选择该 profile。SDK event、envelope、issue 聚合、事件数组均不受支持；UI Download JSON 不自动等同于本 profile。即使一个文件结构相似，也不能靠本地解析证明它由该端点产生。

`load_sentry_api_event(path, exception_index=None, service=None)` 永远离线：不读取凭据，不调用 Sentry、Git、Jev 或网络，不下载 URI。精确 release/path 仅在内存中交给既有版本解析与路径映射；产物继续走 `sanitize_value`，不得保存原始中间对象。默认入口 `load_events()` 及 production-events/v1 严格字段规则不变。

## 固定查阅依据

查阅日期：2026-10-05。仅使用以下官方资料。

- [Retrieve an Event for a Project](https://docs.sentry.io/api/events/retrieve-an-event-for-a-project/)：身份、时间、entries、release 和完整响应示例。查阅页 HTML 原字节 SHA-256：`220f5de69ae5938b87d5d68eba32c43ddf0204ca5d514eb13464221ffce0d2c2`。示例 SHA-256：`424207324eb9cb12ccaad5f0b165e44a3564f62662ccfaa9f410829fdab63610`（HTMLParser 提取含 eventID 的 pre 文本，移除 UI 的 Copied 前缀，从首个 `{` 用 JSONDecoder.raw_decode 取到对象结尾，再以 UTF-8 编码；不含尾随换行）。官方示例只用于 schema 核实，不作为生产事故。
- [event serializer，固定提交](https://github.com/getsentry/sentry/blob/e54d88cf6b77fc44832be3bf341466192de7f7bd/src/sentry/api/serializers/models/event.py)：`get_entries()`、`EventSerializer.serialize()`、`__serialize_error_attrs()`、`_get_release_info()` 与 `SqlFormatEventSerializer.serialize()`。源码原字节 SHA-256：`c7b9eb286793579df3c1b7164680c08baf9fb97c163190601431ba3165b7e420`。
- [stacktrace，固定提交](https://github.com/getsentry/sentry/blob/e54d88cf6b77fc44832be3bf341466192de7f7bd/src/sentry/interfaces/stacktrace.py)：`Frame.get_api_context()`、`Stacktrace` 字段说明、`get_api_context()` 与 `get_stacktrace()`。源码 SHA-256：`acd0de9026be56abd9756848cbb560354b01ddc982f5c060f8dc11d66bdf087a`。API 数组按原顺序序列化，较早调用在前；UI/字符串展示可单独反转。本适配器不反转。
- [exception，固定提交](https://github.com/getsentry/sentry/blob/e54d88cf6b77fc44832be3bf341466192de7f7bd/src/sentry/interfaces/exception.py)：异常项的 `type`/`value`、处理后及 raw 堆栈、`excOmitted`。源码 SHA-256：`021f41090d4bb158d5086103fa36e39ef2eb88d5815d727b29aa51192abf2342`。
- [Contexts Interface](https://develop.sentry.dev/sdk/foundations/envelopes/event-payloads/contexts/)：默认 runtime/os context 的 `name`/`version`。查阅页 HTML SHA-256：`f18c49d03ec9d9db1a550bc56bca5ef4827d40f912ed8b4b6f4f880284f96486`。只读已明确的默认 `runtime`/`os` 键；不解析 raw_description，也不从 SDK 版本推断运行时。

以上固定源码均来自 getsentry/sentry 提交 `e54d88cf6b77fc44832be3bf341466192de7f7bd`。页面内容可更新，哈希记录本次查阅版本；它们不证明输入文件来源。

## 字段映射

| 源字段 | production-events/v1 投影 | 缺失、冲突与来源处理 |
| --- | --- | --- |
| `eventID` | `event_id` | 必须是非空字符串；不回退到 `id`，也不要求二者相等。API 文档及 event serializer。 |
| `dateCreated` | `timestamp` | 缺失留 null 并记 partial；不回退 `dateReceived`；沿用带时区 ISO 时间校验。event serializer 将事件发生时间与接收时间分别序列化。 |
| `tags[key=service].value` 或参数 service | `service` | 同值重复可合并；重复冲突或参数与 tag 冲突留 null、needs_input。缺失 partial。`projectID`/project 不代表应用 service。API tags 结构见 event serializer；参数优先级是本 profile 明确规则。 |
| `tags[key=environment].value` | `environment` | 同值重复可合并；不同值冲突留 null、needs_input；缺失 partial。不从 release 的 lastDeploy 推断。 |
| `release.version` | `release` | 保留精确发布标识，不要求是 SHA。缺失 null、partial。release.lastCommit 不绑定当前目标仓库。API 响应与 release serializer 接入。 |
| 无映射 | `commit=null` | 后续显式 release-map 继续负责目标仓库版本。 |
| `entries[e].data.values[x].type/value` | `exception.type/message` | e 必须是唯一 exception entry；多项要求显式 0-based exception_index。无异常可报告但 partial；重复 exception entry 报 `sentry_unsupported_shape`。exception API serializer。 |
| 选中异常的 `stacktrace.frames[i]` | `exception.frames[i]` | 只用处理后 stacktrace；不混合或回退 rawStacktrace；原序和保存文件数组索引不变。无帧 partial。 |
| frame `filename`，仅缺失/null 时 `absPath` | frame `path` | 空字符串 filename 不回退。URI 保留为未解析证据、partial；不下载。普通本地路径仍须通过既有 source-root/Git 安全映射。Frame API serializer。 |
| frame `lineNo/function/inApp` | frame `line/function/in_app` | 严格类型；缺 path/line/function/inApp 均 partial。缺 inApp 保持 null，绝不变 true。 |
| `framesOmitted`、`excOmitted` | 不拼接缺失内容 | 单一 `[start,end]` 范围计数为 end-start；非负整数上限 2,147,483,647。有遗漏 partial。原帧索引是保存数组的位置，不推算已被服务端删除的完整栈位置。Stacktrace/Exception serializer。 |
| `entries[b].data.values[i].timestamp/category/level/message` | `breadcrumbs[i]` | 只保留四字段；data 不导入。data 非空且 message 缺失/空时记录 `sentry_breadcrumb_details_unimported`、partial。重复 breadcrumbs entry 为未支持结构。API 示例。 |
| `contexts.trace.trace_id/span_id/parent_span_id` | `trace` 同名字段 | 原样保留可用 ID；无 span 时 spans=[]；缺 trace 允许未知，不伪造链路。出现 spans entry 但本 profile 不转换时 partial。API 示例与 contexts。 |
| `contexts.runtime.name/version` | `runtime.language/version` | language 槽保留实际运行时名称（例如 CPython），不从 platform/sdk 推断；缺项 null。Contexts Interface。 |
| `contexts.os.name` | `runtime.os` | 缺项 null；V1 无单独 OS 版本槽，其他 OS 数据忽略。Contexts Interface。 |
| 无映射 | `runtime.arch=null` | device 的 CPU 架构可能与应用架构不同，本 profile 不推断；custom context 键名不导入。 |

## 有界读取、忽略与完整性

原文件最多 2 MiB，容器深度 32。读取拒绝重复 JSON key、非法 UTF-8/Unicode surrogate、NaN/Infinity 和浮点溢出。entries/tags 各最多 200，exception values 最多 32。选中异常投影最多 64 帧，breadcrumbs 最多 100，保留文本最多 4 KiB UTF-8；超限拒绝，不截断。未选异常不导入，其原始堆栈不套选中投影的 64 帧/4 KiB 上限；选择该异常后再应用投影限制。

request/user/headers/cookies、frame vars/context/sourceLink、context/extra、breadcrumb.data、rawStacktrace 和其他未知字段均不保留原文。忽略内容只受文件、深度和 JSON 合法性限制；大块 request 不因 4 KiB 投影限制被误拒。忽略字段名也不能进入诊断，避免名字本身含秘密。范围外 request/user 等内容只有固定类别计数，不单独把事件降为 partial。缺所需调查字段、未处理异常链、服务端漏帧或不支持的帧路径会降级，即使后续仍能生成候选。

返回 normalized 的旧 `schema_version/adapter/input_sha256/events/diagnostics`，附加 `status`、固定 code 的 `incomplete` 和独立 `import_provenance`。conversion status 为 ready 表示本 profile 所支持调查字段完成投影，仍须版本解析和候选检查；多异常待选择或 tag 冲突为 needs_input，其他转换缺口为 partial。完整性字段不是根因、部署或修复验证。

省略计数的口径：request/user 为非空对象或 entry 出现次数；headers/cookies/extra 为容器顶层项数（其他非空值计 1）；frame vars/context/sourceLink、breadcrumb data、raw stack 为已检查位置的非空字段次数；unknown_fields 为已检查白名单对象中未识别字段的数量，unknown_entries 为非 exception/breadcrumbs/request entry 数量；unselected_exceptions 与服务端 omission 为项数。选中堆栈检查帧忽略类别；未选链仅记录异常项数，不窥读其帧原文。所有计数有界，均不输出用户键名。

## import_provenance 与散列

独立 sidecar 字段不进入严格 V1 case。固定结构为：

```text
schema_version=1; adapter; adapter_version; mapping_version
source_sha256; projection_sha256
exception_selection: entry_index, exception_index, exception_count,
                     unselected_count, unselected_indices
field_sources: event_id, timestamp, service, environment, release,
               exception, breadcrumbs, trace, runtime, os
frames[]: normalized_index, entry_index, exception_index, frame_index, path_source
omissions: request, user, headers, cookies, frame_vars, frame_context,
           frame_source_link, raw_stacktrace, extra, breadcrumb_data,
           unknown_fields, unknown_entries, unselected_exceptions,
           sentry_omitted_frames, sentry_omitted_exceptions
diagnostics[]; completeness: status, incomplete[]
```

source 位置仅由固定字段路径和已验证整数索引组成；path_source 是 filename、absPath 或 null。source_sha256 对外部原文件字节计算，等于 normalized.input_sha256。projection_sha256 对 `{schema_version:1,events:[规范化事件]}` 计算，排除每个事件的 provenance/event_ref 与所有外层哈希/import/status 元数据；使用 UTF-8、ensure_ascii=false、sort_keys=true、separators=(',', ':'), allow_nan=false。数组顺序保留。两种散列命名不同，不能互相替代；它们是本地一致性记录，不是来源认证。

## 合成契约样例

`tests/fixtures/incidents/sentry-api/` 的 complete.json、multi-exception.json 和独立手工预期 expected-projection.json 均为 **schema_fixture**：按照上述公开格式重建的合成值，不是实际导出，不是线上事故，也不算真实生产验收。fixture-manifest.json 记录固定文件字节与预期投影哈希。测试对独立预期结果及来源/投影双哈希断言，另覆盖范围外秘密 marker、异常链选择、URI、严格类型和全部读取上限。
