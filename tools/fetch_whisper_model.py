"""从镜像下载 faster-whisper 模型（绕开 huggingface_hub 的符号链接机制）。

为什么要自己写下载器
--------------------
本机的 Windows 账户**没有创建符号链接的权限**。huggingface_hub 的缓存依赖
`snapshots/<rev>/xxx -> ../../blobs/<hash>` 这样的符号链接；链接创建失败时它
会产出 **0 字节的占位文件**，而 `blobs/` 里的数据其实是完整下载好的。
现象是：

    RuntimeError: File model.bin is incomplete: failed to read a value of size 4 at position 0

`local_dir=` 与 `HF_HUB_DISABLE_XET=1` 都绕不过去（实测）。所以这里直接用 curl
从镜像拉真实文件到项目内的普通目录——无符号链接、无缓存间接层、可断点续传、
大小可校验。

模型源
------
`huggingface.co` 在本机不可达（HTTP 000 超时），`hf-mirror.com` 可达，因此
默认走镜像，可用 `--endpoint` 覆盖。

用法
----
    python tools/fetch_whisper_model.py small
    python tools/fetch_whisper_model.py small --force
    python tools/fetch_whisper_model.py --list
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_ENDPOINT = "https://hf-mirror.com"

# 各档模型的大致体积（MB，仅为给用户一个下载预期，不用于校验）
MODEL_SIZES_MB = {
    "tiny": 75,
    "base": 145,
    "small": 484,
    "medium": 1500,
    "large-v3": 3090,
}

# faster-whisper 转换版仓库的必需文件
REQUIRED_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
# 可选文件：缺失不影响加载
OPTIONAL_FILES = ("preprocessor_config.json", "README.md")

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def repo_id(model: str) -> str:
    return f"Systran/faster-whisper-{model}"


def target_dir(model: str) -> Path:
    return ROOT / "models" / f"faster-whisper-{model}"


def _curl(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        ["curl", "-sS", "-L", "--fail", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_NO_WINDOW,
        timeout=3600,
    )
    return proc.returncode, (proc.stderr or "").strip()


def _remote_size(url: str) -> int | None:
    """用 HEAD 取远端文件大小；失败返回 None。"""
    code, output = _curl(["-I", "-o", "/dev/null", "-w", "%{size_download}\n", url])
    if code != 0:
        # 镜像对 HEAD 支持不一，退回 GET + Range 探测
        code, output = _curl(["-r", "0-0", "-o", "/dev/null", "-w", "%{http_code}", url])
        if code != 0:
            return None
        return None
    for line in output.splitlines():
        if line.strip().isdigit():
            return int(line.strip())
    return None


def download_file(url: str, target: Path, *, expected_min_bytes: int = 0) -> bool:
    """下载单个文件。先写 .part 再改名，支持断点续传。"""
    part = target.with_suffix(target.suffix + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)

    # 已有完整文件则跳过
    if target.exists() and target.stat().st_size >= expected_min_bytes > 0:
        print(f"    已存在 {target.name}（{target.stat().st_size / 1024 / 1024:.1f} MB）")
        return True

    if part.exists() and expected_min_bytes and part.stat().st_size >= expected_min_bytes:
        part.replace(target)
        print(f"    复用已下载的 {target.name}")
        return True

    code, error = _curl(["-C", "-", "-o", str(part), url])
    if code != 0:
        print(f"    失败 {target.name}：{error[:200]}")
        return False

    size = part.stat().st_size if part.exists() else 0
    if expected_min_bytes and size < expected_min_bytes:
        print(f"    失败 {target.name}：只拿到 {size} 字节，低于下限 {expected_min_bytes}")
        return False

    part.replace(target)
    print(f"    {target.name}  {size / 1024 / 1024:.1f} MB")
    return True


def fetch(model: str, *, endpoint: str = DEFAULT_ENDPOINT, force: bool = False) -> Path | None:
    directory = target_dir(model)
    base = f"{endpoint}/{repo_id(model)}/resolve/main"

    if force and directory.exists():
        shutil.rmtree(directory, ignore_errors=True)

    print(f"模型：{repo_id(model)}")
    print(f"来源：{endpoint}")
    print(f"目标：{directory}")
    expected = MODEL_SIZES_MB.get(model)
    if expected:
        print(f"预计体积：约 {expected} MB")
    print()

    missing: list[str] = []
    for name in REQUIRED_FILES:
        # model.bin 是权重，必须足够大；其余小文件给一个非零下限即可
        minimum = 1024 * 1024 if name == "model.bin" else 16
        if not download_file(f"{base}/{name}", directory / name, expected_min_bytes=minimum):
            missing.append(name)

    for name in OPTIONAL_FILES:
        download_file(f"{base}/{name}", directory / name)

    if missing:
        print()
        print(f"缺少必需文件：{', '.join(missing)}")
        print("可重复执行本命令续传；若镜像不稳定可换 --endpoint。")
        return None

    # 落一份来源记录，便于以后判断模型从哪来、能不能删
    (directory / "_source.json").write_text(
        json.dumps(
            {
                "repo_id": repo_id(model),
                "endpoint": endpoint,
                "files": list(REQUIRED_FILES),
                "note": "由 tools/fetch_whisper_model.py 下载；本目录不随仓库分发",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    total = sum((directory / name).stat().st_size for name in REQUIRED_FILES)
    print()
    print(f"完成：{len(REQUIRED_FILES)} 个文件，共 {total / 1024 / 1024:.1f} MB")
    return directory


def list_models() -> None:
    print("可用档位（数字为体积量级，中文短剧建议 small 起步）：")
    for name, size in MODEL_SIZES_MB.items():
        print(f"  {name:10s} 约 {size:>5} MB")
    print()
    print("已有本地模型：")
    models_root = ROOT / "models"
    if not models_root.exists():
        print("  （无）")
        return
    for child in sorted(models_root.iterdir()):
        if child.is_dir():
            size = sum(f.stat().st_size for f in child.glob("*") if f.is_file())
            print(f"  {child.name:34s} {size / 1024 / 1024:>8.1f} MB")


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 faster-whisper 模型（走镜像，绕开符号链接）")
    parser.add_argument("model", nargs="?", help=f"模型档位：{', '.join(MODEL_SIZES_MB)}")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help=f"镜像地址，默认 {DEFAULT_ENDPOINT}")
    parser.add_argument("--force", action="store_true", help="删除已有目录后重新下载")
    parser.add_argument("--list", action="store_true", help="列出可用档位与已有本地模型")
    args = parser.parse_args()

    if args.list or not args.model:
        list_models()
        return 0

    if args.model not in MODEL_SIZES_MB:
        print(f"未知模型档位 {args.model!r}，可选：{', '.join(MODEL_SIZES_MB)}")
        return 2

    result = fetch(args.model, endpoint=args.endpoint, force=args.force)
    return 0 if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
