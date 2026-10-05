# boltons constant-backoff 离线公开案例

本案例是 `oss_replay`，`reconstructed_event=true`、`known_answer=true`、`nonblind=true`。它保留真实上游错误/修复源码，用本地异常重建 Sentry API 形状事件；没有连接 Sentry，也没有使用获准的真实生产事件。真实生产效果仍为 pending，P1/A/C 保持 0/12。

来源为 [boltons PR #428](https://github.com/mahmoud/boltons/pull/428) 和后来报告同类问题的 [issue #452](https://github.com/mahmoud/boltons/issues/452)。后来的 issue 不能作为较早修复的触发来源。源码来自已保存的固定公开版本，发布整理没有再次下载代码。

## 上游身份与夹具身份

| 角色 | upstream_revision | fixture_revision |
| --- | --- | --- |
| 错误版本 | `57cb026b7f47cd2765a0d5acdc83849ed5f1f6a3` | `eb110f9f84ca0bdc0d9dac833d61859e15a3d527` |
| 修复版本 | `ead236e278ca0466bf468de746b5960fb12d7e5b` | `60264f0eae8b171ac46c5ecbf487d1304c3570ab` |

`rebuild_repo.py` 仅在新目录中生成两个确定性 snapshot 提交，每个 tree 只有 `boltons/iterutils.py`。提交使用明确的夹具身份与固定时间戳，**不是原上游提交对象或完整历史**。真实上游修复与其父提交关系来自原来源记录；新夹具的两提交关系由重建脚本建立。

旧的 1,889,711 字节完整历史 bundle 不在发布范围中。两个固定源码共 118,322 字节；原通知保留，另附 [BSD-3-Clause 许可](source/LICENSE.boltons)。源码 SHA-256 和实际 blob OID 保持一致，manifest 明确分别记录 upstream_revision、fixture_revision。release-map 现在映射夹具错误提交，事件的 release 标签也显式标为 fixture；extra 保留真实 observed_upstream_commit 和夹具声明。没有把新 SHA 冒充原上游 SHA。

脚本屏蔽继承的 Git 环境及全局/系统配置，禁用 hooks、fsmonitor、属性文件、签名和网络协议；通过 `hash-object`/`mktree` 写固定字节，不使用过滤器、下载或个人缓存。输出必须是尚不存在的目录，其父目录已存在；已有目录及内容不改动。

## 行为与结论边界

`expected.json` 是独立的六项预期，默认测试会同时运行重建后的两份真实源码并比较。异常消息可能随 CPython 版本变化，预期固定异常类型及返回值。

| 输入 | 错误源码 | 修复源码 |
| --- | --- | --- |
| `start=5, stop=5, factor=1.0`，省略 count | ZeroDivisionError | `[5.0]` |
| `start=1, stop=10, factor=1.0`，省略 count | ZeroDivisionError | ValueError |
| `start=0, stop=10, factor=1.0`，省略 count | ZeroDivisionError | ValueError |
| `start=1, stop=10, count=3, factor=1.0` | `[1.0, 1.0, 1.0]` | 相同 |
| `start=1, stop=8, factor=2.0` | `[1.0, 2.0, 4.0, 8.0]` | 相同 |
| `count='repeat', factor=1.0`，只取前三项 | `[1.0, 1.0, 1.0]` | 相同 |

真实错误源码在 651 行调用 `math.log(stop / denom, 1.0)`。`factor=1.0` 通过参数检查，而底数对数为零，导致 ZeroDivisionError。显式 count、repeat 和正常增长因子在错误版本已通过，限定了根因范围。

本地六项窄回归在错误版本有 3 项失败、修复版本 6/6 通过，错误源码应用 `minimal-fix.patch` 后也 6/6 通过。原六项回放测试仍在；另有四项重建测试覆盖确定性与 Git 配置隔离、源码篡改拒绝、既有目录保护，以及 release-map→prepare→动态候选 ID→历史上下文导出。新检查记录保存在本轮本地 PR 交付目录，原始历史日志留在原工作区。

独立本地回放的 reproduction=reproduced、root_cause=confirmed、fix_verification=verified 只适用于上述输入和六项检查；regression_attribution=unknown，未找出引入错误的提交或已知正常基线。工具 evidence 自身的 imported_only/not_attempted/hypothesis/unverified/unknown 默认结论不升级。bundle verified 仅确认一致性及源码绑定，不替代事件来源认证、秘密审核或修复验证。

固定输入的 dateCreated 保留原本地捕获时间，不是历史生产时间。库帧来自真实异常，省略一个驱动调用帧；inApp=true 表示代码属于受查仓库。没有观察到 trace，因此 prepare 保留 selected_trace_missing。单独分享生成的严格 V1 case 时，必须同时附本 manifest/report 来保留 OSS 分类。

## 在仓库根目录执行

以下代码在本仓库根目录运行。Python tempfile 使用平台默认临时目录或用户设置的 TMPDIR；所有产物写入新临时目录，无网络、账号或外部包依赖。

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

依次预期 rebuilt、ready、verified 和 context written；真实输出路径由 JSON 返回。候选 ID 从本次生成的 case 读取，不依赖旧 Git 身份。事件提交应为夹具错误版本，checkout HEAD 为夹具修复版本；历史上下文仍为错误源码的 605–697 行，3,396 字节，SHA-256 `10d270331fbb637349aff0475ec34d835aa2c192bef71a8df12a9c210cf6342d`，审核开关仍为 false。

独立检查六项预期：

```sh
python3 -B tests/evidence/production-next/pilot/regression_test.py \
  --source tests/evidence/production-next/pilot/source/broken/iterutils.py
# 上一条预期 exit 1、3 failures；下一条预期 exit 0、6 tests 通过。
python3 -B tests/evidence/production-next/pilot/regression_test.py \
  --source tests/evidence/production-next/pilot/source/fixed/iterutils.py
```

`minimal-fix.patch` 保留作错误源码的最小修复对照，不是本仓库需要应用的补丁。默认测试在 Linux/Python 3.9、Linux/Python 3.14、macOS/Python 3.14 CI 矩阵运行；本轮实际本地验证为 macOS/CPython 3.14.6，未触发远端 CI，也没有实测其他矩阵环境。
