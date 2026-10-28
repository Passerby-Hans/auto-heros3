---
schema_version: 1
scope: game
run_id: <run_id>
branch_id: main
revision: 1
updated_at: <YYYY-MM-DD HH:MM:SS>
observed_day: <day>
source_refs: [<动作台账 id>, <录像 segment>, <观察日期>]
---

# 本局工作摘要：<run_id> / <branch_id> / epoch <n>

> **填写说明（新局第一版 CURRENT.md）**
> - 用 `memory_write(scope="game", key="current", body=..., expected_revision=..., sources=[...], observed_day=...)` 写入。
>   front-matter 由程序重建（`schema_version/scope/run_id/branch_id/revision/updated_at/observed_day/source_refs`），
>   正文里的 epoch、英雄分工等不会被程序改写；epoch 以 `game_brief` 或 run.json 为准。
> - 只存 **500–1200 字工作摘要**：目标/理由/下一步/分工/已试路线与重试条件/待办与停止条件。
>   不复制 state.json、不抄整张全图对象表、不写可随时用 `game_brief` 重读的原始状态。
> - **压缩恢复**：`game_brief` 直接给 `last_action` / `recent_actions`（最近 ≤5 条，含
>   completed/rejected/submitted/unknown 与 request_id）、`recent_info`（最近 Info 窗口文本）
>   与 `making_turn`；本节只抄判读结论，不抄整张台账。重试沿用原 `request_id`，新动作才用新 id。
> - **旧局路线不作新局事实**：旧 run（含 20261025-181947 测试档）的坐标、营帐/主城位置与外部 wiki/攻略内容不得当成本局已发现信息。
> - 每个有移动力的英雄都要有目标或明确保留理由；失败目标按「对象/位置、实际尝试、阻塞原因、重新尝试条件」记录。

## 身份与观察
- run / branch / epoch：<run_id> / <branch_id> / <epoch>
- observed_day：<day>
- 本段观察来源：<actions id / 录像 segment / 观察时间>

## 当前目标及理由
- 目标（对象 + 位置 x,y,z / 收益或开路价值）：
- 理由（可见兵种与数量区间、己方组成、资源与风险判断）：

## 下一步动作
- 动作名 + 参数（例：`move_hero` hero0 → interaction tile (x,y,z)；`battle_approach` target<id>；`answer` query<id>=1）：
- 若下一步必须是只读核对，写明原因（如 unknown 未收尾、query 未回答、等待验证窗口）：

## 各英雄分工
- hero0 <名/位置>：<当前目标> / <已执行或下一步>
- hero1 <名/位置>：<收集已清路线 / 访问建筑 / 分头开雾>
- （每个仍有移动力的英雄要么有已执行目标，要么写明保留理由）

## 已试路线 / 具体阻塞 / 重试条件
- <对象/位置> | <实际尝试的动作与结果> | <阻塞原因> | <重新尝试条件>
- 同一失败起终点在位置/视野/通行条件变化前不重试；no_path 不代表整图无路，改走已知可达边缘/路口再观察。

## 最后 request/action 状态（压缩恢复核对点）
- last_action_id / request_id：<最近一次动作的 `game_brief.last_action.action_id` 与 `request_id`；
  重试必须沿用同一 request_id，新动作才用新 id>
- 最近动作：<action + status=completed/submitted/rejected/unknown + 证据摘要；可抄
  `game_brief.recent_actions` 的前 2–3 条>
- pending_action：无 | <action_id, status=submitted/unknown>（未收尾前不叠加新的变更动作）
- 未决/待收尾：<resolve_unresolved 状态；无则写无>

## 待办与停止条件
- 待办（按优先级）：<营帐/关卡线索、宝箱、矿点、属性建筑、补给、攻城…>
- 停止条件：预算（≤7 个己方回合或 30 分钟墙钟，先到即止）/ 正式胜负 / unknown 无法收尾 / 公平信息泄漏 / 身份失配；
  停止时保留最后状态、动作台账与录像，并写明停止原因。

## 记忆边界
- 只存工作摘要，不复制全 state；旧局精确路线/坐标/外部 wiki 内容不作本局已发现事实。
- 写入用 `memory_write` + `expected_revision` + `sources` + `observed_day`；历史 CURRENT.md 只读，不把新目标写进已结束对局。
