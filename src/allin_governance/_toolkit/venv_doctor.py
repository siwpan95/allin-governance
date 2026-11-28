"""虚拟环境体检 / 修复 / 重建（provider 侧通用能力）。

为什么在公共核：**每个仓都有 `.venv`，每个仓都会坏**，而修法不依赖任何业务语义。
这正是「通用能力下沉」的典型——allin 里那套 964 行的深度巡检（7 项检查）是它的
超集实现；本模块提供不依赖消费仓即可运行的最小闭环，并在消费仓有深度执行器时委派。

三个动作：
  check   —— 解释器可执行 / pyvenv.cfg 的 home 有效 / pip 可用（只读，退出码语义化）
  repair  —— 只修「可安全原地修」的：pip 缺失走 ensurepip；不动解释器基准
  rebuild —— 改名旧 venv → 按原基准解释器重建 → 装 requirements（破坏性，需显式调用）

边界：repair 不猜、rebuild 不静默删除。旧 venv 一律改名保留时空证据。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

VENV_DIRS = (".venv", "venv", "env")
REQ_FILES = ("requirements-dev.txt", "requirements.txt")


def find_venv(root: Path) -> Path | None:
    for name in VENV_DIRS:
        cand = root / name
        if (cand / ("Scripts/python.exe" if os.name == "nt" else "bin/python")).is_file():
            return cand
    return None


def _python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _run(exe: Path, args: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run([str(exe), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:                              # noqa: BLE001
        return 127, str(exc)


def _home_version(home: str) -> str:
    """从 home 目录名/解释器推断版本（3.13.12 / python3.13 两种形态）。"""
    import re
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", home)
    return ".".join(x for x in m.groups() if x) if m else ""


def classify_home(home: str, interpreter: str) -> str | None:
    """纯函数：home 版本与解释器版本的关系。返回 None / drift-name-only / drift-abi。

    拆成纯函数是为了能被单测直接验证——否则只能靠造一个「坏得刚好」的 venv。
    """
    hv = _home_version(home)
    iv = interpreter.replace("Python ", "").strip()
    if not hv or not iv:
        return None
    if hv == iv:
        return None
    return "drift-abi" if hv.split(".")[0:2] != iv.split(".")[0:2] else "drift-name-only"


def check(root: Path) -> dict:
    """只读体检。返回 {venv, issues:[{code,detail}], interpreter, pip, home}。"""
    venv = find_venv(root)
    if venv is None:
        return {"venv": None, "notes": [],
                "issues": [{"code": "no-venv", "detail": f"{root} 下没有 .venv/venv/env"}]}
    py = _python(venv)
    issues: list[dict] = []

    rc, out = _run(py, ["--version"], 30)
    if rc != 0:
        issues.append({"code": "interpreter-broken", "detail": out.strip()[:200]})
    version = out.strip() if rc == 0 else ""

    home = None
    cfg = venv / "pyvenv.cfg"
    if cfg.is_file():
        for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("home"):
                home = line.split("=", 1)[1].strip()
                break
        if home and not Path(home).exists():
            # 基准解释器没了：典型换机/卸载 Python 后的症状，原地修不了
            issues.append({"code": "home-missing", "detail": home})
    else:
        issues.append({"code": "pyvenv-missing", "detail": str(cfg)})

    # 冒烟导入：ssl/sqlite3/ctypes 任一不可用，venv 就是「活着但废了」——
    # 只查 Path(home).exists() 会把它判成健康（调研指出的判据不足）。
    rc_smoke, smoke = _run(py, ["-c", "import ssl, sqlite3, ctypes; print('ok')"], 60)
    if rc_smoke != 0:
        issues.append({"code": "import-smoke-failed", "detail": smoke.strip()[:200]})
    notes: list[dict] = []
    if home:
        drift = classify_home(home, version)
        if drift == "drift-abi":
            issues.append({"code": drift, "detail": f"home={home} interpreter={version or '-'}"})
        elif drift == "drift-name-only":
            # 同名小版本差（3.13.12 vs 3.13.14）不致命：只提示，不阻断
            # （调研：判据从「路径存在」升级为「版本一致」，但一致性分两档）
            notes.append({"code": drift, "detail": f"home={home} interpreter={version or '-'}"})

    rc, out = _run(py, ["-m", "pip", "--version"], 60)
    pip = out.strip().splitlines()[0] if rc == 0 and out.strip() else ""
    if rc != 0:
        issues.append({"code": "pip-broken", "detail": out.strip()[:200]})

    return {"venv": str(venv), "interpreter": version, "pip": pip, "home": home,
            "issues": issues, "notes": notes}


def repair(root: Path) -> dict:
    """只做安全修复：pip 缺失/损坏 → ensurepip。返回处理结果。"""
    state = check(root)
    if state["venv"] is None:
        return {"repaired": False, "reason": "no-venv（用 rebuild）"}
    venv = Path(state["venv"])
    py = _python(venv)
    codes = {i["code"] for i in state["issues"]}
    if "interpreter-broken" in codes or "home-missing" in codes:
        return {"repaired": False,
                "reason": "解释器基准不可用——原地修复会失败且掩盖问题，请用 rebuild"}
    if "pip-broken" in codes:
        rc, out = _run(py, ["-m", "ensurepip", "--upgrade"], 180)
        after = check(root)
        return {"repaired": rc == 0 and not any(i["code"] == "pip-broken" for i in after["issues"]),
                "detail": out.strip()[-300:], "after": after}
    return {"repaired": False, "reason": "无问题"}


def rebuild(root: Path, *, dry_run: bool = False) -> dict:
    """重建：旧 venv 改名保留 → 以当前解释器重建 → 装 requirements。"""
    old = find_venv(root)
    base_py = sys.executable
    if old:
        cfg = old / "pyvenv.cfg"
        if cfg.is_file():
            hv = ""
            for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.lower().startswith("home"):
                    hv = _home_version(line.split("=", 1)[1])
                    break
            bv = ".".join(str(x) for x in sys.version_info[:2])
            if hv and hv.split(".")[:2] != bv.split("."):
                return {"rebuilt": False, "stage": "base-mismatch",
                        "detail": f"旧环境基准 {hv} 与当前解释器 {bv} 跨 minor；"
                                  "重建会换 ABI，需显式指定基准解释器（--base）后再执行"}
                  
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = (old.with_name(old.name + f".bak-{stamp}") if old else None)
    target = old or (root / ".venv")
    plan = {"old": str(old) if old else None, "backup": str(backup) if backup else None,
            "target": str(target), "base": base_py,
            "requirements": [str(root / f) for f in REQ_FILES if (root / f).is_file()]}
    if dry_run:
        return {"dry_run": True, **plan}

    if old and backup:
        old.rename(backup)
    rc, out = _run(Path(base_py), ["-m", "venv", str(target)], 300)
    if rc != 0:
        # 重建失败要把旧环境还回去，避免「越修越坏」
        if backup and backup.exists() and not target.exists():
            backup.rename(old)
        return {"rebuilt": False, "stage": "create", "detail": out.strip()[-400:]}

    new_py = _python(target)
    installed = []
    for req in plan["requirements"]:
        rc, out = _run(new_py, ["-m", "pip", "install", "-q", "-r", req], 1800)
        installed.append({"file": req, "ok": rc == 0, "detail": "" if rc == 0 else out.strip()[-300:]})
        if rc != 0:
            # 安装失败 = 重建失败：把旧环境还回去，绝不留下「半新半旧」还报成功
            shutil.rmtree(target, ignore_errors=True)
            if backup and backup.exists():
                backup.rename(old)
            return {"rebuilt": False, "stage": "pip", "installed": installed,
                    "detail": out.strip()[-300:], "rolled_back": True}
    state = check(root)
    return {"rebuilt": True, "backup": plan["backup"], "installed": installed, "after": state}


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="venv", description="虚拟环境体检 / 修复 / 重建")
    ap.add_argument("action", choices=["check", "repair", "rebuild"])
    ap.add_argument("--root", default=".", help="仓根（默认当前目录）")
    ap.add_argument("--json", action="store_true", help="机读输出")
    ap.add_argument("--dry-run", action="store_true", help="rebuild 只打印计划")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()

    if args.action == "check":
        state = check(root)
        if args.json:
            print(json.dumps(state, ensure_ascii=False, indent=2))
        else:
            if state["venv"] is None:
                print(f"NO VENV: {root}")
            else:
                print(f"venv: {state['venv']}")
                print(f"  interpreter: {state['interpreter'] or '-'}")
                print(f"  pip: {state['pip'] or '-'}")
                print(f"  home: {state['home'] or '-'}")
                for i in state["issues"]:
                    print(f"  ISSUE [{i['code']}] {i['detail']}")
                for n in state.get("notes", []):
                    print(f"  NOTE  [{n['code']}] {n['detail']}")
        return 0 if not state["issues"] else 1

    if args.action == "repair":
        res = repair(root)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("repaired") else 1

    res = rebuild(root, dry_run=args.dry_run)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if (res.get("dry_run") or res.get("rebuilt")) else 1
