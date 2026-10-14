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
from .providers import CAP_ASR, CAP_JUDGE, ProfileRegistry

__all__ = ["AppConfig", "app_dir", "config_path", "default_models_root", "LOCAL_BACKEND"]

#: backend_prefs 里"用本地模型"的取值；其余取值是厂商档案 id（如 "volc"）
LOCAL_BACKEND = "local"


def app_dir() -> Path:
    """配置与产物的基准目录。打包后取 exe 同级，源码运行取项目根。

    需要"用户可写"的路径（配置、缓存、模型目录）都必须走这里：打包后
    源码位于只读的安装目录里，直接用 `__file__` 推导会写进 Program Files。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def config_path() -> Path:
    return app_dir() / "config.json"


def default_models_root() -> Path:
    """默认模型目录：exe/项目根 下的 `models/`。

    与 `asr.model_search_roots()` 的第一候选保持一致——用户不配置时，
    行为与之前完全一样。
    """
    return app_dir() / "models"


@dataclass
class AppConfig:
    """可持久化的应用配置。"""

    models_dir: str | None = None       # 用户指定的模型根目录
    endpoint: str = "https://hf-mirror.com"
    # 更新包下载镜像前缀（默认空 = 直连 GitHub）。国内直连实测只有几十
    # KB/s，填一个加速前缀能快很多；镜像只影响传输，下载完仍按发布页的
    # SHA-256 校验，所以它无法塞进被篡改的包。用法见 README「应用内更新」。
    update_mirror: str = ""
    asr_model_tier: str | None = None   # 使用中的转写档位；None = 默认档位
    llm_model_key: str | None = None    # 使用中的语义模型（"llm:2B:Q4_K_M"）；None = 自动
    theme: str = "light"                # 界面主题：light / dark
    # 厂商 API 档案（云端模型）；空列表 = 使用内置默认档案，且两个开关都是关的
    providers: list[dict] = field(default_factory=list)
    # 每个能力用哪个后端："local" 或档案 id（如 "volc"）
    backend_prefs: dict = field(default_factory=lambda: {CAP_ASR: LOCAL_BACKEND,
                                                         CAP_JUDGE: LOCAL_BACKEND})
    # 云端不可用时是否回退本地（False = 仅云端，失败即降级为规则评分）
    fallback_to_local: bool = True
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
            update_mirror=str(data.get("update_mirror") or "").strip(),
            asr_model_tier=tier,
            llm_model_key=key,
            theme=theme,
            providers=_clean_providers(data.get("providers")),
            backend_prefs=_clean_backend_prefs(data.get("backend_prefs")),
            fallback_to_local=bool(data.get("fallback_to_local", True)),
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

    def profiles(self) -> ProfileRegistry:
        """档案注册表（用户配置 + 内置默认档案合并）。"""
        return ProfileRegistry.from_config(self)

    def backend_for(self, capability: str) -> str:
        """某能力当前选的后端：`"local"` 或档案 id。"""
        return self.backend_prefs.get(capability) or LOCAL_BACKEND

    def active_profile(self, capability: str):
        """某能力当前生效的档案；选本地、档案不存在或未配置该能力时返回 None。

        注意：这里**只看"选了谁"**，不看 enabled / allow_upload——是否真的允许外发
        由 :meth:`ProviderProfile.egress_allowed` 判定。两件事分开，日志才能准确说出
        "你选了云端但没开外发开关"，而不是笼统地说"没启用"。
        """
        backend = self.backend_for(capability)
        if backend == LOCAL_BACKEND:
            return None
        profile = self.profiles().get(backend)
        if profile is None or not profile.supports(capability):
            return None
        return profile


def _clean_providers(raw: object) -> list[dict]:
    """只保留"长得像档案"的条目：dict 且有非空 id。

    损坏的配置项直接丢掉而不是让 load 抛异常——配置问题不允许阻断启动。
    """
    if not isinstance(raw, list):
        return []
    kept: list[dict] = []
    for item in raw:
        if isinstance(item, dict) and str(item.get("id") or "").strip():
            kept.append(item)
    return kept


def _clean_backend_prefs(raw: object) -> dict:
    """两个能力各取一个非空字符串；缺失或类型不对回落 local。"""
    prefs = {CAP_ASR: LOCAL_BACKEND, CAP_JUDGE: LOCAL_BACKEND}
    if isinstance(raw, dict):
        for capability in (CAP_ASR, CAP_JUDGE):
            value = raw.get(capability)
            if isinstance(value, str) and value.strip():
                prefs[capability] = value.strip()
    return prefs
