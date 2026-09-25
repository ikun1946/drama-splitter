"""应用配置持久化（模型目录等）。

为什么要落盘
------------
打包成桌面应用后，"模型装在哪"必须由用户可指定并可记住——否则用户换个
盘符或把模型放在共享目录，每次启动都要重新指路。

存放位置：
- 打包运行：可执行文件同级目录下的 `config.json`（便携式，跟包走）；
- 源码运行：项目根目录下的 `config.json`。

配置项极少且都是可读的，所以用 JSON 而不是数据库；读写失败一律降级为
"使用默认值"，绝不让配置问题阻断启动。
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .model_manager import WHISPER_TIERS, spec_by_key

__all__ = ["AppConfig", "config_path", "default_models_root"]


def _app_dir() -> Path:
    """配置与产物的基准目录。打包后取 exe 同级，源码运行取项目根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def config_path() -> Path:
    return _app_dir() / "config.json"


def default_models_root() -> Path:
    """默认模型目录：exe/项目根 下的 `models/`。

    与 `asr.model_search_roots()` 的第一候选保持一致——用户不配置时，
    行为与之前完全一样。
    """
    return _app_dir() / "models"


@dataclass
class AppConfig:
    """可持久化的应用配置。"""

    models_dir: str | None = None       # 用户指定的模型根目录
    endpoint: str = "https://hf-mirror.com"
    asr_model_tier: str | None = None   # 使用中的转写档位；None = 默认档位
    llm_model_key: str | None = None    # 使用中的语义模型（"llm:2B:Q4_K_M"）；None = 自动
    theme: str = "light"                # 界面主题：light / dark
    extra: dict = field(default_factory=dict)

    # ---- 读写 ----------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        target = path or config_path()
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        theme = str(data.get("theme") or "light")
        if theme not in ("light", "dark"):
            theme = "light"
        # "使用中"的档位/模型必须真的存在且类型正确；否则回落 None（= 用默认），
        # 避免手改配置或换目录后指向一个不存在的模型。
        tier = data.get("asr_model_tier")
        if tier not in {name for name, _, _ in WHISPER_TIERS}:
            tier = None
        key = data.get("llm_model_key")
        llm_spec = spec_by_key(str(key)) if key else None
        if llm_spec is None or llm_spec.kind != "llm":
            key = None
        return cls(
            models_dir=data.get("models_dir"),
            endpoint=str(data.get("endpoint") or "https://hf-mirror.com"),
            asr_model_tier=tier,
            llm_model_key=key,
            theme=theme,
            extra=dict(data.get("extra") or {}),
        )

    def save(self, path: Path | None = None) -> Path:
        target = path or config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp.replace(target)
        return target

    # ---- 解析 ----------------------------------------------------------

    def resolved_models_dir(self) -> Path:
        """返回实际使用的模型目录（不存在时返回默认位置，不创建）。"""
        if self.models_dir:
            return Path(self.models_dir)
        return default_models_root()
