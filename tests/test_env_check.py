"""环境自检的检测逻辑测试（纯逻辑，离屏，绝不联网、绝不 exec）。

三条要钉死的约定：
1. 报告能正确汇总严重度（取最严重者）、能正确判断"是否可用"（无 required-block）；
2. **只有模型能在应用内一键修复**——`fix.kind == "download_model"` 仅出现在
   模型类条目上；缺 Python 包（尤其 faster-whisper）绝不能给出下载修复；
3. 检测是**只读**的：跑一遍不得创建/修改任何文件（修复必须由用户点按钮触发）。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from app.core.env_check import (  # noqa: E402
    FIX_DOWNLOAD,
    LEVEL_BLOCK,
    LEVEL_OK,
    LEVEL_WARN,
    EnvCheckItem,
    EnvReport,
    check_environment,
)


class TestReportAggregation:
    """报告汇总：严重度取最严重，可用性只看 required-block。"""

    def test_level_takes_worst(self):
        report = EnvReport(items=(
            EnvCheckItem("a", "A", LEVEL_OK, "ok"),
            EnvCheckItem("b", "B", LEVEL_WARN, "warn", required=False),
            EnvCheckItem("c", "C", LEVEL_BLOCK, "block"),
        ))
        assert report.level == LEVEL_BLOCK

    def test_level_is_ok_when_all_ok(self):
        report = EnvReport(items=(EnvCheckItem("a", "A", LEVEL_OK, "ok"),))
        assert report.level == LEVEL_OK

    def test_optional_block_does_not_make_unusable(self):
        """非必需项哪怕是 block，也不该判"不可用"——可用性只看 required。"""
        report = EnvReport(items=(
            EnvCheckItem("a", "A", LEVEL_OK, "ok"),
            EnvCheckItem("b", "B", LEVEL_BLOCK, "block", required=False),
        ))
        assert report.usable is True

    def test_required_block_makes_unusable(self):
        report = EnvReport(items=(
            EnvCheckItem("a", "A", LEVEL_OK, "ok"),
            EnvCheckItem("b", "B", LEVEL_BLOCK, "block", required=True),
        ))
        assert report.usable is False

    def test_by_key_finds_item(self):
        report = EnvReport(items=(EnvCheckItem("ffmpeg", "F", LEVEL_OK, "ok"),))
        assert report.by_key("ffmpeg").key == "ffmpeg"
        assert report.by_key("nope") is None


class TestCheckEnvironmentRuns:
    """真跑一遍：项齐全、结构合法、且**只读**（不改任何文件）。"""

    def test_reports_all_expected_keys(self):
        report = check_environment()
        keys = {i.key for i in report.items}
        for expected in (
            "ffmpeg", "ffplay", "asr_engine", "whisper_model",
            "llm_model", "llm_stack", "inference_device", "models_dir",
            "scenedetect", "zhconv", "pysubs2",
        ):
            assert expected in keys, f"自检缺少项：{expected}"

    def test_every_item_has_valid_level_and_summary(self):
        report = check_environment()
        for item in report.items:
            assert item.level in (LEVEL_OK, LEVEL_WARN, LEVEL_BLOCK), item.key
            assert item.summary, f"{item.key} 没有结论文案"

    def test_detection_is_read_only(self, tmp_path):
        """检测不得创建目录、不得写文件——修复必须由用户点按钮触发。"""
        probe_root = tmp_path / "not-created-yet"
        before = sorted(p.name for p in tmp_path.iterdir())
        check_environment(models_root=probe_root)
        after = sorted(p.name for p in tmp_path.iterdir())
        assert before == after, "自检过程动过文件系统"


class TestRepairHonesty:
    """修复能力的诚实边界——这是本功能的成败点。"""

    def test_only_model_items_offer_download(self):
        """只有模型类条目才给 download_model 修复。"""
        report = check_environment()
        for item in report.items:
            if item.fix is not None and item.fix.kind == FIX_DOWNLOAD:
                assert item.key in ("whisper_model", "llm_model"), (
                    f"{item.key} 给了下载修复，但它不是模型——应用内修不了"
                )

    def test_missing_python_package_offers_no_download(self, monkeypatch):
        """缺转写引擎时**绝不能**给 `download_model`。

        缺的是**包**不是**模型**——下模型解决不了缺包，给它一个"下载模型"
        按钮就是骗用户。这一条把那个区分固化成断言。

        注意：缺引擎现在**可以**给 `install_engine`（应用内装 wheel），
        所以这里只禁 `download_model`，不禁"能在应用内修"这件事。
        """
        import app.core.env_check as ec

        # 强制转写引擎判定为"不可用"，且没有任何外挂依赖目录
        monkeypatch.setattr(ec, "_module_available", lambda name: False)
        monkeypatch.setattr(
            "app.core.deps_manager.deps_present", lambda *a, **k: False
        )
        report = ec.check_environment()
        item = report.by_key("asr_engine")
        assert item is not None and item.level == LEVEL_BLOCK
        assert item.fix is None or item.fix.kind != FIX_DOWNLOAD, (
            "缺 faster-whisper 时给了下载修复——下载模型解决不了缺包"
        )

    def test_missing_engine_offers_install(self, monkeypatch):
        """缺引擎时应给 `install_engine`——应用内真能装上，这才是对的出口。"""
        import app.core.env_check as ec
        from app.core.env_check import FIX_INSTALL_ENGINE

        monkeypatch.setattr(ec, "_module_available", lambda name: False)
        # 让 deps_manager 探测报告"既没装也不完整"
        monkeypatch.setattr(
            "app.core.deps_manager.deps_present", lambda *a, **k: False
        )
        report = ec.check_environment()
        item = report.by_key("asr_engine")
        assert item is not None and item.level == LEVEL_BLOCK
        assert item.fix is not None, "缺引擎却不给任何出口"
        assert item.fix.kind == FIX_INSTALL_ENGINE, (
            f"缺引擎应给 install_engine，实际给了 {item.fix.kind}"
        )
        assert item.fix.label, "有修复动作就必须有按钮文案"

    def test_present_module_reported_ok(self, monkeypatch):
        """模块可导入时该项应为 ok——检测要能双向表态。"""
        import app.core.env_check as ec

        monkeypatch.setattr(ec, "_module_available", lambda name: True)
        report = ec.check_environment()
        for key in ("asr_engine", "scenedetect", "zhconv", "pysubs2"):
            item = report.by_key(key)
            assert item is not None and item.level == LEVEL_OK, key


class TestLlmStackFix:
    """本地推理栈缺失项：给下载页，但**不给**应用内下载。"""

    def test_missing_stack_offers_release_url_not_download(self, monkeypatch):
        """llama-cpp-python 缺失时应指向官方发布页，且绝不出现 download_model。

        与 faster-whisper 同理：装不了包给个"下载"按钮是骗人。但也不能只留一句
        "装不上"——发布页有 py3-none 轮子，把地址给到，用户能自己解决。
        """
        import app.core.env_check as ec
        from app.core import semantic as sem

        monkeypatch.setattr(sem, "_import_llama", lambda: None)
        report = ec.check_environment()
        item = report.by_key("llm_stack")
        assert item is not None and item.level == LEVEL_WARN
        assert item.fix is not None, "缺推理栈却连指引都没有"
        assert item.fix.kind == "open_url", "应给下载页（open_url），不是应用内下载"
        assert item.fix.kind != FIX_DOWNLOAD
        assert item.fix.payload.startswith("https://github.com/abetlen/llama-cpp-python"), (
            f"下载页地址不对：{item.fix.payload}"
        )
        assert item.fix.label, "有修复动作就必须有按钮文案"

    def test_present_stack_reports_ok_without_fix(self, monkeypatch):
        """推理栈就绪时该项为 ok、且不带任何修复动作。"""
        import app.core.env_check as ec
        from app.core import semantic as sem

        monkeypatch.setattr(sem, "_import_llama", lambda: object())
        report = ec.check_environment()
        item = report.by_key("llm_stack")
        assert item is not None and item.level == LEVEL_OK
        assert item.fix is None or item.fix.kind != FIX_DOWNLOAD


class TestFfmpegDegradation:
    def test_missing_ffmpeg_blocks_and_offers_url(self, monkeypatch):
        from app.core import env_check as ec
        from app.core.ffmpeg import FfmpegNotFound

        def boom(*a, **k):
            raise FfmpegNotFound("未找到 ffmpeg")

        monkeypatch.setattr("app.core.ffmpeg.locate_ffmpeg", boom)
        report = ec.check_environment()
        item = report.by_key("ffmpeg")
        assert item.level == LEVEL_BLOCK
        assert item.fix is not None and item.fix.kind == "open_url"
        assert report.usable is False, "FFmpeg 缺失应判为不可用"


class TestModelDownloadOffered:
    def test_missing_whisper_model_offers_download(self, monkeypatch, tmp_path):
        """引擎就绪、只是缺模型时，应给出 download_model 修复，payload 是可解析的 key。"""
        from app.core import env_check as ec
        from app.core.model_manager import spec_by_key

        monkeypatch.setattr(ec, "_module_available", lambda name: True)  # 引擎在
        monkeypatch.setattr("app.core.asr.locate_model", lambda tier: None)
        report = ec.check_environment(models_root=tmp_path)
        item = report.by_key("whisper_model")
        assert item.level == LEVEL_BLOCK
        assert item.fix is not None and item.fix.kind == FIX_DOWNLOAD
        assert spec_by_key(item.fix.payload) is not None, (
            f"下载 payload {item.fix.payload!r} 不是有效模型 key"
        )

    def test_missing_engine_suppresses_model_download(self, monkeypatch, tmp_path):
        """引擎缺失时**不给**模型下载按钮——下了也用不了，别骗用户等几个 GB。

        这是诚实边界的又一体现：两项结论相互牵连时，结论要反映这种牵连。
        """
        from app.core import env_check as ec

        monkeypatch.setattr(ec, "_module_available", lambda name: False)  # 引擎缺
        monkeypatch.setattr("app.core.asr.locate_model", lambda tier: None)
        report = ec.check_environment(models_root=tmp_path)
        item = report.by_key("whisper_model")
        assert item.fix is None or item.fix.kind != FIX_DOWNLOAD, (
            "引擎装不上时给了模型下载按钮——用户会白等"
        )
        assert "引擎" in item.summary or "引擎" in item.detail


class TestInferenceDeviceItem:
    """自检要说清「现在跑在 CPU 还是 GPU」——用户最关心的性能问题。"""

    def test_reports_which_device_transcription_will_use(self):
        from app.core.env_check import LEVEL_OK, check_environment

        report = check_environment()
        item = report.by_key("inference_device")
        assert item is not None, "缺少「推理设备」一项"
        assert item.level == LEVEL_OK, "它是说明项，不该阻断流程"
        assert item.required is False
        assert "转写：" in item.summary and "语义判断：" in item.summary
        assert "cuda" in item.summary or "cpu" in item.summary

    def test_follows_the_configured_device(self, monkeypatch, tmp_path):
        """配置成仅 CPU 时，自检要跟着说 CPU（而不是仍旧报"有显卡"）。"""
        from app.core import env_check as ec
        from app.core.app_config import AppConfig

        path = tmp_path / "config.json"
        AppConfig(asr_device="cpu").save(path)
        real_load = AppConfig.load.__func__
        monkeypatch.setattr(
            AppConfig, "load", classmethod(lambda cls, where=None: real_load(cls, path))
        )

        item = ec._check_inference_device()
        assert "CPU" in item.summary, item.summary

        AppConfig(asr_device="cuda").save(path)
        monkeypatch.setattr(
            "app.core.asr.cuda_device_count", lambda: 1
        )
        item = ec._check_inference_device()
        assert "GPU" in item.summary, item.summary
