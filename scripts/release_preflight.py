#!/usr/bin/env python3
"""发版前置校验 —— 把 1.0.0 那次「wheel 好、sdist 坏」事故变成退出码。

本脚本随公共核提仓从消费仓 allin 迁入；判据执行地只此一份。
静态模式秒级；`--install` 才是那次事故的唯一鉴别实验（从 sdist 装、再跑子命令）。

    python scripts/release_preflight.py             # 静态检查
    python scripts/release_preflight.py --install   # 追加：clean venv 装 sdist + wheel 并跑子命令

退出码：0 通过 / 1 阻断 / 2 用法错 / 3 环境错。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import venv
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

EXIT_OK, EXIT_BLOCKING, EXIT_USAGE, EXIT_ENV = 0, 1, 2, 3

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
VERSION_FILE = ROOT / "VERSION"
INIT = ROOT / "src" / "allin_governance" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"
README = ROOT / "README.md"
DIST = ROOT / "dist"


class Result:
    """三态必须分清：ok / fail / skip；skip 不算 ok。"""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def ok(self, name: str, detail: str = "") -> None:
        self.rows.append(("ok", name, detail))

    def fail(self, name: str, detail: str) -> None:
        self.rows.append(("fail", name, detail))

    def skip(self, name: str, detail: str) -> None:
        self.rows.append(("skip", name, detail))

    def report(self) -> int:
        width = max((len(name) for _, name, _ in self.rows), default=0)
        for status, name, detail in self.rows:
            mark = {"ok": "[ok]  ", "fail": "[FAIL]", "skip": "[skip]"}[status]
            print(f"{mark} {name.ljust(width)}  {detail}")
        failed = [r for r in self.rows if r[0] == "fail"]
        skipped = [r for r in self.rows if r[0] == "skip"]
        print()
        print(f"PREFLIGHT ok={len(self.rows) - len(failed) - len(skipped)} "
              f"fail={len(failed)} skip={len(skipped)}")
        if failed:
            print("RESULT: BLOCKED —— 未通过项见上；不要上传。")
            return EXIT_BLOCKING
        if skipped:
            print("RESULT: INCOMPLETE —— 有项未判（skip 不是 pass）；发版请用 --install 跑全。")
            return EXIT_ENV
        print("RESULT: CLEAN")
        return EXIT_OK


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def version_sources() -> dict[str, str]:
    """从真值源各读一份，不在脚本里手抄版本号。"""
    declared = tomllib.loads(_read(PYPROJECT))["project"]["version"]
    code = re.search(r'^__version__\s*=\s*"([^"]+)"', _read(INIT), re.M)
    change = re.search(r"^## (\S+)", _read(CHANGELOG), re.M)
    readme = re.search(r"当前版本 `([^`]+)`", _read(README))
    return {
        "pyproject": declared,
        "__init__": code.group(1) if code else "",
        "VERSION": VERSION_FILE.read_text(encoding="utf-8").strip(),
        "CHANGELOG首条": change.group(1) if change else "",
        "README当前版本": readme.group(1) if readme else "",
    }


def check_version_single_source(result: Result) -> str:
    sources = version_sources()
    declared = sources["pyproject"]
    problems = [f"{name}={value or '<缺失>'}" for name, value in sources.items()
                if value != declared]
    if problems:
        result.fail("版本单一来源", "；".join(problems))
    else:
        result.ok("版本单一来源", f"{declared}（pyproject / __init__ / VERSION / CHANGELOG / README 五处一致）")
    return declared


def check_dist_freshness(result: Result, version: str) -> None:
    """`twine upload dist/*` 会连旧版本一起传，混版本必须拦。"""
    if not DIST.is_dir():
        result.fail("dist 目录", f"不存在：{DIST}（先跑 python -m build）")
        return
    stale = sorted(p.name for p in DIST.iterdir() if version not in p.name)
    if stale:
        result.fail("dist 目录干净", f"混入非 {version} 的产物：{', '.join(stale)}")
    else:
        result.ok("dist 目录干净", f"只含 {version} 的产物")


def check_artifacts(result: Result, version: str) -> tuple[Path | None, Path | None]:
    wheel = DIST / f"allin_governance-{version}-py3-none-any.whl"
    sdist = DIST / f"allin_governance-{version}.tar.gz"
    missing = [p.name for p in (wheel, sdist) if not p.is_file()]
    if missing:
        result.fail("发布物齐备", f"缺少：{', '.join(missing)}")
    else:
        result.ok("发布物齐备", f"{wheel.name} / {sdist.name}")
    return (wheel if wheel.is_file() else None, sdist if sdist.is_file() else None)


def check_sdist_self_contained(result: Result, version: str, sdist: Path | None) -> None:
    """独立仓已无跨目录 package-dir 映射；sdist 必须自带 src 真身。"""
    if sdist is None:
        result.skip("sdist 自包含", "没有 sdist 可查")
        return
    root = f"allin_governance-{version}"
    with tarfile.open(sdist, "r:gz") as tar:  # gov-python: allow —— 二进制归档
        names = tar.getnames()
        member = tar.extractfile(f"{root}/pyproject.toml")
        inner = tomllib.loads(member.read().decode("utf-8")) if member else {}
    mapping = inner.get("tool", {}).get("setuptools", {}).get("package-dir", {})
    escaping = {k: v for k, v in mapping.items() if str(v).startswith("..")}
    has_src = f"{root}/src/allin_governance/__init__.py" in names
    if escaping:
        result.fail("sdist 已物化", f"package-dir 仍指向项目外：{escaping}")
    elif not has_src:
        result.fail("sdist 自包含", f"缺 {root}/src/allin_governance/__init__.py")
    else:
        result.ok("sdist 自包含", "src 真身与 pyproject 均在包内，无外指 package-dir")


def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, **kwargs,  # type: ignore[arg-type]
    )


def check_twine(result: Result, version: str) -> None:
    probe = run([sys.executable, "-m", "twine", "--version"])
    if probe.returncode != 0:
        result.skip("twine check", "本解释器没装 twine（pip install twine build）")
        return
    targets = [str(DIST / f"allin_governance-{version}-py3-none-any.whl"),
               str(DIST / f"allin_governance-{version}.tar.gz")]
    proc = run([sys.executable, "-m", "twine", "check", *targets])
    if proc.returncode != 0:
        result.fail("twine check", (proc.stdout + proc.stderr).strip()[:400])
    else:
        result.ok("twine check", "元数据与长描述通过")


def _venv_python(base: Path) -> Path:
    candidate = base / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not candidate.exists():
        raise SystemExit(f"ENV ERROR: venv 建好了但找不到解释器：{candidate}")
    return candidate


def install_and_smoke(result: Result, artifact: Path, label: str) -> None:
    """与仓无关的 venv 里装这一个文件，再跑真实子命令。"""
    with tempfile.TemporaryDirectory(prefix="governance-preflight-") as tmp:
        env_dir = Path(tmp) / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(env_dir)
        python = _venv_python(env_dir)
        install = run([str(python), "-m", "pip", "install", "--quiet", str(artifact)])
        if install.returncode != 0:
            tail = (install.stdout + install.stderr).strip().splitlines()[-6:]
            result.fail(f"{label} 可安装", " | ".join(tail)[:500])
            return

        cli = env_dir / ("Scripts/allin-governance.exe" if sys.platform == "win32"
                         else "bin/allin-governance")
        if not cli.exists():
            result.fail(f"{label} 可安装", f"装完了却没有入口点：{cli}")
            return

        listing = run([str(python), "-c",
                       "from allin_governance.cli import SUBCOMMANDS;"
                       "print(' '.join(sorted(SUBCOMMANDS)))"])
        names = listing.stdout.strip().split() if listing.returncode == 0 else []
        if not names:
            head = (listing.stdout + listing.stderr).strip().splitlines()[-4:]
            result.fail(f"{label} 子命令清单", "读不到 SUBCOMMANDS | " + " | ".join(head)[:300])
            return

        smoke: list[tuple[str, list[str]]] = [("--version", [str(cli), "--version"])]
        smoke.extend((name, [str(cli), name, "--help"]) for name in names)
        scaffold = Path(tmp) / "scaffold"
        scaffold.mkdir()
        smoke.append(("init 实跑", [str(cli), "init", "--dry-run", str(scaffold)]))
        for name, argv in smoke:
            proc = run(argv)
            if proc.returncode != 0:
                tail = (proc.stdout + proc.stderr).strip().splitlines()[-4:]
                result.fail(f"{label} 子命令 {name}",
                            f"exit={proc.returncode} | " + " | ".join(tail)[:400])
                return
        result.ok(f"{label} 可安装", f"{len(names)} 个子命令 + init --dry-run 全部 exit 0")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="release_preflight",
        description="allin-governance 发版前置校验：版本一致 / 产物干净 / sdist 自包含 / 可安装",
    )
    parser.add_argument("--install", action="store_true",
                        help="追加干净 venv 安装（sdist + wheel）并跑子命令；发版前必须带这个")
    parser.add_argument("--json", action="store_true", help="输出机器可读结论")
    args = parser.parse_args(argv)

    missing = [p.name for p in (PYPROJECT, VERSION_FILE, INIT, CHANGELOG, README)
               if not p.is_file()]
    if missing:
        print(f"ENV ERROR: 缺少真值源：{', '.join(missing)}（cwd 不影响，请在本仓根执行）",
              file=sys.stderr)
        return EXIT_ENV

    result = Result()
    version = check_version_single_source(result)
    check_dist_freshness(result, version)
    wheel, sdist = check_artifacts(result, version)
    check_sdist_self_contained(result, version, sdist)
    check_twine(result, version)
    if args.install:
        if sdist is not None:
            install_and_smoke(result, sdist, "sdist")
        if wheel is not None:
            install_and_smoke(result, wheel, "wheel")
    else:
        result.skip("安装实测", "未带 --install —— 发版前必须跑 python scripts/release_preflight.py --install")

    code = result.report()
    if args.json:
        print(json.dumps({"version": version, "rows": result.rows, "exit": code},
                         ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
