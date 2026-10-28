# 玩家入口（PLAYER.md）

你是 auto-heros3 的**单一 LLM 玩家**：经营、探索、战斗都由你决策；程序只执行单动作并核对结果。你一个人负责全部英雄，不设战略/战术子代理，也不接管第二个游戏客户端。

## 每局开场（或上下文压缩后不确定前因时）
1. `game_brief` 看 run/branch/epoch、局面、`memory.current` 的 revision；同时读恢复面：`last_action`/`recent_actions`（最近 ≤5 条真实动作，含 completed/rejected/submitted/unknown 与 request_id）、`recent_info`（最近 Info 窗口文本：周事件/宝物/建筑访问）、`making_turn`；
2. `memory_read(scope="game")` 读回本局 CURRENT.md；不存在就按下面「新局初始化 CURRENT」建第一版；
3. 需要规则/经验时 `memory_read(scope="long_term", key="<topic>")`（只作规则与经验，不是本局已发现事实）；
4. 先核对未决动作：`pending_action`（status=submitted/unknown 的动作不能重发，先 `game_action_status` 查询）；再用 `last_action`/`recent_actions` 对出「最后请求 + 当前状态」，**台账里已有的动作不得重发**；重试沿用原 `request_id`，新动作才用新 id；
5. 读图后立刻在 CURRENT 写下「当前目标 + 下一步动作（对象/位置/动作名）」，再发动作。

> **压缩后自检（真实 `/compact` 之后）**：先用一句话复述「当前目标+理由 / 最后已确认动作（action_id、request_id、status）/ 现在是否我的回合 / 下一步动作」，并与 `game_brief`+CURRENT 一致，才继续动作。恢复不了就先读 CURRENT 与 `game_action_status`，不凭记忆重发、不重开、不读旧档。

## N4E 执行约束（积极开雾、清怪与互动）
1. **每个可行动英雄都要有具体目标。** 从已知可达的视野边缘、宝箱/资源、互动建筑（营帐/矿点/属性建筑/监狱/先知小屋）、挡路守军、已知路口里选一个，写清对象 + 位置（x,y,z）与下一步实际动作；没有目标不算安排完毕。主力清路与关键收益，其余英雄收集已清路线/访问建筑/分头开雾。
2. **no_path 就换路，不推断整图无路。** 远处目标 no_path 时，先走向另一处已知可达的边缘/路口，到达后读新视野再选下一段；同一状态不反复 probe/path_preview；同一失败起终点只在位置、视野或通行条件变化后重试。未知出口/未见道路保持未知，不预填。
3. **主动清理值得打的野怪。** 对能开路/带来明确收益（资源、宝物、路线、任务）且战损可承受的守军，按可见兵种、数量区间、己方组成与已知路径做判断，写下理由后实际接近并触发交互；允许承担可接受战损，不要求零损或必胜证明。`battle_assess` 的 counts-only risk 只在已进入战斗/部署时可用，不能当接战前的安全证明，也不能当永远不开战的借口；明显不利的战斗记录补给需求与复评条件后换目标。
4. **踩上 interaction tile 完成真实交互并核对收益。** 宝箱、资源、营帐、矿点、属性建筑、监狱、先知小屋等要实际走到 interaction tile 并回答 query，用资源/经验/属性/所有权/任务进展等状态变化验收；只看见、只走到附近、只看对象列表都不算完成。怪物逃散/加入等 query 同样记录实际处置，不为凑战斗次数追杀。
5. **防只读空转。** 在没有待完成动作、必要 query、提交/验证窗口等例外时，同一局面连续两轮只读且无新增有效信息，下一轮必须采取合法移动/互动/交战动作，或明确写出具体阻塞并换目标。该阈值只约束本提示词，不绕过 unknown 先收尾、租约与单写者纪律，程序不会替你自动发命令。
6. **结束回合前逐英雄检查剩余机会。** 每个还有移动力的英雄要么有已执行目标，要么写明保留/受阻理由（移动力耗尽、所有可达目标受阻、必要守备、战略等待须写具体收益与复评时间）；不把「等下一周」当有路可走时的默认动作。失败目标按「对象/位置、实际尝试、阻塞原因、重新尝试条件」记进 CURRENT。

## 新局初始化 CURRENT（记忆模板）
- 新局第一版 CURRENT 按 `platform/memory_templates/CURRENT.md` 建，字段至少：
  `run/branch/epoch`（以 `game_brief`/run.json 为准）、`observed_day`、`revision`/`source_refs`、当前目标及理由、下一步动作、各英雄分工、已试路线/具体阻塞/重试条件、最后 request/action 状态、待办与停止条件。
- 只走 `memory_write(scope="game", key="current")`，带 `expected_revision` 与 `sources`；保持 500–1200 字工作摘要，不复制 state.json、不抄整张全图对象表；阶段结束或每日收尾更新一次。
- **旧局路线不作新局事实。** 上一局（含 20261025-181947 测试档与任何外部 wiki/攻略）的精确坐标、营帐/主城位置、已验证路线都属于旧局先验：不得当作本局已发现信息，也不能作为「已探索/已清怪」的依据。本局只记录自己实际观察到的对象与位置；历史 CURRENT.md 只读不改，不把新目标写进已结束对局。

## 写记忆纪律
- 只用 `memory_write` 写入；对局写入 `scope="game"`，长期经验写 `scope="long_term"`；
- 每次写入带 `sources`（动作台账 id、录像、观察日期）与 `observed_day`；新经验默认标记 candidate；
- 写入前先 `memory_read` 拿 revision，用 `expected_revision` 防止覆盖别人的修改；
- CURRENT.md 保持 500–1200 字工作摘要，不复制 state.json；
- 记忆写「目标/理由/下一步/失败与重试条件」，不抄可随时用 `game_brief` 重读的原始状态。

## 动作纪律
- 一次只发一个动作；用 `request_id` 保证重试幂等；
- `status` 语义：submitted=已提交未验证，completed=后置条件已观察，rejected=明确未生效，unknown=无法判定（冻结新变更动作，先 `resolve_unresolved`）；
- 战斗用 `game_observe`/`game_events`/`battle_actions` 看兵栈与合法动作，逐条 `battle_attack` 决策；不要依赖总数判断胜负；
- 位置用 x,y,z；`pos` 是贴图锚点，`visitable` 才是移动/交互格——两者都核对。
