#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_pack —— PyInstaller 批量打包能力（由 `pyinstaller-tool` 仓并入，2026-11-26）。

来源与许可
----------
本模块的**能力来源**是 `siwpan/pyinstaller`（`pyinstaller-tool` 1.0.0，MIT，
2026-10-22 发布于 PyPI）。2026-11-26 用户定调「并入 allin-governance，删除其远端和本地仓，
仅保留 PyPI」，因此能力在此重构为 CLI 子命令，原仓下线，PyPI 包 `pyinstaller-tool` 保留不动。
重构范围：只搬「批量打包这一件事」的判据（GUI 自动检测 / 图标 / 附加数据 / UPX / 报告），
**不搬 Tkinter GUI** —— 公共核是零三方依赖的 CLI，GUI 与治理包的服务面不一致。

与原型机的关系
--------------
原仓的 `全能宝.bat` 是「**全哥**」这个称呼的词源（全部能打包 → 全能宝 → 全哥，原型机时期产出）。
该 bat 与源文件字节原样归档于仓内 `tools/archive/prototype-01/pyinstaller-tool/`，
见 `docs/事实源/机器/PROTOTYPE-01/月账/2026-09.md`。

可选依赖
--------
`pyinstaller` **不在** dependencies：不装也能 import 本包，构建时才检查。
装法：``pip install pyinstaller``（或 ``pip install 'allin-governance[pack]'``）。

用法见 ``allin-governance pack --help``。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

__all__ = [
    "PYINSTALLER_AVAILABLE",
    "UPX_AVAILABLE",
    "detect_gui_kind",
    "probe_environment",
    "build",
    "build_all",
]

#: GUI 框架探测表：源码里出现这些标记就按「窗口程序」打包（`-w` 而非 `-c`）。
#: 与 `全能宝.bat` 的 `findstr /i "tkinter"` 同源，但**匹配 import 行**而非任意出现，
#: 避免注释里提到 tkinter 就误判成 GUI 程序。
_GUI_MARKERS: tuple[tuple[str, str], ...] = (
    ("tkinter", "tkinter"),
    ("PyQt5", "PyQt5"),
    ("PyQt6", "PyQt6"),
    ("PySide2", "PySide2"),
    ("PySide6", "PySide6"),
    ("wxPython", "wx"),
    ("import wx", "wx"),
    ("kivy", "kivy"),
)


def _pyinstaller_version() -> str:
    """返回已安装的 PyInstaller 版本；未安装返回空串（不抛异常）。"""
    try:
        from PyInstaller import __version__ as v  # type: ignore

        return str(v)
    except Exception:
        return ""


PYINSTALLER_AVAILABLE: bool = bool(_pyinstaller_version())
UPX_AVAILABLE: bool = shutil.which("upx") is not None


def detect_gui_kind(script: Path) -> str:
    """判断脚本是 GUI 还是控制台程序。返回框架名，空串代表控制台。

    判据只看 `import` / `from ... import` 行 —— 原 bat 用 `findstr` 全文匹配，
    注释或字符串里出现 ``tkinter`` 就会误判成 GUI（打出来没有控制台、报错也看不见）。
    """
    try:
        text = script.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("import ") or stripped.startswith("from ")):
            continue
        for marker, name in _GUI_MARKERS:
            if marker in stripped:
                return name
    return ""


def probe_environment() -> dict:
    """探测构建环境。**不需要** PyInstaller 已安装即可调用。"""
    return {
        "python": sys.version.split()[0],
        "python_exe": sys.executable,
        "pyinstaller": _pyinstaller_version() or None,
        "pyinstaller_ok": PYINSTALLER_AVAILABLE,
        "upx": shutil.which("upx") or None,
        "upx_ok": UPX_AVAILABLE,
        "platform": sys.platform,
    }


def _build_command(
    script: Path,
    *,
    onefile: bool,
    windowed: bool | None,
    icon: Path | None,
    add_data: list[str],
    upx: bool,
    name: str | None,
    clean: bool,
) -> list[str]:
    cmd = [sys.executable, "-m", "PyInstaller"]
    cmd.append("-F" if onefile else "-D")
    # windowed=None ⇒ 自动判；True/False ⇒ 强制。这样「自动」和「我要控制台」都表达得出来。
    if windowed is None:
        cmd.append("-w" if detect_gui_kind(script) else "-c")
    else:
        cmd.append("-w" if windowed else "-c")
    if clean:
        cmd.append("--clean")
    cmd += ["--name", name or script.stem]
    if icon and icon.is_file():
        cmd += ["--icon", str(icon)]
    for item in add_data:
        cmd += ["--add-data", item]
    if upx and UPX_AVAILABLE:
        cmd += ["--upx-dir", str(Path(shutil.which("upx") or ".").parent)]
    cmd.append(str(script))
    return cmd


def build(
    script: str | Path,
    out_dir: str | Path = "dist_all",
    *,
    onefile: bool = True,
    windowed: bool | None = None,
    icon: str | Path | None = None,
    add_data: list[str] | None = None,
    upx: bool = False,
    name: str | None = None,
    clean: bool = True,
    dry_run: bool = False,
) -> dict:
    """打包单个脚本。返回结构化结果（ok / command / artifact / stderr 摘要）。

    ``dry_run=True`` 只回命令不执行 —— 与治理仓「先看要做什么」的习惯一致。
    """
    src = Path(script)
    if not src.is_file():
        return {"ok": False, "error": f"脚本不存在: {src}", "script": str(src)}
    if src.suffix.lower() != ".py":
        return {"ok": False, "error": f"只接受 .py：{src}", "script": str(src)}

    icon_path = Path(icon) if icon else (src.with_suffix(".ico") if src.with_suffix(".ico").is_file() else None)
    data_specs = list(add_data or [])
    # 与原 全能宝.bat 同款：`assets/<脚本名>/` 存在就自动附加
    assets = src.parent / "assets" / src.stem
    if assets.is_dir():
        data_specs.append(f"{assets}{';' if sys.platform.startswith('win') else ':'}assets/{src.stem}")

    cmd = _build_command(
        src, onefile=onefile, windowed=windowed, icon=icon_path,
        add_data=data_specs, upx=upx, name=name, clean=clean,
    )
    result: dict = {
        "ok": False, "script": str(src), "command": cmd, "dry_run": dry_run,
        "out_dir": str(out_dir), "gui_kind": detect_gui_kind(src),
        "icon": str(icon_path) if icon_path else None,
        "add_data": data_specs,
    }
    if dry_run:
        result["ok"] = True
        return result
    if not PYINSTALLER_AVAILABLE:
        result["error"] = "PyInstaller 未安装：pip install pyinstaller"
        return result

    proc = subprocess.run(cmd, cwd=str(src.parent), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    result["returncode"] = proc.returncode
    # 产物位置：PyInstaller 默认把 exe/目录放 cwd/dist 与 build/
    target = Path(out_dir)
    built_name = name or src.stem
    candidates = [
        src.parent / "dist" / (built_name + (".exe" if sys.platform.startswith("win") else "")),
        src.parent / "dist" / built_name,
    ]
    artifact = next((c for c in candidates if c.exists()), None)
    if artifact and target:
        try:
            target.mkdir(parents=True, exist_ok=True)
            dest = target / artifact.name
            if dest.exists():
                shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
            shutil.move(str(artifact), str(dest))
            result["artifact"] = str(dest)
        except Exception as exc:  # 移动失败不该让「构建成功」变失败
            result["artifact"] = str(artifact)
            result["move_error"] = str(exc)
    elif artifact:
        result["artifact"] = str(artifact)
    result["ok"] = proc.returncode == 0 and bool(result.get("artifact"))
    if not result["ok"]:
        result["error"] = (proc.stderr or proc.stdout or "")[-1200:]
    return result


def build_all(
    root: str | Path = ".",
    out_dir: str | Path = "dist_all",
    **kwargs,
) -> list[dict]:
    """批量打包目录下所有 .py（原 `全能宝.bat` / `全宝.bat` 的语义）。

    跳过：`selfcheck.py` / `cli.py` 一类**工具自身**入口？不跳 —— 原 bat 也不跳；
    是否打包由调用方用 ``--include``/``--exclude`` 控制，工具不做隐式决定。
    """
    base = Path(root)
    scripts = sorted(p for p in base.glob("*.py") if p.is_file())
    results = []
    for s in scripts:
        results.append(build(s, out_dir, **kwargs))
    return results
