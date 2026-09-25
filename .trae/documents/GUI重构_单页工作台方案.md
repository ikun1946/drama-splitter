# GUI 重构：单页工作台 + 轻量时间轴

## Context（为什么做）

现状 GUI 同时存在三层复杂度，都已用代码证据核实：

- **界面呈现**：chrome 叠了四层（顶栏含 stepper + 状态条 + 页脚 + 检查器）；导入区的「④ 兼容性」与右侧检查器「问题」页签是同一份 `check_compatibility` 结果；分析日志同时进 [LogPanel](file:///c:/Users/cct20/Desktop/drama-splitter/app/gui/pages/analysis_page.py) 与检查器（[workbench_page.py](file:///c:/Users/cct20/Desktop/drama-splitter/app/gui/pages/workbench_page.py#L161-L164) 的 `_append_with_mirror`）；stepper 与状态条各报一遍进度；导入区四张编号卡片 ①②③④。
- **代码结构**：[main_window.py](file:///c:/Users/cct20/Desktop/drama-splitter/app/gui/main_window.py) 的 `PAGES` 把页面与抽屉混在一个索引表，派生 `_STEPPER_INDEX` / `_STACK_INDEX` / `DRAWER_INDICES` 三张映射；不可见的 `StepNav` 当导航数据模型（`setVisible(False)` 却驱动 `currentRowChanged`）；一批为旧接口保留的垫片：`PreviewPanel` 空派生类、`btn_prev/btn_next`、`_legacy_nav_state`、`QMessageBox` 重导出、隐藏的 `QStatusBar`、`WorkbenchPage.__getattr__` 属性转发与三处「包装私有回调」（`_start/_on_stage/_on_probed`）。
- **视觉风格**：[media_preview.py](file:///c:/Users/cct20/Desktop/drama-splitter/app/gui/widgets/media_preview.py#L42-L47) 仍内联硬编码深色底；按钮档位（primary/ghost/mini/compact）散落各处；卡片标题样式不统一。

已拍板的决策：**激进重做信息架构 → 单页工作台**；新增**轻量时间轴**（切点标记 + 播放头 + 拖拽，不做缩略图）；**允许同步修改 tests/ 与 tools/**（因此可删垫片）。

目标：用户不再"切页"，只有状态推进；日志/问题/进度各只出现一处；代码里不再有兼容垫片与索引体操。

---

## 一、目标信息架构

四阶段由 `ProjectState` 推导（纯函数，无 Qt）：

| 阶段 | 判据 | 左栏（320） | 中栏（自适应） | 右栏（300） | 底栏主动作 |
|---|---|---|---|---|---|
| `EMPTY` | 无 media | 隐藏 | 拖放 hero + 「选择源视频…」 | 检查器（环境：FFmpeg / 模型） | 选择源视频… |
| `READY` | 有 media 无 plan | 源文件、媒体摘要、主对白音轨、兼容性问题表 | 帧级预览 + 时间轴（仅时长条）+ 播放控制 | 检查器 | ▶ 开始分析 |
| `ANALYZING` | 分析线程在跑 | 只读（沿用 READY） | 阶段名 + 进度条 + 最近一条日志 | 检查器（日志页签自动置前） | 取消分析 |
| `REVIEW` | 有 plan | 集列表 + 当前集 + 切点操作（锁定/解除/确认/吸附） | 预览 + 时间轴（切点/锁定/例外/选中集）+ 播放控制 | 检查器（问题/建议/日志）+ 方案层校验徽章 | 导出… |

```
┌ 菜单栏 文件 │ 工具 │ 视图 │ 帮助 ────────────────────────────────┐
├ 顶栏(44px)  源文件名(超 36 字截断, tooltip 全路径)  保存状态  更换视频 ┤
├──────────┬────────────────────────────┬───────────────────────┤
│ 左栏      │ 中栏                        │ 右栏 检查器            │
│ (QStack)  │ (QStack)                    │ 问题 │ 建议 │ 日志      │
│ 素材/集列表│ hero │ 预览+时间轴 │ 分析进度 │ (常驻)                │
├──────────┴────────────────────────────┴───────────────────────┤
│ 底栏(48px) 阶段文案 · 进度 · 提示                [主动作按钮]      │
└───────────────────────────────────────────────────────────────┘
```

三栏各是一个 `QStackedWidget`，由 `stage` 切换；检查器常驻右栏，不再有镜像与重复清单。
菜单：**文件**（打开项目 Ctrl+O / 保存 Ctrl+S / 另存为 / 更换源视频 / 退出）、**工具**（分集设置… / 模型管理… / 导出… Ctrl+E）、**视图**（浅色/深色主题，勾选态）、**帮助**（运行自检 / 关于）。

---

## 二、文件级改动清单

### 新增

| 文件 | 职责 |
|---|---|
| `app/gui/stage.py` | `class Stage(Enum)`、`current_stage(state)`、`primary_action(stage)`、`stage_caption(stage, state)`。纯逻辑，供测试直测 |
| `app/gui/widgets/timeline.py` | `TimelineRuler`（规格见 §三） |
| `app/gui/widgets/stage_view.py` | 中栏舞台：`MediaPreview` + `TimelineRuler` + 播放控制。对外 `set_project(media, plan, exported, selected)` / `goto(seconds)` / `play_episode(i)` / `play_boundary(i)` / `play_stitched(i, j)` / `set_transport_enabled(bool)` |
| `app/gui/widgets/bottom_bar.py` | 单一底栏：`set_stage(caption)` / `set_action(text, enabled, handler)` / `set_progress(value \| None)` / `set_hint(text)`，替代 `status_strip` + `footer` + `hint` 三份 |
| `app/gui/widgets/message.py` | 单一弹窗出口 `information / warning / critical / confirm_continue(parent, title, text, ok_text, cancel_text) -> bool`，替代到处 `QMessageBox` 与 `main_window.QMessageBox` 重导出 |
| `app/gui/dialogs/`（`base.py` + `settings_dialog.py` + `model_dialog.py` + `export_dialog.py` + `__init__.py`） | 三个对话框，承载原页面控件，`base.py` docstring 写明「实例级 exec() 在离屏冒烟里会永久阻塞，测试只走 `build_*_dialog() + show()`」 |

### 改造

- `pages/` → `panes/`（语义纠正：不再有"页"）：
  - `import_page.py → import_pane.py`：`ImportPane` 只保留左栏素材信息职责；删掉 hero 拖放区（上移到中栏空态）、删掉编号卡片与状态标签、删掉「④ 兼容性」整表（与检查器重复）。
  - `analysis_page.py → analysis_pane.py`：`AnalysisPane` 去掉自带 `LogPanel` 与七段 stepper，改为「阶段名 + 进度条 + 最近一条输出」，日志统一走 `inspector.append_log`。
  - `review_page.py → review_pane.py`：`ReviewPane` 只留集列表 + 当前集 + 切点操作；播放/预览/定位全部上移到 `stage_view`。
  - `settings_page.py → settings_form.py`、`model_page.py → model_form.py`、`export_page.py → export_form.py`：纯搬运改名，控件与逻辑原样保留。
  - `_common.py`：`register_worker` 改为沿 `parentWidget()` 上溯找宿主（兼容 QDialog 场景）。
- `pages/workbench_page.py` → 重写为 `app/gui/workbench.py`：三栏装配 + 阶段驱动，**不再需要 `__getattr__` 转发与包装私有回调**——各 pane 暴露公开信号（`import_pane.probed` / `analysis_pane.stage_changed`、`log_appended` / `review_pane.state_changed`），由 workbench 显式 connect。转发与 monkey-patch 之所以必要，是因为原来"谁改了状态"只能靠属性查找；现在改为显式信号。
- [main_window.py](file:///c:/Users/cct20/Desktop/drama-splitter/app/gui/main_window.py)：收缩到约 260 行，只留菜单、装配、项目保存/自动保存、worker 登记、快捷键。删 `PAGES` / `DRAWER_INDICES` / `_STEPPER_INDEX` / `_STACK_INDEX` / `PreviewPanel` / `btn_prev` / `btn_next` / `_legacy_nav_state` / `status_strip` / 隐藏 `QStatusBar` / `_update_stepper_for_drawer` / `Ctrl+1..N` 跳页快捷键。
- `widgets/app_bar.py`：从「标题 + stepper + 5 个按钮」瘦身为「标题 + 保存状态 + 更换视频」。
- [theme.py](file:///c:/Users/cct20/Desktop/drama-splitter/app/gui/theme.py)：新增 `timeline` / `bottom-bar` / `menu` / `video-canvas` / `mono-hud` 角色；两套令牌键集合必须一致（`_validate_tokens` 已守）；`media_preview` 的三处内联样式改角色；统一字号阶（12/13/15/20）与按钮档位（primary / ghost / mini / danger）。
- `widgets/__init__.py`、`panes/__init__.py`：同步导出。

### 删除

`widgets/step_nav.py`（不可见导航模型）、`widgets/step_indicator.py`（stepper 退场，分析进度改用「阶段 n/7 · 名称 + 进度条」）、`widgets/drawer.py`（设置/模型改对话框后无引用，抽屉动画与截图拼接 hack 一并消失）。

---

## 三、时间轴规格（`TimelineRuler`）

**数据来源**（只读、不持副本）：`media.timeline_duration`、`media.snap_to_frame(Fraction)->(帧号, 秒)`、`media.frame_index_at`、`plan.episodes()`、`plan.boundary_ticks`、`plan.locked_ticks`、`plan.exceptions`、`plan.time_base.seconds_to_ticks`、`state.exported`。

**绘制分两层**（性能关键）：
- 静态层：轨道 + 集分段（交替底色，例外集斜纹）+ 内切点标记（锁定实心/普通空心）+ 选中集高亮 + 导出完成集标记 → 绘入缓存 `QPixmap`，键 =（尺寸, 主题模式, plan 结构哈希, 选中集）。改主题/方案/尺寸/选中集才重建。
- 动态层：播放头（1px 竖线 + 三角头），只 `update(QRect)` 新旧播放头区域。

**接口**：

```python
class TimelineRuler(QWidget):
    seek_requested = Signal(float)
    boundary_move_requested = Signal(int, int)   # (边界下标, 新 ticks)
    episode_activated = Signal(int)
    def set_project(self, media, plan, exported, selected_episode) -> None
    def set_selected_episode(self, index: int) -> None
    def set_playhead(self, seconds) -> None
    def set_error(self, boundary_index: int, message: str) -> None
    def invalidate_static(self) -> None
```

**交互**：单击轨道 → `seek_requested`；拖播放头（±6px 命中，move 节流 30Hz）；命中内切点 → 虚线拖拽预览（不写模型），松手先 `media.snap_to_frame` 吸附，再 `time_base.seconds_to_ticks` 转 ticks 发 `boundary_move_requested`；`boundary_ticks[i] in locked_ticks` → 不进入拖拽、光标 `NoDrop`、tooltip「已锁定」；点击分段中部 → `episode_activated`；`setMouseTracking(True)`，悬浮显示「第N集结束 · 时间码 · 帧#k（已锁定）」。

**非法移动反馈**：接线方（`review_pane`）调用 `plan.move_boundary(i, ticks)` —— 该方法越界时**提前返回且不写模型**，所以 `blocking` 非空即"模型未改"，无需回滚。把 problems 转 `InspectorIssue(priority="P0")` 推给检查器、`timeline.set_error(i, 首条描述)` 把该切点画红、底栏 hint 显示原因。合法则更新 `plan.source="manual"` 并发 `state_changed`。

**验收断言**：`set_playhead` 连续 1000 次后静态层重建计数为 0；paint 路径内不调用 `plan.episodes()`（会在每次绘制分配 dataclass 列表）。

---

## 四、设置 / 模型 / 导出 对话框化

- **设置**：`SettingsForm`（原 `collect_settings` / `apply_settings` / `refresh_from_state` / `can_continue` 原样保留）由 `SettingsDialog(QDialog)` 承载；新增「取消即回滚」——打开时快照 `SplitSettings.to_json()`，取消回填。
- **模型**：`ModelForm` 同理由 `ModelDialog` 承载；下载线程仍由 form 持有（`self._worker`），`register_worker` 沿 parentWidget 上溯到 MainWindow。下载进度、实测体积、磁盘预检、删除能力零损失。
- **导出**：`ExportForm`（预设/输出目录/队列/进度/重试/`_start(only)`）搬入 `ExportDialog`；`export_precheck` 与可覆写的 `ExportDialog.request_precheck(warnings, unreviewed) -> "continue" | "review"` 迁到对话框（硬约束仍直接拦截）。「返回审核」= 关闭对话框 + 发 `review_requested(episode, ms)`，MainWindow 收到后：`review_pane.select_episode(episode)` + `stage_view.goto(ms/1000)`（不自动播放）+ `inspector.set_tab(0)` 并高亮该条。

---

## 五、测试与工具链改造

- [tests/test_gui_smoke.py](file:///c:/Users/cct20/Desktop/drama-splitter/tests/test_gui_smoke.py)：删 `test_all_pages_are_present` / `test_page_rows_map_to_stack` / `test_drawer_rows_do_not_switch_stack` / `test_navigation_starts_at_import` / `test_model_button_targets_drawer_row`；设置类用例改为直接构造 `SettingsForm(ProjectState())`；持久化用例改走 `workbench.import_pane.probe()`；§37 前置检查改打 `ExportDialog.request_precheck`，`review_requested` 断言带 `(episode, ms)`；`test_review_ops_buttons_have_hard_minimums` 保留并扩到底栏与对话框按钮。
- 新增 `tests/test_gui_stage.py`（阶段判定纯逻辑）、`tests/test_gui_timeline.py`（几何往返 `_seconds_from_x(_x_from_seconds(s))`、锁定不可拖、非法 move 不改模型、1000 次 set_playhead 零重建）、样式守卫（每个含 `font-size` 的 QSS 块必须同时含 `font-family`，两套主题各查一次）。
- [tools/gui_smoke.py](file:///c:/Users/cct20/Desktop/drama-splitter/tools/gui_smoke.py)：`nav.setCurrentRow(n)` 全部改直接操作 pane / `build_*_dialog()`；`stack.count()==3` 改断言三栏与菜单动作存在；导出段构造 `ExportDialog` 但**不 exec**。逐帧条码回读、帧序列守恒、音画偏差、「无模态阻塞」这些强断言原样保留。
- [tools/screenshot_gui.py](file:///c:/Users/cct20/Desktop/drama-splitter/tools/screenshot_gui.py)：删 `shot_drawer` + 横向拼接 + 抽屉动画等待（抽屉已删），出图改为 7 张：空态 / 素材就绪 / 分析中 / 审核（时间轴）/ 设置 / 模型 / 导出，浅深各一套；对话框只 `show() + pump()`，禁止 `exec()`。
- [run.py](file:///c:/Users/cct20/Desktop/drama-splitter/run.py) `--ui-smoke`：删 stack/PAGES 算术，改断言三栏存在 + 菜单动作存在 + 三个对话框可构造且模型清单非空；`--self-check` 不变。
- 纯逻辑测试（`test_plan.py` / `test_solver.py` / `test_export_e2e.py` / `test_asr.py` 等）不受影响，保持全绿。

---

## 六、分阶段执行（每阶段结束应用可运行、可回滚）

0. **基线**：记录 `pytest -q` 计数、`tools/gui_smoke.py`、`run.py --ui-smoke`、截图工具结果；`theme.py` / `workers.py` / `main_window.py` / `pages/*` 备份为 `.bak-2026-09-25-v3`。
1. **纯新增（零行为变化）**：`stage.py`、`timeline.py`、`bottom_bar.py`、`message.py` + 两个新测试文件。验收：新测试绿、全量计数不变、GUI 未接线故冒烟仍 0。回滚 = 删文件。
2. **pane 解耦**：`pages/` → `panes/`，各 pane 暴露公开信号，去 `__getattr__` 与三处包装回调，`_probe` → `probe`。同步改 tests/tools 引用。验收：`pytest -q` / `gui_smoke.py` / `--ui-smoke` 全绿。
3. **对话框化 + 菜单化**：新增 `dialogs/` 与菜单栏，删 `Drawer` / `StepNav` / `StepIndicator`，弹窗统一走 `message.py`。验收：菜单可开三个对话框；`gui_smoke` 模型段与截图工具通过；设置「取消回滚」有用例。
4. **单页工作台 IA**：重写 `workbench.py` 与 `main_window.py`（瘦身至约 260 行），`AppBar` 瘦身，theme 增补令牌；删 `stack` / `footer` / `status_strip` / 隐藏 `QStatusBar` 与全部垫片。验收：四阶段人工目视 + `pytest -q` + `gui_smoke.py` + `--ui-smoke` + 截图无豆腐块。
5. **时间轴接线 + 导出返回审核定位**：审核操作改由 `review_pane` 经 `TimelineRuler` 信号驱动；导出对话框的「返回审核」定位到首条问题。验收：手工拖切点 → 方案版本递增、左栏与底栏刷新；非法拖拽 → 方案未变且问题进检查器。
6. **视觉收尾 + 文档**：令牌收敛（字号阶/间距/圆角/按钮档位）、`media_preview` 去内联样式、重录 7×2 截图、更新 [README.md](file:///c:/Users/cct20/Desktop/drama-splitter/README.md) 的「界面」与「目录」两节。清理确认：`grep` 中 `StepNav|StepIndicator|Drawer|PAGES|btn_prev|nav.currentRow|__getattr__` 命中为 0。

---

## 七、风险与预防（均为本项目已踩过的坑）

1. **QSS 覆盖字号会重置字体族 → 豆腐块**：凡写 `font-size`/`font-weight` 的规则必须同时写 `font-family: {FONT_SYMBOL}, {FONT_FAMILY}`（新增菜单栏、底栏、对话框标题重点检查）；时间轴标记一律 `QPainter` 画形状，不依赖符号字形；加样式守卫测试。
2. **QSplitter 子控件最小高度之和超可用高度时 `setSizes` 被静默忽略**：三栏最小宽度预算 320+460+300 = 1080，`setMinimumSize(1100, 700)`；左栏/右栏各套滚动容器；保留「setSizes 生效」断言。
3. **QSS `min-height` 不进 `minimumSizeHint` → 按钮被压成细条叠字**：底栏主动作、对话框脚部按钮、时间轴按钮全部显式 `setMinimumHeight(32)`。
4. **实例级模态在离屏冒烟里永久阻塞**（曾挂 25 分钟）：生产只在菜单处理器里 `exec()`；测试/截图只走 `build_*_dialog() + show()`；§37 前置检查保留可覆写方法作为唯一决策缝。
5. **ticks 精度**：拖拽必须 `Fraction → time_base.seconds_to_ticks`，禁止 float 直转；VFR 源在时间轴给出「按时间吸附」提示，未帧对齐只报 P2 不阻断。
6. **`position_changed` 每帧触发**：静态层缓存 + 播放头区域重绘 + 重建计数断言；paint 路径零 `episodes()` 调用。
7. **原地改 plan 无通知 → 底栏/左栏/自动保存脏检测滞后**：所有变更入口（拖切点、锁定、确认、分析完成、探针完成、设置应用）统一发 `state_changed`。
8. **长文件名**：顶栏保留 36 字截断 + tooltip；左栏源文件行与时间轴 hover 同样截断，不出现全路径。
9. **弹窗补丁点分散**：统一走 `widgets/message.py`，根除「monkeypatch 静态方法」这类脆弱手法。
10. **菜单栏占用高度**：新增菜单栏后窗口最小高度需复核，截图尺寸与 `setMinimumSize` 同步调整。

---

## 八、验证方式

```bash
.venv/Scripts/python.exe -m pytest tests/ -q           # 全量；预期 0 failed
.venv/Scripts/python.exe tools/gui_smoke.py            # 全链路冒烟，退出码 0（含逐帧条码回读）
.venv/Scripts/python.exe tools/screenshot_gui.py --theme light
.venv/Scripts/python.exe tools/screenshot_gui.py --theme dark
.venv/Scripts/python.exe run.py --ui-smoke             # 界面完整性
.venv/Scripts/python.exe run.py --self-check           # 打包前提
.venv/Scripts/python.exe -m pyflakes app/gui tools tests   # 零新增告警
```

人工验收：用 `testdata/cfr_120s_25fps.mp4` 走一遍四阶段——拖入即进 READY；点「开始分析」看进度与右栏日志；有方案后在时间轴上拖一个切点（合法 → 左栏与底栏刷新、方案版本递增；非法 → 方案不变且问题进检查器）；打开导出对话框并在前置检查里选「返回审核」，确认落回审核态并定位到问题切点。