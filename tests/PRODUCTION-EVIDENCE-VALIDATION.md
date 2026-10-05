# 本地生产事件证据验收与重建

本层接收明确的 `production-events/v1` 本地文件，执行严格规范化、代表事件选择、release→commit 与 checkout 核对，并从事件提交的 Git blob 生成 V1 候选及来源记录。它只使用 Python 3.9+ 标准库和本地 Git，不读取凭据、转储环境变量、自动 fetch 或启动 Jev。

## 行为覆盖与最小输入

[test_incident_evidence.py](test_incident_evidence.py) 验证严格 schema、限额、时间（兼容 Python 3.9 的小数秒解析，保留原始精度用于排序）、去重、分组、event_ref、脱敏与显示转义；唯一静态完整输入是 [production-events.json](fixtures/incidents/production-events.json)，它是合成事件且包含占位提交，不能直接作为真实仓库版本映射。

[test_incident_versions.py](test_incident_versions.py)、[test_incident_candidates.py](test_incident_candidates.py) 和 [test_prepare_incident.py](test_prepare_incident.py) 使用 [incident_test_support.py](incident_test_support.py) 创建临时本地 Git 仓库。它们覆盖精确映射、歧义与缺对象、checkout/baseline 缺口、禁用 hooks/filter/fsmonitor/replace/grafts 与网络、根相对 tree、事件 blob、源码路径与候选预算、敏感材料及产物事务写入。新增的 `.netrc` 事件 blob 回归还核对凭据片段保留为 local_only，与上游 ranker 保护一致。测试不依赖原工作区、个人缓存或已保存的历史演练产物。

## 两个 synthetic 演练

[synthetic_incident_drills.py](synthetic_incident_drills.py) 内置最小源码、固定作者、时间和提交消息，在新临时 Git 仓库重建两例；[test_incident_drills.py](test_incident_drills.py) 独立核对实际程序输出、候选来源、事件 blob 与 ranker dry-run。原始 RED/GREEN 日志和历史产物无需加入公开测试依赖。

- `runtime_difference`：同一提交与输入 `amount=1,25`，显式参数 `compat-v1` 成功、`compat-v2` 失败。两个运行由同一个宿主 Python 执行，模拟兼容模式差异，不能声称测了两种解释器或真实线上环境。
- `regression`：显式 baseline 成功、event 提交失败，HEAD 留在 baseline。候选与后续符号/调用阅读均绑定 event_commit；在 HEAD 套用同一行范围会取得不同源码。该例只确认合成夹具中已知的变化机制，不证明真实生产回归归因。

从本仓库根目录执行以下完整命令块。输出目录由系统选择并保持为空，驱动的临时源码仓库在结束后清理；结果目录中的来源、输入、观察、case/evidence/report 和 manifest 可供检查，但结束后的临时仓库路径不能继续用于调查。

```sh
production_drills=$(mktemp -d "${TMPDIR:-/tmp}/jev-production-drills.XXXXXX") &&
python3 -B tests/synthetic_incident_drills.py \
  --output-dir "$production_drills" &&
python3 -B skills/jev-bugfix/scripts/rank_candidates.py \
  --input "$production_drills/regression/incident/case.json"
python3 -B -m unittest discover -s tests -p 'test_*.py' -v
git diff --check
```

演练应退出 0，manifest 为 `complete` 且 `synthetic=true`，包含两例。两个 case 都保持 `reviewed_for_secrets=false`；ranker dry-run 的版本预检、评分调用和 HTTP 尝试上界均为 0。准备器不会外发原日志、release-map 或来源记录。

兼容 Jev 0.3.2 已存在时，可另运行 `python3 -B tests/verify_local_cli.py`；它仅导入本地 CLI 源码并模拟响应、阻断凭据与网络，默认事件测试不要求安装该 CLI。

## 自有事件模板与结论边界

以下命令是参数模板，须替换为用户已有的文件和仓库。输入说明、可选映射与状态见 [production-evidence.md](../skills/jev-bugfix/references/production-evidence.md)。

```sh
python3 -B skills/jev-bugfix/scripts/prepare_incident.py \
  --input /path/events.json --repo /path/application \
  --release-map /path/release-map.json --event-id event-001 \
  --source-root /srv/application --baseline-revision refs/tags/known-baseline \
  --output-dir /path/new-output
```

可选参数按证据提供；输出目录须为新目录或空目录。未知或冲突版本不借用 HEAD，缺 trace 不按时间邻近编造链路，无法安全映射的路径保留缺口。ready/partial 只描述准备完整性，不能确认根因、修复或秘密审核。

导入器默认结论仍是线上观察 `imported_only`、本地复现 `not_attempted`、根因 `hypothesis`、修复 `unverified`、回归归因 `unknown`。驱动另记录的 `reproduced/confirmed` 仅适用于已知 synthetic 夹具，两例没有修复验证。真实生产事件验收仍未完成；`cannot_reproduce`、`not_attempted` 与证据缺口须分别记录。

CI 保留 Linux/Python 3.9、Linux/Python 3.14、macOS/Python 3.14；本机通过或 AST 语法检查不等于其他平台/远端 CI 通过。本层不运行在线 Jev、Sentry API/MCP、真实生产事件、自动 bisect、source maps 或 P1 对照修复。P1 仍为 **0/12**，没有新增真实收益样本。
