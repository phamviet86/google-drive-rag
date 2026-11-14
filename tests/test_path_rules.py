"""Per-source search path rules (fix 2): demote lowers a file, exclude leaves it out; both are
search-time only, set through the admin tool or the CLI, shown in list_sources and --explain."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from helpers import FakeDrive, FakeEmbeddings, make_service
from test_admin import CLEARED, ROOT, Env, build_service

from gdrive_rag.rules import clean, matches
from gdrive_rag.service import Service

pytest_plugins = ["test_admin"]  # the `env` fixture: a live server with admin tools

SOURCE = "rootRules00000001"
CANONICAL = "fileCanonical001"
ATTACHMENT = "fileAttachment01"


def ledger(tmp_path: Path, embeddings: FakeEmbeddings | None = None) -> tuple[FakeDrive, Service]:
    """The real case in small: an old original under attachments/ outranks the canonical
    article one folder up."""
    drive = FakeDrive()
    drive.add_root(SOURCE, "Kế toán")
    drive.add_folder("folderAttach0001", "attachments", SOURCE)
    drive.put(
        SOURCE,
        CANONICAL,
        "household-revenue-ledger-and-reconciliation.md",
        "Hướng dẫn: ghi sổ cái doanh thu hằng ngày, rồi đối soát với sao kê.".encode(),
    )
    drive.put(
        SOURCE,
        ATTACHMENT,
        "so-cai-doanh-thu.txt",
        "Sổ cái doanh thu (bản gốc cũ). Sổ cái doanh thu hộ kinh doanh.".encode(),
        parent="folderAttach0001",
    )
    drive.put(SOURCE, "fileOther0000001", "luong.txt", "Bảng lương tháng ba".encode())
    service = make_service(tmp_path, drive, embeddings)
    service.add_sources([SOURCE])
    service.sync()
    return drive, service


def names(result: dict[str, Any]) -> list[str]:
    return [hit["file_name"] for hit in result["results"]]


@pytest.mark.parametrize(
    ("pattern", "path", "hit"),
    [
        ("attachments/**", "Kế toán/attachments/a.docx", True),
        ("attachments/**", "attachments/sub/a.docx", True),
        ("attachments/", "x/attachments/a.docx", True),
        ("/attachments/*", "x/attachments/a.docx", False),
        ("/attachments/*", "attachments/a.docx", True),
        ("*.bak", "a/b/c.bak", True),
        ("**/cũ/*.txt", "Kế toán/CU/a.txt", True),  # no case, no accents
        ("attachments/*", "attachments/sub/a.docx", False),
        ("a?c.txt", "abc.txt", True),
        ("a?c.txt", "a/c.txt", False),
        ("[x].txt", "[x].txt", True),  # brackets are literal
    ],
)
def test_glob_semantics(pattern: str, path: str, hit: bool) -> None:
    assert matches(pattern, path) is hit


def test_invalid_patterns_are_refused_without_echo() -> None:
    for bad, message in (
        ("not a list", "must be a list"),
        ([1], "list of strings"),
        (["**"], "match-everything"),
        (["   "], "match-everything"),
        (["a\x00b"], "control character"),
        (["x" * 201], "longer than"),
        ([f"p{i}" for i in range(51)], "at most 50"),
    ):
        with pytest.raises(ValueError, match=message) as raised:
            clean(bad, "demote")
        assert "x" * 50 not in str(raised.value)
    assert clean([" a/** ", "a/**", "b"], "exclude") == ["a/**", "b"]


def test_demote_moves_the_attachment_below_the_canonical_file(tmp_path: Path) -> None:
    _, service = ledger(tmp_path)
    query = "sổ cái doanh thu"
    before = names(service.search(query, limit=3))
    assert before[:2] == ["so-cai-doanh-thu.txt", "household-revenue-ledger-and-reconciliation.md"]
    source_id = service.list_sources()[0]["id"]
    service.set_path_rules(source_id, {"demote": ["attachments/**"], "exclude": []})
    after = service.search(query, limit=3, explain=True)
    assert names(after)[:2] == before[1::-1]
    demoted = next(h for h in after["results"] if h["file_name"] == "so-cai-doanh-thu.txt")
    assert demoted["explain"]["path_rule"] == "demote: attachments/**"
    assert demoted["score"] == pytest.approx(
        demoted["explain"]["fused_before_rules"] * 0.5, abs=1e-6
    )
    canonical = after["results"][0]["explain"]
    assert canonical["path_rule"] is None
    assert canonical["fused"] == canonical["fused_before_rules"]


def test_exclude_hides_from_search_but_keeps_the_file(tmp_path: Path) -> None:
    drive, service = ledger(tmp_path, FakeEmbeddings())
    source_id = service.list_sources()[0]["id"]
    service.set_path_rules(source_id, {"demote": [], "exclude": ["attachments/"]})
    result = service.search("sổ cái doanh thu", limit=5)
    assert result["mode"] == "hybrid"  # excluded from the vector list too
    assert "so-cai-doanh-thu.txt" not in names(result)
    assert names(result)[0] == "household-revenue-ledger-and-reconciliation.md"
    assert service.read(ATTACHMENT)["chunks"]  # still indexed and readable by ID
    named = service.search("sổ cái doanh thu", files=[ATTACHMENT])  # named files: never hidden
    assert names(named) == ["so-cai-doanh-thu.txt"]
    assert service.status()["documents"]["indexed"] == 3
    downloads = list(drive.downloads)
    service.sync()  # a rule change never re-extracts or re-embeds
    assert drive.downloads == downloads


def test_no_rules_means_the_old_explain_shape(tmp_path: Path) -> None:
    _, service = ledger(tmp_path)
    explain = service.search("sổ cái doanh thu", explain=True)["results"][0]["explain"]
    assert "path_rule" not in explain and "fused_before_rules" not in explain


def test_rules_follow_the_source_that_holds_the_file(tmp_path: Path) -> None:
    drive, service = ledger(tmp_path)
    drive.add_root("rootOther00000001", "Khác")
    drive.put("rootOther00000001", "fileElse00000001", "so-cai-doanh-thu.txt", "Sổ cái".encode())
    service.add_sources(["rootOther00000001"])
    service.sync()
    service.set_path_rules(service.list_sources()[0]["id"], {"exclude": ["*.txt"], "demote": []})
    found = {(h["file_name"], h["sources"]) for h in service.search("sổ cái")["results"]}
    assert ("so-cai-doanh-thu.txt", "Khác") in found  # the other source has no rule
    assert all(source != "Kế toán" or name.endswith(".md") for name, source in found)


# admin tool -------------------------------------------------------------------------------------
def test_set_path_rules_tool_previews_then_applies(env: Env) -> None:
    preview = env.tool("set_path_rules", source=ROOT, demote=["*.txt"])
    assert preview["applied"] is False and preview["confirm_needed"] is True
    assert preview["matches"]["demote"]["*.txt"] == {"documents": 1, "examples": ["hoa-don.txt"]}
    assert preview["matches"]["demoted"] == 1
    assert env.tool("list_sources")["sources"][0]["path_rules"] == {"demote": [], "exclude": []}

    applied = env.tool("set_path_rules", source=ROOT, demote=["*.txt"], confirm=True)
    assert applied["applied"] is True
    rules = env.tool("list_sources")["sources"][0]["path_rules"]
    assert rules == {"demote": ["*.txt"], "exclude": []}

    kept = env.tool("set_path_rules", source=ROOT, exclude=["old/"], confirm=True)
    assert kept["rules"] == {"demote": ["*.txt"], "exclude": ["old/"]}  # omitted list kept
    cleared = env.tool("set_path_rules", source=ROOT, demote=[], exclude=[], confirm=True)
    assert cleared["rules"] == {"demote": [], "exclude": []}


def test_set_path_rules_tool_refuses_bad_arguments(env: Env) -> None:
    secretish = "sk-or-v1-0123456789abcdef0123456789"
    for arguments, message in (
        ({"source": ROOT, "demote": secretish}, "must be a list of strings"),
        ({"source": ROOT, "demote": [secretish * 10]}, "longer than"),
        ({"source": ROOT, "demote": ["a"], "confirm": "true"}, "boolean"),
        ({"source": secretish, "demote": ["a"]}, "unknown source"),
        ({"demote": ["a"]}, "source is required"),
    ):
        refused = env.tool("set_path_rules", **arguments)
        assert isinstance(refused, str) and message in refused, refused
        assert secretish not in refused
    assert env.tool("list_sources")["sources"][0]["path_rules"]["demote"] == []


def test_cli_source_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from gdrive_rag.cli import main

    for name in CLEARED:
        monkeypatch.delenv(name, raising=False)
    drive = FakeDrive()
    drive.add_root(ROOT, "Kế toán")
    drive.put(ROOT, "doc1111111111111", "hoa-don.txt", "Hóa đơn tiền điện".encode())
    service = build_service(tmp_path, drive)
    service.add_sources([ROOT])
    service.sync()
    args = ["--data-dir", str(tmp_path), "--json", "source", "rules", ROOT]
    assert main(args) == 0  # no options: show only
    shown = json.loads(capsys.readouterr().out)
    assert shown["applied"] is False and shown["rules"] == {"demote": [], "exclude": []}
    assert main([*args, "--demote", "*.txt", "attachments/**"]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] is True
    assert service.list_sources()[0]["path_rules"]["demote"] == ["*.txt", "attachments/**"]
    assert main([*args, "--demote"]) == 0  # an option with no patterns clears that list
    capsys.readouterr()
    assert service.list_sources()[0]["path_rules"]["demote"] == []
    assert main(["--data-dir", str(tmp_path), "--json", "status", "--skipped"]) == 0
    assert json.loads(capsys.readouterr().out)["skipped_files"] == []


def test_mcp_status_and_sources_carry_the_new_fields(env: Env) -> None:
    env.drive.put(ROOT, "zip1111111111111", "kho.zip", b"PK", mime="application/zip")
    env.service.sync()
    plain = env.tool("sync_status")
    assert plain["skipped"] == {"total": 1} and plain.get("skipped_files") is None
    assert plain["sync"]["progress"] is None and plain["sync"]["running"] is False
    listed = env.tool("sync_status", include_skipped=True)["skipped_files"]
    assert listed == [
        {
            "source": "Kế toán",
            "path": "kho.zip",
            "mime_type": "application/zip",
            "size": 2,
            "reason": "no extractor for this file type",
        }
    ]
    source = env.tool("list_sources")["sources"][0]
    assert source["skipped"] == 1 and source["first_sync_done"] is True
