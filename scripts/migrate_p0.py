#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 allin 仓迁入公共核（P0：**只报告，不动手**）。

## 为什么有这个脚本

`allin` 仓把「公共核（A 层）」与「治理中枢 / 事实源 / 记录（B、C 层）」混在一个仓里，
边界只能靠目录约定 + 门禁事后拦（§212–219 一路都在用判据追着结构跑）。
参照 `3dworld`（库仓）↔ `3dworld-demos`（消费仓，`pip install 3dworld==0.9.0` 锁版）的形态：
**结构即判据** —— 库仓不读消费仓任何文件，消费仓只通过版本号消费库。

本脚本做 P0 该做的事：**盘清要搬什么、会断什么**，产出清单供人 review，
确认后再由 P1 执行真实迁移（`git mv` / 复制 + 提交）。

## 用法

    python scripts/migrate_p0.py --allin /d/projects/repos/allin        # 只报告
    python scripts/migrate_p0.py --allin ... --json                     # 机读

零第三方依赖。退出码：0 报告完成 / 3 环境错。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

#: allin 仓内的源目录 → 新仓包内落点
SOURCES = {
    "tools/gate/governance": "src/allin_governance/_gatesuite",
    "tools/repocheck": "src/allin_governance/_repocheck",
    "tools/_shared": "src/allin_governance/_toolkit",
    "tools/github": "src/allin_governance/_github",
    "tools/secret_scan": "src/allin_governance/_secret_scan",
    "tools/facts": "src/allin_governance/_facts",
    "packages/allin-governance/src/allin_governance": "src/allin_governance",
}

#: 判定「这是 allin 编排侧的引用」（要改）还是「包内互引」（随迁）
HUB_PATTERNS = (
    ("scripts/run_all_gates.sh", "门禁编排主链"),
    ("scripts/bootstrap.sh", "仓骨架拉起"),
    ("platform/", "platform 门户/CLI 总线"),
    ("preset/", "DSH preset 治理段"),
    (".github/workflows/", "CI"),
    ("docs/", "文档引用（P1 后按需改指针）"),
    ("tests/", "仓测试"),
)


def count_files(base: Path, rel: str) -> list[str]:
    d = base / rel
    if not d.is_dir():
        return []
    return sorted(p.relative_to(base).as_posix() for p in d.rglob("*") if p.is_file()
                  and "__pycache__" not in p.parts and not p.name.endswith((".pyc",)))


def scan_refs(base: Path) -> dict[str, list[dict]]:
    """在 allin 仓里找指向这些目录的引用，按「谁引的」分组。"""
    targets = {src: [] for src in SOURCES}
    names = [s for s in SOURCES if s != "packages/allin-governance/src/allin_governance"]
    pat = re.compile("|".join(re.escape(n) for n in names))
    skip = {"node_modules", ".venv", "build", "dist", "__pycache__", ".git"}
    seen_real: set[str] = set()
    for p in base.rglob("*"):
        if not p.is_file() or p.suffix not in {".py", ".sh", ".mjs", ".ts", ".toml", ".yml", ".yaml", ".md", ".json"}:
            continue
        rel = p.relative_to(base).as_posix()
        if any(part in skip for part in p.parts):
            continue
        # junction 别名去重：docs/facts|spec|records|md 指向中文目录，同一物理文件会被收两次
        real = str(Path(__import__("os").path.realpath(p)))
        if real in seen_real:
            continue
        seen_real.add(real)
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in set(pat.findall(text)):
            # 包内互引（源之一自己引自己）随迁，不算 allin 编排侧
            if any(rel.startswith(src.split("/")[0] + "/" + src.split("/")[1]) for src in names):
                continue
            targets[m].append({"file": rel, "kind": _kind(rel)})
    return targets


def _kind(rel: str) -> str:
    for pref, kind in HUB_PATTERNS:
        if rel.startswith(pref):
            return kind
    return "其他"


def import_risks(base: Path) -> list[dict]:
    """找出**跨目录绝对导入**（迁移后会断的地方）。

    包内代码用 `from _shared import x` / `from ..runtime import y` 这类相对形态是安全的；
    `from tools.facts import ...` / `import tools.repocheck` 这类绝对路径在提仓后必断。
    """
    risky = []
    pat = re.compile(r"^\s*(from|import)\s+(tools\.|tools\b)", re.M)
    for src in SOURCES:
        for relpath in count_files(base, src):
            p = base / relpath
            if p.suffix != ".py":
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            for m in pat.finditer(text):
                line_no = text[:m.start()].count("\n") + 1
                risky.append({"file": relpath, "line": line_no, "code": m.group(0).strip()})
    return risky


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="migrate_p0", description="公共核提仓 P0：只报告")
    ap.add_argument("--allin", required=True, help="allin 仓根")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    base = Path(args.allin).expanduser().resolve()
    if not (base / "tools").is_dir():
        print(f"ENV ERROR: {base} 不是 allin 仓根", file=sys.stderr)
        return 3

    rows, total = [], 0
    for src, dst in SOURCES.items():
        files = count_files(base, src)
        total += len(files)
        size = sum((base / f).stat().st_size for f in files)
        rows.append({"source": src, "dest": dst, "files": len(files), "bytes": size})

    refs = scan_refs(base)
    risks = import_risks(base)
    hub_refs = {}
    for src, items in refs.items():
        for it in items:
            hub_refs.setdefault(it["kind"], set()).add(it["file"])

    out = {"sources": rows, "total_files": total,
           "total_bytes": sum(r["bytes"] for r in rows),
           "references": {k: len(v) for k, v in refs.items()},
           "hub_sites": {k: sorted(v) for k, v in hub_refs.items()},
           "import_risks": risks}

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print("=" * 78)
    print("P0 · 公共核提仓 dry-run（只报告）")
    print("=" * 78)
    print(f"\n源仓：{base}\n")
    print(f"{'源目录':46} {'文件':>5} {'体积':>9}  → 包内落点")
    for r in rows:
        print(f"{r['source']:46} {r['files']:5d} {r['bytes']/1024/1024:8.2f}M  {r['dest']}")
    print(f"\n合计 **{total} 文件 / {out['total_bytes']/1024/1024:.2f} MB**")

    print("\n各目录被引用次数（文件级）：")
    for k, v in out["references"].items():
        print(f"  {k:28} {v:3d} 个文件")

    print("\nallin 编排侧要改的地方（按位置分组）：")
    for kind, files in out["hub_sites"].items():
        print(f"  [{kind}] {len(files)} 个文件")
        for f in files[:6]:
            print(f"      {f}")
        if len(files) > 6:
            print(f"      … 另 {len(files)-6} 个")

    print(f"\n跨目录绝对导入（提仓后必断，P1 需改相对导入）：{len(risks)} 处")
    for r in risks[:12]:
        print(f"  {r['file']}:{r['line']}  {r['code']}")
    if len(risks) > 12:
        print(f"  … 另 {len(risks)-12} 处")

    print("\n下一步（P1，需放行）：复制 120 文件 → 生成无映射的 pyproject → "
          "改相对导入 → 构建 wheel → 解包自检")
    return 0


if __name__ == "__main__":
    sys.exit(main())
