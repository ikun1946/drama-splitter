"""后台工作线程。

设计依据（§6）：转写、视频解码与编码不得阻塞界面线程。
所有耗时操作（探测、规划、导出）都在这里跑，界面只接信号。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from ..core.export import ExportBatchResult, ExportPreset, Exporter
from ..core.ffmpeg import check_encoders
from ..core.plan import BoundaryPlan
from ..core.planner import PlanningError, plan_rule_based
from ..core.probe import MediaInfo, check_compatibility, probe_media
from ..core.settings import SplitSettings

__all__ = ["ProbeWorker", "PlanWorker", "ExportWorker"]


class ProbeWorker(QThread):
    """媒体探测 + 兼容性判定（§17.1，含解码可用性检查）。"""

    completed = Signal(object, list)   # MediaInfo, list[Issue]
    failed = Signal(str)

    def __init__(self, binaries, path: str | Path, parent=None) -> None:
        super().__init__(parent)
        self._binaries = binaries
        self._path = Path(path)

    def run(self) -> None:  # noqa: D102
        try:
            check_encoders(self._binaries)
            info: MediaInfo = probe_media(self._binaries, self._path)
            issues = check_compatibility(info, self._binaries)
            self.completed.emit(info, issues)
        except Exception as exc:  # noqa: BLE001 - 边界层必须把任何异常变成用户可读信息
            self.failed.emit(str(exc))


class PlanWorker(QThread):
    """生成规则草案方案。

    阶段1/2 使用规则规划器；阶段4 接入多模态剧情判断后，
    这里替换为候选图 + 动态规划，并保留规则结果作为对照基线。
    """

    completed = Signal(object, object, list)  # BoundaryPlan, FeasibilityReport, list[PlanProblem]
    failed = Signal(str)

    def __init__(self, media: MediaInfo, settings: SplitSettings, parent=None) -> None:
        super().__init__(parent)
        self._media = media
        self._settings = settings

    def run(self) -> None:  # noqa: D102
        try:
            plan, report, problems = plan_rule_based(self._media, self._settings)
            self.completed.emit(plan, report, problems)
        except PlanningError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"规划失败：{exc}")


class ExportWorker(QThread):
    """批量导出（§14.4 事务式导出、失败重试、可取消）。"""

    episode_done = Signal(object)   # ExportResult
    completed = Signal(object)      # ExportBatchResult
    failed = Signal(str)

    def __init__(
        self,
        binaries,
        media: MediaInfo,
        plan: BoundaryPlan,
        output_root: Path,
        audio_stream_index: int | None,
        preset: ExportPreset,
        *,
        only_episodes: list[int] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._exporter = Exporter(binaries, media, preset)
        self._plan = plan
        self._output_root = Path(output_root)
        self._audio_stream_index = audio_stream_index
        self._only = only_episodes

    @property
    def exporter(self) -> Exporter:
        return self._exporter

    def cancel(self) -> None:
        self._exporter.cancel()

    def run(self) -> None:  # noqa: D102
        try:
            batch: ExportBatchResult = self._exporter.export_plan(
                self._plan,
                self._output_root,
                audio_stream_index=self._audio_stream_index,
                on_episode_done=self.episode_done.emit,
                only_episodes=self._only,
            )
            self.completed.emit(batch)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"导出失败：{exc}")
