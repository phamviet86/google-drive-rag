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
        (["x" * 101], "longer than"),
        ([f"p{i}" for i in range(21)], "at most 20"),
    ):
        with pytest.raises(ValueError, match=message) as raised:
            clean(bad, "demote")
        assert "x" * 50 not in str(raised.value)
    assert clean([" a/** ", "a/**", "b"], "exclude") == ["a/**", "b"]


def real_shape(tmp_path: Path, embeddings: FakeEmbeddings | None) -> Service:
    """The reported case: six old originals under attachments/ whose names and text match the
    Vietnamese query rank 1-6; the canonical article (English name, no whole-query phrase)
    ranks below them and below two notes."""
    drive = FakeDrive()
    drive.add_root(SOURCE, "Kế toán")
    drive.add_folder("folderAttach0001", "attachments", SOURCE)
    for i in range(6):
        drive.put(
            SOURCE,
            f"fileAttach{i:06d}",
            f"so-cai-doanh-thu-ho-kinh-doanh-{i}.txt",
            f"Sổ cái doanh thu hộ kinh doanh, bản gốc số {i}. Mẫu sổ cái doanh thu.".encode(),
            parent="folderAttach0001",
        )
    drive.put(
        SOURCE,
        CANONICAL,
        "household-revenue-ledger-and-reconciliation.md",
        "Hướng dẫn ghi chép doanh thu của hộ kinh doanh vào sổ, đối soát cuối tháng "
        "và lập sổ cái.".encode(),
    )
    for i in range(2):
        drive.put(
            SOURCE,
            f"fileNote{i:08d}",
            f"ghi-chu-{i}.txt",
            f"Sổ cái: ghi chú doanh thu tháng {i}, hộ kinh doanh.".encode(),
        )
    service = make_service(tmp_path, drive, embeddings)
    service.add_sources([SOURCE])
    service.sync()
    return service


@pytest.mark.parametrize("hybrid", [False, True])
def test_demote_lifts_the_canonical_file_above_every_attachment(
    tmp_path: Path, hybrid: bool
) -> None:
    service = real_shape(tmp_path, FakeEmbeddings() if hybrid else None)
    query = "sổ cái doanh thu hộ kinh doanh"
    canonical = "household-revenue-ledger-and-reconciliation.md"
    before = names(service.search(query, limit=10))
    assert all(n.startswith("so-cai") for n in before[:6]) and before.index(canonical) >= 6
    service.set_path_rules(1, {"demote": ["attachments/**"], "exclude": []})
    result = service.search(query, limit=10, explain=True)
    after = names(result)
    assert after.index(canonical) <= 2  # only the two notes may stay above it
    assert after.index(canonical) < min(i for i, n in enumerate(after) if n.startswith("so-cai"))
    assert sum(n.startswith("so-cai") for n in after) >= 2  # still found, only lower
    scores = [hit["score"] for hit in result["results"]]
    assert scores == sorted(scores, reverse=True)  # scores stay in order
    demoted = next(h for h in result["results"] if h["file_name"].startswith("so-cai"))
    assert demoted["explain"]["path_rule"] == "demote: attachments/**"
    assert demoted["score"] < demoted["explain"]["fused_before_rules"]
    top = result["results"][after.index(canonical)]["explain"]
    assert top["path_rule"] is None and top["fused"] == top["fused_before_rules"]


def test_demote_moves_whole_documents_and_keeps_scores_ordered() -> None:
    from gdrive_rag.rules import DEMOTE_PLACES, demote

    # chunks 0-2 belong to the demoted document 100; chunks 3.. to documents 0..14, one each
    order = list(range(18))
    document_of = {c: 100 if c < 3 else c - 3 for c in order}
    scores = {c: 1.0 - c / 100 for c in order}
    result = demote(order, document_of, {100}, scores)
    first_demoted = min(result.index(c) for c in (0, 1, 2))
    assert result[:first_demoted] == list(range(3, 3 + DEMOTE_PLACES))  # 10 other files first
    assert result[first_demoted : first_demoted + 3] == [0, 1, 2]
    assert [scores[c] for c in result] == sorted((scores[c] for c in result), reverse=True)
    few = demote([0, 1, 2, 3], {0: 9, 1: 9, 2: 1, 3: 2}, {9}, {0: 4.0, 1: 3.0, 2: 2.0, 3: 1.0})
    assert few == [2, 3, 0, 1]  # fewer than 10 other files: to the end, still listed
    assert demote([5], {5: 1}, {1}, {5: 1.0}) == [5]


def multi_chunk(tmp_path: Path, embeddings: FakeEmbeddings | None) -> Service:
    """Each attachment has several chunks that match the query on its own; the canonical
    article has several chunks too and an English name."""
    drive = FakeDrive()
    drive.add_root(SOURCE, "Kế toán")
    drive.add_folder("folderAttach0001", "attachments", SOURCE)
    old = " ".join(
        f"Đoạn {n}: sổ cái doanh thu hộ kinh doanh, bản gốc cũ, mẫu sổ cái doanh thu."
        for n in range(6)
    )
    for i in range(3):
        drive.put(
            SOURCE,
            f"fileAttach{i:06d}",
            f"so-cai-doanh-thu-ho-kinh-doanh-{i}.txt",
            old.encode(),
            parent="folderAttach0001",
        )
    canonical = " ".join(
        f"Phần {n}: hướng dẫn ghi chép doanh thu của hộ kinh doanh vào sổ và lập sổ cái."
        for n in range(6)
    )
    drive.put(
        SOURCE, CANONICAL, "household-revenue-ledger-and-reconciliation.md", canonical.encode()
    )
    for i in range(2):
        drive.put(SOURCE, f"fileNote{i:08d}", f"ghi-chu-{i}.txt", f"Ghi chú chi phí {i}.".encode())
    service = make_service(tmp_path, drive, embeddings, chunk_chars=160, chunk_overlap=0)
    service.add_sources([SOURCE])
    service.sync()
    return service


@pytest.mark.parametrize("hybrid", [False, True])
def test_demote_drops_multi_chunk_files_as_a_whole(tmp_path: Path, hybrid: bool) -> None:
    service = multi_chunk(tmp_path, FakeEmbeddings() if hybrid else None)
    query = "sổ cái doanh thu hộ kinh doanh"
    canonical = "household-revenue-ledger-and-reconciliation.md"
    before = service.search(query, limit=8)
    assert all(h["chunk_count"] > 1 for h in before["results"][:3])
    assert all(n.startswith("so-cai") for n in names(before)[:3])
    service.set_path_rules(1, {"demote": ["attachments/**"], "exclude": []})
    after = names(service.search(query, limit=8))
    assert after[:3] == [canonical] * 3  # all its hits (per-file cap) before any old file
    assert sum(n.startswith("so-cai") for n in after) >= 3  # old files still listed, lower


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


def test_largest_rules_still_fit_the_wire(env: Env) -> None:
    from gdrive_rag.rules import MAX_PATTERN_CHARS, MAX_PATTERNS
    from gdrive_rag.server import MAX_WIRE_CHARS, wire_size

    def biggest(tag: str) -> list[str]:  # quotes and backslashes double when escaped twice
        return [
            f"{tag}{i:02d}" + '\\"' * ((MAX_PATTERN_CHARS - 6) // 2) for i in range(MAX_PATTERNS)
        ]

    for n in range(12):
        root = f"rootWire{n:09d}"
        env.drive.add_root(root, f"Nguồn {n}")
        env.service.add_sources([root])
    env.service.sync()
    for source in env.service.list_sources():
        env.service.set_path_rules(source["id"], {"demote": biggest("d"), "exclude": biggest("e")})
    assert wire_size({"sources": env.service.list_sources()}) > MAX_WIRE_CHARS  # untrimmed
    listed = env.tool("list_sources")
    status = env.tool("sync_status", include_skipped=True)
    preview = env.tool("set_path_rules", source=ROOT, demote=biggest("x"), exclude=biggest("y"))
    for value in (listed, status, preview):
        assert isinstance(value, dict) and wire_size(value) <= MAX_WIRE_CHARS
    assert any(s["path_rules_truncated"] for s in listed["sources"])
    assert len(listed["sources"]) == len(env.service.list_sources())  # sources are never dropped


def test_glob_matching_has_no_backtracking_blowup() -> None:
    import time

    started = time.monotonic()
    for pattern in ("**a" * 8, "*a" * 30, "**/" * 10 + "z", "?*" * 40):
        for path in ("a" * 59 + "b", "a/" * 30, "x" * 99 + "y"):
            matches(pattern, path)
    assert time.monotonic() - started < 2.0
    assert matches("**a" * 8, "a" * 8) and not matches("**a" * 8, "a" * 7 + "b")


@pytest.mark.parametrize("pattern", ["*", "**/*", "**/**", "/**/*", "**/*.*/**/*"])
def test_match_everything_patterns_are_refused(pattern: str) -> None:
    if pattern == "**/*.*/**/*":  # needs a dot folder: not everything, allowed
        assert clean([pattern], "exclude") == [pattern]
        return
    with pytest.raises(ValueError, match="matches every file path"):
        clean([pattern], "exclude")


def test_exclude_hiding_every_document_of_a_source_is_refused(env: Env) -> None:
    refused = env.tool("set_path_rules", source=ROOT, exclude=["*.txt"])  # its only file
    assert isinstance(refused, str) and "would hide every document" in refused
    refused = env.tool("set_path_rules", source=ROOT, exclude=["*.txt"], confirm=True)
    assert isinstance(refused, str) and "would hide every document" in refused
    assert env.tool("list_sources")["sources"][0]["path_rules"]["exclude"] == []
    assert env.tool("set_path_rules", source=ROOT, demote=["*.txt"])["applied"] is False
