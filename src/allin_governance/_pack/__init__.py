#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_pack 包入口：只做导出，不在此实现逻辑（逻辑在 builder.py）。

可选依赖纪律：`pyinstaller` 不装时本包**仍可 import**，
探测标志 ``PYINSTALLER_AVAILABLE`` 供调用方降级 —— 与 ``_ml`` 的 ``TORCH_AVAILABLE`` 同款。
"""

from __future__ import annotations

from .builder import (  # noqa: F401
    PYINSTALLER_AVAILABLE,
    UPX_AVAILABLE,
    build,
    build_all,
    detect_gui_kind,
    probe_environment,
)

__all__ = [
    "PYINSTALLER_AVAILABLE",
    "UPX_AVAILABLE",
    "build",
    "build_all",
    "detect_gui_kind",
    "probe_environment",
]
