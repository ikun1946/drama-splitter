"""AI 短剧智能分集器 —— 应用包。

启动时挂载"外挂依赖"目录
------------------------
打包版**有意不含**语音转写引擎（faster-whisper 及其依赖约 81 MB，见
`build_package.py` 的 EXCLUDES）。用户可在应用内下载安装它（见
`app.core.deps_manager`），装好后落在 exe 同级的 ``deps/`` 目录。

这里在**包被导入的最早时机**把它挂上 sys.path，且必须 ``append`` 到末尾：
依赖集里也有 numpy，而打包产物自带 numpy 2.5.4——若插到最前会覆盖它，实测
直接崩（``'numpy.ufunc' object has no attribute '__module__'``）。追加到末尾
则产物内的包优先，外挂目录只补缺的那些。

为什么放在 ``__init__`` 而不是 ``run.py``：任何入口（GUI、``--self-check``、
测试）只要 import 了 app 就该拿到一致的行为，不该指望每个入口自己记得挂。
"""
from __future__ import annotations

__all__ = ["bootstrap_runtime_deps"]


def bootstrap_runtime_deps() -> bool:
    """把外挂依赖目录挂到 sys.path；返回是否有目录被挂上。

    设计上**绝不抛异常**：依赖目录不存在、不可读、`os.add_dll_directory`
    在老系统上缺失，都不该阻断启动——转写用不了是降级，起不来才是事故。
    """
    try:
        from .core.deps_manager import default_deps_root, register_deps

        root = default_deps_root()
        if not root.is_dir():
            return False
        return register_deps(root)
    except Exception:  # noqa: BLE001 - 启动路径上的任何异常都不该冒泡
        return False


bootstrap_runtime_deps()
