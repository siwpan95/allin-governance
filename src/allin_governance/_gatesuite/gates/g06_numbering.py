"""Gate g06 -- numbering is a reference contract, so gaps and drift are defects.

Two shapes are judged, both from the recorded accident class "44 entries, 42
unique numbers, and eight citations pointing at the wrong rule":

* **ordinal version series** -- ``v0.schema.json``, ``v1``, ``v2`` … in one
  directory must form a contiguous run from the smallest member. A gap means a
  reader following the series silently skips a generation.
* **bilingual numbering equality** -- in a paired document the numbered lists of
  both languages must agree item for item: same number of lists, same labels,
  same item counts. This repository's Chinese counterparts are the same contract
  in another language, so a renumbered list on one side breaks every citation
  into the other.

Exit codes follow the governance vocabulary: 0 clean, 1 blocking finding,
2 usage error, 3 environment error.

**Empty scope is a registered decision, not a default.** On 2026-11-25 both
repositories this suite judges (the kernel and the ``allin`` consumer) have
*zero* objects: ``git ls-files`` matches no ``*.zh.md`` and no ``vN.*``, so both
judged surfaces are empty and the gate reports ``skipped 1``, never "pass".
A gate that is empty forever is indistinguishable from a gate that was quietly
disabled, so the emptiness is registered in ``registry.json`` under
``gates[g06-numbering].empty_scope`` -- with a **witness**: the file patterns
that would make "nothing to judge here" a lie. Every run re-derives the witness
from the git index:

* witness hits **and** this gate still judged 0 objects on that side ⇒
  ``scope-claim-expired`` (blocking). The registration is what has to be broken
  for the SKIP to survive, and it cannot: the witness is re-checked from the
  index on every run, so a ``*.zh.md`` appearing tomorrow reds the repository
  until somebody re-judges the claim;
* a registration with no witness ⇒ ``scope-claim-unverifiable`` (blocking):
  a sentence nobody can check is not a registration;
* the witness check does **not** depend on the registration existing: deleting
  ``empty_scope`` from the registry silences the reason, not the criterion.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runtime"))

import govlib  # noqa: E402  (path is fixed up immediately above)
from govlib import EXIT_BLOCKING, EXIT_OK, Baseline, Report  # noqa: E402

GATE = "g06-numbering"
TITLE = "governance: numbering contracts (series continuity, pair equality)"

ORDINAL_FILE = re.compile(r"^v(\d+)(?:\.[A-Za-z0-9]+)*\.(?:json|md|yaml|yml)$")
ORDERED_ITEM = re.compile(r"^(\s*)(\d+)[.)]\s+\S")
UNORDERED_ITEM = re.compile(r"^(\s*)[-*+]\s+\S")
EXEMPT_PREFIXES = (".agents/notes/archived/", "vendor/", "docs/i18n/")

#: ``empty_scope`` 登记在 ``registry.json`` 里的哪一个键下。
SCOPE_KEY = "empty_scope"
#: 登记文件的两种仓内落点（与 ``govlib.baseline_path`` 同一口径），都不在则用随包那份。
REGISTRY_SUBTREES = ("tools/gate/governance/registry.json",
                     "src/allin_governance/_gatesuite/registry.json")
#: 没有登记时的兜底 witness。**判据不依赖"有人登记过"这件事存在** ——
#: 否则删掉登记就能把 SKIP 变回无人过问的默认。
DEFAULT_WITNESS = {"bilingual_pair": ("*.zh.md",), "ordinal_series": ("v[0-9]*",)}
#: 两条判定面在登记里的名字（witness 必须按面分写：两面的"命中意味着什么"不同）。
HALVES = ("bilingual_pair", "ordinal_series")
#: 登记里的 reason 至少这么长 —— 与「一句话不算登记」同一口径。
MIN_SCOPE_REASON = 20


def registry_path(root: Path) -> Path:
    """登记文件的落点：被判的仓优先，其次随包的那份。"""
    for relative in REGISTRY_SUBTREES:
        candidate = root / relative
        if candidate.is_file():
            return candidate
    return govlib.GOVERNANCE_DIR / "registry.json"


def declared_empty_scope(root: Path) -> tuple[dict | None, str]:
    """本门禁的 ``empty_scope`` 登记与它的来源（没登记时来源说明为什么没有）。"""
    path = registry_path(root)
    text = govlib.read_text(path)
    if not text:
        return None, f"{path} 不存在或不可读"
    try:
        registry = json.loads(text)
    except ValueError:
        return None, f"{path} 不是合法 JSON"
    entries = registry.get("gates") if isinstance(registry, dict) else None
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("id") == GATE:
            scope = entry.get(SCOPE_KEY)
            return (scope if isinstance(scope, dict) else None), str(path)
    return None, f"{path} 里没有 {GATE} 的 {SCOPE_KEY} 登记"


def witness_hits(tracked: list[str], patterns: tuple[str, ...]) -> list[str]:
    """在**图索引**里找 witness 命中的文件（文件名与整条路径都比一次）。"""
    hits: list[str] = []
    for path in tracked:
        name = path.rsplit("/", 1)[-1]
        if any(fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(path, pattern)
               for pattern in patterns):
            hits.append(path)
    return hits


def judge_empty_scope(report: Report, tracked: list[str], scope: dict | None, source: str, *,
                      pairs: int, series: int) -> None:
    """把「本仓无适用对象」从一句话变成**能被打破的判据**。

    ``pairs`` / ``series`` 是两条判定面这次真正判了几个对象。witness 命中而该面
    仍然 0 个对象 ⇒ 「无适用对象」已经是假话，判红并指名是哪些文件让它变假的。
    """
    actual = {"bilingual_pair": pairs, "ordinal_series": series}
    if scope is None:
        report.note(f"empty_scope 未登记（{source}）：SKIP 眼下是默认行为，还没成为"
                    "「被登记过的决定」；判据仍按内置 witness 生效。")
        witness = dict(DEFAULT_WITNESS)
    else:
        reason = str(scope.get("reason", "")).strip()
        if len(reason) < MIN_SCOPE_REASON:
            report.block("scope-claim-unverifiable", source, 0,
                         f"{GATE} 的 {SCOPE_KEY}.reason 缺失或过短（少于 {MIN_SCOPE_REASON} "
                         "字符）—— 登记要说得清「为什么这里没有对象」")
        raw = scope.get("witness")
        witness = {}
        if isinstance(raw, dict):
            witness = {name: tuple(value) for name, value in raw.items()
                       if name in HALVES and isinstance(value, list) and value
                       and all(isinstance(item, str) and item.strip() for item in value)}
        if not witness:
            report.block("scope-claim-unverifiable", source, 0,
                         f"{GATE} 的 {SCOPE_KEY} 没有可用 witness（应按面给出 "
                         f"{' / '.join(HALVES)} 的文件模式）：一句没法被门禁自动核的声明"
                         "不算登记 —— 它只会让「永远 SKIP」变成没人能打破的默认")
            return
    for half in HALVES:
        patterns = witness.get(half)
        if not patterns:
            report.note(f"empty_scope 未声明「{half}」这一面的 witness：该面的空判只有默认证，"
                        "没有登记担保")
            patterns = DEFAULT_WITNESS[half]
        hits = witness_hits(tracked, patterns)
        if not hits:
            continue
        if actual[half] == 0:
            report.block("scope-claim-expired", source, 0,
                         f"{SCOPE_KEY} 对「{half}」已失效：witness {list(patterns)} 在图索引里"
                         f"命中 {len(hits)} 个文件（{', '.join(hits[:3])}"
                         f"{' 等' if len(hits) > 3 else ''}），而 {GATE} 这一面仍然 0 个对象 "
                         "⇒「本仓无适用对象」这句话已不成立。补齐对象让门禁真正判定，"
                         "或删掉该登记并重判 —— 不许让它继续 SKIP")
        else:
            report.note(f"empty_scope 对「{half}」已自动失效（witness 命中 {len(hits)} 个文件，"
                        "该面已判到对象）：请从 registry.json 删掉这条登记")



def ordinal_series(root: Path, tracked: list[str]) -> dict[tuple[str, str], list[tuple[int, str]]]:
    """Group tracked ordinal files by directory and suffix shape."""
    series: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for path in tracked:
        if path.startswith(EXEMPT_PREFIXES):
            continue
        name = path.rsplit("/", 1)[-1]
        match = ORDINAL_FILE.match(name)
        if match is None:
            continue
        directory = path.rsplit("/", 1)[0] if "/" in path else ""
        shape = name.replace(match.group(1), "N", 1)
        series.setdefault((directory, shape), []).append((int(match.group(1)), path))
    return series


def list_shapes(text: str) -> list[tuple[str, int, tuple[int, ...]]]:
    """Ordered and unordered list shapes, in document order.

    Each entry is ``(kind, item count, ordered labels)``; continuation lines and
    fenced code blocks are ignored so a code sample never counts as prose.
    """
    shapes: list[tuple[str, int, tuple[int, ...]]] = []
    current: tuple[str, list[int], int] | None = None
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        ordered = ORDERED_ITEM.match(line)
        unordered = UNORDERED_ITEM.match(line) if ordered is None else None
        if ordered is not None:
            label = int(ordered.group(2))
            if current is None or current[0] != "ordered":
                if current is not None:
                    shapes.append((current[0], current[2], tuple(current[1])))
                current = ("ordered", [label], 1)
            else:
                current[1].append(label)
                current = (current[0], current[1], current[2] + 1)
        elif unordered is not None:
            if current is None or current[0] != "unordered":
                if current is not None:
                    shapes.append((current[0], current[2], tuple(current[1])))
                current = ("unordered", [], 1)
            else:
                current = (current[0], current[1], current[2] + 1)
        elif line.strip() == "":
            if current is not None:
                shapes.append((current[0], current[2], tuple(current[1])))
                current = None
    if current is not None:
        shapes.append((current[0], current[2], tuple(current[1])))
    return shapes


def scan(root: Path) -> Report:
    """Judge ordinal series continuity and bilingual list equality."""
    report = Report(title=TITLE)
    tracked = govlib.tracked_files(root)
    series_members = 0
    for (directory, shape), members in sorted(ordinal_series(root, tracked).items()):
        if len(members) < 2:
            continue
        numbers = sorted(number for number, _path in members)
        expected = list(range(numbers[0], numbers[0] + len(numbers)))
        series_members += len(members)
        report.scanned += len(members)
        if numbers != expected:
            missing = sorted(set(expected) - set(numbers))
            report.block("series-gap", f"{directory}/{shape}", 0,
                         f"ordinal series skips {missing} (found {numbers})")
        else:
            report.exempt("contiguous ordinal series", len(members))

    pairs = 0
    for path in tracked:
        if not path.endswith(".md") or path.endswith(".zh.md") or path.startswith(EXEMPT_PREFIXES):
            continue
        counterpart = f"{path[:-3]}.zh.md"
        if counterpart not in tracked:
            continue
        pairs += 1
        report.scanned += 1
        source = govlib.read_text(root / path) or ""
        translated = govlib.read_text(root / counterpart) or ""
        left = list_shapes(source)
        right = list_shapes(translated)
        if len(left) != len(right):
            report.block("pair-list-count", counterpart, 0,
                         f"{len(left)} list(s) in the English source vs {len(right)} in the counterpart")
            continue
        for index, (a, b) in enumerate(zip(left, right, strict=True), start=1):
            if a[0] != b[0] or a[1] != b[1] or a[2] != b[2]:
                report.block("pair-list-shape", counterpart, 0,
                             f"list {index} differs: source {a[0]}/{a[1]} item(s)/{a[2]} vs counterpart "
                             f"{b[0]}/{b[1]} item(s)/{b[2]}")
    if pairs == 0:
        report.skip("no tracked bilingual pair in scope", 1)
    # 「这里没有对象」必须是被登记过、且**每次都能被重新证伪**的决定（见模块 docstring）。
    scope, source = declared_empty_scope(root)
    judge_empty_scope(report, tracked, scope, source, pairs=pairs, series=series_members)
    return report


#: 自检夹具用的登记：与真登记同形（真登记在 ``registry.json`` 里，见仓根）。
#: 夹具**必须自带**登记文件：否则自检会去读宿主仓的真登记，测的就成了宿主。
SCOPE_REGISTRY = {"gates": [{"id": GATE, SCOPE_KEY: {
    "since": "2026-11-25",
    "reason": "自检夹具：本仓两条判定面都没有对象，SKIP 有据",
    "witness": {"bilingual_pair": ["*.zh.md"], "ordinal_series": ["v[0-9]*"]}}}]}


def write_scope(sandbox: govlib.Sandbox, registry: dict = SCOPE_REGISTRY) -> None:
    """把登记放进被夹具的仓里（``tools/gate/governance/`` 是消费仓布局）。"""
    sandbox.write("tools/gate/governance/registry.json",
                  json.dumps(registry, ensure_ascii=False) + "\n")


def selfcheck() -> int:
    """Prove a series gap and a drifted pair are caught, and clean input is not."""
    check = govlib.SelfCheck(gate=GATE)
    with govlib.Sandbox() as sandbox:
        write_scope(sandbox)
        sandbox.write("docs/formats/v0.schema.json", "{}\n")
        sandbox.write("docs/formats/v1.schema.json", "{}\n")
        sandbox.write("docs/formats/v2.schema.json", "{}\n")
        sandbox.write("guide/index.md", "# Guide\n\n1. one\n2. two\n\n- a\n- b\n")
        sandbox.write("guide/index.zh.md", "# 指南\n\n1. 一\n2. 二\n\n- 甲\n- 乙\n")
        report = scan(sandbox.path)
        check.quiet("a contiguous series and an equal pair", bool(report.findings))

    with govlib.Sandbox() as sandbox:
        write_scope(sandbox)
        sandbox.write("docs/formats/v0.schema.json", "{}\n")
        sandbox.write("docs/formats/v2.schema.json", "{}\n")
        report = scan(sandbox.path)
        check.detects("a gap in an ordinal series",
                      any(finding.kind == "series-gap" for finding in report.findings))

    with govlib.Sandbox() as sandbox:
        write_scope(sandbox)
        sandbox.write("guide/index.md", "# Guide\n\n1. one\n2. two\n3. three\n")
        sandbox.write("guide/index.zh.md", "# 指南\n\n1. 一\n2. 二\n")
        report = scan(sandbox.path)
        check.detects("a renumbered list in the counterpart",
                      any(finding.kind in ("pair-list-count", "pair-list-shape") for finding in report.findings))

    with govlib.Sandbox() as sandbox:
        write_scope(sandbox)
        sandbox.write("guide/index.md", "# Guide\n\n```\n1. code line\n2. code line\n```\n\n1. real\n")
        sandbox.write("guide/index.zh.md", "# 指南\n\n```\n1. 代码行\n```\n\n1. 真的\n")
        report = scan(sandbox.path)
        check.quiet("fenced code is not prose numbering", bool(report.findings))

    # 「本仓无适用对象」这句登记**必须能被打破**，否则它就只是让 SKIP 永久化的措辞。
    # 三条负对照：① 登记还在、但 *.zh.md 已经出现 ⇒ 登记失效必须判红；
    #             ② 登记还在、witness 也确实没命中 ⇒ 安静（不能一律判红）；
    #             ③ 登记没有 witness ⇒ 不可核，判红（一句话不算登记）。
    with govlib.Sandbox() as sandbox:
        write_scope(sandbox)
        sandbox.write("guide/index.zh.md", "# 指南\n\n1. 一\n2. 二\n")
        report = scan(sandbox.path)
        check.detects("a declared-empty scope a .zh.md has already voided",
                      any(finding.kind == "scope-claim-expired" for finding in report.findings))

    with govlib.Sandbox() as sandbox:
        write_scope(sandbox)
        sandbox.write("docs/index.md", "# 索引\n\n1. 一\n")
        report = scan(sandbox.path)
        check.quiet("a declared-empty scope that still holds", bool(report.findings))

    with govlib.Sandbox() as sandbox:
        write_scope(sandbox, {"gates": [{"id": GATE, SCOPE_KEY: {
            "since": "2026-11-25",
            "reason": "自检夹具：没有 witness 的登记",
            "witness": {}}}]})
        sandbox.write("docs/index.md", "# 索引\n")
        report = scan(sandbox.path)
        check.detects("an unverifiable empty-scope claim",
                      any(finding.kind == "scope-claim-unverifiable" for finding in report.findings))

    # 登记被删掉也不能让判据消失：没有登记时按内置 witness 判（删登记 ≠ 消音）。
    with govlib.Sandbox() as sandbox:
        write_scope(sandbox, {"gates": [{"id": GATE}]})
        sandbox.write("guide/index.zh.md", "# 指南\n\n1. 一\n")
        report = scan(sandbox.path)
        check.detects("a deleted registration does not restore silence",
                      any(finding.kind == "scope-claim-expired" for finding in report.findings))
    return check.finish()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=TITLE)
    parser.add_argument("--root", default=None, help="work tree to judge (default: discovered from this file)")
    parser.add_argument("--quiet", action="store_true", help="print the summary line only")
    parser.add_argument("--selfcheck", action="store_true", help="inject fixtures and prove the gate works")
    govlib.add_baseline_argument(parser)
    args = parser.parse_args(argv)
    if args.selfcheck:
        return selfcheck()

    root = Path(args.root).resolve() if args.root else govlib.repo_root()
    baseline_path = Path(args.baseline) if args.baseline else govlib.GOVERNANCE_DIR / "baseline" / f"{GATE}.txt"
    report = scan(root)
    baseline = Baseline(baseline_path)
    if args.write_baseline:
        baseline.write(report.findings)
        print(f"{TITLE}: baseline written with {len(report.findings)} finding(s) -> {baseline_path}")
        return EXIT_OK
    if not args.no_baseline:
        baseline.load()
    new_findings = report.findings if args.no_baseline else baseline.filter_new(report.findings)
    report.findings = report.findings if args.list_all else new_findings
    print(report.render().splitlines()[0] if args.quiet else report.render())
    return report.exit_code(new_findings)


if __name__ == "__main__":
    raise SystemExit(govlib.run_gate(main))
