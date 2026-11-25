#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PyPI 发版链路：`check` / `build` / `verify` / `upload`。

**为什么有它**（2026-11-25 事故与全哥指令）：

1. 2.1.0 首发只传了 wheel —— 手打 `python -m build --wheel` 就上传，sdist 漏了。
   而 `.github/workflows/release.yml` 里写着「构建 sdist + wheel」「双产物各装一次」，
   **判据存在，但手动发版绕过了它**。判据写在 CI 里而人不在 CI 里，只保护了走 CI 那条路。
2. 建仓推送与发版原本散在「allin 的 gitee 工具 + 包内 github + 包仓 release.sh」三处。
   统一到 `allin-governance publish ...` 一个入口，纪律才有承载物。

四个子命令对应四道**可独立执行的判据**：

| 子命令 | 判什么 | 什么时候跑 |
| --- | --- | --- |
| `check` | 版本三处一致（pyproject / `__init__.py` / VERSION）+ 工作树无未提交的版本漂移 | 改完代码、还没构建 |
| `build` | 产出 **wheel + sdist 双产物**（不带 `--wheel`/`--sdist`） | 发版前 |
| `verify` | dist 里**每个产物各装一次**，装出来的版本必须等于发布版本 | 上传前（这一步抓 sdist 不自包含） |
| `upload` | twine check + 上传双产物 | 放行后 |

`release` = build → verify → upload 一条龙（仍需人在场放行）。

用法：
    allin-governance publish check     --repo .
    allin-governance publish build     --repo .
    allin-governance publish verify    --repo .
    allin-governance publish upload    --repo . --repository pypi
    allin-governance publish release   --repo .            # 只 build+verify
    allin-governance publish release   --repo . --upload   # 真的上传
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_ENV = 0, 1, 2, 3


# --------------------------------------------------------------------- 基础
def find_repo_root(start: Path | None = None) -> Path | None:
    """向上找含 `pyproject.toml` 的目录。"""
    p = (start or Path.cwd()).resolve()
    for parent in (p, *p.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    return None


def read_versions(root: Path) -> dict[str, str | None]:
    """读三处版本：pyproject / `__init__.py` / VERSION。任一缺失记为 None。"""
    out: dict[str, str | None] = {"pyproject": None, "init": None, "VERSION": None}
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.M)
        out["pyproject"] = m.group(1) if m else None
    init = root / "src" / "allin_governance" / "__init__.py"
    if init.is_file():
        m = re.search(r'^__version__\s*=\s*"([^"]+)"', init.read_text(encoding="utf-8"), re.M)
        out["init"] = m.group(1) if m else None
    vf = root / "VERSION"
    if vf.is_file():
        out["VERSION"] = vf.read_text(encoding="utf-8").strip() or None
    return out


def _py() -> str:
    """构建/校验用的解释器：当前解释器即可（build/twine 是它的依赖）。"""
    return sys.executable


def _have(module: str, python: str | None = None) -> bool:
    return subprocess.run([python or _py(), "-c", f"import {module}"],
                          capture_output=True).returncode == 0


def _twine_python() -> str:
    """twine 不一定装在建包解释器里（本机在 E:/Python314）。单独探测。"""
    for cand in (_py(), os.environ.get("TWINE_PY", ""),
                 r"E:\Python314\python.exe", "python3", "python"):
        if cand and _have("twine", cand):
            return cand
    return ""


def _dist_artifacts(root: Path) -> tuple[list[Path], list[Path]]:
    dist = root / "dist"
    wheels = sorted(dist.glob("*.whl")) if dist.is_dir() else []
    sdists = sorted(dist.glob("*.tar.gz")) if dist.is_dir() else []
    return wheels, sdists


# ----------------------------------------------------------------- 四道判据
def cmd_check(args) -> int:
    root = find_repo_root(Path(args.repo))
    if root is None:
        print("[ERR] 找不到 pyproject.toml（--repo 指向仓根）", file=sys.stderr)
        return EXIT_USAGE
    vers = read_versions(root)
    missing = [k for k, v in vers.items() if not v]
    if missing:
        print(f"[ERR] 读不到版本：{missing}", file=sys.stderr)
        return EXIT_FAIL
    uniq = set(vers.values())
    if len(uniq) != 1:
        print("[ERR] 版本三处不一致 —— " + " / ".join(f"{k}={v}" for k, v in vers.items()),
              file=sys.stderr)
        print("      三处必须同时改（pyproject / __init__.py / VERSION），漏一处就会发出错版本号。",
              file=sys.stderr)
        return EXIT_FAIL
    ver = uniq.pop()
    wheels, sdists = _dist_artifacts(root)
    print(f"[OK] 版本三处一致：{ver}")
    print(f"     dist：wheel {len(wheels)} 个 / sdist {len(sdists)} 个"
          f"{'（干净）' if not wheels and not sdists else '（构建后会被清空重建）'}")
    return EXIT_OK


def cmd_build(args) -> int:
    root = find_repo_root(Path(args.repo))
    if root is None:
        return EXIT_USAGE
    if not _have("build"):
        print("[ERR] 缺 build 模块 —— pip install build", file=sys.stderr)
        return EXIT_ENV
    # 清旧产物：留着上一版会让「双产物」判据拿到错版本的文件
    for d in ("dist", "build"):
        shutil.rmtree(root / d, ignore_errors=True)
    print("== 构建双产物（不带 --wheel/--sdist，让 build 两个都出）")
    r = subprocess.run([_py(), "-m", "build", "--outdir", "dist", "."],
                       cwd=str(root), capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        print((r.stderr or r.stdout)[-1500:], file=sys.stderr)
        return EXIT_FAIL
    wheels, sdists = _dist_artifacts(root)
    if not wheels or not sdists:
        # 这就是 2.1.0 的坑：只有 wheel
        print(f"[ERR] 产物不全：wheel {len(wheels)} / sdist {len(sdists)}"
              "（2.1.0 就是这样只发了 wheel）", file=sys.stderr)
        return EXIT_FAIL
    warns = (r.stderr or "").count("Package would be ignored")
    if warns:
        print(f"[WARN] setuptools 报了 {warns} 条「Package would be ignored」——"
              "数据目录没登记进 packages，长期告警会淹没真告警", file=sys.stderr)
    for f in (*wheels, *sdists):
        print(f"     {f.name}  {f.stat().st_size:,} B")
    return EXIT_OK


def cmd_verify(args) -> int:
    """dist 里**每个产物各装一次** —— 抓 sdist 不自包含（缺文件/缺 package-data）。"""
    root = find_repo_root(Path(args.repo))
    if root is None:
        return EXIT_USAGE
    wheels, sdists = _dist_artifacts(root)
    if not wheels or not sdists:
        print(f"[ERR] 产物不全，先 `publish build`：wheel {len(wheels)} / sdist {len(sdists)}",
              file=sys.stderr)
        return EXIT_FAIL
    vers = read_versions(root)
    want = vers["pyproject"]
    tmp = Path(tempfile.mkdtemp(prefix="publish_verify_"))
    try:
        for art in (*wheels, *sdists):
            vdir = tmp / f"v{abs(hash(art.name)) % 10**6}"
            subprocess.run([_py(), "-m", "venv", str(vdir)], check=False,
                           capture_output=True)
            py = vdir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            ins = subprocess.run([str(py), "-m", "pip", "install", "-q", str(art)],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace")
            if ins.returncode != 0:
                print(f"[ERR] {art.name} 装不上：{(ins.stderr or '')[-600:]}", file=sys.stderr)
                return EXIT_FAIL
            got = subprocess.run([str(py), "-c",
                                  "import allin_governance as a;print(a.__version__)"],
                                 capture_output=True, text=True, encoding="utf-8")
            ver = (got.stdout or "").strip()
            if ver != want:
                print(f"[ERR] {art.name} 装出来是 {ver or '（导入失败）'}，应为 {want}",
                      file=sys.stderr)
                return EXIT_FAIL
            print(f"[OK] {art.name} 独立装成 {ver}")
            shutil.rmtree(vdir, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return EXIT_OK


def cmd_upload(args) -> int:
    root = find_repo_root(Path(args.repo))
    if root is None:
        return EXIT_USAGE
    wheels, sdists = _dist_artifacts(root)
    if not wheels or not sdists:
        print("[ERR] 产物不全（wheel 与 sdist 都要）—— 先 `publish build`", file=sys.stderr)
        return EXIT_FAIL
    tw = _twine_python()
    if not tw:
        print("[ERR] 找不到带 twine 的解释器（设 TWINE_PY=<python>）", file=sys.stderr)
        return EXIT_ENV
    arts = [str(f) for f in (*wheels, *sdists)]
    chk = subprocess.run([tw, "-m", "twine", "check", *arts], capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    if chk.returncode != 0:
        print((chk.stdout or "")[-1000:] + (chk.stderr or "")[-600:], file=sys.stderr)
        return EXIT_FAIL
    print("[OK] twine check 通过（双产物）")
    if not args.upload:
        print("     （未加 --upload，只校验不传）")
        return EXIT_OK
    up = subprocess.run([tw, "-m", "twine", "upload", "--non-interactive",
                         "--repository", args.repository, *arts],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = ((up.stdout or "") + (up.stderr or "")).strip().splitlines()[-3:]
    print("\n".join(tail))
    return EXIT_OK if up.returncode == 0 else EXIT_FAIL


# --------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="allin-governance publish",
        description="PyPI 发版链路（check / build / verify / upload / release）",
        epilog="判据与踩过的坑见包根 PUBLISHING.md。")
    ap.add_argument("--repo", default=".", help="仓根（含 pyproject.toml），默认 cwd")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="版本三处一致 + dist 状态").set_defaults(func=cmd_check)
    sub.add_parser("build", help="构建 wheel + sdist 双产物").set_defaults(func=cmd_build)
    sub.add_parser("verify", help="双产物各装一次（抓 sdist 不自包含）").set_defaults(func=cmd_verify)
    up = sub.add_parser("upload", help="twine check + 上传（需 --upload 才真传）")
    up.add_argument("--upload", action="store_true", help="真的上传（默认只校验）")
    up.add_argument("--repository", default="pypi")
    up.set_defaults(func=cmd_upload)
    rel = sub.add_parser("release", help="build → verify →（可选）upload 一条龙")
    rel.add_argument("--upload", action="store_true")
    rel.add_argument("--repository", default="pypi")
    rel.set_defaults(func=None)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "release":
        for step in (cmd_check, cmd_build, cmd_verify):
            code = step(args)
            if code != EXIT_OK:
                return code
        args.upload = getattr(args, "upload", False)
        return cmd_upload(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
