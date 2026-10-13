"""打包脚本必须把 zhconv 的词典带进产物。

为什么单独钉这一条：v1.0.7 的真实故障——PyInstaller 把 zhconv 的**代码**冻进了
产物，却没带它的词典 ``zhcdict.json``。装出来的程序界面能开、``--self-check``
与 ``--ui-smoke`` 全绿，但用户一跑「分析分集」，繁简转换就抛
``FileNotFoundError: ...\\_internal\\zhconv\\zhcdict.json``，整次分析失败。

三件事一起钉死：
1. 脚本里有收集逻辑，并且真的接进了 ``--add-data``（函数在、没接上等于没有）；
2. 落点是 ``zhconv`` 包目录（zhconv 按包目录找词典，落错地方照样找不到）；
3. 收不到词典时**构建直接失败**——不许再产出一个「能开界面、一分析就炸」的包。
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "tools" / "build_package.py"


def build_package_module():
    """按文件路径加载打包脚本（它在 tools/ 下，不在包内）。"""
    spec = importlib.util.spec_from_file_location("_bp_for_test_zhconv", SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_collects_zhconv_data():
    text = SOURCE.read_text(encoding="utf-8")
    assert "zhconv_data_args" in text, "打包脚本没有收集 zhconv 词典的逻辑"
    assert f"{{os.pathsep}}zhconv" in text, "--add-data 的落点不是 zhconv 包目录"
    assert '"--hidden-import"' in text and '"zhconv"' in text, "没有把 zhconv 代码钉进产物"


def test_missing_dictionary_is_fatal_not_silent():
    """静默跳过正是 v1.0.7 的错法：收不到词典必须让构建失败。"""
    text = SOURCE.read_text(encoding="utf-8")
    assert "if not zhconv_args:" in text, "没有对缺失的 zhconv 词典做判断"
    assert "return 1" in text, "缺词典时没有中止构建"


def test_built_product_is_checked_for_the_dictionary():
    """自检不能只看返回码——`--self-check` 碰不到繁简转换，必须直接查文件。"""
    text = SOURCE.read_text(encoding="utf-8")
    assert '"_internal"' in text and '"zhconv"' in text, "打包后没有查产物里的 zhconv 目录"
    assert "dict_files" in text, "没有校验词典是否真的落进产物"


def test_helper_points_at_installed_zhconv():
    module = build_package_module()
    if importlib.util.find_spec("zhconv") is None:
        assert module.zhconv_data_args() == []
        return

    args = module.zhconv_data_args()
    assert args, "本机装了 zhconv 却没收集到词典数据"
    assert len(args) % 2 == 0, f"--add-data 参数不成对：{args}"
    for flag, value in zip(args[0::2], args[1::2]):
        assert flag == "--add-data"
        source, dest = value.rsplit(os.pathsep, 1)
        assert Path(source).is_file() and source.endswith(".json"), source
        assert dest == "zhconv", f"词典落点应为 zhconv，实际 {dest!r}"
