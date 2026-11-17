# Agent 工作台

多个 AI agent 围着同一块看板干活：它们不互相聊天，只读写任务卡、交接总结和事件日志。
runner 是纯代码，负责派活、轮询异步任务、跑校验、额度用完时改派，本身不调用任何模型。

只需要 macOS 自带的 python3，不用安装任何依赖。

## 先看演示

```bash
./wb demo          # 在 demo/ 里生成两个项目（服装 + 游戏），agent 全部是模拟的，不花额度
demo/wb run        # 启动后会自动打开 http://127.0.0.1:8765
```

演示里能看到：两个项目同时跑、Codex 额度用完后自动改派 Kimi、V 领被校验打回后重做、
异步任务轮询、小模型代读日志，以及三种需要你处理的卡：一张等你在三个选项里做决定、
一张手动任务、一张等你审阅。
页面采用约定版完整布局：顶部项目筛选与皮肤切换，左侧任务队列，中间真实 Agent 工位，下方双页审批笔记本。
左页是从真实事件日志生成的决策记录，右页处理当前要你做的事（方案决定、审阅、手动完成和失败重试），产物里的图片直接显示成缩略图；已完成事项以删除线保留，可按权限重开。
工位显示真实任务、尝试次数、执行状态、耗时信息和运行日志，不伪造进度百分比，也不提供后端并不支持的单工位暂停。
右上角可以切换夜班、2077、软拟物和陶绘四套皮肤，选择会保存在当前浏览器中。
Cinzel、IBM Plex 和 Nunito 字体均已内嵌，离线打开也能使用，不需要安装依赖或联网加载字体。

## 在你的电脑上接入多个 agent

约 15 分钟。需要 python3 3.9 以上（macOS 自带的就行）和 Node.js（用来装各家命令行）。

1. 克隆仓库：
   ```bash
   git clone https://github.com/NOAHnorya/agent-workbench.git && cd agent-workbench
   ```
2. 装三家 agent 的命令行，再装操作桌面 App 用的 `agent-desktop`：
   ```bash
   npm i -g @anthropic-ai/claude-code @openai/codex @moonshot-ai/kimi-code agent-desktop
   ```
3. 每家登录一次：`claude auth login`、`codex login`；Kimi Code 运行一次 `kimi`，按提示登录。
4. 存 SiliconFlow 密钥，小模型读日志要用。二选一：
   ```bash
   security add-generic-password -a agent-workbench -s SILICONFLOW_API_KEY -w sk-...   # 存进 macOS 钥匙串
   echo 'export SILICONFLOW_API_KEY=sk-...' >> ~/.zshrc                               # 或者写成环境变量
   ```
5. 生成配置并检查：`./wb init && ./wb doctor`。四行都是 ✓ 就准备好了。
   只装了其中几家也能用：缺哪家，只是派给它的卡发不出去。在卡上写 `--fallback`，就能自动改派给别家。

接下来按「接入真实项目」建项目、派卡，再按「需要你在自己电脑上确认的地方」每家先用一张小卡试跑。

## 协作规范

克隆下来就生效，不用另外配置。Claude Code、Codex、Kimi Code 都会自动读仓库根目录的 `AGENTS.md`。

| 文件 | 谁读 | 管什么 |
|---|---|---|
| `AGENTS.md`（`CLAUDE.md` 指向它） | 在本仓库里改代码的 agent | 四条规范：和主人协商方案、输出规范、代码约定、操作桌面 App |
| `协作规则.md` | 所有 agent | 哪些事开工前要先商量，方案怎么写，资源怎么调度，待确认的东西怎么交付 |
| `templates/prompt.md` | 工作台派活出去的 agent | 只做卡片上的事，交接总结的格式，不碰凭证，要花积分先问 |

文中的「主人」指运行工作台的你。

**可选**：想让 `协作规则.md` 在工作台以外的项目里也生效，就在仓库目录里运行下面这段，
把它挂进三家的全局配置。重复运行不会重复写入。

```bash
RULES="$(pwd)/协作规则.md"
mkdir -p ~/.claude ~/.codex ~/.kimi-code
grep -qF "$RULES" ~/.claude/CLAUDE.md 2>/dev/null || echo "@$RULES" >> ~/.claude/CLAUDE.md
for f in ~/.codex/AGENTS.md ~/.kimi-code/AGENTS.md; do
  grep -qF "$RULES" "$f" 2>/dev/null || printf '\n开工前先读 %s ，按里面的规则和主人协商方案。\n' "$RULES" >> "$f"
done
```

`协作规则.md` 资源表里的 Hi3D 和 Kimi 3D Make 是作者自己在用的生成工具，换成你手上的工具就行。
如果你给 Codex 配了 Hi3D MCP，在 `workbench.json` 里 codex 的 `cmd` 中、`--json` 前面加上
`"-c", "mcp_servers.hi3d.default_tools_approval_mode=\"approve\""`。这样非交互运行时，Hi3D 工具不用等人点批准。

## 接入真实项目

```bash
./wb init                                   # 生成 workbench.json
./wb doctor                                 # 检查 claude / codex / kimi 命令和 SiliconFlow 密钥
export SILICONFLOW_API_KEY=sk-...           # 小模型查日志用；建议写进 ~/.zshrc

./wb new-project fashion --title "服装设计" --repo ~/path/to/服装项目 --color "#C2477A" --priority 1
./wb new-project ripperdoc --title "赛博义体诊所" --repo ~/Downloads/黑客松：义体诊所 --color "#2F7FD1"

./wb card fashion plan_collar claude "按术语库拆出三种领型并写规格"
./wb card fashion collar_v codex "生成 V 领 3D 模型" --after plan_collar --fallback kimi \
     --check "python3 scripts/check_glb.py assets/collar_v.glb"
./wb card ripperdoc key_art chatgpt "网页版出主视觉" --manual

./wb run
```

`--repo` 是 agent 干活的目录，也就是项目代码所在的文件夹。每个 agent 只在当前卡片所属项目的目录里工作，
两个项目的文件和上下文互不混用。

## 多项目怎么区分

- 每个项目一个文件夹：`projects/<项目>/`，里面有自己的看板、交接总结、运行记录和事件日志。
- agent 是全局共享的：一个 Codex 订阅同时服务所有项目。`max_parallel` 控制它同时接几张卡。
- 派活顺序：先看项目优先级（`--priority`，数字大的优先），同优先级的项目轮流来，不会一个项目把别的饿死。
- 看板顶部可以按项目筛选；工位上的彩色标签显示 agent 正在给哪个项目干活。
- `query-log` 只读本项目的日志，不会把另一个项目的内容带进来。

## 一张卡怎么流转

```
待办 → 进行中 → (等异步任务) → 校验中 → 完成
                     ↑                 │
                     └──── 打回重做 ────┘   超过 max_attempts 次就变成「失败」，等你处理
手动任务：待办 → 等你处理 → 在审批笔记本里「标记完成」
需要审阅：校验通过 → 等你处理 → 在审批笔记本里「通过」或「打回重做」
```

任务卡字段（`projects/<项目>/board/<id>.json`）：

| 字段 | 说明 |
|---|---|
| `assignee` / `fallback` | 首选 agent 和备用 agent |
| `goal` / `spec` / `refs` | 要做什么、硬约束、契约文件路径 |
| `depends_on` | 依赖的卡片；它们的交接总结会自动放进这张卡的提示词 |
| `check` | 校验命令，在项目目录里运行，退出码 0 为通过 |
| `mode: "manual"` | 你在 ChatGPT 网页版等地方手动完成 |
| `review: true` | 通过校验后先等你审阅 |
| `max_attempts` | 最多尝试几次，默认 3 |

## agent 怎么交接

提示词模板在 `templates/prompt.md`。agent 干完活要在最后一条回复里输出 `<handoff>` 交接总结，
runner 从输出里取出来存成 `projects/<项目>/handoffs/<id>.md`，并在里面写上对应的日志编号。

交接总结的小节顺序是固定的，第一节永远是接手的人能直接执行的动作：

1. 下一步
2. 现在能用的（含验证方法）
3. 关键决策与原因
4. 试过但放弃的
5. 未解决 / 风险

agent 遇到需要你拿主意的事时，会把状态写成 `blocked`，并在「需要你决定」一节里给出 2 到 4 个选项。
这张卡会出现在审批笔记本里，选项可以直接点。失败和打回的原因统一写成「位置 / 原因 / 修复」三段。
下一张卡的 agent 会先读到它；需要更多细节时运行 `./wb query-log <项目> <卡片> "问题"`，
由 SiliconFlow 上的小模型读完日志只回答这个问题。

提交了 Hi3D 这类要等很久的任务时，agent 在交接总结里写一个 `job.poll` 检查命令就可以退出，
runner 会按 `every` 秒轮询，完成后再跑校验。

## 需要你在自己电脑上确认的地方

`workbench.json` 里三家命令行的参数是按各家文档写的，请先用一张小卡试跑一次：

- **Claude Code**：`claude -p ... --permission-mode acceptEdits` 允许改文件，但不自动允许执行命令。
  想让它能运行 `wb query-log` 或校验脚本，需要在 `cmd` 里加上 `--allowedTools`。
- **Codex**：`codex exec -s workspace-write` 只能写项目目录，比较安全。
- **Kimi Code**：`kimi -p` 非交互模式会自动批准工具调用，只给它可信的项目目录。
- 额度用完的判断靠 `quota_patterns` 匹配输出文字。如果某家的报错措辞不同，把它加进去。

## 文件一览

```
wb                      命令入口
AGENTS.md               给改这个仓库的 agent 看的四条规范（CLAUDE.md 指向它）
协作规则.md             所有 agent 通用的协商规则
workbench.example.json  agent 配置模板，./wb init 复制成 workbench.json（本机配置，不进仓库）
templates/prompt.md     给 agent 的提示词模板
dashboard/index.html    Agent 办公室看板
workbench/              runner 源码（纯 Python 标准库）
projects/<项目>/        每个项目的看板、交接总结、运行记录、事件日志
logs/                   runner 自身的日志和 agent 状态
```

## 在看板上处理你的事

左侧「一句话说你要什么」可以直接新增真实任务卡，默认交给 Claude Code；没有配置 Claude 时选择第一个可用的 CLI agent。
新卡进入等待队列，由 `./wb run` 自动派发。双页笔记本的右页会列出所有需要你处理的卡片，每张都可以直接在页面上操作：

| 情况 | 能做什么 |
|---|---|
| 等你审阅 | 写审阅意见后「通过」（意见会附在交接总结里给下一棒），或「打回重做」（意见必填，agent 会带着它重做） |
| 手动任务 | 填写完成说明、产物路径和给下一棒的话，「标记完成」 |
| 需要你决定 | 点 agent 给出的选项，补充细节后「把决定交给 agent」 |
| 多次没通过 | 看失败原因，补充说明后「重新排队」，或手动完成 |

这些按钮只接受本机页面发来的请求。终端里也有对应命令：`./wb approve <项目> <卡片> --comment "..."`、
`./wb reject <项目> <卡片> "修改意见"`、`./wb answer <项目> <卡片> "你的决定"`、`./wb done`、`./wb retry`。

常用命令：`./wb status`、`./wb run`、`./wb serve`（只看不派活）、`./wb retry <项目> <卡片>`、`./wb -h`。
