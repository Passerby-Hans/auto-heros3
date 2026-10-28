# 长期记忆索引（INDEX.md）

按主题读写：`memory_read(scope="long_term", key="<topic>")` / `memory_write(...)`。

| 主题文件 | 内容 |
|---|---|
| battle.md | 战斗经验（目标优先级、远程保距、损失评估） |
| economy.md | 经营经验（建筑顺序、资源分配） |
| exploration.md | 探索与路线经验 |
| rules.md | 规则与资料出处 |

新经验默认 `candidate`；标 `supported` 必须有可检查依据；过时条目改 `retired`，不无痕覆盖。
