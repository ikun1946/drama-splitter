"""「一键修复环境」的计划与执行：只修真能修的、顺序固定、失败如实汇报。

后端的能力边界由 tests/test_env_check.py 钉死；这里盯 env_repair 自己的三件事：
1) ``repair_steps`` 只收 auto_fixable 的项，且按「FFmpeg → 引擎 → 转写模型 →
   语义模型」排序（乱序注入也要排对）；
2) ``execute_repair`` 逐步执行、每步结果进 summary，回调按约定触发；
3) 失败不静默、不阻塞后续独立步骤；取消立即停住剩余步骤。
"""

from __future__ import annotations

from app.core.deps_manager import DepsInstallResult
from app.core.ffmpeg_setup import FfmpegInstallResult
from app.core.env_check import (
    FIX_DOWNLOAD,
    FIX_INSTALL_ENGINE,
    FIX_INSTALL_EXTRA,
    FIX_INSTALL_FFMPEG,
    FIX_NAVIGATE,
    FIX_OPEN_URL,
    LEVEL_BLOCK,
    LEVEL_OK,
    LEVEL_WARN,
    EnvCheckItem,
    EnvFix,
    EnvReport,
)
from app.core.env_repair import (
    RepairStep,
    execute_repair,
    repair_steps,
)
from app.core.model_manager import DownloadResult, spec_by_key


def _item(key, level, fix=None, *, label="项", required=True):
    return EnvCheckItem(
        key=key, label=label, level=level, summary="s", fix=fix, required=required
    )


def _report(*items):
    return EnvReport(items=tuple(items))


PLAN_TWO = (
    RepairStep(
        kind=FIX_INSTALL_ENGINE, payload="", label="安装转写引擎", item_key="asr_engine"
    ),
    RepairStep(
        kind=FIX_DOWNLOAD,
        payload="llm:2B:Q4_K_M",
        label="下载语义模型",
        item_key="llm_model",
    ),
)


class TestRepairPlan:
    def test_only_auto_fixable_items_enter_the_plan(self):
        report = _report(
            _item("ffmpeg", LEVEL_OK),
            _item(
                "llm_stack",
                LEVEL_WARN,
                EnvFix(FIX_OPEN_URL, "打开下载页", "https://example.com"),
                required=False,
            ),
            _item(
                "models_dir",
                LEVEL_BLOCK,
                EnvFix(FIX_NAVIGATE, "打开模型管理", "models"),
            ),
            _item(
                "llm_model",
                LEVEL_WARN,
                EnvFix(FIX_DOWNLOAD, "下载语义模型", "llm:2B:Q4_K_M"),
                required=False,
            ),
        )
        plan = repair_steps(report)
        assert [s.payload for s in plan] == ["llm:2B:Q4_K_M"]
        assert plan[0].kind == FIX_DOWNLOAD

    def test_order_engine_then_whisper_then_llm(self):
        # 故意乱序注入：计划必须重排成 引擎 → 转写模型 → 语义模型
        report = _report(
            _item(
                "llm_model",
                LEVEL_WARN,
                EnvFix(FIX_DOWNLOAD, "下载语义模型", "llm:2B:Q4_K_M"),
                required=False,
            ),
            _item(
                "whisper_model",
                LEVEL_BLOCK,
                EnvFix(FIX_DOWNLOAD, "下载 whisper-small", "whisper:small"),
            ),
            _item(
                "asr_engine",
                LEVEL_BLOCK,
                EnvFix(FIX_INSTALL_ENGINE, "安装转写引擎", ""),
            ),
        )
        plan = repair_steps(report)
        assert [s.kind for s in plan] == [FIX_INSTALL_ENGINE, FIX_DOWNLOAD, FIX_DOWNLOAD]
        assert [s.payload for s in plan] == ["", "whisper:small", "llm:2B:Q4_K_M"]
        assert [s.item_key for s in plan] == ["asr_engine", "whisper_model", "llm_model"]

    def test_ffmpeg_comes_before_engine_and_models(self):
        report = _report(
            _item(
                "llm_model",
                LEVEL_WARN,
                EnvFix(FIX_DOWNLOAD, "下载语义模型", "llm:2B:Q4_K_M"),
                required=False,
            ),
            _item(
                "asr_engine",
                LEVEL_BLOCK,
                EnvFix(FIX_INSTALL_ENGINE, "安装转写引擎", ""),
            ),
            _item(
                "ffmpeg",
                LEVEL_BLOCK,
                EnvFix(FIX_INSTALL_FFMPEG, "下载并配置 FFmpeg", ""),
            ),
        )
        plan = repair_steps(report)
        assert [s.kind for s in plan] == [
            FIX_INSTALL_FFMPEG,
            FIX_INSTALL_ENGINE,
            FIX_DOWNLOAD,
        ]
        assert [s.item_key for s in plan] == ["ffmpeg", "asr_engine", "llm_model"]

    def test_extras_come_between_engine_and_models(self):
        report = _report(
            _item(
                "llm_model",
                LEVEL_WARN,
                EnvFix(FIX_DOWNLOAD, "下载语义模型", "llm:2B:Q4_K_M"),
                required=False,
            ),
            _item(
                "whisper_model",
                LEVEL_BLOCK,
                EnvFix(FIX_DOWNLOAD, "下载 whisper-small", "whisper:small"),
            ),
            _item(
                "scenedetect",
                LEVEL_WARN,
                EnvFix(FIX_INSTALL_EXTRA, "安装镜头检测", "scene"),
                required=False,
            ),
            _item(
                "llm_stack",
                LEVEL_WARN,
                EnvFix(FIX_INSTALL_EXTRA, "安装本地推理栈", "llama"),
                required=False,
            ),
            _item(
                "asr_engine",
                LEVEL_BLOCK,
                EnvFix(FIX_INSTALL_ENGINE, "安装转写引擎", ""),
            ),
        )
        plan = repair_steps(report)
        assert [s.kind for s in plan] == [
            FIX_INSTALL_ENGINE,
            FIX_INSTALL_EXTRA,
            FIX_INSTALL_EXTRA,
            FIX_DOWNLOAD,
            FIX_DOWNLOAD,
        ]
        assert [s.payload for s in plan[:3]] == ["", "llama", "scene"]
        llm_step = next(s for s in plan if s.payload.startswith("llm:"))
        assert llm_step.requires_extra == "llama", "语义模型应等待推理栈装好"

    def test_healthy_report_has_empty_plan(self):
        assert repair_steps(_report(_item("ffmpeg", LEVEL_OK))) == ()

    def test_auto_fixable_property(self):
        ok_item = _item("ffmpeg", LEVEL_OK)
        url_item = _item("x", LEVEL_WARN, EnvFix(FIX_OPEN_URL, "打开", "https://x"))
        dl_item = _item("y", LEVEL_BLOCK, EnvFix(FIX_DOWNLOAD, "下载", "llm:2B:Q4_K_M"))
        ffmpeg_item = _item(
            "z", LEVEL_BLOCK, EnvFix(FIX_INSTALL_FFMPEG, "下载并配置 FFmpeg", "")
        )
        assert not ok_item.auto_fixable
        assert not url_item.auto_fixable
        assert dl_item.auto_fixable
        assert ffmpeg_item.auto_fixable


def _fake_install(*, ok=True, cancelled=False, message="引擎安装完成", calls=None):
    def fake(deps_root=None, *, endpoint=None, on_progress=None, cancel_check=None):
        if calls is not None:
            calls.append("install")
        return DepsInstallResult(
            ok=ok,
            cancelled=cancelled,
            message=message,
            installed=1,
            total=1,
            deps_root=deps_root,
        )

    return fake


def _fake_extra_install(*, ok=True, cancelled=False, message="安装完成", calls=None):
    def fake(
        deps_root=None,
        *,
        group="asr",
        endpoint=None,
        on_progress=None,
        cancel_check=None,
    ):
        if calls is not None:
            calls.append(group)
        if on_progress is not None:
            on_progress(10, 100, group, 1, 1)
        return DepsInstallResult(
            ok=ok,
            cancelled=cancelled,
            message=message,
            group=group,
            label=group,
        )

    return fake


def _fake_download(*, ok=True, cancelled=False, message="下载完成", calls=None):
    def fake(
        spec,
        models_root,
        *,
        endpoint=None,
        on_progress=None,
        cancel_check=None,
        verify_remote=True,
    ):
        if calls is not None:
            calls.append("download")
        if on_progress is not None:
            on_progress(10, 100, spec.big_file, 1, 1)
        return DownloadResult(
            spec=spec, ok=ok, cancelled=cancelled, downloaded_bytes=10, message=message
        )

    return fake


def _fake_ffmpeg(*, ok=True, cancelled=False, message="FFmpeg 已配置", calls=None):
    def fake(*, on_progress=None, cancel_check=None, urls=None):
        if calls is not None:
            calls.append("ffmpeg")
        if on_progress is not None:
            on_progress(10, 100)
        return FfmpegInstallResult(
            ok=ok,
            cancelled=cancelled,
            message=message,
            downloaded_bytes=10,
        )

    return fake


class TestExecuteRepair:
    def test_ffmpeg_step_runs_and_reports(self, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(
            "app.core.ffmpeg_setup.install_ffmpeg", _fake_ffmpeg(calls=calls)
        )
        plan = (
            RepairStep(
                kind=FIX_INSTALL_FFMPEG,
                payload="",
                label="下载并配置 FFmpeg",
                item_key="ffmpeg",
            ),
        )
        summary = execute_repair(plan, models_root=tmp_path)
        assert calls == ["ffmpeg"]
        assert summary.results[0].ok
        assert "FFmpeg 已配置" in summary.results[0].message

    def test_extra_group_runs_with_its_group_key(self, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(
            "app.core.deps_manager.install_deps",
            _fake_extra_install(calls=calls),
        )
        plan = (
            RepairStep(
                kind=FIX_INSTALL_EXTRA,
                payload="scene",
                label="安装镜头检测",
                item_key="scenedetect",
            ),
        )
        summary = execute_repair(plan, models_root=tmp_path)
        assert calls == ["scene"]
        assert summary.results[0].ok

    def test_failed_llama_install_skips_model_download(self, monkeypatch, tmp_path):
        report = _report(
            _item(
                "llm_stack",
                LEVEL_WARN,
                EnvFix(FIX_INSTALL_EXTRA, "安装本地推理栈", "llama"),
                required=False,
            ),
            _item(
                "llm_model",
                LEVEL_WARN,
                EnvFix(FIX_DOWNLOAD, "下载语义模型", "llm:2B:Q4_K_M"),
                required=False,
            ),
        )
        plan = repair_steps(report)
        install_calls, download_calls = [], []
        monkeypatch.setattr(
            "app.core.deps_manager.install_deps",
            _fake_extra_install(ok=False, message="网络不可达", calls=install_calls),
        )
        monkeypatch.setattr(
            "app.core.model_manager.download_model",
            _fake_download(calls=download_calls),
        )
        summary = execute_repair(plan, models_root=tmp_path)
        assert install_calls == ["llama"]
        assert download_calls == [], "推理栈没装好时不应下载语义模型"
        assert summary.results[-1].skipped
        assert "跳过" in summary.describe()

    def test_all_steps_succeed(self, monkeypatch, tmp_path):
        monkeypatch.setattr("app.core.deps_manager.install_deps", _fake_install())
        monkeypatch.setattr("app.core.model_manager.download_model", _fake_download())
        summary = execute_repair(PLAN_TWO, models_root=tmp_path, deps_root=tmp_path / "deps")
        assert summary.ok_count == 2
        assert summary.failed_count == 0
        assert summary.cancelled is False
        assert summary.all_ok
        assert "2 项全部成功" in summary.describe()

    def test_failed_step_does_not_block_next(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "app.core.deps_manager.install_deps",
            _fake_install(ok=False, message="网络不可达"),
        )
        monkeypatch.setattr("app.core.model_manager.download_model", _fake_download())
        summary = execute_repair(PLAN_TWO, models_root=tmp_path, deps_root=tmp_path / "deps")
        assert [r.ok for r in summary.results] == [False, True]
        assert summary.failed_count == 1
        assert summary.ok_count == 1
        assert "网络不可达" in summary.describe()

    def test_cancel_stops_remaining_steps(self, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(
            "app.core.deps_manager.install_deps",
            _fake_install(ok=False, cancelled=True, message="已取消", calls=calls),
        )
        monkeypatch.setattr(
            "app.core.model_manager.download_model", _fake_download(calls=calls)
        )
        summary = execute_repair(PLAN_TWO, models_root=tmp_path, deps_root=tmp_path / "deps")
        assert summary.cancelled is True
        assert calls == ["install"], "取消后不该再执行第二步"
        assert "已取消" in summary.describe()

    def test_precancelled_does_nothing(self, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(
            "app.core.deps_manager.install_deps", _fake_install(calls=calls)
        )
        monkeypatch.setattr(
            "app.core.model_manager.download_model", _fake_download(calls=calls)
        )
        summary = execute_repair(
            PLAN_TWO,
            models_root=tmp_path,
            deps_root=tmp_path / "deps",
            cancel_check=lambda: True,
        )
        assert summary.cancelled is True
        assert calls == []
        assert summary.total_steps == 2

    def test_step_callback_and_progress_relay(self, monkeypatch, tmp_path):
        monkeypatch.setattr("app.core.deps_manager.install_deps", _fake_install())
        monkeypatch.setattr("app.core.model_manager.download_model", _fake_download())
        seen_steps = []
        seen_progress = []
        execute_repair(
            PLAN_TWO,
            models_root=tmp_path,
            deps_root=tmp_path / "deps",
            on_step=lambda i, n, label: seen_steps.append((i, n, label)),
            on_progress=lambda done, tot, name, i, n: seen_progress.append((done, tot)),
        )
        assert seen_steps == [(1, 2, "安装转写引擎"), (2, 2, "下载语义模型")]
        assert seen_progress == [(10, 100)]

    def test_unknown_model_key_fails_cleanly(self, monkeypatch, tmp_path):
        monkeypatch.setattr("app.core.model_manager.download_model", _fake_download())
        plan = (
            RepairStep(
                kind=FIX_DOWNLOAD, payload="不存在的模型", label="下载未知", item_key="x"
            ),
        )
        summary = execute_repair(plan, models_root=tmp_path)
        assert summary.failed_count == 1
        assert "无法识别" in summary.results[0].message

    def test_step_exception_is_captured(self, monkeypatch, tmp_path):
        def boom(*args, **kwargs):
            raise RuntimeError("假装的崩溃")

        monkeypatch.setattr("app.core.deps_manager.install_deps", boom)
        monkeypatch.setattr("app.core.model_manager.download_model", _fake_download())
        summary = execute_repair(PLAN_TWO, models_root=tmp_path, deps_root=tmp_path / "deps")
        assert summary.results[0].ok is False
        assert "修复过程异常" in summary.results[0].message
        assert summary.results[1].ok is True, "单步异常不该拖垮后续步骤"

    def test_download_receives_resolved_spec_and_root(self, monkeypatch, tmp_path):
        got = {}
        monkeypatch.setattr("app.core.deps_manager.install_deps", _fake_install())

        def fake(spec, models_root, *, endpoint=None, on_progress=None, cancel_check=None, verify_remote=True):
            got["spec"] = spec
            got["root"] = models_root
            got["endpoint"] = endpoint
            return DownloadResult(spec=spec, ok=True, downloaded_bytes=1)

        monkeypatch.setattr("app.core.model_manager.download_model", fake)
        plan = (PLAN_TWO[1],)
        execute_repair(
            plan, models_root=tmp_path, model_endpoint="https://mirror.example"
        )
        assert got["spec"].key == "llm:2B:Q4_K_M"
        assert got["root"] == tmp_path
        assert got["endpoint"] == "https://mirror.example"
        assert spec_by_key("llm:2B:Q4_K_M") is not None


class TestEngineDependency:
    """引擎缺失时：计划预排转写模型下载，执行时按引擎状态决定跑或跳过。"""

    def test_plan_prepares_whisper_download_when_engine_missing(self, monkeypatch):
        monkeypatch.setattr("app.core.asr.locate_model", lambda tier: None)
        report = _report(
            _item(
                "asr_engine",
                LEVEL_BLOCK,
                EnvFix(FIX_INSTALL_ENGINE, "安装转写引擎"),
            ),
            _item("whisper_model", LEVEL_BLOCK),    # 被引擎压住：报告里没有 fix
        )
        plan = repair_steps(report)
        assert [s.kind for s in plan] == [FIX_INSTALL_ENGINE, FIX_DOWNLOAD]
        assert plan[1].payload.startswith("whisper:")
        assert plan[1].requires_engine is True

    def test_plan_skips_prearranged_step_when_model_present(self, monkeypatch):
        monkeypatch.setattr("app.core.asr.locate_model", lambda tier: object())
        report = _report(
            _item(
                "asr_engine",
                LEVEL_BLOCK,
                EnvFix(FIX_INSTALL_ENGINE, "安装转写引擎"),
            ),
            _item("whisper_model", LEVEL_BLOCK),
        )
        plan = repair_steps(report)
        assert [s.kind for s in plan] == [FIX_INSTALL_ENGINE]

    def test_plan_does_not_duplicate_existing_whisper_step(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "app.core.asr.locate_model", lambda tier: called.append(tier) or object()
        )
        report = _report(
            _item(
                "whisper_model",
                LEVEL_BLOCK,
                EnvFix(FIX_DOWNLOAD, "下载 whisper-small", "whisper:small"),
            ),
        )
        plan = repair_steps(report)
        assert [s.kind for s in plan] == [FIX_DOWNLOAD]
        assert called == [], "已有下载步骤时不该再查模型"

    def test_dependent_step_skipped_when_engine_fails(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "app.core.deps_manager.install_deps",
            _fake_install(ok=False, message="装引擎失败"),
        )
        calls = []
        monkeypatch.setattr(
            "app.core.model_manager.download_model", _fake_download(calls=calls)
        )
        plan = (
            RepairStep(FIX_INSTALL_ENGINE, "", "安装转写引擎", "asr_engine"),
            RepairStep(
                FIX_DOWNLOAD,
                "whisper:small",
                "下载 whisper-small",
                "whisper_model",
                requires_engine=True,
            ),
        )
        summary = execute_repair(plan, models_root=tmp_path, deps_root=tmp_path / "deps")
        assert summary.failed_count == 1, "只有引擎失败计入 failed"
        assert summary.skipped_count == 1
        assert summary.results[1].skipped is True
        assert calls == [], "引擎没装好不该下模型"
        assert "跳过" in summary.describe()

    def test_dependent_step_runs_when_engine_ok(self, monkeypatch, tmp_path):
        monkeypatch.setattr("app.core.deps_manager.install_deps", _fake_install())
        calls = []
        monkeypatch.setattr(
            "app.core.model_manager.download_model", _fake_download(calls=calls)
        )
        plan = (
            RepairStep(FIX_INSTALL_ENGINE, "", "安装转写引擎", "asr_engine"),
            RepairStep(
                FIX_DOWNLOAD,
                "whisper:small",
                "下载 whisper-small",
                "whisper_model",
                requires_engine=True,
            ),
        )
        summary = execute_repair(plan, models_root=tmp_path, deps_root=tmp_path / "deps")
        assert summary.results[1].ok is True
        assert calls == ["download"]
        assert summary.skipped_count == 0
