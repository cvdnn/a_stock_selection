# a_stock_selection · A股定盘实时任务

> 技能名：`a-stock-selection` ｜ 工程名：`a_stock_selection` ｜ 版本：v3 主线龙头低吸版（见 `VERSION`）
> 内核：`preopen.py`（竞价）→ `watch.py`（盘中）→ `daily.py`（收盘）

按 `docs/定盘实时任务_公式清单.md`（唯一公式依据，保持只读）完整实现「大盘红绿灯 → 主线判定 → 龙头排序 → 买点/卖点 → 仓位风控」的 A 股盘中实时决策链路，按交易时段自动分流，每 10 分钟运行一次。

- 主仓库：Gitee `https://gitee.com/cvdnn/a_stock_selection` ｜ 镜像：GitHub `https://github.com/cvdnn/a_stock_selection`
- 本目录**既是工程仓库、也是技能包来源**，可直接作为一个完整 skill 安装到各 AI 平台（安装后技能目录名 `a-stock-selection`；工程仓库名 `a_stock_selection` 不变）。

---

## 一、特性

- **零依赖**：仅用 Python 3.8+ 标准库，无需 `pip install`。
- **直连数据源**：腾讯（实时/分时/日K）、新浪（全市场快照/流通市值）、东方财富（板块排行/个股板块/涨停池）。
- **只出信号不下单**：不接入任何交易接口，符合风控铁律定位。
- **自带离线样例**：无网络也能用 `--offline` 跑通全流程，便于验证与演示。
- **跨平台**：以 WorkBuddy 为主要目标平台，hermes / trae / qwenwork 等亦可复制即用。

## 二、目录结构

```
a_stock_selection/
├── README.md                # 项目说明与使用方式（本文件）
├── SKILL.md                 # 技能说明（供 AI 平台识别与路由）
├── VERSION                  # 版本号唯一事实源（形如 v3，与 git tag 同名）
├── scripts/                 # 核心代码
│   ├── common.py            # 取数 / 公式 / 配置 / 状态
│   ├── preopen.py           # 竞价选股内核
│   ├── watch.py             # 盘中盯盘内核
│   ├── daily.py             # 收盘复盘内核
│   └── run.py               # 统一入口（时段分流 + 参数收集）
├── references/formulas.md   # 公式与阈值速查
├── assets/                  # 配置/持仓/候选池示例 + sample 离线样例
├── bin/                     # 启动器 a_stock_selection(.ps1/.cmd)
├── output/                  # 运行数据（候选池.txt / config.json / positions.json / 主线存档.json）
├── log/  temp/              # 日志与中间件
└── docs/定盘实时任务_公式清单.md   # 原始公式清单（只读）
```

## 三、环境要求

- Python 3.8 及以上（Windows / macOS / Linux 均可）。
- 可访问腾讯/新浪/东方财富行情接口的网络（离线模式除外）。

## 四、安装

### 方式 A：让 AI 帮你安装（推荐，全程由 AI 执行）

把**本 README 的链接**发给任意支持联网的 AI，并说明目标平台（WorkBuddy）即可。直接复制以下「用户指令模板」：

```text
请阅读 https://gitee.com/cvdnn/a_stock_selection/raw/master/README.md
（GitHub 镜像：https://raw.githubusercontent.com/cvdnn/a_stock_selection/master/README.md）
按其中「四、安装 → 方式 A」把 a-stock-selection 技能安装到 WorkBuddy 的技能目录：
1) 先做「安装前现有技能检索分析」（按公式清单内容匹配，非名称）：同名→就地升级/复用；异名但内容匹配→装入该技能之下并遵循其规范、不改其既有文件；冲突→先问我覆盖还是独立安装；
2) 再做「安装前一致性评估」（比对本环境已有清单与项目清单），输出评估报告；
3) 安装时保留我现有的 output/ 使用记录，不要覆盖（如已存在请先备份）；
4) 技能目录名使用 a-stock-selection（与 SKILL.md 的 name 一致；工程仓库名仍为 a_stock_selection）；
5) 安装后执行 python scripts/run.py selfcheck 校验并汇报结果。
```

AI 会：读取 README → **先做「安装前现有技能检索分析」**（按清单内容判定升级/宿主挂载/独立安装并遵循现有技能规范）→ 按下方「文件复制位置与目录规范」复制技能本体（**保留 `output/`**）→ 运行 `python scripts/run.py selfcheck` 校验 → 输出【安装评估报告】。

### 方式 B：一行命令复制安装

把项目目录复制到 WorkBuddy 的技能目录即可完成注册，无需改动代码。**安装后技能目录名使用 `a-stock-selection`**（与 `SKILL.md` 的 `name` 一致；工程仓库名仍为 `a_stock_selection`）。技能目录路径由 AI 自动检索：

```bash
# WorkBuddy：克隆后即为技能目录（SKILL.md 所在目录名＝技能名）
git clone https://gitee.com/cvdnn/a_stock_selection.git <WorkBuddy技能目录>/a-stock-selection
```

```bat
:: Windows：复制工程目录为技能目录（源目录名 a_stock_selection，目标技能目录名 a-stock-selection）
xcopy /E /I a_stock_selection "%USERPROFILE%\<WorkBuddy技能目录>\a-stock-selection"
```

> 技能目录路径 AI 自动检索；**无权限或无法获取时，提示用户确认或输入路径**。

### 方式 C：从最新 Release 下载（版本可追溯，无需 git）

不装 git、或希望锁定可追溯的发行版本时，从**最新 release** 的 tag 源码归档安装：

```bash
# 1) 查最新版本 tag（Gitee 主仓库，失败可换 GitHub 镜像）
curl -s https://gitee.com/api/v5/repos/cvdnn/a_stock_selection/releases/latest | grep -o '"tag_name":"[^"]*"'
# GitHub：curl -s https://api.github.com/repos/cvdnn/a_stock_selection/releases/latest | grep -o '"tag_name": *"[^"]*"'

# 2) 下载该 tag 的源码归档（把 <tag> 换成上一步结果，如 v3）
curl -L -o a_stock_selection.zip "https://gitee.com/cvdnn/a_stock_selection/repository/archive/<tag>?format=zip"
# GitHub：curl -L -o a_stock_selection.tar.gz "https://github.com/cvdnn/a_stock_selection/archive/refs/tags/<tag>.tar.gz"

# 3) 解压并复制技能本体到技能目录（重命名为 a-stock-selection，保留已有 output/）
unzip a_stock_selection.zip -d /tmp/a_stock_selection_rel
# 解压目录名可能为 a_stock_selection-<tag>/（GitHub）或 a_stock_selection/（Gitee）
cp -r /tmp/a_stock_selection_rel/a_stock_selection*/ <WorkBuddy技能目录>/a-stock-selection
```

装好后用 `python scripts/run.py version` 查看本地版本并比对最新 release（见 5.3）。

### 文件复制位置与目录规范（AI 安装依据）

本仓库**既是工程仓库、也是技能包来源**：`SKILL.md` 位于仓库根。安装时把技能本体复制到 WorkBuddy 技能目录；**工程仓库名 `a_stock_selection` 保持不变，安装后技能目录名改为 `a-stock-selection`**（与 `SKILL.md` 的 `name` 一致）：

| 开发仓库路径（`a_stock_selection/`） | 复制到技能目录（`a-stock-selection/`） | 说明 |
|---|---|---|
| `SKILL.md` | `a-stock-selection/SKILL.md` | **必需**；frontmatter 的 `name: a-stock-selection` 须与技能目录名一致 |
| `VERSION` | `a-stock-selection/VERSION` | **必需**；版本号唯一事实源，`run.py version` 与 selfcheck 读取 |
| `scripts/` | `a-stock-selection/scripts/` | 三内核 + 入口（排除 `__pycache__/`） |
| `docs/定盘实时任务_公式清单.md` | `a-stock-selection/docs/` | **必需**（selfcheck 校验依据，只读） |
| `references/`、`assets/`、`bin/`、`README.md`、`LICENSE` | 同名路径 | 文档 / 示例 / 启动器 / 许可 |
| `output/` | **不复制** | 用户使用记录；目标端已有则**原样保留**，无则建空目录 |
| `log/`、`temp/`、`scripts/__pycache__/` | **不复制** | 运行时目录 / 中间产物 |

> 技能目录路径由 AI 自动检索；无权限或无法获取时提示用户确认或输入。`docs/` 内除公式清单外的文档（如本「交付设计」）为维护者资料，可选是否随技能包分发。
>
> **检索命中例外**：若「安装前现有技能检索分析」判定为**异名宿主挂载**，则以宿主技能的目录规范为准，本技能装入其下，只新增文件、不改宿主既有文件。

**Skill 目录规范（Agent Skills 标准，WorkBuddy 遵循）**：

```
a-stock-selection/                    # 技能目录名＝技能名（kebab-case，须与 SKILL.md 的 name 一致）
├── SKILL.md                          # 必需：YAML frontmatter(name/description) + 指令正文
├── scripts/  references/  assets/    # 可选资源目录
└── ...                               # 允许额外文件/目录（docs/ bin/ README.md LICENSE）
```

> 合规：`SKILL.md` 位于技能目录根、`name: a-stock-selection` 与技能目录名一致（kebab-case）、资源目录齐备。工程仓库名 `a_stock_selection` 保持不变，仅技能名与安装后技能目录名使用 `a-stock-selection`。

### 安装前现有技能检索分析（推荐先做）

> 目的：先摸清目标环境已有哪些**涉及清单文件**的技能/智能体，据此决定安装方式，避免产生平行重复副本。**以公式清单内容语义匹配，非文件名/技能名比对**——用户环境可能存在与本项目不同的文件名（如 `formula.md`/`公式清单.md`/`清单.txt`）。

```text
安装前现有技能检索分析（推荐先做）：
1) 遍历目标平台技能目录，逐个读取现有技能/智能体，识别是否"涉及清单文件"——存在与项目 docs/定盘实时任务_公式清单.md 内容同源/一致的公式清单文件（文件名可能不同），按章节与阈值语义比对，不得只按文件名或技能名判定。
2) 命中处理三选一：
   · 同名技能（目录名/SKILL.md 的 name 与本项目 a-stock-selection 一致）→ 就地升级/复用该技能，不另复制平行副本；
   · 异名技能/智能体但清单内容匹配 → 装入该现有技能之下，遵循该现有技能的目录规范，只新增本技能文件、不改其既有文件；
   · 冲突（异名技能已占用同名文件或目录规范不兼容）→ 先问用户二选一：①覆盖该现有技能；②独立安装为项目规定的技能名 a-stock-selection。
3) 无命中 → 全新安装为 a-stock-selection。
4) 结果记入【安装评估报告】：命中技能 / 判定（升级|宿主挂载|独立安装）/ 遵循的目录规范 / 是否改动宿主文件。
```

### 安装前一致性评估（推荐）

> 目的：比对**安装环境已有清单**（用户期望/基线）与**本项目自带清单**（当前实现依据），避免安装后不满足用户选股需求。两份清单均只读。

```text
安装前一致性评估（推荐先做）：
1) 在目标平台技能目录查找已存在的 `docs/定盘实时任务_公式清单.md`（找不到=全新安装）。
2) 读取本仓库同名清单，按 10 节逐项比对阈值（红绿灯/主线/龙头/趋势/买点四件套/三不买/卖点/仓位/竞价/时段）与版本标识。
3) 一致→正常安装并执行 `python scripts/run.py selfcheck --baseline <环境清单路径>`（自动校验，非 0 即不通过）；环境基线有而项目缺→提示用户联系管理员/开发者升级项目；项目更新→提示环境侧同步升级；仅措辞差异→放行。
4) 输出【安装评估报告】：平台 / 环境清单(路径|有否|版本) / 项目清单(版本) / 差异明细 / 结论 / 建议动作 / selfcheck 结果。
清单只读；`selfcheck` 会自动校验清单存在性与版本指纹，加 `--baseline <环境清单路径>` 可做一致性比对（不一致时非 0 退出）。
```

### 校验安装

安装后 `selfcheck` 会自动校验：`skill_root` 路径、公式清单是否存在/可读（版本、行数、sha256、章节数）。清单缺失或不可读时**非 0 退出**。

```bash
python scripts/run.py selfcheck                                   # 基础校验，应显示 skill_root 为安装后的新路径
python scripts/run.py selfcheck --baseline /path/to/环境清单.md    # 追加与安装环境基线清单的一致性比对；不一致则非 0 退出
```

> 说明：技能自包含、使用相对路径定位自身，复制到任何位置都能运行；WorkBuddy 以 `SKILL.md` 所在目录名作为技能名，故安装后技能目录名应与 `SKILL.md` 的 `name`（`a-stock-selection`）一致（工程仓库名 `a_stock_selection` 不变）。

## 五、使用方式

本项目主要面向**不懂程序的用户**：无需记忆任何命令，用自然语言告诉 AI 即可，Agent 会自动按交易时段执行并把结论转述给你。

### 5.1 自然语言使用（推荐，面向不懂程序的用户）

装好技能后，直接在对话里说人话即可，例如：

- 「看下现在的大盘红绿灯和主线龙头」
- 「帮我盯盘，有买点/卖点信号就告诉我」
- 「竞价阶段选股，给我龙头排序和买点计划」
- 「收盘复盘，看看明天方向」
- 「我的账户总资金 20 万」「我持有 600519，成本 12.5，止损 11.8，目标 15」
- 「把 600519 归到白酒板块」

Agent 会自动完成：按当前时段分流运行 → 参数缺失时在对话里问你 → 落库 → 用自然语言汇报结论。

> 每 10 分钟的定时任务由平台/Agent 配置，你无需手动执行任何命令。若你偏好自己动手（懂程序的用户），见 5.3 进阶用法。

### 5.2 时段分流

| 时段 | 动作 |
|---|---|
| 9:25-9:35 | 竞价选股 + 主线判定 + 龙头排序 + 买点计划（preopen） |
| 9:35-11:30 / 13:00-15:00 | 盯盘快照 + 买点/卖点信号（watch） |
| 15:00-15:35 | 数据定格中 |
| 15:35-23:59 | 收盘复盘 + 次日方向（daily） |
| 休市（周末或配置节假日） | 一行提示，不跑脚本 |

### 5.3 进阶用法（懂程序的用户）

以下命令由 Agent 内部调用，懂程序的用户也可直接执行。统一入口 `python scripts/run.py`（或 `bin/a_stock_selection`），工作目录任意。

```bash
# 每 10 分钟由定时任务执行：按当前时段自动分流
python scripts/run.py auto

# 调试时可强制指定阶段
python scripts/run.py auto --stage preopen    # 竞价
python scripts/run.py auto --stage watch      # 盘中
python scripts/run.py auto --stage daily      # 收盘

# 结构化输出（便于其他平台二次加工）
python scripts/run.py auto --json

# 无网络演示
python scripts/run.py --offline auto --stage watch

# 版本号：查看本地版本 / 联网比对最新 release（Gitee 主、GitHub 回退）
python scripts/run.py version
python scripts/run.py version --local          # 只看本地，不联网
```

参数落库（脚本不假设你知道配置位置；自然语言使用时由 Agent 代为执行）：

```bash
# 账户总资金（仓位计算必需）
python scripts/run.py config set account.total_capital=200000

# 持仓（成本必填；自设止损/目标价可选）
python scripts/run.py positions add 600519 cost=12.50 stop=11.80 target=15.00 shares=1000

# 候选池板块列回填（供「板块回流」「后排」判定）
python scripts/run.py pool set 600519 白酒

# 交易日历（覆盖期临期/到期时，Agent 联网搜索后落库）
python scripts/run.py calendar show
python scripts/run.py calendar set holidays='["2027-01-01"]' coverage_to='2027-12-31' source='上交所'

# 数据缓存（默认关闭；开启以加速同一时段内的二次响应）
python scripts/run.py config set datasource.cache.enabled=true
python scripts/run.py config set datasource.cache.ttl_seconds=60
python scripts/run.py cache show
python scripts/run.py cache clear

# 离线样例（开发者夹具：校验/生成，替代手改 JSON）
python scripts/run.py sample verify
python scripts/run.py sample gen --codes 600000,000001 --date 2026-10-08

# 查看
python scripts/run.py config show
python scripts/run.py positions list
python scripts/run.py pool
```

交互式终端下也可 `python scripts/run.py config init` / `positions add`（不带参数）逐项问答。

## 六、数据与配置

- 运行数据默认写入项目内 `output/`，可用 `--data-dir <目录>` 或环境变量 `DINGPAN_DATA_DIR` 覆盖（技能目录只读时建议指向可写目录）。
- `output/` 主要文件：`候选池.txt`、`config.json`、`positions.json`、`主线存档.json`。
- 阈值全部来自 `docs/定盘实时任务_公式清单.md`，可在 `config.json` 的 `thresholds` 中覆盖，无需改代码；速查见 [references/formulas.md](references/formulas.md)。
- **交易日历**存于 `config.json` 的 `calendar`（`holidays` / `coverage_to` / `generated_at` / `source` / `lead_days`），内置 2026 年沪深北休市安排基线；覆盖期临期（默认 30 天）或到期时各阶段会输出刷新指引，由 Agent 联网搜索下一期后 `calendar set` 落库。
- **数据源**：仅腾讯/新浪/东财直连；接口不可用时输出「⚠️ 数据源降级」并在 `--json` 的 `degraded` 字段透出，**默认不缓存**，不以旧数据冒充实时。
- **可选数据缓存（默认关闭）**：`config.json` 的 `datasource.cache` 开启后，同一时段内重复取数直接复用落盘结果以加速二次响应；命中会在文本输出与 `--json` 的 `cache` 字段显式标注「⚡ 命中数据缓存」，TTL 到期或跨日实时行情自动回源，绝不冒充实时。**TTL 按数据源差异化**（行情 60s、板块 120s、日K/涨停池 300s、板块归属 600s），可用 `datasource.cache.ttl`（如 `{"daily":600}`）逐项覆盖，`datasource.cache.ttl_seconds` 为全局兜底。管理：`run.py cache show|clear`。
- **离线样例**：`assets/sample/` 是开发者夹具（非用户配置项，仅供 `--offline` 演示与验证）。由 `run.py sample gen` 一键生成、`run.py sample verify` 校验字段契约与跨文件一致性，替代手工编辑 JSON。
- **盘中取数**：盘中把当日实时拼接为日 K 末根，`trend_ok` 的 MA5今/MA5昨/zt5 与「首阴」据此取数；「首阴」的「昨日涨幅」取上一根完整日 K。
- **同板块判定**：候选池缺板块列时自动用东财个股板块补齐；「后排/板块回流」优先检索东财板块成分股，不可用时回退候选池同板块列。

## 七、版本与发布（维护者）

- **版本号唯一事实源**：根目录 [`VERSION`](VERSION)，内容形如 `v3`，与 git tag 同名；采用整数递增 `v1 → v2 → … → vN`。
- **发版流程**：
  ```bash
  # 1) 修改 VERSION（如 v3 → v4）并提交
  # 2) 打同名 tag 并推送双远端（origin=Gitee，github=GitHub）
  git tag -a v4 -m "v4" && git push origin v4 && git push github v4
  # 3) 在两端各建一个「发行版 / Release」（tag 选 v4）
  #    GitHub：gh release create v4 --title v4 --notes "..."   或网页创建
  #    Gitee ：仓库 → 发行版 → 新建（注意：Gitee 的「发行版」须单独创建，仅打 tag 不生成 release）
  ```
- **下载**：安装侧用「方式 C：从最新 Release 下载」；`python scripts/run.py version` 会读取最新 release 的 tag 并提示是否有更新（Gitee 主、GitHub 回退；无 release 或网络不可达时显式降级，不报错）。
- **联动约定**：`docs/定盘实时任务_公式清单.md` 发生变更时，同步递增 `VERSION`。

## 八、许可

仅供个人研究与学习使用，不构成任何投资建议。