# 交付设计 · AI 代装（a-stock-selection）

> 渠道：**AI 代装**（用户把 README 链接 + 指令模板发给 AI，由 AI 读文档后自动安装）
> 依据：[README.md](../README.md)「四、安装」＋「安装前一致性评估」
> 原则：**不改变用户当前使用记录或情况**

---

## 一、交付目标与硬约束

**目标**：把技能装到用户端 AI 平台的技能目录，开箱可用、可被定时任务每 10 分钟调用。

**硬约束（红线，AI 必须遵守）**：

1. **不得覆盖/删除用户现有 `output/` 使用记录**：`config.json`、`positions.json`、`候选池.txt`、`主线存档.json`（必要时先备份）。
2. **公式清单只读**：`docs/定盘实时任务_公式清单.md` 不得改动（它是 selfcheck 的校验依据）。
3. **不装任何第三方库**：项目零依赖，仅用 Python 标准库，禁止 `pip install`。
4. **只出信号不下单**：不接入任何交易接口。

---

## 二、交付物 / 不交付物

| 类别 | 内容 | 说明 |
|---|---|---|
| **交付** | `scripts/`、`docs/`、`references/`、`assets/`、`bin/`、`README.md`、`SKILL.md`、`LICENSE` | 技能本体与说明 |
| **不交付（保留在目标端）** | `output/`（`config.json`/`positions.json`/`候选池.txt`/`主线存档.json`/`cache/`） | **用户使用记录，绝不覆盖** |
| **不交付（噪音）** | `scripts/__pycache__/`、`log/`、`temp/` | 运行中间产物 |

首次安装（目标端无 `output/`）时，创建**空** `output/`（含 `.gitkeep`）以保证可写；已有则原样保留。

---

## 三、交付渠道与触发

- **渠道**：AI 代装。用户把下方「指令模板」原文发给一个**支持联网**的 AI。
- **前置条件**：
  - 目标 AI 平台支持联网抓取 README（或用户已把项目目录放到工作区）；
  - 运行环境有 **Python 3.8+**（`python3 --version` 可查）；
  - 目标端技能目录可写（只读时用 `--data-dir` 指向可写目录，见 README 第六节）。

用户话术（模板全文见 [附录](#附录用户指令模板复制即用)）：

```text
请阅读 https://gitee.com/cvdnn/a_stock_selection/raw/master/README.md
（GitHub 镜像：https://raw.githubusercontent.com/cvdnn/a_stock_selection/master/README.md）
按其中「四、安装 → 方式 A」把 a-stock-selection 技能安装到本平台技能目录（工程仓库名 a_stock_selection）：
1) 先做「安装前一致性评估」（比对本环境已有清单与项目清单）；
2) 安装时保留我现有的 output/ 使用记录，不要覆盖；
3) 安装后执行 python scripts/run.py selfcheck 校验并汇报结果。
```

---

## 四、AI 代装标准流程（SOP）

> 供 AI 执行；每步给出命令与判据，任一步不通过则停止并告知用户。

**步骤 1 · 取文档**
读取 README（及项目内 `docs/定盘实时任务_公式清单.md`），确认目标平台与技能目录（AI 自动检索技能目录；无权限或获取不到时提示用户确认或输入路径）。

**步骤 2 · 安装前一致性评估**（比对"环境已有清单"与"项目自带清单"）
1. 在目标平台技能目录查找是否已有 `docs/定盘实时任务_公式清单.md`（找不到＝全新安装）。
2. 若已有：与项目同名清单逐节比对（红绿灯/主线/龙头/趋势/买点四件套/三不买/卖点/仓位/竞价/时段）与版本标识。
3. 一致→继续；**环境基线有而项目缺→提示用户联系管理员/开发者升级项目**；项目更新→提示环境侧同步升级；仅措辞差异→放行。

**步骤 3 · 备份现有使用记录（仅当目标端已存在 `output/`）**

```bash
cp -r <技能目录>/output  <技能目录>/output.bak.$(date +%Y%m%d%H%M%S)
```

**步骤 4 · 复制技能本体（显式排除 `output/`）**

```bash
# 技能目录名必须是 a-stock-selection（WorkBuddy 以 SKILL.md 所在目录名作为技能名）
# 工程仓库名 a_stock_selection 不变：复制到技能目录时重命名为 a-stock-selection
cp -r <源>/scripts  <源>/docs  <源>/references  <源>/assets  <源>/bin \
      <源>/README.md  <源>/SKILL.md  <源>/LICENSE  <技能目录>/   # <技能目录> 结尾须为 /a-stock-selection
# 显式排除：output/、scripts/__pycache__/、cache/、log/、temp/
```

**步骤 5 · 保留/初始化 `output/`**
- 目标端已有 → 不动作（第 3 步已备份）。
- 目标端没有 → `mkdir -p <技能目录>/output && touch <技能目录>/output/.gitkeep`。

**步骤 6 · 安装后自动校验**

```bash
python <技能目录>/scripts/run.py selfcheck
# 若存在"环境基线清单"，追加一致性比对（不一致非 0 退出）：
python <技能目录>/scripts/run.py selfcheck --baseline <环境清单路径>
```

判据：输出含 `skill_root` 为安装后新路径；清单存在/可读；结尾为 `✓ 安装后校验通过`。

**步骤 7 · 输出【AI 代装报告】并转述给用户**
报告字段：平台 / 技能目录 / 环境清单(路径|有否|版本) / 项目清单(版本) / 差异明细 / 结论 / 建议动作 / selfcheck 结果。

---

## 五、平台适配

> 技能自包含、使用相对路径定位自身，复制到任何位置都能运行；安装后技能目录名须与 `SKILL.md` 的 `name` 一致（`a-stock-selection`；工程仓库名 `a_stock_selection` 不变）。

| 平台 | 技能目录（示例） | 启动器 | 备注 |
|---|---|---|---|
| **WorkBuddy（主）** | `<WorkBuddy技能目录>/a-stock-selection` | `bin/a_stock_selection.cmd`（Win）/ `.ps1` | 技能目录由 AI 自动检索，失败则询问用户 |
| 其他平台（可选） | 各自 skills 目录 | 按系统选 `.sh`/`.cmd` | hermes / trae / qwenwork 等 |

> AI 安装时**先自动检索本平台的实际技能目录**，不要臆造路径；**无权限或无法获取时，提示用户确认或输入路径**。

---

## 六、安全边界（AI 必须遵守）

- ❌ 禁止 `rm -rf output/`、禁止覆盖 `output/` 内任何既有文件。
- ❌ 禁止修改 `docs/定盘实时任务_公式清单.md`（只读）。
- ❌ 禁止 `pip install` 或引入第三方依赖。
- ❌ 禁止接入交易/下单接口。
- ✅ 仅新增/覆盖"交付物"清单内的代码与文档。

---

## 七、验收清单 & 回滚

**验收（全部通过才算交付成功）**
1. `python scripts/run.py selfcheck` → `✓ 安装后校验通过`；
2. `stage_now` 与当前时段一致（见 README 5.2）；
3. 三内核离线可跑通：
   ```bash
   python scripts/run.py --offline auto --stage preopen
   python scripts/run.py --offline auto --stage watch
   python scripts/run.py --offline auto --stage daily
   ```
4. 用户既有 `output/` 内容与安装前一致（未被改写）。

**回滚**
```bash
rm -rf <技能目录>/output
mv <技能目录>/output.bak.<时间戳>  <技能目录>/output
```

---

## 八、异常处理

| 现象 | 处理 |
|---|---|
| `selfcheck` 报"公式清单缺失或不可读" | 安装不完整，重新完整复制（含 `docs/`） |
| `selfcheck --baseline` 不一致（非 0） | 反馈差异给管理员/开发者，升级项目后再装 |
| 技能目录名不符 `a-stock-selection` | 重命名技能目录为 `a-stock-selection`（与 `SKILL.md` 的 name 一致） |
| `Python < 3.8` | 提示用户升级 Python（无需装库） |
| 无网络 | 用离线样例 `--offline` 演示；实盘取数需腾讯/新浪/东财可达 |
| 技能目录只读 | 用 `--data-dir <可写目录>` 或环境变量 `DINGPAN_DATA_DIR` |

---

## 九、交付前置检查（README 要素对照）

交付前确认 README 已包含：安装方式 A/B、安装前一致性评估、selfcheck 说明、使用方式（自然语言 + 时段分流）、数据源与降级说明。缺任一项，先补 README 再交付。

---

## 附录：用户指令模板（复制即用）

```text
请阅读 https://gitee.com/cvdnn/a_stock_selection/raw/master/README.md
（GitHub 镜像：https://raw.githubusercontent.com/cvdnn/a_stock_selection/master/README.md）
按其中「四、安装 → 方式 A」把 a-stock-selection 技能安装到本平台技能目录（工程仓库名 a_stock_selection）。
要求：
1) 先做「安装前一致性评估」（比对本环境已有清单与项目清单），输出评估报告；
2) 安装时保留我现有的 output/ 使用记录，不要覆盖（如已存在请先备份）；
3) 技能目录名使用 a-stock-selection（与 SKILL.md 的 name 一致）；
4) 技能目录路径由你自动检索，无权限或无法获取时再问我确认；
5) 安装后执行 python scripts/run.py selfcheck 校验并汇报结果。
```