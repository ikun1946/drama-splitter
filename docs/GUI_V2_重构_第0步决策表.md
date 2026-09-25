# GUI V2 重构 · 第 0 步决策表

面向《Drama_Splitter_GUI_V2_重构详细方案》落地前的三项前置决策。
这三项不拍板，后续每一步都会返工。其余 11 条评估项（时间轴降采样、右栏控件、
模型三态、程序设置、`_common.py`、快捷键、VFR 提示等）依赖本表结论，暂不单独决策。

对账基准：本次会话已对完整代码库核对，结论基于实际文件，不是推断。

---

## 决策一：主题是「双套令牌」还是「单套深色」

| 项 | 内容 |
| --- | --- |
| 方案要求 | §31–33 深色底 `#181A1D` / 面板 `#202226` / `#25282C`，单强调色 `#4AA6FF`，圆角 4–6px，无阴影 |
| 现有实现 | `app/gui/theme.py`（480+ 行）已按**浅色**写完全套令牌 + 全局 QSS；`drawer.py`、`app_bar.py` 的 QSS 全部依赖它 |
| 冲突点 | 硬换深色 = 推翻上一版重构（提交 `f0d0899`）的全部 UI 成果，并需重录 6 张截图 |
| 隐藏坑 | `theme.py` 内每条设置 `font-size`/`font-weight` 的规则都手写了 `font-family: {FONT_SYMBOL}, {FONT_FAMILY}`。这是为了让 ⚙🤖🎬 不变豆腐块（Qt 的 QSS 一旦覆盖字号就重置字体族）。**换主题必须整套照抄，漏一条就重现豆腐块** |

**方案 A：双套令牌（推荐）**
- `TOKENS = {"light": {...}, "dark": {...}}`，`apply_theme(app, mode)` 按 mode 取一套，QSS 由模板字符串生成
- 工作量：`theme.py` 改结构 + 两份色值表，约半天
- 收益：拿到主题切换能力，浅色成果零损失，截图工具加 `--theme dark` 参数即可出两套图

**方案 B：只留浅色，暂不做深色**
- 把方案 §31–33 的深色规格记为待办，等 V2 主体完成后再统一做
- 收益：第 1 步立刻开工，不阻塞
- 代价：若最终要做深色，仍要回来做方案 A 的改造

**方案 C：直接换成深色**
- 不推荐。理由：只拿到方案要求的一种观感，却丢掉浅色与已投入的字体回退链成果，且未来若要浅色还得再做一遍双套。

**待拍板：A / B / C**

---

## 决策二：页类拆分是「追 500 行」还是「按页拆文件」

| 项 | 内容 |
| --- | --- |
| 方案要求 | §35 `main_window.py < 500 行`；§42 新建 `pages/` + `widgets/` + `dialogs/` |
| 现有实现 | `main_window.py` **2262 行**，六个页类全在同文件：`ImportPage`(122) / `SettingsPage`(319) / `AnalysisPage`(624) / `ReviewPage`(817) / `ExportPage`(1248) / `ModelPage`(1525)，另有 `MainWindow`(1841) 与模块级 helper |
| 已有基础 | `app/gui/widgets/` 已存在（12 个通用控件：drawer / app_bar / 等），**只缺 `pages/`** |
| 冲突点 | 页类之间共享大量模块级辅助函数与相互引用，且 `_scrollable()`、`PAGES`、`_STEPPER_INDEX` 等是模块级符号。强行压到 500 行会把共享符号挤成循环引用 |
| 数字问题 | 六个页类合计 4655 行。就算全搬走，`MainWindow` 自身仍有约 400 行装配逻辑。500 行是可达的，但**只有把 4655 行全部搬走才可达**，届时 `main_window.py` 只剩装配——这个目标本身合理 |

**方案 A：按页拆文件，不设行数硬指标（推荐）**
- 新建 `app/gui/pages/`，每个页类一个文件；模块级 helper 提到 `app/gui/pages/_common.py`
- `main_window.py` 只留 `MainWindow` + 装配逻辑，预期落到 400–600 行
- 纯搬运，零逻辑改动，每搬一个文件跑一次 `pytest` + `gui_smoke.py`
- 收益：达到方案的真实目标（可维护性），且风险最低

**方案 B：严格按 500 行硬指标**
- 需同时拆分 `MainWindow` 内部逻辑（页面构建、信号接线、状态同步）到 mixin 或 controller
- 代价：引入新抽象层，改动面从「搬运」变成「重构」，回归风险显著上升

**方案 C：先不动，只加 `pages/` 空目录占位**
- 不推荐。只占位不拆分没有任何收益，反而让目录结构自相矛盾。

**待拍板：A / B / C**

---

## 决策三：任务层是「补 BaseWorker 基类」还是「新建 TaskManager」

| 项 | 内容 |
| --- | --- |
| 方案要求 | §46 统一任务系统 `TaskManager`，统一进度 / 取消 / 错误处理 |
| 现有实现 | `app/gui/workers.py`（399 行）已有 5 个 QThread worker：`ProbeWorker` / `PlanWorker` / `AnalysisWorker` / `ExportWorker` / `ModelDownloadWorker` |
| 已有能力 | `AnalysisWorker` 已是**分阶段**设计：字幕解析 → 语音转写 → 镜头与黑场 → 候选点 → 整体规划 → 语义判断 → 整集复核，带 `stage_started` / `stage_progress` / `stage_log` 信号与 `cancel()` |
| 实质判断 | 方案描述的「统一任务系统」**本质已存在**，缺的只是公共基类与「当前活跃任务」的集中登记 |
| 冲突点 | 新建 TaskManager 会打断已跑通的导出链路（事务导出 + 5 层校验 + 原子改名）与转写链路（word-level 时间戳 + 句子重建 + zhconv 兜底） |

**方案 A：补 `BaseWorker` 基类 + 活跃任务登记（推荐）**
- 抽 `BaseWorker`，收敛 `cancel()` / `error` / `finished` / 进度信号
- `MainWindow` 内用一个 dict 记录「当前活跃任务」，供 UI 统一禁用/取消/显示
- 收益：拿到方案想要的统一性，不动已验证的逻辑

**方案 B：新建 TaskManager 重写任务层**
- 不推荐。5 个 worker 的取消语义各不相同（`AnalysisWorker` 取消后 `transcript`/`candidates` 是保留还是回滚，**方案没定义，现有代码也需先确认**），重写会连带该未决问题一起放大

**方案 C：不动任务层**
- 可行。第 1 步只做页类拆分与主题令牌，任务层收敛推到第 2 步之后。代价是方案 §46 暂时无从落地。

**待拍板：A / B / C**

---

## 一页速览

| 决策点 | 方案原要求 | 现有状态 | 推荐 | 主要风险 |
| --- | --- | --- | --- | --- |
| 一、主题 | 深色单套 | 浅色全套已提交，含字体回退链 | A 双套令牌 | 硬换深色导致豆腐块重现 + 重录截图 |
| 二、页类拆分 | 六页类拆出，`< 500 行` | 4655 行全在 `main_window.py`，`widgets/` 已有 | A 按页拆文件 | 追行数会引入新抽象，回归风险高 |
| 三、任务层 | 新建 `TaskManager` | 5 个 QThread worker 已在，含分阶段与取消 | A 补 `BaseWorker` | 重写会打断导出与转写链路 |

三项推荐均为 A。若三项都取 A，第 1 步就是「纯搬运重构、零行为变化」，风险最小。

---

## 三项决策对后续步骤的连带影响

- 决策一取 A → 第 1 步含 `theme.py` 改造；取 B → 第 1 步不含，深色推后
- 决策二取 A → 第 1 步为页类搬运 + `_common.py`；取 B → 第 1 步需额外拆 `MainWindow`
- 决策三取 A → 第 1 步含 `workers.py` 收敛；取 C → 第 1 步仅做前两项

## 第 1 步验收线（三项决策拍板后统一适用）

- `pytest` 仍为 **309 passed / 29 skipped / 0 failed**（共 338 项）
- `python tools/gui_smoke.py` 退出码 0，六页全通过
- `run.py` 的 `VERSION` 与 `pyproject.toml` 的 `version` 仍一致（有 `tests/test_version.py` 守着）
- 若改动 `theme.py`：六张截图需重录，且逐张肉眼确认无豆腐块
- 改动前对 `theme.py` / `workers.py` / `main_window.py` 备份为 `.bak-2026-09-25`
