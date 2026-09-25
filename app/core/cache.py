"""缓存层：内容指纹、缓存键与一致性。

设计依据（§12.2 缓存键和一致性）：
- 缓存记录**源文件内容指纹**、音轨、时间范围、ASR/检测器版本、模型ID、
  提示词版本、字幕版本及分析参数。
- **文件名相同不等于素材相同**，换片后不能误用旧缓存。
- 每次开始任务使用不可变的参数快照；运行中修改设置时旧任务不能覆盖新方案。

另据 §12.1，改参数时大部分基础分析（媒体信息、转写、镜头边界）应可复用，
所以缓存必须能把"与参数无关的基础数据"和"依赖参数的结果"分开存放。

另据 §18.2，`cache/` 目录是可重建的，删掉只会导致重算，不应影响项目正确性。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

__all__ = [
    "SourceFingerprint",
    "CacheKey",
    "CacheStore",
    "CACHE_NAMESPACES",
]

# 缓存命名空间。按 §12.1 的分层原则：与分集参数无关的放前面，依赖参数的放后面。
CACHE_NAMESPACES = {
    "media": "媒体信息（与参数无关）",
    "audio": "音频提取与波形统计（与参数无关）",
    "subtitles": "字幕解析结果（与参数无关）",
    "asr": "语音转写（与参数无关）",
    "vad": "语音活动检测（与参数无关）",
    "shots": "镜头切换检测（与参数无关）",
    "candidates": "候选点集合（依赖合并距离等分析参数）",
    "scores": "规则评分（依赖策略权重）",
}

_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SourceFingerprint:
    """源文件内容指纹。

    对整段视频做全量哈希成本过高（一部 121 分钟短剧可达数 GB），因此采用
    **大小 + 修改时间 + 多位置抽样哈希**。这与 §12.2 的要求一致：目标是
    "文件名相同不等于素材相同"能被识别，而不是内容级唯一性证明。

    偏向：宁可过度失效（重算一次），也不可漏判（误用旧缓存）。
    mtime 变化即视为新素材，因此复制文件会导致缓存未命中——这是刻意的取舍。
    """

    size_bytes: int
    mtime_ns: int
    digest: str

    @classmethod
    def of(cls, path: str | Path, *, sample_bytes: int = 512 * 1024, samples: int = 5) -> "SourceFingerprint":
        path = Path(path)
        stat = path.stat()
        size = stat.st_size

        hasher = hashlib.sha256()
        hasher.update(f"size={size}".encode())

        # 在一组相对位置上抽样：开头、结尾，以及中间若干等分点
        if size <= sample_bytes:
            offsets = [0]
        else:
            span = size - sample_bytes
            offsets = sorted(
                {int(span * i / (samples - 1)) for i in range(samples)} if samples > 1 else {0}
            )

        with path.open("rb") as handle:
            for offset in offsets:
                handle.seek(offset)
                hasher.update(offset.to_bytes(8, "big"))
                hasher.update(handle.read(sample_bytes))

        return cls(size_bytes=size, mtime_ns=stat.st_mtime_ns, digest=hasher.hexdigest())

    def short(self, length: int = 12) -> str:
        return self.digest[:length]

    def describe(self) -> str:
        return f"{self.size_bytes / 1024 / 1024:.1f}MB/{self.short()}"

    def to_json(self) -> dict:
        return {"size_bytes": self.size_bytes, "mtime_ns": self.mtime_ns, "digest": self.digest}

    @classmethod
    def from_json(cls, data: dict) -> "SourceFingerprint":
        return cls(
            size_bytes=int(data["size_bytes"]),
            mtime_ns=int(data["mtime_ns"]),
            digest=str(data["digest"]),
        )


@dataclass(frozen=True)
class CacheKey:
    """一个确定性的缓存键。

    parts 里放一切会影响结果的**版本与参数**：模型 ID、检测器版本、
    提示词版本、采样率、合并距离等。任何一项变了就应当命中不同的键。
    """

    namespace: str
    source: SourceFingerprint
    parts: tuple[tuple[str, str], ...] = ()

    @classmethod
    def build(cls, namespace: str, source: SourceFingerprint, **parts: Any) -> "CacheKey":
        if namespace not in CACHE_NAMESPACES:
            raise ValueError(
                f"未知缓存命名空间 {namespace!r}，可用：{sorted(CACHE_NAMESPACES)}"
            )
        normalized = tuple(
            sorted((str(key), _normalize(value)) for key, value in parts.items())
        )
        return cls(namespace=namespace, source=source, parts=normalized)

    def digest(self) -> str:
        payload = {
            "schema": _SCHEMA_VERSION,
            "namespace": self.namespace,
            "source": self.source.to_json(),
            "parts": self.parts,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def describe(self) -> str:
        detail = " ".join(f"{k}={v}" for k, v in self.parts) or "（无附加参数）"
        return f"{self.namespace}｜{self.source.short(8)}｜{detail}"


def _normalize(value: Any) -> str:
    """把参数值规范化为稳定字符串，保证跨进程/跨平台键一致。

    float 走 repr 会因平台差异漂移，凡涉及浮点的参数调用方应传 Decimal/str；
    这里对 float 做定点化，避免 0.1 与 0.10000000000000001 得到两个键。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.9g}"
    if isinstance(value, (int, str)):
        return str(value)
    if value is None:
        return "none"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_normalize(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(f"{k}:{_normalize(v)}" for k, v in sorted(value.items())) + "}"
    return str(value)


@dataclass
class CacheEntryMeta:
    """缓存条目的元信息，写入 sidecar 便于人工诊断与清理。"""

    key: str
    namespace: str
    created_at: str
    source_digest: str
    parts: dict[str, str] = field(default_factory=dict)
    note: str = ""


class CacheStore:
    """文件缓存。每个条目 = 一个 JSON 载荷 + 一份 sidecar 元信息。

    选文件而非数据库：§18.2 明确 `cache/` 是可清理、可重建的。
    文件形式让"删掉整个目录"成为安全操作，也让用户能直接看懂缓存内容。
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- 路径 ----------------------------------------------------------

    def entry_dir(self, key: CacheKey) -> Path:
        return self.root / key.namespace / key.digest()[:2]

    def payload_path(self, key: CacheKey) -> Path:
        return self.entry_dir(key) / f"{key.digest()}.json"

    def meta_path(self, key: CacheKey) -> Path:
        return self.entry_dir(key) / f"{key.digest()}.meta.json"

    # ---- 读写 ----------------------------------------------------------

    def has(self, key: CacheKey) -> bool:
        return self.payload_path(key).exists()

    def load(self, key: CacheKey) -> dict | None:
        path = self.payload_path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 缓存损坏只应导致重算，绝不能让整个任务失败
            self._quarantine(path)
            return None
        if not isinstance(data, dict) or data.get("__key__") != key.digest():
            # 键不匹配说明文件被误放或被外部改动，按损坏处理
            self._quarantine(path)
            return None
        return data.get("payload")

    def save(self, key: CacheKey, payload: dict, *, note: str = "") -> Path:
        """写入缓存。先写临时文件再原子替换，避免中断留下半个 JSON。"""
        path = self.payload_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)

        body = {
            "__key__": key.digest(),
            "__schema__": _SCHEMA_VERSION,
            "__namespace__": key.namespace,
            "payload": payload,
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

        meta = CacheEntryMeta(
            key=key.digest(),
            namespace=key.namespace,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            source_digest=key.source.digest,
            parts=dict(key.parts),
            note=note,
        )
        meta_tmp = self.meta_path(key).with_suffix(".meta.json.tmp")
        meta_tmp.write_text(
            json.dumps(meta.__dict__, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        meta_tmp.replace(self.meta_path(key))
        return path

    def get_or_compute(
        self,
        key: CacheKey,
        compute: Callable[[], dict],
        *,
        note: str = "",
    ) -> tuple[dict, bool]:
        """命中缓存直接返回，否则计算并写入。返回 (载荷, 是否命中缓存)。"""
        cached = self.load(key)
        if cached is not None:
            return cached, True
        payload = compute()
        self.save(key, payload, note=note)
        return payload, False

    # ---- 维护 ----------------------------------------------------------

    def _quarantine(self, path: Path) -> None:
        """把损坏条目移到隔离名，保留现场供诊断，而不是直接删除。"""
        target = path.with_name(path.name + ".corrupt-" + time.strftime("%Y%m%d%H%M%S"))
        try:
            path.replace(target)
        except OSError:
            pass

    def iter_entries(self) -> Iterator[tuple[Path, dict]]:
        for meta_file in self.root.rglob("*.meta.json"):
            try:
                yield meta_file, json.loads(meta_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

    def size_bytes(self) -> int:
        total = 0
        for path in self.root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
        return total

    def describe(self) -> str:
        entries = list(self.iter_entries())
        return (
            f"{len(entries)} 个缓存条目，占用 {self.size_bytes() / 1024 / 1024:.1f} MB"
            f"（位于 {self.root}，可安全删除，只会导致重算）"
        )

    def invalidate_source(self, fingerprint: SourceFingerprint) -> int:
        """删除某个源文件的所有缓存条目，返回删除数（§12.2 换片失效）。

        "重算"不是清空整个缓存目录——那会把其他项目的缓存也一并删掉。
        按源指纹精确失效，只影响当前素材。
        """
        removed = 0
        for meta_file, meta in self.iter_entries():
            if meta.get("source_digest") != fingerprint.digest:
                continue
            payload = meta_file.with_name(meta_file.name.replace(".meta.json", ".json"))
            for target in (meta_file, payload):
                try:
                    if target.exists():
                        target.unlink()
                        removed += 1
                except OSError:
                    continue
        return removed

    def clear(self) -> int:
        """清空缓存。仅删除本工具自己的缓存目录内容。"""
        removed = 0
        for child in self.root.iterdir():
            if child.is_dir():
                for path in sorted(child.rglob("*"), reverse=True):
                    try:
                        if path.is_file():
                            path.unlink()
                            removed += 1
                        else:
                            path.rmdir()
                    except OSError:
                        continue
                try:
                    child.rmdir()
                except OSError:
                    pass
            else:
                try:
                    child.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed


def probe_fingerprint_change(
    store: CacheStore,
    namespace: str,
    old: SourceFingerprint,
    new: SourceFingerprint,
) -> list[Path]:
    """换片后列出因源文件变化而不再适用的缓存条目（§12.2）。

    仅报告，不自动删除——用户可能还要保留旧项目的缓存做对照。
    """
    stale: list[Path] = []
    namespace_dir = store.root / namespace
    if not namespace_dir.exists():
        return stale
    for meta_file, meta in store.iter_entries():
        if meta.get("namespace") != namespace:
            continue
        if meta.get("source_digest") == old.digest and old.digest != new.digest:
            stale.append(meta_file)
    return stale


def workspace_cache_root(project_dir: str | Path) -> Path:
    """项目缓存目录（§18.2 的 cache/）。"""
    return Path(project_dir) / "cache"


def cache_env_summary() -> dict[str, str]:
    """影响缓存命中的环境项，写入缓存元信息便于排查"为什么没命中"。"""
    return {
        "python": os.sys.version.split()[0],
        "platform": os.sys.platform,
    }
