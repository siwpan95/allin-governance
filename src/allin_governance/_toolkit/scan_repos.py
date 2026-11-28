#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scan_repos.py —— 发布区扫盘：项目指纹、重复检出、合集内多副本。

为什么需要它
------------
2026-11-24 清理冻结仓时，判断「某项目是否重复」全靠一次性临时脚本：
逐个目录算文件哈希、与合集子目录比对。那次暴露出三个问题：

1. **判据散在对话里**。同一件事（「这个目录是不是那份仓的副本」）每次重写一遍，
   口径还不一样：有时比文件哈希，有时比目录名，有时只看 git remote。
2. **浅克隆会骗人**。在浅克隆里 `git log origin/x` 只看得见已下载对象，
   于是「远端只有 6 个提交」这种判断是假象（本项目实测踩过两次）。
3. **「本机不存在」被当成「已废弃」**。当场误删了 3 条 registry 登记
   （anatomy3d / write-agent / portal）—— 它们只是本机没检出。

本工具把这些判据固化成一次扫盘：对每个顶层目录提取**稳定指纹**，
再按指纹找重复。它是只读的，不改任何仓。

指纹的四个来源（缺一不可，任何单一来源都会漏判）
------------------------------------------------
| 来源 | 抓什么 | 抓不到什么 |
|---|---|---|
| ``git remote origin`` | 远端仓名 —— 判断「是不是同一个远端仓的两份检出」最硬 | 本机新建未推的仓 |
| ``pyproject.toml`` / ``setup.py`` / ``package.json`` 的 name | 发布名 —— 判断「PyPI/npm 上是同一个包」 | 非打包项目 |
| 关键文件哈希集合 | 内容相似度 —— 抓「改了名但内容是同一份」 | 空壳/仅 README 的仓 |
| 目录名 | 最弱，只作辅助 | 什么都抓不到 |

**故意不把目录名当主判据**：`mcclone` / `mc-clone`、`txtlegend` / `legend`
这类 id 与目录名不一致的重复登记，本轮实测正是按文件名匹配才漏掉的。

用法
----
    python tools/scan_repos.py                      # 扫默认发布区，输出重复与多副本
    python tools/scan_repos.py --root <发布区>
    python tools/scan_repos.py --json               # 机读
    python tools/scan_repos.py --dup-only           # 只列可疑重复

退出码：0 无重复 / 1 发现可疑重复（可进 CI）/ 2 用法错误 / 3 发布区不存在。

与相邻工具的分工
----------------
| 工具 | 管什么 |
|---|---|
| ``check_repos.mjs`` | registry 清单 ↔ 远端仓的**锚点**一致性（名字、存在性） |
| ``repo_hygiene.py`` | 发布区**卫生**（未登记目录、空壳仓、产物入库） |
| ``docs_backlinks.py`` | 文档之间的**引用**关系 |
| **本工具** | **内容层面的重复** —— 同一份东西出现在几处 |
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

#: 遍历时跳过的目录：这些不是项目内容，且体量大。
SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "target", "output", "cache", ".cache",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".idea", ".vscode", "site-packages", ".next", ".nuxt",
})

#: 算内容指纹时只取这些扩展名的文件 —— 二进制/产物既慢又无鉴别力。
TEXT_SUFFIXES = frozenset({
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs",
    ".java", ".kt", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".rb",
    ".php", ".lua", ".swift", ".sh", ".bash", ".ps1", ".bat", ".cmd",
    ".md", ".txt", ".rst", ".toml", ".yaml", ".yml", ".json", ".ini",
    ".cfg", ".html", ".css", ".sql",
})

#: 单个项目的指纹采样上限 —— 大仓只需代表性样本即可判断「是不是同一份」。
MAX_FILES_FOR_FINGERPRINT = 400

#: 扫描单元的「区」分类：**有序规则表，first-match-wins**（借鉴 kylin-x-kernel
#: `code_stats.py` 的 ordered path_rules：规则对象本身参与回归测试，不靠散文约定）。
#: 为什么需要：2026-11-26 整盘实战中 `archive/` 的**有意归档备份**被报成
#: 「同远端仓多份检出」/「内容高度相似」—— 归档不是残留，是资产保全；把它和发布区
#: 放在同一平面比较，会产生永久假告警，而永久告警等于没有告警。
#: 匹配口径：按路径组件（component）比，不是子串 —— 与 ADR-0011「路径段不含」同款。
ZONE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("release", ("repos",)),                                  # 发布区：正式检出
    ("archive", ("archive", "__archive", "_archive",
                 "backup", "_backup", "legacy", "_git_backup", "_safety_backup")),
    ("runtime", ("nodejs", "nodejs_v22_backup", "python314", "python",
                 "git", "cargo", "rustup", "go", "go_1270", "mingw64", "powershell",
                 ".pnpm-store", "steamlibrary")),
    ("temp", ("tmp", "temp", "logs", "_go_tmp", "work_area", "_8rsv60v_e2e_20261125")),
    ("system", ("$recycle.bin", "system volume information", "config.msi",
                "found.000", "found.001", "found.002", "found.003", "found.004",
                "found.005", "found.006", "found.007", "found.008", "found.009",
                "found.010", "found.011", "found.012", "found.013", "found.014")),
)
#: 系统修复碎片目录 `found.NNN` 是固定形态，上表穷举不完 ⇒ 再补一条前缀规则。
ZONE_PREFIX_RULES: tuple[tuple[str, str], ...] = (
    ("system", "found."),
    ("system", "$recycle"),
    ("temp", "_tmp_"),
)

#: 参与**重复判定**的区：只有发布区与未能分类的 other。
#: 归档区（资产保全）、运行时区（nodejs/python 等）、临时区、系统碎片区都**不参与**
#: —— 它们之间的「相似」不是重复，是同一类东西的正常形态（2026-11-26 整盘实战：
#: `c` vs `tmp` 79%、`nodejs` vs `nodejs_v22_backup` 50%，全是噪声）。
JUDGED_ZONES = frozenset({"release", "other"})

#: 归档区单独提示「只报不警」——它不是残留，但值得让读者知道「这里有多份」。
NON_ALERT_ZONES = frozenset({"archive"})

#: 规则指纹：口径要能比对。同一事实两次统计若规则不同，数字不可比
#: （借鉴业界「Record exclusions and revision IDs when saving metrics」）。
def rules_digest() -> str:
    """规则表的稳定摘要（前 12 位）。改规则即改指纹，便于判断两次读数是否同口径。"""
    payload = repr(ZONE_RULES) + repr(ZONE_PREFIX_RULES) + repr(sorted(SKIP_DIRS))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def classify_zone(rel_parts: tuple[str, ...]) -> str:
    """按路径组件判区。first-match-wins；未命中记 ``other``。

    ``rel_parts`` 是**相对扫描根**的路径组件（小写比较）。
    只看第一段与任一段的命中：`archive/xxx` 命中 archive；`repos/xxx` 命中 release。
    """
    for part in rel_parts:
        low_exact = part.lower()
        for zone, names in ZONE_RULES:
            if low_exact in names:
                return zone
        for zone, prefix in ZONE_PREFIX_RULES:
            if low_exact.startswith(prefix):
                return zone
    return "other"


def collect_skipped(root: Path, infos: list[dict], deep: bool) -> list[dict]:
    """列出**不参与重复判定**的条目并给原因。

    语义统一为「skipped = 不参与判定的所有项」，覆盖两类：
    a) 进了扫描单元但区属 runtime/temp/system 的（如 `found.000`、`nodejs`）；
    b) 压根没进扫描单元的（非目录、隐藏、skip-dir、无项目信号）。

    为什么必须有：2026-11-26 整盘实测里 `found.000`~`found.014` **既不在项目数里、
    也不在任何一节**，从输出无法判断是「按规则跳过」还是「漏掉了」。
    业界（tokei / kylin code_stats）都把 uncounted 与 reason 显式列出，本次照做。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for it in infos:
        seen.add(str(Path(it["path"]).resolve()))
        zone = it["zone"]
        if zone not in JUDGED_ZONES and zone != "archive":
            out.append({"path": it["label"], "reason": f"zone={zone}（不参与重复判定）"})
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        key = str(entry.resolve())
        if key in seen:
            continue
        if not entry.is_dir():
            out.append({"path": entry.name, "reason": "not-a-directory"})
            continue
        if entry.name.startswith("."):
            out.append({"path": entry.name, "reason": "hidden-directory"})
            continue
        if entry.name in SKIP_DIRS:
            out.append({"path": entry.name, "reason": "skip-dir (build/venv/cache)"})
            continue
        if deep:
            out.append({"path": entry.name, "reason": "no-project-signal (无 .git/打包清单)"})
        else:
            out.append({"path": entry.name, "reason": "shallow (未递归；用 --deep 展开)"})
    return out

#: 「这里的东西已经搬走了」的标记文件。2026-11-24 实测：`musickit/remix` 只剩
#: `MIGRATED.txt` + 旧代码，而顶层 `remix` 才是现行项目 —— 这类残留不会被
#: 「项目标识」判据抓到（它没有 .git/pyproject），但它恰恰是「多副本」的真身。
MIGRATION_MARKERS = ("MIGRATED.txt", "MIGRATED.md", "ARCHIVED.md", "ARCHIVED.txt",
                     "MOVED.md", "DEPRECATED.md", ".migrated")

_NAME_IN_PYPROJECT = re.compile(r'^\s*name\s*=\s*["\']([^"\']+)["\']', re.M)
_VERSION_IN_PYPROJECT = re.compile(r'^\s*version\s*=\s*["\']([^"\']+)["\']', re.M)


def _git(root: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=20, errors="replace",
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def remote_repo_name(root: Path) -> str:
    """``origin`` 的仓名（不含 owner、去 .git）。取不到返回空串。"""
    url = _git(root, "remote", "get-url", "origin")
    if not url:
        return ""
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    return url.rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def declared_name(root: Path) -> tuple[str, str]:
    """读发布名（PyPI/npm）。返回 ``(name, 来源文件)``，取不到返回 ``("", "")``。"""
    for fname, regex in (
        ("pyproject.toml", _NAME_IN_PYPROJECT),
        ("package.json", None),
    ):
        p = root / fname
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if fname == "package.json":
            try:
                data = json.loads(text)
                name = str(data.get("name") or "")
                if name:
                    return name, fname
            except Exception:
                pass
            continue
        m = regex.search(text)
        if m:
            return m.group(1), fname
    # setup.py 只做低置信度兜底（正则读代码不可靠，仅取第一处 name=）
    sp = root / "setup.py"
    if sp.is_file():
        m = re.search(r'name\s*=\s*["\']([^"\']+)["\']', sp.read_text(encoding="utf-8", errors="replace"))
        if m:
            return m.group(1), "setup.py"
    return "", ""


def content_fingerprint(root: Path) -> tuple[frozenset[str], int]:
    """内容指纹 = 前 N 个文本文件的 sha256 前缀集合；同时返回文件总数。

    用**集合**而非单个哈希：目录增减一个文件不应让「同一份东西」判成不同，
    但内容改写要能被察觉 —— 集合的 Jaccard 距离正好表达这件事。
    """
    hashes: set[str] = set()
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            path = Path(dirpath) / fn
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            total += 1
            if len(hashes) >= MAX_FILES_FOR_FINGERPRINT:
                continue
            try:
                data = path.read_bytes()
            except Exception:
                continue
            hashes.add(hashlib.sha256(data).hexdigest()[:16])
    return frozenset(hashes), total


def migration_marker(p: Path) -> str:
    """目录里是否有「已迁走」标记。返回标记名或空串。"""
    for m in MIGRATION_MARKERS:
        if (p / m).exists():
            return m
    return ""


def looks_like_project(p: Path) -> bool:
    """子目录是否像一个独立项目（而非源码子包/资源目录）。"""
    return (p / ".git").exists() or any(
        (p / f).is_file() for f in ("pyproject.toml", "setup.py", "package.json", "Cargo.toml", "go.mod")
    )


def is_collection(p: Path) -> bool:
    """顶层目录是否是「合集」（多个子项目塞在一个仓里）。

    判据：子目录 ≥ 3，且其中 ≥ 2 个自带项目标识。阈值取 3/2 是因为——
    - 取 2 会把「src/ + tests/」这类普通单仓误判成合集；
    - 取 4 会漏掉 music-prototypes 这种只有 3 个子项的合集。
    """
    try:
        children = [c for c in p.iterdir() if c.is_dir() and not c.name.startswith(".")
                    and c.name not in SKIP_DIRS]
    except Exception:
        return False
    if len(children) < 3:
        return False
    return sum(1 for c in children if looks_like_project(c)) >= 2


def iter_units(root: Path, deep: bool) -> list[tuple[str, Path]]:
    """扫描单元：顶层项目；``deep`` 时额外展开子项目。

    返回 ``(显示名, 路径)``。合集子项显示为 ``合集/子项`` —— 这既是报告里的
    定位，也是判断「同一项目出现在多个合集」的依据。

    ``deep`` 的展开**不要求父目录是「合集」**：2026-11-24 实测，`musickit/remix`
    与 `bloodmusic/remix` 的父目录子项不足 3 个，按合集阈值会漏掉，而它们恰恰
    与顶层 `remix` 是同一项目的三个版本。所以 deep 只要求子目录「像项目」，
    宁可多扫 —— 噪声由下面的判据（自带 .git 才取 remote、包含关系不算相似）压住。
    """
    units: list[tuple[str, Path]] = []
    tops = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    for top in tops:
        units.append((top.name, top))
        if not deep:
            continue
        for child in sorted(top.iterdir()):
            if not child.is_dir() or child.name.startswith(".") or child.name in SKIP_DIRS:
                continue
            if looks_like_project(child) or migration_marker(child):
                units.append((f"{top.name}/{child.name}", child))
    return units


def describe(root: Path, label: str = "") -> dict:
    # 只认**自带** .git：合集的子目录没有自己的 .git，`git -C` 会向上找到父仓的，
    # 于是所有子项的 remote 都变成合集仓名 —— 那会把「合集 + 子项」全判成
    # 「同远端仓多份检出」。2026-11-24 实测：dev-toolkit 的 3 个子项全被误报。
    is_git = (root / ".git").exists()
    remote = remote_repo_name(root) if is_git else ""
    name, name_src = declared_name(root)
    fp, nfiles = content_fingerprint(root)
    rel_parts = tuple((label or root.name).split("/"))
    return {
        "dir": root.name,
        "path": str(root),
        "remote": remote,
        "name": name,
        "name_src": name_src,
        "is_git": is_git,
        "is_shallow": _git(root, "rev-parse", "--is-shallow-repository") == "true" if is_git else False,
        "commits": _git(root, "rev-list", "--count", "HEAD") if is_git else "",
        "files": nfiles,
        "marker": migration_marker(root),
        "zone": classify_zone(rel_parts),
        "fp": fp,
    }


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)



# ---------------------------------------------------------------------------
# 发现目录（--catalog）：公共核替消费仓解决「发布区里有哪些项目、怎么启动、远端是谁」。
#
# 分工：本模块只产出**机器可判**的观测字段；人工语义（name/category/note/verify）
# 绝不出现在这里——那是消费仓的 registry/projects/ 该管的事。
# 产物是 stdout 的 JSON（可用 --out 落盘），消费方自行决定写不写、写到哪。
# ---------------------------------------------------------------------------
import datetime as _dt
import socket as _socket

CATALOG_SCHEMA = "allin-governance/discovery-catalog/v1"


def _git_info(path: Path) -> tuple[str, str]:
    """git remote / 当前分支；失败返回空串，不猜、不伪造。"""
    repo, branch = "", ""
    try:
        url = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=8).stdout.strip()
        m = re.search(r"(?:gitee\.com|github\.com)[/:]([^/]+)/([^/]+?)(?:\.git)?$", url, re.I)
        if m:
            repo = f"{m.group(1)}/{m.group(2)}"
        branch = subprocess.run(["git", "-C", str(path), "symbolic-ref", "--short", "HEAD"],
                                capture_output=True, text=True, timeout=8).stdout.strip()
    except Exception:                                   # noqa: BLE001
        pass
    return repo, branch


def _launch_probe(path: Path) -> dict | None:
    """按仓库内真实入口探测启动命令；只有依据明确才给，探测不到返回 None。"""
    for name, cmd in (("main.py", "python main.py"), ("app.py", "python app.py"),
                      ("index.js", "node index.js"), ("run.bat", "run.bat")):
        if (path / name).is_file():
            return {"command": cmd, "basis": name}
    pkg = path / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            if (data.get("scripts") or {}).get("start"):
                return {"command": "npm start", "basis": "package.json scripts.start"}
        except Exception:                               # noqa: BLE001
            pass
    return None


def build_catalog(root: Path, *, deep: bool = False) -> dict:
    """发布区 → 发现目录。只含机器字段 + 探针，不含任何人工语义。"""
    units = iter_units(root, deep)
    rows = []
    for label, path in units:
        repo, branch = _git_info(path)
        probe = _launch_probe(path)
        rows.append({
            "id": label.split("/", 1)[0],
            "path": f"repos/{label}",
            "checkout": "local",
            "launch": probe,
            "publish": {"repo": repo, "branch": branch},
            "machine_fields": ["id", "path", "checkout", "publish.repo", "publish.branch"],
            "human_fields": ["name", "category", "version", "manual", "note", "verify"],
        })
    return {
        "schema": CATALOG_SCHEMA,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "root": str(root),
        "hostname": _socket.gethostname().lower(),
        "discovered": len(rows),
        "probe_unavailable": sum(1 for r in rows if not r["launch"]),
        "rows": rows,
        "_note": "机器观测态，可覆盖；人工语义由消费仓 registry/projects/ 持有，本产物不含。",
    }

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="发布区扫盘：项目指纹与重复检出")
    ap.add_argument("--root", default=None, help="发布区根（默认 <仓根>/../repos 或 ALLIN_REPOS_ROOT）")
    ap.add_argument("--json", action="store_true", help="机读输出")
    ap.add_argument("--dup-only", action="store_true", help="只输出可疑重复")
    ap.add_argument("--deep", action="store_true",
                    help="展开合集内部子项目（找「同一项目出现在多个合集」必用）")
    ap.add_argument("--sim", type=float, default=0.5, help="内容相似度阈值（默认 0.50）")
    ap.add_argument("--catalog", action="store_true",
                    help="输出发现目录 JSON（机器字段 + 探针；消费仓据此写观测态）")
    ap.add_argument("--out", type=Path, default=None, help="--catalog 的落盘路径（不传则 stdout）")
    args = ap.parse_args(argv)

    here = Path(__file__).resolve().parent
    if args.root:
        root = Path(args.root)
    else:
        env = os.environ.get("ALLIN_REPOS_ROOT")
        if env:
            root = Path(env)
        else:
            # 本仓住在发布区里：<发布区>/allin/tools/scan_repos.py
            # ⇒ 仓根 = here.parent，发布区 = 仓根的父目录（当父目录名是 repos 时）
            repo_root = here.parent
            root = repo_root.parent if repo_root.parent.name.lower() == "repos" else repo_root / "repos"
    if not root.is_dir():
        print(f"[ERR] 发布区不存在: {root}", file=sys.stderr)
        return 3

    if args.catalog:
        catalog = build_catalog(root, deep=args.deep)
        text = json.dumps(catalog, ensure_ascii=False, indent=2) + chr(10)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text, encoding="utf-8")
            print(f"catalog -> {args.out}（{catalog['discovered']} 项，探针不可用 {catalog['probe_unavailable']}）")
        else:
            print(text, end="")
        return 0

    units = iter_units(root, args.deep)
    infos = []
    for label, path in units:
        info = describe(path, label)
        info["label"] = label
        info["top"] = label.split("/", 1)[0]
        info["is_child"] = "/" in label
        infos.append(info)

    # 只判 release / other 两类区；归档/运行时/临时/系统区不参与，避免永久假告警。
    judged = [it for it in infos if it["zone"] in JUDGED_ZONES]
    non_alert = [it for it in infos if it["zone"] == "archive"]
    out_of_scope = [it for it in infos if it["zone"] not in JUDGED_ZONES and it["zone"] != "archive"]
    skipped = collect_skipped(root, infos, args.deep)

    # ---- 重复判定：三条独立判据，任一命中即列入（不合并成「综合分」）----
    by_remote: dict[str, list[dict]] = defaultdict(list)
    by_name: dict[str, list[dict]] = defaultdict(list)
    for it in judged:
        if it["remote"]:
            by_remote[it["remote"].lower()].append(it)
        if it["name"]:
            by_name[it["name"].lower()].append(it)

    findings: list[dict] = []
    for key, group in sorted(by_remote.items()):
        if len(group) > 1:
            findings.append({"kind": "同远端仓多份检出", "key": key,
                             "holders": [g["label"] for g in group]})
    for key, group in sorted(by_name.items()):
        if len(group) > 1:
            findings.append({"kind": "同发布名多份", "key": key,
                             "holders": [g["label"] for g in group]})

    # 内容相似：只在「文件数相近」的对之间比，避免 O(n²) 全量算
    pairs = []
    for i in range(len(judged)):
        for j in range(i + 1, len(judged)):
            a, b = judged[i], judged[j]
            # 包含关系不是重复：合集的整体必然与其子项高度相似
            pa, pb = str(a["path"]), str(b["path"])
            if pa.startswith(pb + os.sep) or pb.startswith(pa + os.sep):
                continue
            if min(a["files"], b["files"]) == 0:
                continue
            if max(a["files"], b["files"]) > min(a["files"], b["files"]) * 3:
                continue
            sim = jaccard(a["fp"], b["fp"])
            if sim >= args.sim:
                pairs.append((sim, a["label"], b["label"], a["files"], b["files"]))
    for sim, x, y, nx, ny in sorted(pairs, reverse=True):
        findings.append({"kind": "内容高度相似", "key": f"{sim:.0%}", "holders": [f"{x}({nx})", f"{y}({ny})"]})

    # ---- 同名项目跨合集 / 合集内多副本 ----
    by_leaf: dict[str, list[dict]] = defaultdict(list)
    for it in judged:
        by_leaf[it["label"].split("/")[-1].lower()].append(it)
    for key, group in sorted(by_leaf.items()):
        if len(group) > 1:
            tops_in = {g["top"] for g in group}
            kind = "同名项目跨合集" if len(tops_in) > 1 else "合集内同名多副本"
            findings.append({"kind": kind, "key": key,
                             "holders": [g["label"] for g in group]})

    # ---- 迁移残留：有 MIGRATED/ARCHIVED 标记的目录，多半是「搬走没删」----
    residues = [it for it in judged if it.get("marker")]
    if residues:
        findings.append({"kind": "疑似迁移残留（带搬走标记）", "key": str(len(residues)),
                         "holders": [f"{it['label']} [{it['marker']}]" for it in residues]})

    zones: dict[str, int] = defaultdict(int)
    for it in infos:
        zones[it["zone"]] += 1

    if args.json:
        print(json.dumps({
            "root": str(root),
            #: 口径三件套：范围、规则指纹、时点。缺任一项，两次读数不可比
            #: （业界：Record exclusions and revision IDs when saving metrics）。
            "scope": {
                "depth": "deep" if args.deep else "shallow",
                "note": "shallow=仅一级目录，未递归；递归请 --deep" if not args.deep
                        else "deep=一级 + 合集内像项目的子目录",
                "sim": args.sim,
            },
            "rules_digest": rules_digest(),
            "projects": len(infos),
            "projects_judged": len(judged),
            "projects_non_alert": len(non_alert),
            "projects_out_of_scope": len(out_of_scope),
            "zones": dict(sorted(zones.items())),
            "skipped": skipped,
            "findings": findings,
            "details": [{k: v for k, v in it.items() if k != "fp"} for it in infos],
        }, ensure_ascii=False, indent=2))
        return 1 if findings else 0

    scope = ("仅一级目录，未递归 —— 递归请加 --deep" if not args.deep
             else "一级 + 合集内像项目的子目录")
    print(f"发布区：{root}")
    print(f"扫描范围：{scope}")
    print(f"项目数：{len(infos)}　（含 git {sum(1 for i in infos if i['is_git'])}，"
          f"浅克隆 {sum(1 for i in infos if i['is_shallow'])}；"
          f"规则指纹 {rules_digest()}）")
    if zones:
        print("分区：" + " · ".join(f"{k} {v}" for k, v in sorted(zones.items())))
    if skipped:
        reasons: dict[str, int] = defaultdict(int)
        for s in skipped:
            reasons[s["reason"]] += 1
        print(f"未纳入扫描 {len(skipped)} 项：" +
              " · ".join(f"{r} {n}" for r, n in sorted(reasons.items(), key=lambda kv: -kv[1])))
    if non_alert:
        print(f"归档区 {len(non_alert)} 项**只报不警**（资产保全，不算迁移残留）")
    if out_of_scope:
        print(f"运行时/临时/系统区 {len(out_of_scope)} 项不参与重复判定（非项目形态）")
    print(f"参与判定：{len(judged)} 项（release + other）")
    if not args.dup_only:
        print(f"\n未取到发布名的项目 {sum(1 for i in infos if not i['name'])} 个"
              f"（非打包项目属正常）；未取到 remote 的 {sum(1 for i in infos if not i['remote'])} 个")
    if not findings:
        print("\n未发现重复：每个项目只有一份。")
        return 0
    print(f"\n可疑重复 {len(findings)} 组：")
    for f in findings:
        print(f"  [{f['kind']}] {f['key']}")
        for h in f["holders"]:
            print(f"      - {h}")
    print("\n提示：本工具只报告，不处置。删除前请按「冗余三查」确认接任者。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
