# 第一版验收记录

日期：2026-10-01 至 2026-10-02。验证目标是排序、降级、数据边界和修复流程；没有开展阅读量或成本对照实验。

## 结果

| 检查 | 实际结果 |
| --- | --- |
| 离线回归测试 | 41 项通过；无网络、无凭据依赖 |
| 已安装 Jev 源码契约 | 5 项通过；版本 0.3.2；用合成 transport 验证真实 CLI 代码 |
| skill-creator 结构检查 | `Skill is valid!` |
| 独立代理修复演练 | 根因候选零分、两项评分超时，仍完成一行修复；5 项目标测试通过 |
| 独立真实 API 冒烟 | 修正舍入兼容后 exit 0，`status=ranked` |
| 独立代码审查 | 已修复发现的问题，无剩余实质性问题 |

可重复执行的离线命令（在项目根目录）：

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 tests/verify_local_cli.py --jev /Users/yangxuesong/.local/bin/jev
```

结构检查使用 skill-creator 自带 `quick_validate.py`。本机默认 Python 缺少 PyYAML，获得授权后仅安装到了临时目录，实际通过的命令为：

```sh
PYTHONPATH=/private/tmp/jev-bugfix-validation-deps python3 \
  /Users/yangxuesong/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  skills/jev-bugfix
```

## 本次发现并修复的问题

- 约定要求保留 `label` / `confidence`，原实现未输出。现在评分成功时保留，重复或失效时清空。
- 转义的孤立 Unicode 代理码片段会在编码时崩溃。现在在输入校验阶段拒绝并返回 JSON 降级报告。
- 超大整数响应会在 `math.isfinite` 中溢出。现在先检查数值范围，再检查有限性，单项异常不丢弃其他评分。
- 原超时测试只给新建可执行替身 150 毫秒；实测首次启动约 172–211 毫秒。现将进程管道/部分输出测试改为直接启动 Python，并独立验证完整 CLI 的期限降级。
- 真实 API 的 score 和展示概率存在独立舍入差异，原 `0.001` 一致性阈值误判。加入真实字段样例、容差边界与明显冲突回归；详见[输入输出约定](../skills/jev-bugfix/references/contract.md)。

## 独立修复演练

评估代理仅获得 skill、隔离样例和需求“checkout 显式传入 `promo_percent=0` 时金额不对”，未获知根因或预期补丁，也未查看假 Jev 源码。

- 初始失败：金额 `97.2 != 108`。
- 一次离线评分：4 个候选；失败测试与 checkout 超时，tax 得分 1.0，pricing 得分 0.0。
- 代理按调查顺序核对堆栈、调用入口、税额和折扣模块；没有因零分排除 pricing，也没有重试。
- 本地直接调用确认 `promo_percent or 10` 将数值零当作默认优惠。
- 最小修复为仅 `None` 使用默认值；原零优惠测试和其他优惠行为共 5 项通过。

留存证据：

- [候选输入](evidence/forward/candidates.json)
- [逐项评分与降级报告](evidence/forward/ranking-report.json)
- [最小修复及测试差异](evidence/forward/fix.diff)
- [修复前：5 项、1 失败](evidence/forward/tests-before-expanded.txt)
- [修复后：5 项通过](evidence/forward/tests-after.txt)

该演练使用离线替身，实际远程调用数为零。评分报告中的 HTTP 上界 8 是预算预留。

## 真实 API 验证

与默认离线测试分开运行，只发送 `tests/fixtures/smoke_case.json` 中的合成代码与证据，每次载荷 430 字节。没有上传本仓库实现、用户代码或凭据文件。

开发验证共进行了 3 次独立 CLI 调用：初次冒烟、字段诊断、修正后的冒烟。每次 1 个候选，合计物理 HTTP 尝试上界 6；实际尝试数和费用未测量。助手内部没有增加重试；日常使用仍遵守每个 bug 一次评分批次。

诊断观测到 `score=value=3.99`、`label="4"`、概率最高档为 1、其余为 0、`confidence=0.99`。修正后真实助手报告为：

```json
{"status":"ranked","score":0.9975,"label":"4","confidence":0.99,
 "cli_invocations":1,"submitted_candidates":1,"http_attempts_upper_bound":2}
```

这是报告字段摘要。调用命令：

```sh
python3 tests/live_smoke.py --jev /Users/yangxuesong/.local/bin/jev
```

一次集成成功仅确认当时的连接与响应兼容。修复正确性仍由复现、根因证据与目标仓库测试确定；尚未证明减少实际阅读量或成本。敏感信息检测也不能替代外发前的内容检查。
