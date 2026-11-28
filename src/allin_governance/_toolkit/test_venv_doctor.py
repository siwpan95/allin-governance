"""venv 体检/修复/重建的判据测试（只用临时目录，不碰真实环境）。"""

from pathlib import Path

from .venv_doctor import check, find_venv, rebuild


def _fake_venv(root: Path, *, broken_home: bool = False) -> Path:
    venv = root / ".venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_bytes(b"")
    home = str(root / "missing-python") if broken_home else str(root)
    (venv / "pyvenv.cfg").write_text(f"home = {home}\nversion = 3.13.0\n", encoding="utf-8")
    return venv


def test_find_venv(tmp_path):
    _fake_venv(tmp_path)
    assert find_venv(tmp_path) == tmp_path / ".venv"


def test_no_venv(tmp_path):
    state = check(tmp_path)
    assert state["venv"] is None
    assert state["issues"][0]["code"] == "no-venv"


def test_broken_home_detected(tmp_path):
    _fake_venv(tmp_path, broken_home=True)
    codes = {i["code"] for i in check(tmp_path)["issues"]}
    assert "home-missing" in codes
    assert "interpreter-broken" in codes


def test_rebuild_dry_run_keeps_old(tmp_path):
    venv = _fake_venv(tmp_path)
    res = rebuild(tmp_path, dry_run=True)
    assert res["old"] == str(venv)
    assert venv.exists(), "dry-run 不得改动现有环境"
