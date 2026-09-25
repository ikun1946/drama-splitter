"""下载本地 LLM（GGUF）用于阶段4 的语义切点判断。

为什么走 GGUF + llama.cpp
------------------------
阶段4 需要对候选切点做语义判断，而「素材不出机器」排除了云端接口。
本机没有 transformers/torch，也没有 Ollama；llama-cpp-python 0.3.35 有
cp313/win_amd64 的预编译轮子（无需编译），GGUF 是唯一顺路的本地推理方案。

模型源与 whisper 相同：huggingface.co 不可达，走 hf-mirror.com；
且必须绕开 huggingface_hub（本机账户无符号链接权限，会产出 0 字节文件）。

用法：
    python tools/fetch_llm.py --list
    python tools/fetch_llm.py Qwen3.5-2B-GGUF:Q4_K_M
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
DEFAULT_REPO = "unsloth/Qwen3.5-2B-GGUF"
DEFAULT_QUANT = "Q4_K_M"

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def list_remote_gguf(repo: str, endpoint: str) -> list[str]:
    """列出仓库内的 GGUF 文件。

    用 curl 而不是 urllib：hf-mirror 会拒绝 Python-urllib 的默认 User-Agent
    （实测返回 403，而 curl 正常），且本工具的下载路径本来就走 curl。
    """
    url = f"{endpoint}/api/models/{repo}"
    proc = subprocess.run(
        ["curl", "-sS", "-L", "--fail", "-m", "30", url],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_NO_WINDOW,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"查询仓库失败：{(proc.stderr or '').strip()[:200]}")
    data = json.loads(proc.stdout)
    return sorted(
        s["rfilename"] for s in data.get("siblings", []) if s["rfilename"].endswith(".gguf")
    )


def target_path(repo: str, filename: str) -> Path:
    return ROOT / "models" / "llm" / repo.split("/")[-1] / filename


def download(url: str, target: Path) -> bool:
    part = target.with_suffix(target.suffix + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.stat().st_size > 1024 * 1024:
        print(f"    已存在 {target.name}（{target.stat().st_size / 1024 / 1024:.0f} MB）")
        return True
    if part.exists() and part.stat().st_size > 1024 * 1024:
        print(f"    续传 {target.name}（已有 {part.stat().st_size / 1024 / 1024:.0f} MB）")
    proc = subprocess.run(
        ["curl", "-sS", "-L", "--fail", "-C", "-", "-o", str(part), url],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_NO_WINDOW,
        timeout=7200,
    )
    if proc.returncode != 0:
        print(f"    失败：{(proc.stderr or '').strip()[:200]}")
        return False
    size = part.stat().st_size if part.exists() else 0
    if size < 1024 * 1024:
        print(f"    失败：只拿到 {size} 字节")
        return False
    part.replace(target)
    print(f"    {target.name}  {size / 1024 / 1024:.0f} MB")
    return True


def fetch(repo: str, quant: str, *, endpoint: str, force: bool = False) -> Path | None:
    print(f"查询仓库文件：{endpoint}/api/models/{repo}")
    try:
        available = list_remote_gguf(repo, endpoint)
    except Exception as exc:  # noqa: BLE001
        print(f"查询失败：{exc}")
        return None

    matches = [name for name in available if quant.upper() in name.upper()]
    if not matches:
        print(f"找不到量化 {quant}。可用：\n  " + "\n  ".join(available))
        return None
    filename = matches[0]

    target = target_path(repo, filename)
    print(f"目标：{target}")
    if force and target.exists():
        target.unlink()
    url = f"{endpoint}/{repo}/resolve/main/{filename}"
    if not download(url, target):
        return None

    (target.parent / "_source.json").write_text(
        json.dumps(
            {
                "repo": repo,
                "file": filename,
                "endpoint": endpoint,
                "note": "由 tools/fetch_llm.py 下载；不随仓库分发",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return target


def main() -> int:
    """命令行入口：委托给 core 的模型下载服务。"""
    import argparse
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    from app.core.model_manager import download_model, installed_state, list_remote_gguf, llm_spec

    parser = argparse.ArgumentParser(description="下载本地 LLM（GGUF）用于语义判断")
    parser.add_argument("spec", nargs="?", default="2B:Q4_K_M", help="尺寸:量化，如 2B:Q4_K_M")
    parser.add_argument("--endpoint", default="https://hf-mirror.com")
    parser.add_argument("--list", action="store_true", help="列出远端可用量化档位")
    args = parser.parse_args()

    root = _Path(__file__).resolve().parent.parent / "models"
    if args.list:
        size = args.spec.split(":")[0]
        for name in list_remote_gguf(f"unsloth/Qwen3.5-{size}-GGUF", args.endpoint):
            print(" ", name)
        return 0

    if ":" not in args.spec:
        print("格式应为 尺寸:量化，如 2B:Q4_K_M")
        return 2
    size, quant = args.spec.split(":", 1)
    spec = llm_spec(size, quant)
    print(f"目标：{spec.target_dir(root)}")

    last = {"pct": -1}

    def show(done: int, total: int, name: str, index: int, count: int) -> None:
        if total:
            pct = int(done / total * 100)
            if pct != last["pct"]:
                last["pct"] = pct
                print(f"  [{pct:3d}%] {done // 1048576} MB / {total // 1048576} MB", flush=True)

    result = download_model(spec, root, endpoint=args.endpoint, on_progress=show)
    print()
    print(result.describe())
    return 0 if result.ok else 1


def _legacy_main() -> int:
    parser = argparse.ArgumentParser(description="下载本地 LLM（GGUF）用于语义切点判断")
    parser.add_argument("spec", nargs="?", default=f"{DEFAULT_REPO}:{DEFAULT_QUANT}",
                        help="仓库名:量化，如 unsloth/Qwen3.5-2B-GGUF:Q4_K_M")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--list", action="store_true", help="列出仓库内可用的 GGUF")
    args = parser.parse_args()

    if args.list:
        repo = args.spec.split(":")[0]
        for name in list_remote_gguf(repo, args.endpoint):
            print(" ", name)
        return 0

    if ":" not in args.spec:
        print("格式应为 仓库名:量化，如 unsloth/Qwen3.5-2B-GGUF:Q4_K_M")
        return 2
    repo, quant = args.spec.rsplit(":", 1)
    result = fetch(repo, quant, endpoint=args.endpoint, force=args.force)
    if result:
        print()
        print("下载完成。可用下面的冒烟验证：")
        print(f'  python -c "from app.core.semantic import LocalLlmJudge; '
              f'LocalLlmJudge.from_model_dir(r\'{result.parent}\').probe()"')
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
