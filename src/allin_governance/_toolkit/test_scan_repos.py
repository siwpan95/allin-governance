"""scan_repos 回归用例：区语义 / skipped 显式化 / 规则指纹。

为什么这些用例必须存在
----------------------
2026-11-26 整盘实战暴露三处不足，本文件逐条钉住修复：

1. **默认单层读数缺分母语义** —— `scan --root G:/` 报「47 项 / 含 git 0」，
   而整盘实有 87 个 git 仓。修复是在读数行显式声明扫描范围；
   本文件用「scope 字段存在且 depth 与 --deep 一致」钉住。
2. **缺「区」语义** —— `archive/` 的有意归档备份被判成「同远端仓多份检出」/「内容高度相似」，
   产生永久假告警。修复是 ordered `ZONE_RULES` + 只有 release/other 参与判定；
   本文件用「归档区两份同内容**不**产生 findings」钉住。
3. **非项目目录被纳入相似度** —— `c` vs `tmp` 79%、`nodejs` vs `nodejs_v22_backup` 50%。
   修复是 runtime/temp/system 区不参与判定；本文件用 `tmp/` 与 `logs/` 同内容不报警钉住。

另：`skipped` 必须带 reason（业界 tokei / kylin `code_stats.py` 同款）——
2026-11-26 实测 `found.000`~`found.014` 既不在项目数、也不在任何一节，
从输出无法判断「跳过」还是「漏掉」。本文件断言未纳入项必有 reason。

跑法：``pytest src/allin_governance/_toolkit/test_scan_repos.py -q``
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scan_repos  # noqa: E402


def _mk(root: Path, rel: str, files: dict[str, str]) -> Path:
    """建一个目录并写入文件；返回目录。"""
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (d / name).write_text(content, encoding="utf-8")
    return d


def test_classify_zone_ordered_rules() -> None:
    """区判定：按路径组件、first-match-wins、未命中记 other。"""
    assert scan_repos.classify_zone(("repos",)) == "release"
    assert scan_repos.classify_zone(("archive",)) == "archive"
    assert scan_repos.classify_zone(("_git_backup",)) == "archive"
    assert scan_repos.classify_zone(("nodejs",)) == "runtime"
    assert scan_repos.classify_zone(("tmp",)) == "temp"
    assert scan_repos.classify_zone(("$RECYCLE.BIN",)) == "system"
    assert scan_repos.classify_zone(("something-unclassified",)) == "other"


def test_classify_zone_prefix_rules() -> None:
    """`found.NNN` 是固定形态且数量不定 ⇒ 必须靠前缀规则，不能穷举。"""
    assert scan_repos.classify_zone(("found.000",)) == "system"
    assert scan_repos.classify_zone(("found.042",)) == "system"
    assert scan_repos.classify_zone(("_tmp_67876_abc",)) == "temp"


def test_classify_zone_component_not_substring() -> None:
    """按组件比，不按子串 —— `myrepos` 不该命中 `repos`（与 ADR-0011 同款）。"""
    assert scan_repos.classify_zone(("myrepos",)) == "other"
    assert scan_repos.classify_zone(("repos-archive",)) == "other"


def test_rules_digest_is_stable_and_changes_with_rules() -> None:
    """规则指纹：同规则同摘要；改规则必变（口径可比对的前提）。"""
    a = scan_repos.rules_digest()
    b = scan_repos.rules_digest()
    assert a == b and len(a) == 12
    original = scan_repos.ZONE_RULES
    try:
        scan_repos.ZONE_RULES = tuple(original) + (("extra", ("never",)),)
        assert scan_repos.rules_digest() != a
    finally:
        scan_repos.ZONE_RULES = original


def test_archive_duplicates_are_not_findings() -> None:
    """归档区两份同内容**不**算重复告警（资产保全，不是迁移残留）。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        same = {"a.py": "print('same')\n", "README.md": "# x\n"}
        _mk(root, "archive/old-thing", same)
        _mk(root, "repos/new-thing", same)
        out = subprocess.run(
            [sys.executable, str(Path(scan_repos.__file__)),
             "--root", str(root), "--json"],
            capture_output=True, text=True, encoding="utf-8",
        )
        data = json.loads(out.stdout)
        assert data["projects"] == 2
        assert data["projects_judged"] == 1, "归档区不应进判定池"
        assert data["projects_non_alert"] == 1
        assert data["findings"] == [], f"归档区相似不该报：{data['findings']}"
        assert out.returncode == 0


def test_runtime_and_temp_are_not_judged() -> None:
    """运行时/临时区的「相似」不是重复：`tmp` 与 `logs` 同内容不报警。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        same = {"x.txt": "same\n"}
        _mk(root, "tmp", same)
        _mk(root, "logs", same)
        out = subprocess.run(
            [sys.executable, str(Path(scan_repos.__file__)),
             "--root", str(root), "--json"],
            capture_output=True, text=True, encoding="utf-8",
        )
        data = json.loads(out.stdout)
        assert data["projects_judged"] == 0
        assert data["findings"] == []
        assert data["projects_out_of_scope"] == 2


def test_real_duplicates_still_alert() -> None:
    """release 区内的真重复必须照报 —— 降噪不能把真问题也压掉。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        same = {"a.py": "print('dup')\n", "README.md": "# dup\n"}
        # `--deep` 只展开「像项目」的子目录 ⇒ 夹具必须带打包清单
        for name in ("one", "two"):
            d = _mk(root, f"repos/{name}", same)
            (d / "pyproject.toml").write_text(
                f'[project]\nname = "pkg-{name}"\nversion = "1.0.0"\n', encoding="utf-8")
        out = subprocess.run(
            [sys.executable, str(Path(scan_repos.__file__)),
             "--root", str(root), "--json", "--deep"],
            capture_output=True, text=True, encoding="utf-8",
        )
        data = json.loads(out.stdout)
        assert data["findings"], "release 区真重复必须报"
        assert out.returncode == 1


def test_skipped_entries_always_have_reason() -> None:
    """未纳入扫描的条目必须带 reason（不能「没出现」就当「没有」）。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _mk(root, "repos/a", {"a.py": "x=1\n"})
        _mk(root, ".hidden", {"x.py": "x=1\n"})
        (root / "loose.txt").write_text("hi", encoding="utf-8")
        _mk(root, "found.000", {"dir_00000004.chk": "junk"})
        out = subprocess.run(
            [sys.executable, str(Path(scan_repos.__file__)),
             "--root", str(root), "--json"],
            capture_output=True, text=True, encoding="utf-8",
        )
        data = json.loads(out.stdout)
        skipped = data["skipped"]
        assert skipped, "必须列出未纳入项"
        assert all(s.get("reason") for s in skipped), "每条都要有 reason"
        paths = " ".join(s["path"] for s in skipped)
        assert "loose.txt" in paths or any(s["path"] == "loose.txt" for s in skipped)
        assert any(s["path"] == ".hidden" for s in skipped)
        # found.000 被识别为 system 区（不进判定，但要出现在 skipped 里给理由）
        assert any(s["path"] == "found.000" for s in skipped)


def test_scope_declared_in_json() -> None:
    """读数必须带扫描范围与规则指纹，否则两次数字不可比。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _mk(root, "repos/a", {"a.py": "x=1\n"})
        _mk(root, "repos/b", {"b.py": "y=2\n"})
        for extra, want_depth in (([], "shallow"), (["--deep"], "deep")):
            out = subprocess.run(
                [sys.executable, str(Path(scan_repos.__file__)),
                 "--root", str(root), "--json", *extra],
                capture_output=True, text=True, encoding="utf-8",
            )
            data = json.loads(out.stdout)
            assert data["scope"]["depth"] == want_depth
            assert data["scope"]["note"]
            assert data["rules_digest"] == scan_repos.rules_digest()


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                fails += 1
                print(f"FAIL {name}: {exc}")
    print("—" * 40)
    print(f"selfcheck: {'PASS' if not fails else f'{fails} FAILED'}")
    sys.exit(1 if fails else 0)
