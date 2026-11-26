#!/usr/bin/env python3
"""实测 registry 里的 `scope` 声明是不是真的 —— 「声明了却没扫」必须能红。

## 为什么需要这个脚本

`registry.json` 的 `scope` 是「这道门禁判哪些文件的」的**唯一声明处**。它一旦漂了，
就变成第二个真相源：文档说扫 `**/*.py`、门禁实际扫 `**/*`，两边都自洽，没人会发现。
g00-meta 只能校验形状（反斜杠、空数组、裸通配当默认值），语义只能靠实测：

    加一个**在域内**的文件 ⇒ 该门禁的 `scanned` 必须动
    加一个**在排除项里**的文件 ⇒ `scanned` 必须不动

这两条就是「glob 命中集合 == 实际扫过集合」的可执行形式。

## 边界（必须写在脚本里，不然下一个人会误用）

* **只在一次性副本上跑**：`git add` 由本脚本在临时目录里做，源仓一个字节都不动。
  这也是它不做成 CI 门禁的原因 —— 它要往工作树里写文件、要按 20 道门禁的判据造探针，
  放进 CI 会让「门禁红了」和「探针造得不对」长得一样。
* **删探针用于字面路径**：`scope` 写成具体文件（g05 的 LICENSE、g16 的载体）时，
  「加同路径文件」是不可能的（已经存在），改为**删掉它看 scanned 掉不掉** ——
  同一个命题的反面：在域内的东西消失了，判定必须看得见。
* 探针内容一律最干净（不含 findings 的形状），只测「在不在域内」，不测判据对不对。

用法：
    python scripts/verify_gate_scope.py                 # 跑真 registry
    python scripts/verify_gate_scope.py --self-test     # 负对照：把声明改成谎话，必须红
    python scripts/verify_gate_scope.py --gate g09-python-hygiene --keep
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 探针内容按扩展名给最干净的形状：能被解析、不产生 findings、非空。
PROBE_CONTENT = {
    ".py": "x = 1\n",
    ".md": "# scope probe\n",
    ".yml": "a: 1\n",
    ".yaml": "a: 1\n",
    ".txt": "scope probe\n",
    ".sh": "true\n",
}
FALLBACK_CONTENT = "scope probe\n"

SCANNED_QUIET = re.compile(r"scanned (\d+) file\(s\)")
SCANNED_RATCHET = re.compile(r"scanned=(\d+)")


def scanned_of(output: str) -> int | None:
    for pattern in (SCANNED_QUIET, SCANNED_RATCHET):
        match = pattern.search(output)
        if match:
            return int(match.group(1))
    return None


def materialise(glob: str) -> str:
    """把一个 scope glob 变成一个具体探针路径（`**`/`*`/`?` 都换成可读的占位）。

    只求「这个路径一定命中该 glob」，不求唯一：探针只需要落在域内。
    **字面路径（无通配）必须原样返回** —— 给它补扩展名会造出一个 ``LICENSE.txt``，
    那既不在域内、又让探针「测不到」而非「测出问题」，是很坏的假红。
    """
    has_wildcard = any(token in glob for token in ("*", "?"))
    path = glob.replace("**/", "zz_scope_probe/")
    path = path.replace("**", "zz_scope_probe")
    path = re.sub(r"\*", "zz_scope_probe", path)
    path = re.sub(r"\?", "z", path)
    if path.endswith("/"):
        path += "zz_scope_probe"
        has_wildcard = True
    # 通配出来的路径可能没有扩展名（`**/*`）：补一个 .txt，让它读得动，
    # 且不触发「未知扩展名」这类与本次命题无关的判定。
    if has_wildcard and not Path(path).suffix:
        path += ".txt"
    return path


def content_for(path: str) -> str:
    """探针内容：能用、非空、不产生 findings。

    一个例外要按门禁的判据造：g16 把 preset 当 ``GOVERNANCE:BEGIN/END`` **段**读，
    裸文本会被判 ``carrier-structure`` 而根本不进 scanned —— 那时探针测的是
    「我造得对不对」而不是「门禁看不看这个文件」。
    """
    if path.endswith((".yml", ".yaml")) and path.startswith("preset/"):
        return "<!-- GOVERNANCE:BEGIN -->\nscope probe\n<!-- GOVERNANCE:END -->\n"
    return PROBE_CONTENT.get(Path(path).suffix.lower(), FALLBACK_CONTENT)


class CopiedRepo:
    """一次性的仓副本 —— 所有 `git add` 都发生在它里面。"""

    def __init__(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="gov-scope-")
        self.root = Path(self._temp.name) / "repo"

    def __enter__(self) -> Path:
        shutil.copytree(REPO_ROOT, self.root,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
        return self.root

    def __exit__(self, *_exc) -> None:
        self._temp.cleanup()


def resolve_script(root: Path, declared: str) -> Path:
    """把 registry 的 `script`（仓内布局 `governance/gates/x.py`）解析成盘上路径。

    同一份 registry 要服务两种布局（与 g00-meta 的 `resolve_gate_script` 同一口径）：
    仓内形态是 `<root>/tools/gate/governance/gates/x.py`，包内形态没有那层
    `governance/`（`gates/` 与 registry.json 同级）。两种都试，谁在盘上用谁。
    """
    candidates = [
        root / declared,
        root / "tools/gate" / declared,
        root / "src/allin_governance/_gatesuite" / declared[len("governance/"):],
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SystemExit(f"找不到门禁脚本 {declared}；试过：{', '.join(str(c) for c in candidates)}")


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def run_gate(root: Path, script: str) -> tuple[int, str]:
    completed = subprocess.run(
        [sys.executable, str(resolve_script(root, script)), "--quiet", "--root", str(root)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def baseline_scanned(root: Path, script: str) -> int | None:
    _code, output = run_gate(root, script)
    return scanned_of(output)


def probe_create(root: Path, script: str, rel: str) -> tuple[int | None, str]:
    """加一个文件，返回 scanned 的增量。"""
    before = baseline_scanned(root, script)
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content_for(rel), encoding="utf-8", newline="\n")
    git(root, "add", "-f", "--", rel)
    after = baseline_scanned(root, script)
    git(root, "rm", "-q", "--cached", "--", rel)
    target.unlink()
    for folder in sorted({target.parent, *target.parent.parents}, key=lambda p: -len(str(p))):
        if folder == root:
            break
        try:
            folder.rmdir()
        except OSError:
            break
    if before is None or after is None:
        return None, f"读不到 scanned（before={before} after={after}）"
    return after - before, ""


def probe_delete(root: Path, script: str, rel: str) -> tuple[int | None, str]:
    """删掉一个已存在的域内文件，返回 scanned 的增量（应为负）。"""
    before = baseline_scanned(root, script)
    target = root / rel
    stash = target.read_bytes()
    git(root, "rm", "-q", "--cached", "--", rel)
    target.unlink()
    after = baseline_scanned(root, script)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(stash)
    git(root, "add", "-f", "--", rel)
    if before is None or after is None:
        return None, f"读不到 scanned（before={before} after={after}）"
    return after - before, ""


def tracked(root: Path, rel: str) -> bool:
    return bool(git(root, "ls-files", "--error-unmatch", "--", rel).returncode == 0)


def verify(root: Path, registry: dict, only: str | None = None) -> tuple[list[str], list[str]]:
    """返回 (失败清单, 未实测清单)。失败 = 声明被实测推翻；未实测 = 本仓环境跑不了。

    两者**必须分开**，且未实测绝不算通过：本仓没有 `docs/`，g15 会 exit 3
    —— 把它记成「绿」就是「SKIP 当 PASS」，正是本仓反复吃过亏的那件事。
    """
    failures: list[str] = []
    unverified: list[str] = []
    for entry in registry.get("gates", []):
        identifier = str(entry.get("id"))
        scope = entry.get("scope")
        if not scope or (only and identifier != only):
            continue
        script = str(entry["script"])
        probe_code, probe_output = run_gate(root, script)
        if scanned_of(probe_output) is None:
            unverified.append(f"{identifier}: 跑不起来（exit {probe_code}）："
                              f"{probe_output.strip().splitlines()[-1][:90] if probe_output.strip() else '无输出'}")
            print(f"  ??  {identifier:24s} UNVERIFIED —— 本仓跑不了，声明只能算代码确证")
            continue
        for glob in scope:
            rel = materialise(glob)
            if tracked(root, rel):
                delta, error = probe_delete(root, script, rel)
                expectation = "domain-file-deleted"
            else:
                # 字面路径指向的文件在盘上（只是没进 index）时，同样用删除探针
                if (root / rel).is_file() and not tracked(root, rel):
                    git(root, "add", "-f", "--", rel)
                    delta, error = probe_delete(root, script, rel)
                else:
                    delta, error = probe_create(root, script, rel)
                expectation = "domain-file-added"
            if delta is None:
                failures.append(f"{identifier}: scope `{glob}` 无法实测 —— {error}")
                continue
            if expectation == "domain-file-added" and delta <= 0:
                failures.append(
                    f"{identifier}: 声明 scope `{glob}`，但加 `{rel}` 后 scanned 增量 {delta}"
                    " —— 声明了却没扫（scope 已漂成第二个真相源）")
            elif expectation == "domain-file-deleted" and delta >= 0:
                failures.append(
                    f"{identifier}: 声明 scope `{glob}`，但删掉 `{rel}` 后 scanned 增量 {delta}"
                    " —— 域内文件消失而判定看不见")
            else:
                print(f"  ok  {identifier:24s} in-scope  {glob:42s} Δscanned={delta:+d}  ({rel})")
        for glob in entry.get("scope_exclude") or []:
            rel = materialise(glob)
            delta, error = probe_create(root, script, rel)
            if delta is None:
                failures.append(f"{identifier}: scope_exclude `{glob}` 无法实测 —— {error}")
                continue
            if delta != 0:
                failures.append(
                    f"{identifier}: 声明 scope_exclude `{glob}`，但加 `{rel}` 后 scanned 增量 "
                    f"{delta:+d} —— 排除项是假的，域比声明的大")
            else:
                print(f"  ok  {identifier:24s} excluded  {glob:42s} Δscanned={delta:+0d}  ({rel})")
    return failures, unverified


def lying_registry(registry: dict, gate: str, scope: list[str],
                   exclude: list[str] | None = None) -> dict:
    """把某道门禁的 scope 改成一句谎话，用来证明本脚本会红。"""
    fake = json.loads(json.dumps(registry))
    for entry in fake["gates"]:
        if str(entry.get("id")) == gate:
            entry["scope"] = scope
            if exclude is None:
                entry.pop("scope_exclude", None)
            else:
                entry["scope_exclude"] = exclude
    return fake


#: 四组负对照。每组都是「声明看起来合理、实际是谎话」，必须每组都红。
#: 四种说谎方式分别对应四种真实漂移：**声明的类型不是门禁判的类型**（g09 说判 md）、
#: **声明的目录被豁免掉了**（g01 说判 vendor）、**声明的扩展名门禁根本不认**（g19 说判 txt）、
#: **声明的目录门禁只认其中一种扩展名**（g17 说判 py）。
LIARS = [
    ("g09-python-hygiene", ["**/*.md"], None, "声明的类型不是它判的类型"),
    ("g01-paths", ["vendor/**/*.py"], None, "声明的目录其实被豁免了"),
    ("g19-docs-refs", ["**/*.txt"], None, "声明的扩展名门禁根本不认"),
    ("g17-registry", ["platform/registry/**/*.py"], None, "声明的目录里它只认 yaml"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gate", default=None, help="只验一道门禁（id）")
    parser.add_argument("--self-test", action="store_true",
                        help="负对照：把 scope 改成谎话，证明本脚本会红")
    parser.add_argument("--keep", action="store_true", help="保留临时副本（排查用）")
    args = parser.parse_args()

    with CopiedRepo() as root:
        registry = json.loads((root / "src/allin_governance/_gatesuite/registry.json")
                              .read_text(encoding="utf-8"))
        declared = [entry["id"] for entry in registry["gates"] if entry.get("scope")]
        print(f"scope 已声明 {len(declared)} 道：{', '.join(declared)}")
        print(f"（副本 {root}）\n")

        failures, unverified = verify(root, registry, args.gate)
        print()
        if failures:
            print(f"FAIL {len(failures)} 条：")
            for line in failures:
                print(f"  RED {line}")
        else:
            print("PASS 每条能跑的 scope 声明都实测成立（域内加法动计数、排除项不动计数）")
        if unverified:
            print(f"UNVERIFIED {len(unverified)} 道（未实测 ≠ 通过）：")
            for line in unverified:
                print(f"  ?? {line}")

        if args.self_test:
            print("\n=== 负对照：故意说谎的 scope 必须被判红 ===")
            self_test_failed = 0
            for gate, scope, exclude, why in LIARS:
                fake = lying_registry(registry, gate, scope, exclude)
                detected, _ = verify(root, fake, gate)
                status = "RED(正确)" if detected else "GREEN(漏判!)"
                print(f"  {status:12s} {gate:24s} 谎话={scope} 理由={why}")
                if not detected:
                    self_test_failed += 1
            if self_test_failed:
                print(f"\n负对照失败：{self_test_failed} 组谎话没被判红 —— 本脚本的探针不可信")
                return 1
            print(f"\n负对照全部判红（{len(LIARS)}/{len(LIARS)}）：探针口径可信")
        return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
