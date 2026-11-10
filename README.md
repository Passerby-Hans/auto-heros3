# auto-heros3

> **让 LLM agent 像玩家一样亲自玩《魔法门之英雄无敌 III》——基于 VCMI，公平、可复核、可回放。**
> **An LLM agent plays Heroes of Might and Magic III on the VCMI engine — fair, verifiable, recordable.**

[简体中文](#简体中文) · [English](#english)

---

## 简体中文

`auto-heros3` 是把 [VCMI](https://vcmi.eu/)（《魔法门之英雄无敌 III》的开源引擎重实现）改造成
"LLM agent 可以当玩家亲自游玩"的工程：

- agent 通过**标准 MCP server**（stdio）接入，用 `game_observe` 读取**玩家可见**局面，用 `game_act` 下达**玩家能做的**动作；
- 动作在引擎内走正常玩家校验（`CCallback` / `BattleAction`），**不用 GM、不改存档、不开图、不预读 AI 状态**；
- 关键操作（招兵、雇英雄等）会**模拟真实鼠标点击走完游戏 UI**，观众能完整看到；
- 每个动作都带前后状态核验与证据（ledger / 事件 / 录像），可复盘、可回放。

> 长期目标：agent 从开局自主经营、招募、战斗、攻城，赢下一整局。
> 当前状态：打补丁的客户端与标准 MCP 工具面可用，端到端对局已跑通。**固定图 `Key to Victory`
> （Easy / 红 Castle）上已有两次无辅助胜局：2026-10-26 正式候选局（day 28，`ACCEPT_WITH_DISCLOSURE`）
> 与 2026-10-28 干净局（day 68，墙钟 41m27s；无作弊、无读档、无调试资源、无外部攻略、单 LLM 玩家，
> 452 条动作来源全为 LLM，3 段 1080p 全程录像独立审计 PASS）。**
> 披露：固定图已被反复游玩（地图熟悉度已披露），单局 ≠ 跨地图胜率；**盲图 / 多图的正式公平验收仍未完成**。
> 录制：默认已切换为自有 native **libobs / a3capture / shared_memory** 链路（2026-11-09 完成默认推广；
> 2026-11-10 真实 VCMI 90 分钟长录复核通过），显式 `legacy`/`disk` 回退保留；离线回归
> **46 套 / 1529 项断言** 全部通过（2026-11-10 复核 1529/1529）。

### 已实现能力

| 类别 | 能力 | 主要入口 |
|---|---|---|
| 感知 | 资源/日期/英雄/军队/城镇/可见地图对象 | `game_observe` `game_brief` |
| 感知 | 英雄详情（四维/技能/法术书/装备/魔法值/运气士气/特长） | `game_brief` `game_observe` |
| 感知 | 战斗单位/格子/回合顺序/合法动作/可用法术 | `game_observe` `battle_actions` |
| 感知 | 路径预览（引擎寻路，含传送/上船标记） | `path_preview` |
| 经营 | 建造、招兵、兵种升级、拆/合/换兵、市场交易 | `game_act` `town_info` `army_info` |
| 英雄 | 招第二英雄（酒馆）、移动、就近移动、居中镜头、打开英雄装备界面 | `game_act` `move_near` |
| 魔法 | 冒险法术与战斗法术施放；两阶段法术（传送/牺牲）显式拒绝 | `game_act` `battle_actions` |
| 战斗 | 攻击（走打/射击）、接近、移动、防御、等待、整回合接管 | `battle_*` `game_act` |
| 探索 | 黑幕边界 BFS 探索：整层一次只读 probe + 沿边界逐格开雾 | `explore_toward` `path_preview` |
| 对话 | 阻塞对话框转 `query` / `answer`，不弹阻塞 UI | `game_events` `game_act` |
| 界面 | 城镇/酒馆/英雄装备界面（真实窗口）/ 控件树 / 模拟点击 / 关窗（观众可见） | `game_act` `ui_tree` |
| 证据 | 存档/读档（身份核验）、事件、账本、离屏录像：默认 native libobs，1920×1080/60 + H.264 8Mbps + AAC 44.1k 双声道，900s/1.5GB 分片与故障恢复 | `save_game` `load_game` `recording_*` |
| 记忆 | 玩家/对局/长期记忆（带 revision 的 CAS 写入） | `memory_read` `memory_write` |

> **寻路注意**：客户端寻路把**未揭开的黑幕格**判为 `BLOCKED`（VCMI `PathfinderUtil.h`），
> 所以 `path_preview` 的 `reachable=false` 只能说明目标不在当前已揭开的连通区域，
> **不能推断"不可达"**。正常推进卡住时用 `explore_toward` 沿黑幕边界把整条边逐格踩开；
> 它是卡住时才用的暴力兜底，不是常规寻路。

### 安装

> 目前**仅支持 Windows 10/11 x64**：屏幕外窗口抓帧、进程静音、单写者租约都依赖 Win32 API。Linux / macOS 未验证。
> 本仓库**不包含 VCMI 二进制**，也**不包含任何原版游戏数据**；OBS/libobs 运行库与私有录制产物
> （`platform/obs-recorder/build-media/`、`platform/obs-recorder/bin/`、`platform/sdl-capture-patch/stage/`）
> 同样不入库，需按对应 README 本地构建/部署。

#### 0. 环境要求

| 组件 | 要求 |
|---|---|
| 操作系统 | Windows 10/11 x64 |
| 原版游戏数据 | 自备正版《英雄无敌 III 完整版 / 死亡阴影》（Steam / GOG 均可） |
| Git | 较新版本 |
| PowerShell | Windows PowerShell 5.1（启动客户端用）+ PowerShell 7（构建脚本用） |
| Python | 3.14.x（`pyproject.toml` 要求 >=3.14,<3.15；实测 3.14.2） |
| uv | 创建 `.venv` 并按 `uv.lock` 冻结安装依赖 |
| MSVC | Visual Studio 2022 或更新版本的 Build Tools，含"使用 C++ 的桌面开发"与 Windows SDK |
| CMake | 较新版本 |
| Ninja | 较新版本 |
| Conan | 2.x（装在独立 venv） |
| ffmpeg | 可选：仅 legacy 录制后端与部分诊断需要；默认 native 录制不需要 |
| MCP 客户端 | 任意支持 stdio 的 MCP host |

> VCMI 官方预编译依赖包由 v142 编译；本项目实测以 MSVC 14.51（v143 及以上）链接通过，ABI 兼容。

#### 1. 获取代码

```powershell
git clone <你的仓库地址> auto-heros3
cd auto-heros3
```

#### 2. 创建 Python 环境

依赖完全由 `pyproject.toml` + `uv.lock` 固定（当前 **52 包**，实测 CPython 3.14.2）：

```powershell
uv venv --python 3.14 .venv
uv sync --frozen
```

- 不要用手工 `pip install` 的旧六包清单覆盖冻结环境；需要变更依赖时修改 `pyproject.toml` 后重新 `uv lock`。
- 核对：`uv sync --frozen --dry-run` 应报告无需变更；`uv lock --check` 应通过。
- `pycaw` 用于进程级静音；缺失时只会跳过静音。默认游戏录音走 **SDL a3capture → 共享内存 → native**；`process-audio-capture`（WASAPI process loopback）仅保留为历史/诊断路径。

> 可选（逆向/手册/OCR 研究工具）已纳入 lock；如只需最小 MCP 服务器，仍建议保留同一冻结环境以保证可复现。

#### 3. 构建带桥接补丁的 VCMI 客户端

需要从 VCMI 1.7.5 源码构建一个打了 `AgentBridge` 补丁的客户端。补丁的唯一来源是
[`platform/vcmi-patch/`](platform/vcmi-patch/)，由构建脚本自动应用（幂等）。

**3.1 拉取 VCMI 1.7.5 源码**

```powershell
git clone --depth 1 --branch 1.7.5 https://github.com/vcmi/vcmi vendor\vcmi-src
git -C vendor\vcmi-src submodule update --init --recursive --depth 1
```

**3.2 安装 Conan，并恢复官方预编译依赖**

```powershell
py -3.12 -m venv vendor\conan-venv
vendor\conan-venv\Scripts\python.exe -m pip install conan
vendor\conan-venv\Scripts\conan.exe profile detect

# 官方预编译依赖，约 108 MB；来源：https://github.com/vcmi/vcmi-dependencies/releases/tag/2026-07-07
New-Item -ItemType Directory -Force vendor\downloads | Out-Null
curl.exe -L -o vendor\downloads\dependencies-windows-x64.txz `
  https://github.com/vcmi/vcmi-dependencies/releases/download/2026-07-07/dependencies-windows-x64.txz
vendor\conan-venv\Scripts\conan.exe cache restore vendor\downloads\dependencies-windows-x64.txz
```

> 没有 `py` 启动器时，把 `py -3.12` 换成你本机的 Python（3.12 或更高）。

**3.3 解析依赖（在 VCMI 源码目录内执行）**

```powershell
Push-Location vendor\vcmi-src
..\conan-venv\Scripts\conan.exe install . --output-folder=conan-msvc --build=never `
  --profile=dependencies\conan_profiles\msvc-x64 `
  -s '&:build_type=RelWithDebInfo' `
  -c 'tools.env.virtualenv:powershell=powershell.exe'
Pop-Location
```

> 输出目录必须叫 `conan-msvc`：应用补丁的脚本按这个路径查找 `conan_toolchain.cmake` 和 `conanrun.ps1`。

**3.4 按本机路径修改构建脚本**

编辑 [`tools/vcmi_build.ps1`](tools/vcmi_build.ps1) 顶部与配置段的路径，指向你的工具链：

```powershell
$vs    = 'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools'  # 你的 VS / Build Tools 目录
$ninja = 'C:\msys64\mingw64\bin\ninja.exe'                               # 你的 ninja.exe
# 脚本后面调用的 cmake.exe 路径（默认 C:\msys64\mingw64\bin\cmake.exe）同样要改
```

**3.5 构建**

```powershell
# 首次构建：客户端 + 服务端（单机对局需要 VCMI_server.exe）
pwsh -NoProfile -File tools\vcmi_build.ps1 -Jobs 16

# 之后只改 client 时做增量构建（首次全量约 10 分钟，增量约 1-3 分钟）
pwsh -NoProfile -File tools\vcmi_build.ps1 -Targets VCMI_client.exe -Jobs 16
```

脚本会先调用 `platform\vcmi-patch\apply.py` 把补丁注入 `vendor\vcmi-src`，再进入 MSVC + Ninja 构建。
产物：

```text
vendor\vcmi-src\build\bin\VCMI_client.exe
vendor\vcmi-src\build\bin\VCMI_server.exe
vendor\vcmi-src\build\bin\VCMI_lib.dll
```

> - 首次构建前确认 `vendor\vcmi-src\conan-msvc\conanrun.ps1` 已由 3.3 生成；MCP server 启动客户端时会先
>   dot-source 它来设置依赖 DLL 的 PATH。
> - [`tools/bridge_contract.json`](tools/bridge_contract.json) 固定了开发用客户端的 sha256。**你本地重建的 exe
>   哈希必然不同**，启动时会打印一条 WARNING（预期行为）；只要 `protocol` 仍是 2，协议层不受影响。
> - 修改补丁源码后需要重新构建，并同步更新 `tools/bridge_contract.json`（协议变更必须显式更新，否则会被拒绝启动）。

#### 4. 导入原版游戏数据

VCMI 自身不含任何原版美术/音效/地图，需要你合法拥有的《英雄无敌 III》数据：

1. 下载官方 VCMI 1.7.5 Windows 包（[Releases](https://github.com/vcmi/vcmi/releases/tag/1.7.5)，`VCMI-Windows.zip`），
   或使用你已安装的 VCMI。
2. 运行 `VCMI_launcher.exe`，在启动器里选择从 Heroes III 安装目录**导入数据**，指向你的正版游戏目录（Steam / GOG）。
3. 导入完成后，确认默认用户目录里已有数据：

```text
%USERPROFILE%\Documents\My Games\vcmi\Data
%USERPROFILE%\Documents\My Games\vcmi\Maps
%USERPROFILE%\Documents\My Games\vcmi\Mp3
```

```powershell
# 快速验证（应返回 True）
Test-Path "$env:USERPROFILE\Documents\My Games\vcmi\Data\H3bitmap.lod"
```

> 本项目自编译的客户端读取同一个用户目录，不需要把数据复制到 `vendor\` 下。
> 源游戏目录建议保持只读；VCMI 只从副本读。

#### 5. 安装自检

```powershell
# MCP server 自检：打印能力清单并做一次租约 acquire/release（不会启动游戏）
.\.venv\Scripts\python.exe -X utf8 tools\mcp_server_v2.py --selftest

# 构建产物与依赖环境检查
Test-Path vendor\vcmi-src\build\bin\VCMI_client.exe
Test-Path vendor\vcmi-src\build\bin\VCMI_server.exe
Test-Path vendor\vcmi-src\conan-msvc\conanrun.ps1
```

#### 6. 注册到 MCP 客户端

把 MCP server 注册到任意支持 stdio 的 MCP 客户端。通用 `mcpServers` 配置（把路径换成你的绝对路径）：

```json
{
  "mcpServers": {
    "auto-heros3": {
      "type": "stdio",
      "command": "E:\\path\\to\\auto-heros3\\.venv\\Scripts\\python.exe",
      "args": ["-X", "utf8", "E:\\path\\to\\auto-heros3\\tools\\mcp_server_v2.py"],
      "env": { "PYTHONIOENCODING": "utf8" }
    }
  }
}
```

Claude Code 可直接：

```powershell
claude mcp add auto-heros3 -- "E:\path\to\auto-heros3\.venv\Scripts\python.exe" -X utf8 "E:\path\to\auto-heros3\tools\mcp_server_v2.py"
```

DeepSeek Harness（DSH）：在它的 MCP 客户端配置里填同样的 stdio 命令即可。

验收：

1. 客户端工具列表出现 `auto-heros3` 的工具（如 `game_status`）；
2. 调用 `game_status` **不会**启动游戏（进程列表无 `VCMI_client.exe`）；
3. 只有显式调用 `session_start` 才会开局。

#### 7. 开第一局

在 MCP 客户端里按顺序操作：

1. `game_status()`：确认当前没有对局。
2. `session_start(map_name="Maps/Arrogance", difficulty="easy", player_color="red", starting_town="castle")`
   - `map_name` 必须是你的数据目录里存在的地图；`Maps/Arrogance` 是 H3 自带图，导入完整数据后即有。
   - 高级选项只对新局生效；恢复存档时传 `Saves/agent/<name>` 或 `*.vsgm1` 路径。
3. `lease_acquire()`：取得单写者租约（MCP 进程重启后需要重新获取）。
4. `game_observe()` / `game_brief()`：读取玩家可见局面。
5. `game_act(action="end_turn")`、`game_act(action="build", args={"town": 0, "id": 8})`、
   `game_act(action="recruit", args={"town": 0, "level": 1, "amount": 12})` 等；每个动作返回 `status` 与证据。
6. 出现决策点（升级/宝箱/战斗/结算）时先 `game_events()`；对话框用 `game_act(action="answer", args={"value": 0})` 应答。
7. 战斗：`battle_actions()` 读合法目标 → `battle_round(target=…)` 接管一整轮（`target` 取 `battle_actions()`
   列出的敌方 stack id），或单步 `game_act`（`battle_attack` / `battle_approach` / `battle_move` / `battle_defend` / `battle_wait` / `battle_start`）；
   战斗法术见 `battle_actions()` 的 `spells`，用 `game_act(action="battle_cast_spell", …)` 施放。
8. 其他动作：`game_act(action="open_hero", args={"hero": 0})` 打开英雄装备界面（保持打开，便于观察/录屏）；
   冒险法术 `game_act(action="cast_spell", args={"hero": 0, "spell": <id>})`。
9. 卡住时探索：`explore_toward(hero=0, x=…, y=…, z=…, map_w=36, map_h=36)` 沿黑幕边界逐格开雾；
   遇到战斗或未应答 query 会直接返回对应状态。
10. 需要录像：`recording_start()` … `recording_stop()`。默认使用链接 **libobs** 的自有原生进程，
    不启动 OBS 界面。当前支持 1920×1080、CFR60、H.264（8Mbps CBR）和 AAC 44.1kHz 双声道；
    游戏窗口以 WGC 捕获，右侧显示思考/操作。容器 60fps 不代表 60 个不同游戏画面，沿用 basic-60 合同。
    默认每 900 秒或接近 1.5GB 分段；在闭 GOP 关键帧切换，实际切点和 TIME/SIZE 原因进入原生证据。
    1.5GB 指十进制最终 MP4 上限；触发预留 2%，封口后仍逐段校验，超限标 partial，不能当成功。
    音频默认为私有 **SDL a3capture → 带 QPC/样本索引/CRC 的共享内存实时流 → native source → AAC**，
    同一 PCM 独立归档为 framed 文件，磁盘写入不承担实时送音。不打开物理播放端点，
    不修改 Windows 音量或静音。若游戏音量为 0，session 临时提升到 30/40，
    session_stop 逐字节恢复设置。音频源绑定当前 session，缺 ready、错误 DLL 或旧会话身份会明确拒绝。
    首次使用需安装/构建本机验证的 native checkpoint，见 `platform/obs-recorder/README.md` 与 `NOTICE.md`。
    手动回退：设置 `AUTO_HEROS3_AUDIO_DRIVER=disk`，结束并重新启动 session，再调用
    `recording_start(backend='legacy')`（legacy 需要 `ffmpeg`）。不在同一次录制中自动切换后端。
11. 结束：`session_stop()`（只关闭本会话拥有的客户端）。

> 游戏由 MCP server 自动以**屏幕外**窗口（虚拟桌面之外，如 -1600,0）、**仅向录制链路输出游戏声音**的方式启动，不抢你的鼠标和焦点。
> 想用命令行调试也可以：
> `python -X utf8 tools\vcmi_act.py start --map Maps/Arrogance`，随后 `state` / `do "end_turn"` / `shot` / `stop`。

### 工程结构

```text
platform/vcmi-patch/            唯一补丁源（AgentBridge C++ + apply.py）
platform/memory_templates/      LLM 玩家记忆模板（PLAYER / CURRENT / long_term）
platform/obs-recorder/          libobs 原生录制器（源码/构建/私有产物清单；不启动 OBS 界面）
platform/sdl-capture-patch/     a3capture 的 SDL2 私有补丁与部署
platform/audio-protocol/        a3 音频共享内存协议、framing 与 A/V 验收工具
src/auto_heros3/                实现包（MCP server、runtime context、recorder、投影、动作表、探索）
tools/mcp_server_v2.py          MCP 入口兼容壳（module alias → src/auto_heros3/mcp/server.py）
tools/vcmi_agent.py             客户端驱动兼容壳（→ src/auto_heros3/runtime/vcmi_agent.py）
tools/vcmi_build.ps1            MSVC + Ninja + Conan 构建脚本
tools/vcmi_overlay_recorder.py  离屏录屏兼容壳（→ src/auto_heros3/recording/overlay_recorder.py）
tools/fog_path.py               黑幕边界 BFS 探索器兼容壳（→ src/auto_heros3/exploration/fog_path.py）
tools/fog_path_live.py          实机黑幕探索演示 CLI
tools/fog_battle.py             战斗机械执行器（演示/诊断用；正式对局由 LLM 决策）
tests/offline/                  离线回归套件（46 套 / 1529 断言，统一入口 tools/run_offline_suite.py）
tests/live/                     隔离 live 套件（11 套；tools/test_*.py 保留兼容入口）
tools/bridge_contract.json      锁定客户端 sha256 / protocol / 冻结行为
agent/h3/                       Track A 零售版逆向（历史保留，当前主轨不使用）
var/                            运行期状态、证据、录像（部分目录不入 git）
doc/                            工程文档 / HANDOVER / LOG（本地维护，不入 git）
```

### 验证与测试

- MCP 自检：`.\.venv\Scripts\python.exe -X utf8 tools\mcp_server_v2.py --selftest`
  （打印能力清单并做一次租约 acquire/release；不会启动游戏）。
- 离线回归：`tests/offline/` 共 **46 套 / 1529 项断言**，统一入口一次跑完（显式白名单，11 个 live
  套件默认跳过），输出 `N/N checks passed` 且退出码 0 表示通过（2026-11-10 复核 **1529/1529 PASS**；
  含 native 录制媒体/停止/故障注入套件）：

  ```powershell
  .\.venv\Scripts\python.exe -X utf8 tools\run_offline_suite.py
  .\.venv\Scripts\python.exe -X utf8 tests\offline\test_fog_path.py
  ```

  历史入口 `tools/test_*.py` 保留为兼容壳（转发到 `tests/{offline,live}/`）。

- 隔离 live 套件（`tests/live/test_*_live_*.py`）会启动离屏客户端做真实链路验证，一次只跑一个，
  结束后必须停止客户端。

### 状态与路线图

- 已有：打补丁的 VCMI 1.7.5 客户端（协议 2、已固定哈希）、标准 MCP 工具面（**37 个工具**，
  含探索 operation/cancel 合同）、
  英雄/战斗/法术/神器观察、冒险与战斗施法、黑幕边界探索、存档/读档身份核验、事件持久化、
  离屏录像（默认 native libobs：1080p60 + AAC 44.1k，900s/1.5GB 分片与恢复）、玩家/对局记忆、
  单写者租约与"诚实状态"合同。
- 对局结果：
  - 2026-10-25 辅助测试档 WIN（外部 wiki + 子代理，**不作为公平证据**）；
  - 2026-10-26 正式候选局 WIN（day 28，固定图、未注入旧局坐标/外部攻略；
    独立验收结论 `ACCEPT_WITH_DISCLOSURE`）；
  - **2026-10-28 干净局 WIN**：`Maps/Key to Victory`（Easy / 红 Castle / 随机英雄 / 金币奖励），
    day 68 胜利，墙钟 **41m27s**；无作弊、无读档、无调试资源、无外部攻略，单 LLM 玩家，
    **452 条动作来源全为 `llm`**；3 段 1920×1080 全程录像独立审计 **3/3 PASS**。
    本地证据：`var/recordings/`（三段源视频 + `_meta/` 中的 manifest、三段审计与 WIN 证据）。
  - 边界：以上胜局均为**固定图**（地图熟悉度已披露），单局 ≠ 跨地图胜率；
    **盲图 / 多图的正式公平验收仍未完成**。
- 路线图：
  1. 盲图 / 多图（未见过的地图）正式公平胜局与验收材料；
  2. 战斗 AI 改进（集火 / 风筝 / 撤退判断；当前由 LLM 依据 `battle_actions` 决策）；
  3. 法术与神器：换装/背包操作、两阶段法术（传送/牺牲）目标序列；
  4. 覆盖更多真实 UI（造船厂、任务窗、魔法行会、市场窗口）；
  5. 录制收尾：真实游戏内容逐事件 A/V、任意播放器兼容、断电恢复与自动中途 fallback；演示成片与可复现实验。

### 参考项目与致谢

本项目在设计与实现阶段参考/使用了以下开源项目：

- **[VCMI](https://vcmi.eu/)**（[GitHub](https://github.com/vcmi/vcmi)，**GPL-2.0-or-later**）—
  英雄无敌 3 的开源引擎重实现。本项目在其 1.7.5 源码上打最小补丁并在本地构建
  （补丁源在 [`platform/vcmi-patch/`](platform/vcmi-patch/)，保留出处与许可证）。
- **[OBS Studio / libobs](https://obsproject.com/)**（[GitHub](https://github.com/obsproject/obs-studio)，
  **GPL-2.0-or-later**）— 自有原生录制器链接 **libobs 32.1.0** 并选择性加载其模块
  （win-capture/WGC、obs-x264、obs-ffmpeg/AAC、obs-outputs 等）；不启动 OBS 界面、不使用 OBS 场景配置。
  `platform/obs-recorder/custom-output/` 是 OBS Studio 32.1.0 `plugins/obs-outputs` 的衍生修改，
  按 GPL-2.0-or-later 分发；对应源码、修改与许可证声明见 `platform/obs-recorder/NOTICE.md`
  与 `platform/obs-recorder/custom-output/README.md`。
- **[SDL2](https://www.libsdl.org/)**（[GitHub](https://github.com/libsdl-org/SDL)，**zlib license**）—
  游戏音频 `a3capture`：在 SDL 2.32.10 的音频输出路径上打私有补丁
  （`platform/sdl-capture-patch/`），把 PCM 送入共享内存实时链路；私有补丁源码与构建锁随仓库保留。
- **[Model Context Protocol / MCP](https://modelcontextprotocol.io/)** — 本项目的 agent 接入协议。
  服务端基于 **[官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk)**（**MIT**，
  `mcp==2.3.0`）实现；SDK 文档见 [py.sdk.modelcontextprotocol.io](https://py.sdk.modelcontextprotocol.io/)。
- **[heros3-agent-companion](https://github.com/sibylsea-hub/heros3-agent-companion)** —
  在 VCMI 上让 AI agent 入局的先行项目（mod 本体 **MIT**；其 `tools/vcmi_src/` 为 VCMI 修改版，
  **GPL-2.0-or-later**）。本项目仅作设计参考；其 GM / 改档 / 开图类命令**违反本项目公平性约束，未使用**。
- **[vcmi-llm-opponent](https://github.com/SergiiMytakii/vcmi-llm-opponent)** —
  更早把 LLM 接进 VCMI 当对手的探索项目，作为相关工作参考。
- **[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)** —
  开发期使用的 MCP host；其官方 `@deepseek-ai/dsh-mcp-client` 支持 stdio MCP。
- **[VCMI dependencies](https://github.com/vcmi/vcmi-dependencies/releases/tag/2026-07-07)** —
  VCMI 1.7.5 的官方预编译依赖包（Qt / SDL2 / Boost / ffmpeg 等）。
- **[FFmpeg](https://ffmpeg.org/)** — 录像编码。

以下大模型也参与了本项目的开发：GPT-6 astra, GLM 5.3, GLM 5.3 flash, Kimi K3.1 pro, deepseek v4.1flash 1017。

> 引用规则：若在代码/工具中使用上述项目的代码、数据或接口，必须在相关源码头与本文注明出处
> （仓库链接 + 许可证）。

### 免责声明

本项目不包含任何原版游戏文件。《魔法门之英雄无敌》是 Ubisoft 的商标，本项目与 Ubisoft、VCMI 团队均无关联。
运行需要玩家自备正版游戏，请遵守所在地区的法律与游戏许可协议。
`debug_resources` 为引擎调试通道，调用会写入 ledger 并标记 `debug-cheat`，不得用于公平性证据。
VCMI 本体及其衍生补丁遵循 GPL-2.0-or-later；原版游戏数据不在本项目分发范围内。

### 许可证

本项目以 **GPL-2.0-or-later**（GNU General Public License v2.0 或更高版本）发布，完整文本见 [LICENSE](LICENSE)。

原因：`platform/vcmi-patch/` 是 VCMI（GPL-2.0-or-later）的衍生补丁，
`platform/obs-recorder/custom-output/` 是 OBS Studio 32.1.0 `plugins/obs-outputs` 的衍生修改，
均按 GPL 要求以同一许可证分发；为保持一致，仓库内的原创代码也在同一许可证下发布。
第三方组件保留各自许可证（VCMI、OBS Studio/libobs、x264：GPL-2.0-or-later；SDL2：zlib；
MCP Python SDK：MIT 等）。

---

## English

`auto-heros3` turns [VCMI](https://vcmi.eu/) — the open-source reimplementation of the *Heroes of Might and
Magic III* engine — into a game that an **LLM agent can play as a real player**:

- the agent connects through a **standard MCP server** (stdio), reads the **player-visible** state with
  `game_observe`, and issues **player-legal** actions with `game_act`;
- every action passes the engine's normal player validation (`CCallback` / `BattleAction`) — **no GM
  commands, no save editing, no map reveal, no peeking at AI state**;
- key operations (recruiting units, hiring a tavern hero) **simulate real mouse clicks through the actual
  game UI**, so a spectator can watch the whole interaction;
- every action comes with before/after verification and evidence (ledger / events / video), so runs are
  auditable and replayable.

> Long-term goal: the agent manages, recruits, fights and sieges its way to winning a full scenario on its own.
> Current status: the patched client and the MCP tool surface are working and end-to-end games have been
> played. **There are now two unaided wins on the fixed map `Key to Victory` (Easy / red Castle): a
> formal-candidate run on 2026-10-26 (day 28, acceptance `ACCEPT_WITH_DISCLOSURE`) and a clean run on
> 2026-10-28 (day 68, 41m27s wall clock; no cheats, no load/save-scumming, no debug resources, no external
> guides, a single LLM player, all 452 ledger actions from `llm`, 3 independently audited 1080p recording
> segments).**
> Disclosure: the fixed map has been played repeatedly (map familiarity disclosed); one game is not a
> cross-map win rate, and the **blind-map / multi-map formal acceptance is still pending**.
> Recorder: the default is now our own native **libobs / a3capture / shared_memory** pipeline (default
> promotion completed 2026-11-09; a real 90-minute VCMI recording was reviewed and accepted 2026-11-10);
> the explicit `legacy`/`disk` fallback is retained; offline regression **46 suites / 1529 assertions** all
> pass (re-checked 2026-11-10: 1529/1529).

### Features

| Area | Capability | Entry points |
|---|---|---|
| Observe | resources/date/heroes/army/towns/visible map objects | `game_observe` `game_brief` |
| Observe | full hero detail (primary stats / skills / spellbook / artifacts / mana / luck, morale / specialty) | `game_brief` `game_observe` |
| Observe | battle stacks/hexes/turn order/legal actions/available spells | `game_observe` `battle_actions` |
| Observe | path preview (engine pathfinder, teleport/embark flags) | `path_preview` |
| Economy | build, recruit, upgrade, split/merge/transfer stacks, market trade | `game_act` `town_info` `army_info` |
| Heroes | hire a second hero (tavern), move, move near, center the camera, open the hero screen | `game_act` `move_near` |
| Magic | adventure and battle spellcasting; two-stage spells (Teleport/Sacrifice) refused explicitly | `game_act` `battle_actions` |
| Battle | attack (walk-and-attack / shot), approach, move, defend, wait, full-round takeover | `battle_*` `game_act` |
| Explore | shroud-border BFS exploration: one read-only whole-level probe + reveal tile by tile | `explore_toward` `path_preview` |
| Dialogs | blocking dialogs become `query` / `answer` | `game_events` `game_act` |
| UI | town/tavern/hero-equipment screens (real windows) / widget tree / simulated clicks / close window (visible to spectators) | `game_act` `ui_tree` |
| Evidence | save/load (identity-checked), events, ledger, off-screen recording (default native libobs: 1920×1080/60 + H.264 8Mbps + AAC 44.1k stereo, 900s/1.5GB rollover and crash recovery) | `save_game` `load_game` `recording_*` |
| Memory | player / game / long-term memory (CAS writes with revisions) | `memory_read` `memory_write` |

> **Pathfinding caveat**: the client pathfinder treats every **never-revealed shroud tile** as `BLOCKED`
> (VCMI `PathfinderUtil.h`), so `reachable=false` from `path_preview` only means that the target is not in
> the currently revealed connected region — it does **not** prove that it is unreachable. When normal
> progress is stuck, use `explore_toward` to step along the whole shroud border and reveal it; it is a
> brute-force fallback for being stuck, not the regular pathfinder.

### Installation

> **Windows 10/11 x64 only** for now: off-screen capture, process muting and the single-writer lease rely on
> Win32 APIs. Linux/macOS are untested.
> This repository ships **no VCMI binaries** and **no original game data**; the OBS/libobs runtime and the
> private recording artifacts (`platform/obs-recorder/build-media/`, `platform/obs-recorder/bin/`,
> `platform/sdl-capture-patch/stage/`) are not committed either — build/deploy them locally as described in
> the corresponding READMEs.

#### 0. Requirements

| Component | Requirement |
|---|---|
| OS | Windows 10/11 x64 |
| Original game data | A legally owned *Heroes III Complete / Shadow of Death* copy (Steam or GOG) |
| Git | any recent version |
| PowerShell | Windows PowerShell 5.1 (used to start the client) + PowerShell 7 (build script) |
| Python | 3.14.x (`pyproject.toml` requires >=3.14,<3.15; tested 3.14.2) |
| uv | creates `.venv` and installs the frozen `uv.lock` environment |
| MSVC | Visual Studio 2022 or newer Build Tools with "Desktop development with C++" and the Windows SDK |
| CMake | a recent version |
| Ninja | a recent version |
| Conan | 2.x (in a separate venv) |
| ffmpeg | optional; only the legacy recording backend and some diagnostics need it (the default native recorder does not) |
| MCP client | any stdio-capable MCP host |

> VCMI's official prebuilt dependencies were compiled with v142; this project builds and links with MSVC
> 14.51 (v143+) and is ABI-compatible in practice.

#### 1. Get the code

```powershell
git clone <your-repo-url> auto-heros3
cd auto-heros3
```

#### 2. Create the Python environment

Dependencies are fully pinned by `pyproject.toml` + `uv.lock` (**52 packages**, tested with CPython 3.14.2):

```powershell
uv venv --python 3.14 .venv
uv sync --frozen
```

- Do not overwrite the frozen environment with an unlocked manual `pip install` list; change
  `pyproject.toml` and re-lock if dependencies must change.
- Verify with `uv sync --frozen --dry-run` (no changes) and `uv lock --check`.
- `pycaw` provides per-process muting; if it is missing, muting is simply skipped.
- The default game-audio path is **SDL a3capture → shared memory → native**; `process-audio-capture`
  (WASAPI process loopback) is kept only for legacy/diagnostic use.

> Optional reverse-engineering / manual / OCR tooling is part of the same lock. Keep the frozen
> environment for reproducibility even if you only need the MCP server.
- The pins above are the versions tested in development; feel free to relax them.

#### 3. Build the patched VCMI client

The client must be built from VCMI 1.7.5 sources with the `AgentBridge` patch. The patch's single source of
truth is [`platform/vcmi-patch/`](platform/vcmi-patch/); the build script applies it automatically (idempotent).

**3.1 Clone VCMI 1.7.5**

```powershell
git clone --depth 1 --branch 1.7.5 https://github.com/vcmi/vcmi vendor\vcmi-src
git -C vendor\vcmi-src submodule update --init --recursive --depth 1
```

**3.2 Install Conan and restore the official prebuilt dependencies**

```powershell
py -3.12 -m venv vendor\conan-venv
vendor\conan-venv\Scripts\python.exe -m pip install conan
vendor\conan-venv\Scripts\conan.exe profile detect

# Official prebuilt dependencies (~108 MB), from
# https://github.com/vcmi/vcmi-dependencies/releases/tag/2026-07-07
New-Item -ItemType Directory -Force vendor\downloads | Out-Null
curl.exe -L -o vendor\downloads\dependencies-windows-x64.txz `
  https://github.com/vcmi/vcmi-dependencies/releases/download/2026-07-07/dependencies-windows-x64.txz
vendor\conan-venv\Scripts\conan.exe cache restore vendor\downloads\dependencies-windows-x64.txz
```

> If you do not have the `py` launcher, replace `py -3.12` with your local Python (3.12 or newer).

**3.3 Resolve dependencies (from inside the VCMI source tree)**

```powershell
Push-Location vendor\vcmi-src
..\conan-venv\Scripts\conan.exe install . --output-folder=conan-msvc --build=never `
  --profile=dependencies\conan_profiles\msvc-x64 `
  -s '&:build_type=RelWithDebInfo' `
  -c 'tools.env.virtualenv:powershell=powershell.exe'
Pop-Location
```

> Keep the output folder name `conan-msvc`: the patch script looks for
> `conan_toolchain.cmake` and `conanrun.ps1` under that path.

**3.4 Point the build script at your toolchain**

Edit the paths at the top of [`tools/vcmi_build.ps1`](tools/vcmi_build.ps1):

```powershell
$vs    = 'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools'  # your VS / Build Tools dir
$ninja = 'C:\msys64\mingw64\bin\ninja.exe'                               # your ninja.exe
# also update the cmake.exe path used later in the script (default: C:\msys64\mingw64\bin\cmake.exe)
```

**3.5 Build**

```powershell
# First build: client + server (single-player needs VCMI_server.exe)
pwsh -NoProfile -File tools\vcmi_build.ps1 -Jobs 16

# Later, client-only incremental builds (first full build ~10 min; incremental ~1-3 min)
pwsh -NoProfile -File tools\vcmi_build.ps1 -Targets VCMI_client.exe -Jobs 16
```

The script first runs `platform\vcmi-patch\apply.py` to inject the patch into `vendor\vcmi-src`, then builds
with MSVC + Ninja. Artifacts:

```text
vendor\vcmi-src\build\bin\VCMI_client.exe
vendor\vcmi-src\build\bin\VCMI_server.exe
vendor\vcmi-src\build\bin\VCMI_lib.dll
```

> - Make sure `vendor\vcmi-src\conan-msvc\conanrun.ps1` exists (created in 3.3); the MCP server dot-sources
>   it before starting the client so that dependency DLLs are on `PATH`.
> - [`tools/bridge_contract.json`](tools/bridge_contract.json) pins the SHA-256 of the development client.
>   **Your locally rebuilt exe will hash differently** and a WARNING is expected on start; as long as
>   `protocol` is still 2, the protocol layer is unaffected.
> - After changing patch sources, rebuild and update `tools/bridge_contract.json` deliberately; a protocol
>   change must be recorded there or startup is refused.

#### 4. Import the original game data

VCMI ships no original art/audio/maps; you need data from a legally owned *Heroes III* copy:

1. Download the official VCMI 1.7.5 Windows package
   ([releases](https://github.com/vcmi/vcmi/releases/tag/1.7.5), `VCMI-Windows.zip`), or use an existing
   VCMI installation.
2. Run `VCMI_launcher.exe` and use its **import data** wizard, pointing it at your legal game install
   (Steam/GOG).
3. Verify the default user directory now contains the data:

```text
%USERPROFILE%\Documents\My Games\vcmi\Data
%USERPROFILE%\Documents\My Games\vcmi\Maps
%USERPROFILE%\Documents\My Games\vcmi\Mp3
```

```powershell
# quick check (should print True)
Test-Path "$env:USERPROFILE\Documents\My Games\vcmi\Data\H3bitmap.lod"
```

> The self-built client reads the same user directory; do not copy data into `vendor\`.
> Keep the source game directory read-only; VCMI only reads from the imported copy.

#### 5. Verify the installation

```powershell
# MCP server self-test: prints the capability list and does one lease acquire/release (does NOT start the game)
.\.venv\Scripts\python.exe -X utf8 tools\mcp_server_v2.py --selftest

# Build artifacts and runtime environment
Test-Path vendor\vcmi-src\build\bin\VCMI_client.exe
Test-Path vendor\vcmi-src\build\bin\VCMI_server.exe
Test-Path vendor\vcmi-src\conan-msvc\conanrun.ps1
```

#### 6. Register the MCP server

Register the server with any stdio-capable MCP client. Generic `mcpServers` config (use absolute paths):

```json
{
  "mcpServers": {
    "auto-heros3": {
      "type": "stdio",
      "command": "E:\\path\\to\\auto-heros3\\.venv\\Scripts\\python.exe",
      "args": ["-X", "utf8", "E:\\path\\to\\auto-heros3\\tools\\mcp_server_v2.py"],
      "env": { "PYTHONIOENCODING": "utf8" }
    }
  }
}
```

With Claude Code:

```powershell
claude mcp add auto-heros3 -- "E:\path\to\auto-heros3\.venv\Scripts\python.exe" -X utf8 "E:\path\to\auto-heros3\tools\mcp_server_v2.py"
```

For DeepSeek Harness (DSH), configure the same stdio command in its MCP client settings.

Acceptance:

1. The client lists `auto-heros3` tools (e.g. `game_status`);
2. Calling `game_status` does **not** start the game (no `VCMI_client.exe` process);
3. Only an explicit `session_start` starts a game.

#### 7. Play the first game

In your MCP client:

1. `game_status()` — confirm no game is running.
2. `session_start(map_name="Maps/Arrogance", difficulty="easy", player_color="red", starting_town="castle")`
   - `map_name` must be a map present in your data directory; `Maps/Arrogance` ships with H3 Complete.
   - Advanced options only apply to a new scenario; to resume, pass `Saves/agent/<name>` or a `*.vsgm1` path.
3. `lease_acquire()` — take the single-writer lease (re-acquire after an MCP process restart).
4. `game_observe()` / `game_brief()` — read the player-visible state.
5. `game_act(action="end_turn")`, `game_act(action="build", args={"town": 0, "id": 8})`,
   `game_act(action="recruit", args={"town": 0, "level": 1, "amount": 12})`, etc. Each action returns a
   `status` and evidence.
6. On decision points (level-up/chest/battle/game-over) call `game_events()` first; answer dialogs with
   `game_act(action="answer", args={"value": 0})`.
7. Battle: `battle_actions()` lists legal targets → `battle_round(target=…)` leads a full round (`target` is
   one of the enemy stack ids returned by `battle_actions()`), or step one action at a time with `game_act`
   (`battle_attack` / `battle_approach` / `battle_move` / `battle_defend` / `battle_wait` / `battle_start`);
   battle spells are listed under `battle_actions()["spells"]` and cast with
   `game_act(action="battle_cast_spell", …)`.
8. Other actions: `game_act(action="open_hero", args={"hero": 0})` opens the hero screen (left open for
   observation/recording); adventure spells: `game_act(action="cast_spell", args={"hero": 0, "spell": <id>})`.
9. When stuck: `explore_toward(hero=0, x=…, y=…, z=…, map_w=36, map_h=36)` walks the shroud border and
   reveals it tile by tile; an active battle or an unanswered query is reported as its own status.
10. Recording: `recording_start()` … `recording_stop()`. The default is our own native process linked
    to **libobs**, with no OBS UI. The supported profile is 1920×1080 CFR60, H.264 at 8Mbps CBR,
    and stereo AAC at 44.1kHz. WGC captures the game with a reasoning/action panel alongside it.
    The existing basic-60 capture contract applies; a 60fps container does not prove 60 distinct frames.
    Rollover defaults to 900 seconds or approaching 1.5GB at closed-GOP keyframes; native evidence records
    the presentation boundary and TIME/SIZE reason. The decimal final-MP4 cap has a 2% trigger reserve;
    finalized files are checked and any excess is partial, never successful.
    Private **SDL a3capture → QPC/sample-index/CRC shared-memory live ring → native source → AAC** captures audio.
    The same PCM is archived independently in a framed file; disk writes do not drive live delivery.
    This path opens no physical playback endpoint and does not change Windows mute/volume. Zero game volume is
    temporarily raised to 30/40 and settings are restored byte-for-byte on session_stop. Audio selection
    follows the current session; missing readiness, mismatched DLLs and old session identity fail closed.
    Native use requires the locally verified checkpoint described in `platform/obs-recorder/README.md`
    and `NOTICE.md`.
    Manual fallback: set `AUTO_HEROS3_AUDIO_DRIVER=disk`, stop and restart the game session, then call
    `recording_start(backend='legacy')` (requires `ffmpeg`). A run never silently changes backends.
11. Finish with `session_stop()` (only closes the client owned by this session).

> The MCP server starts the game **off-screen** (outside the virtual desktop, e.g. -1600,0) with **recording-only game audio**, so
> it never steals your mouse or focus. For command-line debugging you can also use:
> `python -X utf8 tools\vcmi_act.py start --map Maps/Arrogance`, then `state` / `do "end_turn"` / `shot` / `stop`.

### Repository layout

```text
platform/vcmi-patch/            single patch source (AgentBridge C++ + apply.py)
platform/memory_templates/      LLM player memory templates (PLAYER / CURRENT / long_term)
platform/obs-recorder/          native libobs recorder (sources / build / private artifact manifest; no OBS UI)
platform/sdl-capture-patch/     private SDL2 patch and deployment for a3capture
platform/audio-protocol/        a3 shared-memory audio protocol, framing and A/V verification tools
src/auto_heros3/                implementation package (MCP server, runtime context, recorder, projections, action table, exploration)
tools/mcp_server_v2.py          MCP entry compatibility shim (module alias → src/auto_heros3/mcp/server.py)
tools/vcmi_agent.py             client driver compatibility shim (→ src/auto_heros3/runtime/vcmi_agent.py)
tools/vcmi_build.ps1            MSVC + Ninja + Conan build script
tools/vcmi_overlay_recorder.py  off-screen recorder compatibility shim (→ src/auto_heros3/recording/overlay_recorder.py)
tools/fog_path.py               shroud-border BFS explorer compatibility shim (→ src/auto_heros3/exploration/fog_path.py)
tools/fog_path_live.py          live shroud-exploration demo CLI
tools/fog_battle.py             mechanical battle executor (demo/diagnostics; real games are LLM-decided)
tests/offline/                  offline regression suites (46 suites / 1529 assertions; unified entry tools/run_offline_suite.py)
tests/live/                     isolated live suites (11; tools/test_*.py kept as compatibility entries)
tools/bridge_contract.json      pinned client sha256 / protocol / frozen behaviour
agent/h3/                       Track A retail-binary reverse engineering (kept for reference; unused by the main track)
var/                            runtime state, evidence, recordings (parts excluded from git)
doc/                            engineering docs / handover / log (kept locally, not in git)
```

### Verification and tests

- MCP self-test: `.\.venv\Scripts\python.exe -X utf8 tools\mcp_server_v2.py --selftest`
  (prints the capability list and does one lease acquire/release; it does not start the game).
- Offline regression: **46 suites / 1529 assertions** under `tests/offline/`, run through the explicit
  whitelist entry (11 live suites are skipped by default); it prints `N/N checks passed` and exits 0 on
  success (re-checked 2026-11-10: **1529/1529 PASS**, including the native recorder media/stop/fault
  injection suites):

  ```powershell
  .\.venv\Scripts\python.exe -X utf8 tools\run_offline_suite.py
  .\.venv\Scripts\python.exe -X utf8 tests\offline\test_fog_path.py
  ```

  The historical `tools/test_*.py` entries remain as compatibility shims forwarding to
  `tests/{offline,live}/`.

- Isolated live suites (`tests/live/test_*_live_*.py`) start the off-screen client for real end-to-end checks; run one
  at a time and always stop the client afterwards.

### Status and roadmap

- Available now: a patched VCMI 1.7.5 client (protocol 2, pinned hash), a standard MCP tool surface
  (**37 tools**, including the explore operation/cancel contract), hero/battle/spell/artifact observation,
  adventure and battle spellcasting, shroud-border exploration, identity-checked save/load, persisted
  events, off-screen recording (default native libobs: 1080p60 + AAC 44.1k, 900s/1.5GB rollover and
  recovery), player/game memory, single-writer lease and an honest status contract.
- Game results:
  - 2026-10-25 an *assisted* test-slot win (external map wiki + a sub-agent; **not** fair-play evidence);
  - 2026-10-26 a formal-candidate WIN (day 28, fixed map, no old-coordinate/external-guide injection;
    acceptance verdict `ACCEPT_WITH_DISCLOSURE`);
  - **2026-10-28 clean-game WIN**: `Maps/Key to Victory` (Easy / red Castle / random hero / gold bonus),
    victory on day 68, wall clock **41m27s**; no cheats, no load/save-scumming, no debug resources, no
    external guides, a single LLM player; **all 452 ledger actions from `llm`**; 3 full 1920×1080 recording
    segments independently audited **3/3 PASS**. Local evidence: `var/recordings/` (the three source
    videos plus the `_meta/` manifest, segment audit and WIN evidence).
  - Boundary: all those wins are **fixed-map** games (map familiarity disclosed); one game is not a
    cross-map win rate, and the **blind-map / multi-map fair-play acceptance is still pending**.
- Roadmap:
  1. blind-map / multi-map (unseen maps) fair-play win and acceptance material;
  2. better battle AI (focus fire / kiting / retreat decisions; currently the LLM decides from `battle_actions`);
  3. spells and artifacts: equipment/backpack actions and two-stage spell (Teleport/Sacrifice) target sequences;
  4. more real UI coverage (shipyard, quest windows, mage guild, market window);
  5. recording wrap-up: per-event A/V on real game content, arbitrary-player compatibility, power-loss
     recovery and automatic mid-run fallback; demo videos and reproducible experiments.

### References and acknowledgements

This project was designed and implemented with reference to (and use of) the following open-source projects:

- **[VCMI](https://vcmi.eu/)** ([GitHub](https://github.com/vcmi/vcmi), **GPL-2.0-or-later**) — the
  open-source Heroes III engine. This project patches its 1.7.5 sources with a minimal bridge and builds them
  locally (patch source: [`platform/vcmi-patch/`](platform/vcmi-patch/); attribution and license are kept).
- **[OBS Studio / libobs](https://obsproject.com/)** ([GitHub](https://github.com/obsproject/obs-studio),
  **GPL-2.0-or-later**) — the native recorder links **libobs 32.1.0** and selectively loads its modules
  (win-capture/WGC, obs-x264, obs-ffmpeg/AAC, obs-outputs, …); it never starts the OBS frontend or uses OBS
  scene profiles. `platform/obs-recorder/custom-output/` is a derivative of OBS Studio 32.1.0
  `plugins/obs-outputs`, distributed under GPL-2.0-or-later; corresponding sources, modifications and
  license notices are in `platform/obs-recorder/NOTICE.md` and `custom-output/README.md`.
- **[SDL2](https://www.libsdl.org/)** ([GitHub](https://github.com/libsdl-org/SDL), **zlib license**) —
  the game-audio `a3capture` path patches SDL 2.32.10's audio output (`platform/sdl-capture-patch/`) to feed
  PCM into the shared-memory live stream; the private patch sources and build lock ship with the repository.
- **[Model Context Protocol / MCP](https://modelcontextprotocol.io/)** — the agent-facing protocol used here.
  The server is built on the **[official Python SDK](https://github.com/modelcontextprotocol/python-sdk)**
  (**MIT**, `mcp==2.3.0`); SDK docs: [py.sdk.modelcontextprotocol.io](https://py.sdk.modelcontextprotocol.io/).
- **[heros3-agent-companion](https://github.com/sibylsea-hub/heros3-agent-companion)** — an earlier project
  that lets an AI agent join VCMI as a player (mod: **MIT**; its `tools/vcmi_src/`: **GPL-2.0-or-later**).
  Used as a design reference only; its GM / save-editing / map-reveal commands **violate this project's
  fairness constraints and are not used**.
- **[vcmi-llm-opponent](https://github.com/SergiiMytakii/vcmi-llm-opponent)** — an earlier exploration of
  LLM opponents in VCMI, referenced as related work.
- **[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)** — the MCP host used during
  development; its official `@deepseek-ai/dsh-mcp-client` supports stdio MCP.
- **[VCMI dependencies](https://github.com/vcmi/vcmi-dependencies/releases/tag/2026-07-07)** — official
  prebuilt dependencies for VCMI 1.7.5 (Qt / SDL2 / Boost / ffmpeg, …).
- **[FFmpeg](https://ffmpeg.org/)** — video encoding.

The following large language models also took part in the development: GPT-6 astra, GLM 5.3, GLM 5.3 flash, Kimi K3.1 pro, deepseek v4.1flash 1017.

> Attribution rule: whenever code, data or interfaces from these projects are used, the source file headers
> and this document must state the origin (repository link + license).

### Disclaimer

This project contains no original game files. *Heroes of Might and Magic* is a trademark of Ubisoft; this
project is not affiliated with Ubisoft or the VCMI team. You need your own legal copy of the game, and you
must comply with the laws and game licenses of your jurisdiction. `debug_resources` is an engine debug
channel; every use is written to the ledger and tagged `debug-cheat` and must never be used as fair-play
evidence. VCMI itself and the derived patch are licensed under GPL-2.0-or-later; original game data is not
distributed by this project.

### License

This project is released under **GPL-2.0-or-later** (GNU General Public License v2.0 or any later version);
the full text is in [LICENSE](LICENSE).

Rationale: `platform/vcmi-patch/` is a derivative patch of VCMI (GPL-2.0-or-later) and
`platform/obs-recorder/custom-output/` is a derivative of OBS Studio 32.1.0 `plugins/obs-outputs`; both must
be distributed under the same license, and to stay consistent the original code in this repository is
released under the same license as well. Third-party components keep their own licenses (VCMI,
OBS Studio/libobs, x264: GPL-2.0-or-later; SDL2: zlib; MCP Python SDK: MIT; etc.).
