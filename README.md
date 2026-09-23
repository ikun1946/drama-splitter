# AI 短剧智能分集器

按《AI短剧智能分集器 V1.2.0 可实施修订方案》落地的桌面工具。
本仓库是**阶段1（媒体与手动分集闭环）的完整实现**，并附阶段2 所需的约束求解与版本机制。

定位：AI 辅助分集、用户审核、程序精确导出。**默认不重排、不补拍、不生成对白。**

## 界面

五页工作流。截图由 `tools/screenshot_gui.py` 离屏渲染真实界面得到，
预览区显示的是 FFmpeg 解出的**真实解码帧**（可见测试素材自带的帧号条码）。

| 页面 | 截图 |
| --- | --- |
| 导入 | [01-导入](docs/screenshots/01-导入.png) |
| 设置 | [02-设置](docs/screenshots/02-设置.png) |
| 分析 | [03-分析](docs/screenshots/03-分析.png) |
| 审核 | [04-审核](docs/screenshots/04-审核.png) |
| 导出 | [05-导出](docs/screenshots/05-导出.png) |

![审核页](docs/screenshots/04-审核.png)

审核页要求「连看切点前后」，因此提供三种检视方式：播放整集、播放切点前后 ±3 秒、
以及**用已导出的成片连看相邻两集**（后者能发现编码层面的衔接问题，前者只能验证规划）。

## 当前实现范围

已完成（可运行、可验证）：

| 方案条款 | 实现位置 | 状态 |
| --- | --- | --- |
| §9.1 真实时间轴（有理数 + 源时间基准整数 ticks） | `app/core/timebase.py`、`probe.py` | 完成 |
| §2.2 / §9.3 媒体探测、VFR/HDR/多音轨/非零 PTS 诊断 | `app/core/probe.py` | 完成 |
| §4 / §5 分集模式、参数规则、可行性与末集处理 | `app/core/settings.py` | 完成 |
| §5.3 剩余时长约束 | `app/core/settings.py::remaining_feasible` | 完成 |
| §9.2 连续覆盖（边界数组 + 共享边界） | `app/core/plan.py` | 完成 |
| §13.3 帧边界吸附与编辑拦截 | `plan.py::snap_to_frames` / `move_boundary` | 完成 |
| §12.3 方案版本、锁定切点、差异比较 | `plan.py`、`app/gui/state.py` | 完成 |
| §14 精确裁切导出、事务式落盘、失败重试 | `app/core/export.py` | 完成 |
| §14.5 计划/视频/边界/音频/时长五层校验 | `export.py::verify_episode_output` | 完成 |
| §13.1 五页工作流界面 | `app/gui/main_window.py` | 完成 |
| §13.3 帧级预览（解码帧驱动，非播放器近似） | `app/gui/frame_source.py` | 完成 |
| §16 阶段1「不依赖 AI 也能正确剪出指定区间」 | 全部 | 验收通过 |
| §6.1 规则草案兜底路径 | `app/core/planner.py` | 完成 |

未实现（后续阶段，代码中已留出接口与显式提示，不会静默蒙混）：

- **阶段3**：ASR（faster-whisper）、VAD、字幕解析、镜头检测（PySceneDetect）、候选点数据库。
  分析页明确标注"跳过"，不假装做过。
- **阶段4**：多模态剧情判断、候选图 + 动态规划求解、三种策略评分、
  API 请求校验与预算控制。当前规划器是**规则草案**，因此审核卡片**不给出推荐等级**
  ——方案本身禁止编造无法验证的判断。
- **阶段5**：打包安装、VFR 完整帧表、快速复制模式、HDR 转换、单集字幕导出。
  VFR 素材在导入阶段被识别并**拦截在精确导出之外**（§9.3），不会静默错位。

## 环境

- Windows（其他平台应可运行，未验证）
- Python 3.12+（实测 3.13.14）
- **FFmpeg**（实测 9.0 full build，需含 `libx264` 与 `aac`）
  查找顺序：显式目录 → 环境变量 `DRAMA_FFMPEG_DIR` → `PATH` → 已知安装位置

## 安装与运行

```bash
# 依赖（建议用独立虚拟环境）
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 生成合成测试素材（带帧号条码与声音标记）
.venv/Scripts/python.exe tools/make_test_assets.py testdata --seconds 120

# 启动界面
.venv/Scripts/python.exe run.py
```

## 验证

```bash
# 单元与端到端测试（102 项）
.venv/Scripts/python.exe -m pytest tests/ -q

# 界面全链路冒烟（离屏运行，含真实编码与逐帧校验）
.venv/Scripts/python.exe tools/gui_smoke.py
```

测试不依赖目测。`tools/markers.py` 在合成素材的每一帧顶部画 14 位二进制条码编码帧序号，
`tools/verify.py` 把帧号从成片里**读回来**，因此每个断言都能指出"第几帧错了"，
而不是"看起来还行"。

## 目录

```
app/core/          引擎（不依赖界面，可独立测试）
  timebase.py        有理数时间与时间基准
  ffmpeg.py          FFmpeg 定位与进程调用
  probe.py           媒体探测、VFR 判定、兼容性诊断
  settings.py        参数规则与可行性检查（§4、§5）
  plan.py            边界方案模型、帧对齐、编辑与版本
  planner.py         规则分集规划器（§6.1 兜底路径）
  export.py          精确裁切导出与产物校验（§14）
app/gui/           界面（PySide6-Essentials）
  state.py           项目状态与方案版本链
  workers.py         后台工作线程（探测/规划/导出）
  frame_source.py    帧级预览（单帧解码 + 流式播放）
  main_window.py     五页工作流
tools/             开发与验证工具
  markers.py         帧号条码与声音标记规范
  verify.py          帧号读回、提示音检测、音画偏差测量
  make_test_assets.py 合成素材生成
  gui_smoke.py       界面全链路冒烟
  diagnose_*.py      切帧行为诊断（下述三个坑的取证过程）
tests/             102 项自动化测试
testdata/          合成素材（不入版本库）
```

## 实测结论汇编

开发过程中发现了三个**只看容器元数据或肉眼看片子发现不了**的问题，
均已在 `SKILL.md` 之外留下取证脚本（`tools/diagnose_seek.py`、`diagnose_seek2.py`、`diagnose_dup.py`）：

1. **`-t` 会在非零起始 PTS 素材上少切最后一帧。**
   `-t` 从首个输出时间戳起算，而 `-ss` 会让输出时间轴平移，
   于是每次都少一帧。已改为只用 `-frames:v N` 控制视频长度。
   在起始 PTS 为 0 的素材上这个错误被完全掩盖。

2. **`-avoid_negative_ts make_zero` 会引入约一帧的音画偏移。**
   它按包时间戳对齐，而 AAC 编码器延迟使包时间戳与解码样本时间戳差 1024 个采样点。
   实测音画偏差从 9.9ms 恶化到 31.2ms。已移除该选项。

3. **校验读取器默认会补帧，制造"帧数不符"的假警报。**
   默认恒定帧率输出策略在首帧 PTS 不为 0 时补帧填充起始空隙。
   已显式加 `-fps_mode passthrough`。

另外修正了两处设计缺陷：

4. `BoundaryPlan.from_seconds` 原先会**静默把末边界改写为片尾**，
   使"调用方漏给一个边界"变成"方案悄悄少一集"且顺利通过全部校验。
   已改为直接报错，并新增语义明确的 `from_interior_cuts`。

5. 可行性检查原先在提前返回的分支里没算可行集数范围，
   界面于是打印出 `可行集数范围：0 – 0 集` / `2 – 1 集` 这类
   把"未计算"当成"计算结果"的假信息。已调整计算顺序并补上可操作的说明文案。

## 验收证据

在 120 秒 / 25fps / 3000 帧的合成素材上，切 4 集（每集 750 帧）：

- 逐帧读回条码，各集帧序列分别为 `0–749`、`750–1499`、`1500–2249`、`2250–2999`
- 四集拼接后**正好等于源片完整序列**（既无重复也无遗漏）
- 每集解码帧数与容器声明一致，均为 750
- 音画偏差 20.1ms（一帧 = 40ms），来自素材自身的 AAC 编码器延迟
- 非法边界移动被阻止且方案未被改动；锁定切点随边界迁移

§19 的四个演算示例（7200/96–144、5700/120–180→32–47 集、2820/94/75.2–112.8、
7296/121.6/97.28–145.92）与 §5.1、§5.3 的示例均作为断言固化在
`tests/test_settings_and_timebase.py` 中。

## 已知限制

- 快速复制模式未启用（§14.2 允许第一阶段不提供），传入会明确报错而非静默降级
- VFR 素材不做精确导出，仅识别 + 提示（完整帧表属阶段5）
- 界面未做高分屏适配与多语言
- 尚未在真实短剧素材上验证，所有性能与效果结论均基于合成素材
