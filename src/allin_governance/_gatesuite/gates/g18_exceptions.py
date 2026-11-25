"""Gate g18 -- accepted risk is registered, reasoned, and expires.

Every frozen baseline key, every hygiene exemption and every declared registry
gap is risk somebody accepted. Unregistered accepted risk has three properties,
all bad: nobody remembers why it was accepted, nobody knows who owns it, and it
never comes up for review again -- it simply becomes permanent.

The governance literature is blunt about the fix: an exception is a record with
an id, the control it disables, a business reason, a compensating control, an
approver, an **expiry date** and a status, reviewed on a cadence. This gate is
that record turned into an exit code:

* ``governance/exceptions.yml`` parses and every entry is complete --
  ``id / kind / gate / target / reason / compensating / approver / expires /
  status`` -- so an exception can never be a one-line shrug;
* ids are unique and ``status`` comes from the closed set ``open | closed``;
* ``kind`` comes from the closed set, so the exception ledger stays sortable;
* **an open exception past its expiry blocks.** Accepted risk that outlives its
  review date is not accepted any more, it is just forgotten;
* coverage is reported, never silently assumed: how many frozen keys and
  exemption rows exist versus how many have a registered exception.

Coverage is a note and not a block on purpose: turning it into a block on day
one would mean writing fifty-odd records in one sitting, and a gate that reds
the repository on its first run is a gate that gets switched off.

Exit codes follow the governance vocabulary: 0 clean, 1 blocking finding,
2 usage error, 3 environment error.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runtime"))

import govlib  # noqa: E402  (path is fixed up immediately above)
from govlib import EXIT_BLOCKING, EXIT_OK, EXIT_ENV, Report  # noqa: E402

GATE = "g18-exceptions"
TITLE = "governance: accepted risk is registered, reasoned, and expires"

EXCEPTIONS_REL = "tools/gate/governance/exceptions.yml"
BASELINE_DIR = "tools/gate/governance/baseline"
# 豁免台账与 BASELINE_DIR / GAPS_REL 同类（B 类：被核准的证据），2026-11-24 三层分域
# 从 tools/gate/audit/ 迁到 governance/ledger/ —— 那里才是「基线/例外」的家。
EXEMPTIONS_REL = "tools/gate/governance/ledger/repo_hygiene_exemptions.csv"
GAPS_REL = "tools/gate/governance/registry-known-gaps.txt"

#: 上两条路径里属于「治理子树」的那一层。提仓后台账**随包走**，包内形态没有这一层
#: （``exceptions.yml`` / ``baseline/`` / ``ledger/`` 直接躺在 ``_gatesuite/`` 下），
#: 与 ``gates/`` 的包内布局同一口径。剥掉它即得包内相对路径。
REPO_SUBTREE = "tools/gate/governance/"

#: 包内那份台账的**显示标签**。为什么不是绝对路径：报告要能回答「用的是哪一份」，
#: 但本机绝对路径一旦进了报告，换台机器就会变成一条假的「漂移」。
PACKAGE_LABEL = "allin_governance/_gatesuite/"

REQUIRED_FIELDS = ("id", "kind", "gate", "target", "reason", "compensating", "approver", "expires", "status")
ALLOWED_KINDS = ("baseline-freeze", "hygiene-exemption", "known-gap")
ALLOWED_STATUS = ("open", "closed")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def locate(root: Path, relative: str, package_dir: Path | None = None) -> tuple[Path, str] | None:
    """定位一份台账资源：**仓内优先，包内兜底**，返回 ``(路径, 显示标签)``。

    为什么要兜底（2026-11-25，提仓后第一现场）：台账随源码迁进了包里，
    消费仓里那条 ``tools/gate/governance/exceptions.yml`` 已经不存在，
    于是本门禁在**它自己的仓**里报 ``ledger-unusable: exceptions.yml 不可读`` ——
    判据没错，错在把「台账只可能在被判的仓里」当成了事实。

    顺序不能反：仓内有就用仓内那份（消费方可以覆盖厂商默认值），
    仓内没有才用包内那份。标签只写相对位置，不写本机绝对路径。
    """
    in_repo = root / relative
    if in_repo.exists():
        return in_repo, relative
    if relative.startswith(REPO_SUBTREE):
        bundled = (package_dir or govlib.GOVERNANCE_DIR) / relative[len(REPO_SUBTREE):]
        if bundled.exists():
            return bundled, PACKAGE_LABEL + relative[len(REPO_SUBTREE):]
    return None


def load_exceptions(path: Path, label: str) -> tuple[list[dict] | None, str]:
    """Parse the exception ledger. Returns (entries, error)."""
    try:
        import yaml  # type: ignore
    except ImportError:
        return None, "PyYAML 不可用（无法解析 exceptions.yml）"
    try:
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None, f"{label} 不可读"
    try:
        payload = yaml.safe_load(raw)
    except Exception as error:  # noqa: BLE001  (yaml raises many types)
        return None, f"{label} 解析失败：{error}"
    if not isinstance(payload, dict):
        return None, f"{label} 顶层必须是对象"
    entries = payload.get("exceptions")
    if entries is None:
        return [], ""
    if not isinstance(entries, list):
        return None, "'exceptions' 必须是列表"
    return [entry for entry in entries if isinstance(entry, dict)], ""


def count_frozen(root: Path, package_dir: Path | None = None) -> tuple[dict[str, int], list[str]]:
    """Frozen baseline keys per gate, exemption rows, and declared gaps.

    返回 ``(计数, 来源说明)``：来源说明只在**用到包内那份**时才非空，
    用来把「本仓一条都没有」与「读到的是包自带的那份」区分开 ——
    否则燃尽口径会安静地显示 0，看上去像「没有存量」，实际是「没找到台账」。
    """
    counts: dict[str, int] = {}
    origins: list[str] = []
    baseline_entry = locate(root, BASELINE_DIR, package_dir)
    if baseline_entry is not None:
        baseline, label = baseline_entry
        if label != BASELINE_DIR:
            origins.append(f"baseline 来自包内 {label}（仓内 {BASELINE_DIR} 不存在）")
        if baseline.is_dir():
            for path in sorted(baseline.glob("*.txt")):
                try:
                    keys = [ln.strip() for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
                            if ln.strip() and not ln.strip().startswith("#")]
                except OSError:
                    continue
                if keys:
                    counts[path.stem] = len(keys)
    for rel, label in ((EXEMPTIONS_REL, "repo_hygiene_exemptions.csv"), (GAPS_REL, "registry-known-gaps.txt")):
        found = locate(root, rel, package_dir)
        if found is None:
            continue
        path, source = found
        if source != rel:
            origins.append(f"{label} 来自包内 {source}（仓内 {rel} 不存在）")
        try:
            lines = [ln.strip() for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
                     if ln.strip() and not ln.strip().startswith("#")]
        except OSError:
            continue
        if not lines:
            continue
        # 剥表头：豁免表首行是列名（check/rule 起头），不是数据行。
        # 旧判断写死 startswith("rule")，但本仓 CSV 表头是 "check,repo,..."，
        # 于是表头被当成一条豁免计入「燃尽口径」—— 计数整体 +1（off-by-one）。
        if label.endswith(".csv") and lines[0].lower().startswith(("rule", "check")):
            lines = lines[1:]
        if lines:
            counts[label] = len(lines)
    return counts, origins


def run(root: str, args: argparse.Namespace, package_dir: Path | None = None) -> Report:
    report = Report(title=TITLE)
    base = Path(root)
    located = locate(base, EXCEPTIONS_REL, package_dir)
    if located is None:
        # 两处都找不到才是真缺陷。措辞要点明「找过哪两处」，
        # 否则下一个人只会在仓内那一条路径上反复打转。
        report.block("ledger-unusable", EXCEPTIONS_REL, 0,
                     f"exceptions.yml 不可读：仓内 {EXCEPTIONS_REL} 与包内 "
                     f"{PACKAGE_LABEL}{EXCEPTIONS_REL[len(REPO_SUBTREE):]} 均不存在")
        return report
    ledger_path, ledger_label = located
    if ledger_label != EXCEPTIONS_REL:
        report.note(f"例外台账来源：包内 {ledger_label}（仓内 {EXCEPTIONS_REL} 不存在）")
    entries, error = load_exceptions(ledger_path, ledger_label)
    if error:
        report.block("ledger-unusable", ledger_label, 0, error)
        return report
    assert entries is not None
    if not entries:
        report.skip("ledger 为空（还没有登记任何例外）")
        return report

    today = dt.date.today()
    seen: dict[str, int] = {}
    open_count = 0
    for index, entry in enumerate(entries, start=1):
        report.scanned += 1
        missing = [field for field in REQUIRED_FIELDS if not str(entry.get(field, "")).strip()]
        if missing:
            report.block("incomplete-exception", ledger_label, index,
                         f"第 {index} 条缺字段：{', '.join(missing)}")
            continue
        identifier = str(entry["id"]).strip()
        if identifier in seen:
            report.block("duplicate-exception", ledger_label, index,
                         f"id '{identifier}' 重复（首次出现在第 {seen[identifier]} 条）")
        else:
            seen[identifier] = index
            report.exempt("id-unique")
        kind = str(entry["kind"]).strip()
        if kind in ALLOWED_KINDS:
            report.exempt("kind-known")
        else:
            report.block("kind-unknown", ledger_label, index,
                         f"kind '{kind}' 越界，必须是 {', '.join(ALLOWED_KINDS)} 之一")
        status = str(entry["status"]).strip()
        if status in ALLOWED_STATUS:
            report.exempt("status-known")
        else:
            report.block("status-unknown", ledger_label, index,
                         f"status '{status}' 越界，必须是 {', '.join(ALLOWED_STATUS)} 之一")
        expires = str(entry["expires"]).strip()
        if not DATE.match(expires):
            report.block("bad-expiry", ledger_label, index,
                         f"expires '{expires}' 不是 YYYY-MM-DD")
            continue
        try:
            when = dt.date.fromisoformat(expires)
        except ValueError:
            report.block("bad-expiry", ledger_label, index, f"expires '{expires}' 不是合法日期")
            continue
        if status == "closed":
            report.exempt("closed-exception")
            continue
        open_count += 1
        if when < today:
            report.block("expired-exception", ledger_label, index,
                         f"{identifier}（{kind} / {entry['gate']}）已于 {expires} 到期，"
                         f"必须复核后关闭或续期 —— 过期即视为未复核的接受风险")
        else:
            report.exempt(f"open-until-{expires}")
            # liveness 判据：open 且过期日过远（>18 个月）≈ 永久豁免，
            # 失去「到期复核」的意义，记一条 note 逼出复核，不阻断
            # （g18 不在首次运行就红；本仓既有条目 expire 均在 ~3 个月内，不会触发）。
            if (when - today).days > 548:
                report.note(
                    f"liveness: {identifier} 过期日 {expires} 过远（>18 个月），"
                    f"建议复核确认该接受风险仍有效，避免沦为永久豁免")

    frozen, origins = count_frozen(base, package_dir)
    for origin in origins:
        report.note(f"燃尽口径来源：{origin}")
    total_frozen = sum(frozen.values())
    report.note(f"燃尽口径：冻结/豁免/缺口共 {total_frozen} 条，已登记例外 {len(entries)} 条"
                f"（open {open_count}）")
    for source, count in sorted(frozen.items(), key=lambda kv: -kv[1])[:6]:
        report.note(f"   {source}: {count}")
    return report


# --------------------------------------------------------------------------- #
# self-check
# --------------------------------------------------------------------------- #

FIXTURE_ENTRY = "\n".join([
    "- id: EX-001",
    "  kind: baseline-freeze",
    "  gate: g09-python-hygiene",
    "  target: baseline/g09-python-hygiene.txt",
    "  reason: 存量冻结",
    "  compensating: 棘轮只冻结既有键",
    "  approver: siwpan",
    "  expires: {expires}",
    "  status: {status}",
])

SELFCHECK_CASES = [
    ("clean", True),
    ("expired", False),
    ("missing-field", False),
    ("bad-status", False),
    ("duplicate-id", False),
]


def ledger_text(kind: str) -> str:
    """一份夹具台账的正文（五种 kind 各造一个可判的红/绿）。"""
    far = (dt.date.today() + dt.timedelta(days=365)).isoformat()
    past = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    body = "version: 1\nexceptions:\n" + FIXTURE_ENTRY.format(expires=far, status="open") + "\n"
    if kind == "expired":
        body = "version: 1\nexceptions:\n" + FIXTURE_ENTRY.format(expires=past, status="open") + "\n"
    if kind == "missing-field":
        body = "version: 1\nexceptions:\n" + "\n".join(
            ln for ln in FIXTURE_ENTRY.format(expires=far, status="open").splitlines()
            if "approver" not in ln) + "\n"
    if kind == "bad-status":
        body = "version: 1\nexceptions:\n" + FIXTURE_ENTRY.format(expires=far, status="maybe") + "\n"
    if kind == "duplicate-id":
        body = ("version: 1\nexceptions:\n"
                + FIXTURE_ENTRY.format(expires=far, status="open") + "\n"
                + FIXTURE_ENTRY.format(expires=far, status="open") + "\n")
    return body


def build_fixture(kind: str, base: str) -> str:
    ledger = Path(base) / "tools" / "gate" / "governance" / "exceptions.yml"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(ledger_text(kind), encoding="utf-8", newline="\n")
    return base


def selfcheck() -> int:
    check = govlib.SelfCheck(gate=GATE)
    try:
        import yaml  # noqa: F401
    except ImportError:
        print("ENV ERROR: self-check needs PyYAML")
        return EXIT_ENV
    for kind, expect_clean in SELFCHECK_CASES:
        tmp = tempfile.mkdtemp(prefix="g18-exceptions-")
        try:
            build_fixture(kind, tmp)
            report = run(tmp, argparse.Namespace())
            flagged = bool(report.findings)
            if expect_clean:
                if flagged:
                    print(f"  detail: {[f'{f.kind}:{f.text}' for f in report.findings][:3]}")
                    check.quiet(kind, True)
                else:
                    check.quiet(kind, False)
            else:
                check.detects(kind, flagged)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # 台账「仓内 → 包内」回退的三组负对照（2026-11-25）。
    # 光证明「回退能找到」是不够的：回退最容易出的错是**把找不到也放过去**，
    # 所以 A 证回退生效、B 证两处皆无时仍判红、C 证优先级没有反（仓内不许被包内顶掉）。
    tmp_repo = Path(tempfile.mkdtemp(prefix="g18-repo-"))
    tmp_pkg = Path(tempfile.mkdtemp(prefix="g18-pkg-"))
    try:
        bundled = tmp_pkg / "exceptions.yml"
        bundled.write_text(ledger_text("clean"), encoding="utf-8", newline="\n")
        # A：仓内没有台账、包内有 ⇒ 用包内那份（clean 夹具不得判红），且必须标明来源。
        report = run(str(tmp_repo), argparse.Namespace(), package_dir=tmp_pkg)
        check.quiet("a package-bundled ledger when the repository has none", bool(report.findings))
        check.detects("the bundled ledger is named in the output",
                      any("包内" in note for note in report.notes))
        # B：两处都没有 ⇒ 仍判 ledger-unusable 红（回退不许退化成静默通过）。
        bundled.unlink()
        report = run(str(tmp_repo), argparse.Namespace(), package_dir=tmp_pkg)
        check.detects("neither the repository nor the package has a ledger",
                      any(finding.kind == "ledger-unusable" for finding in report.findings))
        # C：仓内一份**过期**、包内一份干净 ⇒ 必须用仓内那份并判红（优先级不许反）。
        bundled.write_text(ledger_text("clean"), encoding="utf-8", newline="\n")
        (tmp_repo / "tools" / "gate" / "governance").mkdir(parents=True, exist_ok=True)
        (tmp_repo / "tools" / "gate" / "governance" / "exceptions.yml").write_text(
            ledger_text("expired"), encoding="utf-8", newline="\n")
        report = run(str(tmp_repo), argparse.Namespace(), package_dir=tmp_pkg)
        check.detects("the repository ledger outranks the bundled one",
                      any(finding.kind == "expired-exception" for finding in report.findings))
    finally:
        shutil.rmtree(tmp_repo, ignore_errors=True)
        shutil.rmtree(tmp_pkg, ignore_errors=True)
    return check.finish()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=TITLE)
    parser.add_argument("--root", default=None, help="work tree to judge (default: discovered from this file)")
    parser.add_argument("--quiet", action="store_true", help="print the summary line only")
    parser.add_argument("--selfcheck", action="store_true", help="inject fixtures and prove the gate works")
    govlib.add_baseline_argument(parser)
    args = parser.parse_args(argv)
    govlib.configure_stdio()
    if args.selfcheck:
        return selfcheck()
    root = govlib.resolve_root(args.root)
    if not root.is_dir():
        print(f"ENV ERROR: root 不存在：{root}")
        return EXIT_ENV
    report = run(str(root), args)
    baseline_path = Path(args.baseline) if args.baseline else (
        govlib.GOVERNANCE_DIR / "baseline" / "g18-exceptions.txt")
    baseline = govlib.Baseline(baseline_path).load()
    if args.write_baseline:
        baseline.write(report.findings)
        print(f"wrote baseline {baseline_path} ({len(report.findings)} key(s))")
        return EXIT_OK
    new = list(report.findings) if args.no_baseline else baseline.filter_new(report.findings)
    shown = Report(title=report.title, findings=report.findings if args.list_all else new,
                   exempted=report.exempted, skipped=report.skipped, notes=report.notes,
                   scanned=report.scanned)
    print(shown.render().splitlines()[0] if args.quiet else shown.render())
    return report.exit_code(new)


if __name__ == "__main__":
    raise SystemExit(govlib.run_gate(main))
