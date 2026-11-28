"""物料主数据：只建可判真字段（digest/status/title），不造数量与库位。"""

from pathlib import Path

from .materials import build


def _repo(tmp_path: Path) -> Path:
    root = tmp_path
    docs = root / "docs"
    (docs / "规范").mkdir(parents=True)
    (docs / "INDEX.md").write_text(
        "# 索引\n\n| 项 | 链接 |\n|---|---|\n| 规范 | [SPEC-001](规范/SPEC-001.md) |\n",
        encoding="utf-8")
    (docs / "规范" / "SPEC-001.md").write_text("# 铁律\n\n内容\n", encoding="utf-8")
    (docs / "随手记.md").write_text("没有一级标题\n", encoding="utf-8")
    return root


def test_build_materials(tmp_path):
    data = build(_repo(tmp_path))
    assert data["schema"].endswith("/materials/v1")
    rows = {r["path"]: r for r in data["rows"]}
    assert rows["docs/规范/SPEC-001.md"]["status"] == "registered"
    assert rows["docs/规范/SPEC-001.md"]["title"] == "铁律"
    assert rows["docs/随手记.md"]["status"] == "unregistered"
    assert rows["docs/随手记.md"]["title"] == ""
    # 物料号去 .md；digest 是内容判据
    assert rows["docs/规范/SPEC-001.md"]["id"] == "docs/规范/SPEC-001"
    assert len(rows["docs/规范/SPEC-001.md"]["digest"]) == 12


def test_digest_changes_with_content(tmp_path):
    root = _repo(tmp_path)
    first = {r["path"]: r for r in build(root)["rows"]}
    (root / "docs" / "随手记.md").write_text("改过了\n", encoding="utf-8")
    second = {r["path"]: r for r in build(root)["rows"]}
    assert first["docs/随手记.md"]["digest"] != second["docs/随手记.md"]["digest"]


def test_no_quantity_fields(tmp_path):
    """不造文档没有语义的列：数量/批次/库位一律不出现。"""
    data = build(_repo(tmp_path))
    for row in data["rows"]:
        assert not ({"qty", "uom", "lot", "bin"} & set(row))
