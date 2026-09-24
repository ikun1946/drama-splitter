"""界面层的项目状态容器。

单一事实来源：媒体信息、主对音轨、分集设置、方案版本链。
界面各页只读写这里，不各自缓存副本，避免出现"界面上显示的参数"与
"实际参与计算的参数"不一致（§18.1 配置唯一来源）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..core.ffmpeg import FfmpegBinaries
from ..core.plan import BoundaryPlan
from ..core.probe import MediaInfo
from ..core.settings import SplitSettings

__all__ = ["ProjectState"]


@dataclass
class ProjectState:
    binaries: FfmpegBinaries | None = None

    media: MediaInfo | None = None
    audio_stream_index: int | None = None

    settings: SplitSettings = field(default_factory=SplitSettings)

    # 方案版本链：末尾为当前版本（§12.3 每次自动规划与人工编辑均生成版本）
    plans: list[BoundaryPlan] = field(default_factory=list)
    plan_cursor: int = 0

    output_root: Path | None = None
    project_path: Path | None = None

    # 已导出产物：集号 → (路径, 方案版本)，用于判断是否过期
    exported: dict[int, tuple[Path, int]] = field(default_factory=dict)

    # 阶段3 产物：候选点集合（供审核页展示与阶段2 使用）
    candidates: "object | None" = None

    # 阶段4 产物：整集复核结论（供审核页展示）
    episode_reviews: "object | None" = None

    # ---- 方案版本 ------------------------------------------------------

    @property
    def current_plan(self) -> BoundaryPlan | None:
        if not self.plans:
            return None
        index = max(0, min(self.plan_cursor, len(self.plans) - 1))
        return self.plans[index]

    def add_plan(self, plan: BoundaryPlan, *, as_new_version: bool = True) -> BoundaryPlan:
        """登记新方案。版本号按已有方案自动递增。"""
        if as_new_version:
            plan.version = (max((p.version for p in self.plans), default=0)) + 1
        self.plans.append(plan)
        self.plan_cursor = len(self.plans) - 1
        self._retire_stale_versions(plan.version)
        return plan

    def _retire_stale_versions(self, new_version: int) -> None:
        """新方案产生后，旧版本已导出的产物标记为过期（§14.4）。

        只改内存标记，不做文件改名——删改用户成片必须由用户显式确认。
        """
        self.exported = {
            episode: entry
            for episode, entry in self.exported.items()
            if entry[1] == new_version
        }

    # ---- 便捷访问 ------------------------------------------------------

    @property
    def total_seconds(self):
        return self.media.timeline_duration if self.media else None

    @property
    def audio_track_label(self) -> str:
        if self.media is None or self.audio_stream_index is None:
            return "未选择"
        for track in self.media.audio_tracks:
            if track.index == self.audio_stream_index:
                return track.describe()
        return "未选择"

    def reset_media(self) -> None:
        self.media = None
        self.audio_stream_index = None
        self.plans.clear()
        self.plan_cursor = 0
        self.exported.clear()
