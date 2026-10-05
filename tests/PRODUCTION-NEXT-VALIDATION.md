# Sentry 离线导入与事件版本交接验收

本层提供 bundle 内容绑定、严格引用核验、事件提交上下文导出、显式 `sentry-api-event/v1` profile 及 `--bug-context` 本地补充。格式及限制见 [production-evidence.md](../skills/jev-bugfix/references/production-evidence.md) 和 [sentry-event-import.md](../skills/jev-bugfix/references/sentry-event-import.md)。仅接收已保存的单事件 API 响应，不连接 Sentry，不支持把 SDK/envelope、issue 聚合或 UI 导出自动当作相同格式。

## 默认测试与身份边界

- [test_incident_bundle.py](test_incident_bundle.py)、[test_inspect_incident.py](test_inspect_incident.py) 覆盖 case/evidence/input 绑定、候选引用、篡改、缺对象、旧包、审核与 local_only 收紧，以及基于历史 blob 的有界上下文导出。
- [test_incident_sentry.py](test_incident_sentry.py)、[test_production_next_prepare.py](test_production_next_prepare.py) 覆盖白名单投影、多异常显式选择、缺字段/不完整链、冲突与诊断、双输入哈希、严格补充和产物事务。完整合成输入及来源见 [fixture-manifest.json](fixtures/incidents/sentry-api/fixture-manifest.json)。
- [test_oss_incident_replay.py](test_oss_incident_replay.py) 保留六项真实开源源码回放覆盖；[test_oss_fixture_rebuild.py](test_oss_fixture_rebuild.py) 增加四项确定性重建、Git 配置隔离、源码篡改拒绝、已有目录保护及 prepare→动态候选→inspect 集成覆盖。

bundle `verified` 只说明内容、引用及本地 Git 源码一致，不替代事件来源认证、秘密审核、根因确认或修复验证。生成的 case 默认 reviewed_for_secrets=false。预检、导入和上下文读取均不触发外部评分。整套自洽替换不能靠自带哈希证明真实性，sidecar/input 与分类材料仍须保留。

## 可直接运行的公开 OSS 回放

从本仓库根目录执行以下完整命令块。Python tempfile 使用平台默认临时目录或 TMPDIR；不依赖个人绝对路径、已有仓库或网络。候选 ID 从本次生成的 case 读取。

```sh
incident_scripts=skills/jev-bugfix/scripts
incident_pilot=tests/evidence/production-next/pilot
incident_demo=$(python3 -B -c 'import tempfile; print(tempfile.mkdtemp(prefix="jev-incident-demo-"))') &&
python3 -B "$incident_pilot/rebuild_repo.py" --output "$incident_demo/repo" &&
python3 -B "$incident_scripts/prepare_incident.py" \
  --input "$incident_pilot/sentry-api-event.json" --input-format sentry-api-event/v1 \
  --repo "$incident_demo/repo" --release-map "$incident_pilot/release-map.json" \
  --output-dir "$incident_demo/output" &&
python3 -B "$incident_scripts/inspect_incident.py" \
  --bundle "$incident_demo/output/bundle.json" --repo "$incident_demo/repo" &&
incident_candidate=$(python3 -B -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["candidates"][0]["id"])' "$incident_demo/output/case.json") &&
python3 -B "$incident_scripts/inspect_incident.py" \
  --bundle "$incident_demo/output/bundle.json" --repo "$incident_demo/repo" \
  --candidate-id "$incident_candidate" --context-lines 20 \
  --source-output "$incident_demo/context.py"
```

应依次得到 rebuilt、ready、verified、context written。prepare 保留 selected_trace_missing，表示没有观察到 trace；不自动补造链路。event_commit 是错误 fixture revision，checkout_head 是修复 fixture revision，历史上下文仍读取错误源码，审核开关保持 false。

来源、许可证、独立六项预期与重建身份见 [pilot/report.md](evidence/production-next/pilot/report.md)、[manifest.json](evidence/production-next/pilot/manifest.json) 和 [expected.json](evidence/production-next/pilot/expected.json)。两份固定源码与 BSD-3-Clause 许可保留；公开资产用两个确定性 snapshot 提交代替完整历史 bundle。真实 upstream_revision 与新 fixture_revision 分别记录，release-map 和事件同步绑定后者。源码、blob OID 与 3,396 字节历史上下文 SHA-256 保持一致。

该案例分类为 oss_replay，reconstructed_event=true、known_answer=true、nonblind=true；不是 synthetic 自造源码，也不是获准真实生产事件。错误版本六项窄回归有三项失败，修复与最小补丁版本六项通过；这仅支持已知回放条件的本地根因与修复验证，回归引入提交仍 unknown。工具 evidence 默认的 hypothesis/unverified 不因回放或 bundle 核验自动升级。

## 从干净 checkout 验收

```sh
python3 -B -m unittest discover -s tests -p 'test_*.py' -v
python3 -B tests/verify_local_cli.py
git diff --check
```

第二条仅适用于已安装兼容 Jev 0.3.2 的环境；它检查真实本机源码并阻断凭据/配置/网络，不属于默认发现。没有该依赖时明确未运行，不安装另一份 CLI。两例 synthetic 演练仍见 [首期验证说明](PRODUCTION-EVIDENCE-VALIDATION.md)，与 OSS 回放及真实生产样本分开。

| 验收范围 | 状态 |
| --- | --- |
| 离线格式、绑定、历史源码读取与公开重建 | 默认行为测试和公开命令覆盖 |
| 获准真实生产单事件及对应历史 | pending；未提供获准样本 |
| 真实生产定位与修复效果 | pending；已知答案回放不证明效果 |
| 真实 A/C 收益与成本 | P1 0/12，未启动 |

CI 保留 Linux/Python 3.9、Linux/Python 3.14、macOS/Python 3.14。各本地提交的实际环境与检查结果另存交付记录；本机测试不代表未实测平台或未触发的 GitHub Actions 通过。本层不调用在线 Jev/Sentry、读取凭据、发布 PR 或验证线上效果。

自有事件需要替换参数的模板见 README 及接口文档；输出目录必须新建或为空。在线读取是后续独立范围，需要明确事件/项目授权、HTTPS 地址、只读凭据、存放/脱敏责任与请求/字节/时间上限。
