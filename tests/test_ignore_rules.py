"""The ``ignore`` path-rule effect: files and folders kept out of the index entirely (never
downloaded, extracted or embedded), hidden from search at once, removed by the next complete
listing, indexed again when the entry is removed."""

from __future__ import annotations

import itertools
import json
import time
from pathlib import Path
from typing import Any

import pytest
from helpers import FakeDrive, FakeEmbeddings, make_service
from test_admin import CLEARED, ROOT, Env, build_service
from test_local_sources import local_service

from gdrive_rag.admin import Admin, AdminRefused
from gdrive_rag.drive import DriveFolder, DriveItem, DriveSourceError, Listing, folder_link
from gdrive_rag.rules import (
    MAX_PATTERNS,
    OTHER_SOURCE_REASON,
    clean_ignore,
    compile_pattern,
    ignore_listing,
)
from gdrive_rag.service import Service

pytest_plugins = ["test_admin"]  # the `env` fixture: a live server with admin tools

SRC = "rootIgnore0000001"
OLD = "1OldFolderAbCdEf234"  # "cu": an old folder to keep out of the index
DEEP = "1DeepFolderXyZ56789"  # "cu/2024"
KEEP = "1KeepFolderQwErTy12"  # "moi"
F_OLD = "fileOldLedger0001"
F_DEEP = "fileDeepDetail001"
F_NEW = "fileNewLedger0001"
F_TOP = "fileTopPayroll001"
F_DRAFT = "fileDraftNote0001"


def setup(tmp_path: Path, embeddings: FakeEmbeddings | None = None) -> tuple[FakeDrive, Service]:
    drive = FakeDrive()
    drive.add_root(SRC, "Kế toán")
    drive.add_folder(OLD, "cu", SRC)
    drive.add_folder(DEEP, "2024", OLD)
    drive.add_folder(KEEP, "moi", SRC)
    drive.put(SRC, F_OLD, "so-cai-cu.txt", "Sổ cái doanh thu cũ".encode(), parent=OLD)
    drive.put(SRC, F_DEEP, "chi-tiet.txt", "Chi tiết doanh thu năm cũ".encode(), parent=DEEP)
    drive.put(SRC, F_NEW, "so-cai-moi.txt", "Sổ cái doanh thu mới".encode(), parent=KEEP)
    drive.put(SRC, F_TOP, "luong.txt", "Bảng lương tháng ba".encode())
    drive.put(SRC, F_DRAFT, "nhap.md", "Bản nháp doanh thu".encode())
    service = make_service(tmp_path, drive, embeddings or FakeEmbeddings())
    service.add_sources([SRC])
    assert service.sync()["indexed"] == 5
    return drive, service


def found(service: Service, query: str = "doanh thu", **scope: Any) -> set[str]:
    return {hit["file_name"] for hit in service.search(query, limit=50, **scope)["results"]}


def stored(service: Service) -> set[str]:
    with service.db.read() as connection:
        return {str(row[0]) for row in connection.execute("SELECT name FROM documents")}


def skipped(service: Service) -> dict[str, str]:
    return {f["path"]: f["reason"] for f in service.skipped_files()["files"]}


def ignore(service: Service, *entries: str, source: str = SRC) -> dict[str, Any]:
    return Admin(service).set_path_rules(source, confirm=True, ignore=list(entries))


# -- entries ------------------------------------------------------------------------------------
def test_entries_are_globs_or_ids_and_errors_never_echo() -> None:
    assert clean_ignore(
        [
            f"https://drive.google.com/drive/folders/{OLD}?usp=sharing",
            f"https://docs.google.com/document/d/{F_NEW}/edit",
            DEEP,  # a bare Drive ID (mixed case and digits)
            "id:some-opaque-id",
            "local:/hop-dong/./2025/",
            "id:local:Kế toán/cũ",  # a local path may have spaces
            "cu/",
            "bao-cao-2025-ban-cu-khong-dung",  # looks like a name, not an ID: a glob
            " *.md ",
            "*.md",
        ]
    ) == [
        f"id:{OLD}",
        f"id:{F_NEW}",
        f"id:{DEEP}",
        "id:some-opaque-id",
        "id:local:hop-dong/2025",
        "id:local:Kế toán/cũ",
        "cu/",
        "bao-cao-2025-ban-cu-khong-dung",
        "*.md",
    ]
    assert clean_ignore(["fileOldLedger0001"], {"fileOldLedger0001"}.__contains__) == [
        "id:fileOldLedger0001"
    ]
    secret = "sk-or-v1-0123456789abcdef0123456789"
    for values, message in (
        ([f"https://example.com/{secret}"], "not a Google Drive folder or file link"),
        ([f"https://drive.google.com/drive/{secret[:5]}"], "not a Google Drive"),
        (["**/*"], "matches every file path"),
        ([f"a\n{secret}"], "control character"),
        (["id:"], "needs one ID"),
        ([f"id:{secret} x"], "needs one ID"),
        ([f"{secret}/" * 5], "longer than"),
        ([f"x{n}/" for n in range(MAX_PATTERNS + 1)], f"at most {MAX_PATTERNS}"),
        (secret, "must be a list"),
        ([1], "must be a list of strings"),
    ):
        with pytest.raises(ValueError, match=message) as refused:
            clean_ignore(values)
        assert secret not in str(refused.value)


@pytest.mark.parametrize(
    "pattern", ["a/**/b", "**/ab", "a*/b", "?/a", "**/a/**", "/a/", "a/", "b**a", "*.a", "a?b"]
)
def test_the_literal_prefilter_never_skips_a_match(pattern: str) -> None:
    glob = compile_pattern(pattern)
    for size in range(7):
        for chars in itertools.product("ab/.", repeat=size):
            path = "".join(chars)
            if glob.match(path):
                assert glob.needle in path, (pattern, path, glob.needle)


def test_listing_filter_works_on_any_backend_items() -> None:
    def item(file_id: str, path: str, parent: str) -> DriveItem:
        return DriveItem(
            file_id, path.rpartition("/")[2], "text/plain", path, "", "", 1, "", parent
        )

    listing = Listing(
        [item("a", "x/a.txt", "fx"), item("b", "x/y/b.txt", "fy"), item("c", "c.txt", "root")],
        folders=[DriveFolder("root", "R", ""), DriveFolder("fx", "x", "root")]
        + [DriveFolder("fy", "y", "fx")],
    )
    kept, gone = ignore_listing(listing, ["id:fy"])
    assert [i.id for i in kept.items] == ["a", "c"] and gone == {"fy", "b"}
    assert [f.id for f in kept.folders] == ["root", "fx"]
    assert kept.skipped[0].reason == "ignored by rule: id:fy" and kept.skipped[0].parent_id == "fy"
    kept, gone = ignore_listing(listing, ["x/*"])  # one level: the folder itself stays
    assert [i.id for i in kept.items] == ["b", "c"] and gone == {"a"}
    kept, gone = ignore_listing(listing, ["/x/"])
    assert [i.id for i in kept.items] == ["c"] and gone == {"fx", "fy", "a", "b"}
    kept, gone = ignore_listing(listing, [], {"a"})  # what another source ignores
    assert gone == {"a"} and kept.skipped[0].reason == OTHER_SOURCE_REASON
    assert ignore_listing(listing, []) == (listing, set())


# -- sync -------------------------------------------------------------------------------------
def test_folder_link_removes_the_folder_at_sync_and_downloads_nothing(tmp_path: Path) -> None:
    drive, service = setup(tmp_path)
    drive.put(SRC, "fileZip000000001", "kho.zip", b"PK", mime="application/zip", parent=DEEP)
    service.sync()
    assert skipped(service) == {"cu/2024/kho.zip": "no extractor for this file type"}
    result = ignore(service, folder_link(OLD))
    assert result["applied"] and result["rules"]["ignore"] == [f"id:{OLD}"]
    assert result["sync"]["queued"] and service.take_request() is not None
    assert "removed from the index" in result["message"] or "queued" in result["message"]
    # hidden at once, before any sync
    assert found(service) == {"so-cai-moi.txt", "nhap.md", "luong.txt"}
    with pytest.raises(ValueError, match="ignored by a path rule"):
        service.read(F_OLD)

    drive.downloads.clear()
    drive.put(SRC, "fileAdded0000001", "moi-them.txt", b"them", parent=DEEP)
    drive.put(SRC, F_OLD, "so-cai-cu.txt", "Sổ cái đã sửa".encode(), version="2", parent=OLD)
    report = service.sync()
    assert drive.downloads == [] and report["removed"] == 2
    assert stored(service) == {"so-cai-moi.txt", "luong.txt", "nhap.md"}
    reason = f"ignored by rule: id:{OLD}"
    assert skipped(service) == {
        "cu/so-cai-cu.txt": reason,
        "cu/2024/chi-tiet.txt": reason,
        "cu/2024/moi-them.txt": reason,
        "cu/2024/kho.zip": reason,  # an unsupported file below gets the ignore reason
    }
    assert service.list_sources()[0]["skipped"] == 4
    assert [f["name"] for f in service.list_folders(parent=SRC)["folders"]] == ["moi"]
    with pytest.raises(ValueError, match="unknown or not indexed folder"):
        service.search("doanh thu", folder=OLD)
    with pytest.raises(ValueError, match="unknown document id"):
        service.read(F_OLD)


def test_a_folder_entry_follows_renames_and_moves(tmp_path: Path) -> None:
    drive, service = setup(tmp_path)
    service.set_path_rules(1, {"ignore": [OLD]})
    service.sync()
    drive.add_folder(OLD, "luu-tru", KEEP)  # renamed and moved under moi/
    drive.put(SRC, "fileAdded0000001", "moi-them.txt", "doanh thu thêm".encode(), parent=DEEP)
    drive.downloads.clear()
    service.sync()
    assert drive.downloads == []
    assert "moi/luu-tru/2024/moi-them.txt" in skipped(service)
    assert found(service) == {"so-cai-moi.txt", "nhap.md", "luong.txt"}
    assert [f["name"] for f in service.list_folders(parent=KEEP)["folders"]] == []


def test_glob_entries_and_preview_counts(tmp_path: Path) -> None:
    _, service = setup(tmp_path)
    preview = Admin(service).set_path_rules(SRC, ignore=["*.md", "cu/", "cu/*"])
    assert preview["applied"] is False and preview["confirm_needed"] is True
    matches = preview["matches"]
    assert matches["ignore"]["*.md"] == {"documents": 1, "folders": 0, "examples": ["nhap.md"]}
    assert matches["ignore"]["cu/"]["documents"] == 2 and matches["ignore"]["cu/"]["folders"] == 2
    assert matches["ignore"]["cu/*"] == {
        "documents": 1,
        "folders": 0,
        "examples": ["cu/so-cai-cu.txt"],
    }
    assert (matches["ignored"], matches["ignored_folders"], matches["documents"]) == (3, 2, 5)
    assert "3 indexed documents and 2 folders" in preview["would"]
    assert service.path_rules(1)["ignore"] == [] and service.take_request() is None
    ignore(service, "*.md", "cu/")
    assert service.sync()["removed"] == 3
    assert stored(service) == {"so-cai-moi.txt", "luong.txt"}


def test_incomplete_listing_deletes_nothing_but_search_hides(tmp_path: Path) -> None:
    drive, service = setup(tmp_path)
    service.set_path_rules(1, {"ignore": ["cu/"]})
    drive.incomplete.add(SRC)
    assert service.sync()["removed"] == 0
    assert {"so-cai-cu.txt", "chi-tiet.txt"} <= stored(service)
    assert found(service) == {"so-cai-moi.txt", "nhap.md", "luong.txt"}
    drive.incomplete.clear()
    assert service.sync()["removed"] == 2


def test_a_stopped_pass_deletes_nothing_but_search_hides(tmp_path: Path) -> None:
    drive, service = setup(tmp_path)
    service.set_path_rules(1, {"ignore": [f"id:{OLD}"]})
    drive.put(SRC, F_NEW, "so-cai-moi.txt", "Sổ cái mới sửa".encode(), version="2", parent=KEEP)
    drive.download_errors[F_NEW] = DriveSourceError("connection lost")
    report = service.sync()
    assert report["stopped"] and report["removed"] == 0
    assert {"so-cai-cu.txt", "chi-tiet.txt"} <= stored(service)
    assert found(service) == {"so-cai-moi.txt", "nhap.md", "luong.txt"}


def test_files_scope_naming_an_ignored_file_returns_nothing_with_a_warning(
    tmp_path: Path,
) -> None:
    _, service = setup(tmp_path)
    service.set_path_rules(1, {"exclude": ["so-cai-moi.txt"], "ignore": ["cu/"]})
    for when in ("before sync", "after sync"):
        result = service.search("doanh thu", files=[F_OLD])
        assert result["results"] == [], when
        assert any("ignored by a path rule" in w for w in result["warnings"]), when
        both = service.search("doanh thu", files=[F_OLD, F_NEW])  # exclude still yields
        assert {hit["file_name"] for hit in both["results"]} == {"so-cai-moi.txt"}, when
        service.sync()


def test_a_file_any_holding_source_ignores_is_not_indexed(tmp_path: Path) -> None:
    drive, service = setup(tmp_path)
    drive.add_root(OLD, "cu")
    [inner] = service.add_sources([OLD])  # a second source: the "cu" folder itself
    service.sync()
    with pytest.raises(AdminRefused, match="another source"):
        ignore(service, "cu/so-cai-cu.txt", "cu/2024/")  # would empty the other source
    ignore(service, "cu/2024/")
    # hidden at once also in the other source's scope
    assert found(service, source=str(inner["id"])) == {"so-cai-cu.txt"}
    drive.downloads.clear()
    service.sync()
    assert "chi-tiet.txt" not in stored(service) and drive.downloads == []
    reasons = {(f["source"], f["path"]): f["reason"] for f in service.skipped_files()["files"]}
    assert reasons[("Kế toán", "cu/2024/chi-tiet.txt")] == "ignored by rule: cu/2024/"
    assert reasons[("cu", "2024/chi-tiet.txt")] == OTHER_SOURCE_REASON
    service.sync(source_id=int(inner["id"]))  # the ignoring source is not in this pass
    assert "chi-tiet.txt" not in stored(service) and drive.downloads == []


def test_removing_the_entry_indexes_the_files_again(tmp_path: Path) -> None:
    drive, service = setup(tmp_path)
    ignore(service, folder_link(OLD))
    service.sync()
    service.finish_request()
    drive.downloads.clear()
    result = ignore(service)  # empty list: no ignore entries
    assert result["sync"]["queued"] and skipped(service) == {}
    report = service.sync()
    assert report["indexed"] == 2 and sorted(drive.downloads) == sorted([F_OLD, F_DEEP])
    assert found(service) >= {"so-cai-cu.txt", "chi-tiet.txt"}


def test_guards_and_only_ignore_changes_queue_a_sync(tmp_path: Path) -> None:
    _, service = setup(tmp_path)
    admin = Admin(service)
    with pytest.raises(AdminRefused, match="every document of this source"):
        admin.set_path_rules(SRC, ignore=["*.txt", "*.md"])
    with pytest.raises(ValueError, match="names the source itself"):
        admin.set_path_rules(SRC, ignore=[folder_link(SRC)])
    with pytest.raises(AdminRefused, match="hide every document"):
        admin.set_path_rules(SRC, exclude=["*.txt"], ignore=["*.md"])
    admin.set_path_rules(SRC, demote=["cu/"], confirm=True)
    assert service.take_request() is None
    shown = admin.set_path_rules(SRC)  # nothing given: only shows
    assert shown["applied"] is False and "Nothing is re-indexed" in shown["would"]
    ignore(service, "*.md")
    assert service.take_request() == (None,)  # every source: a file any source ignores goes
    service.finish_request()
    ignore(service, "*.md")  # the same entries again: no new sync
    assert service.take_request() is None


def test_ignore_on_a_local_source(tmp_path: Path) -> None:
    root, service = local_service(tmp_path)
    [source] = service.add_sources(["local:hop-dong"])
    service.sync()
    assert "thue.txt" in stored(service)
    result = Admin(service).set_path_rules(
        source["name"], confirm=True, ignore=["local:/hop-dong/./2025/"]
    )
    assert result["rules"]["ignore"] == ["id:local:hop-dong/2025"]
    assert result["matches"]["ignored"] == 1 and result["matches"]["ignored_folders"] == 1
    (root / "hop-dong" / "2025" / "sub").mkdir()
    (root / "hop-dong" / "2025" / "sub" / "moi.txt").write_text("Hợp đồng mới", "utf-8")
    report = service.sync()
    assert report["removed"] == 1 and stored(service) == {"mau.md"}
    assert set(skipped(service)) == {"2025/thue.txt", "2025/sub/moi.txt"}
    assert service.list_folders(parent="local:hop-dong")["folders"] == []
    Admin(service).set_path_rules(source["name"], confirm=True, ignore=[])
    service.sync()
    assert stored(service) == {"mau.md", "thue.txt", "moi.txt"}


# -- MCP and CLI --------------------------------------------------------------------------------
def test_set_path_rules_tool_with_ignore(env: Env) -> None:
    env.drive.put(ROOT, "doc3333333333333", "ban-cu.txt", "Hóa đơn cũ".encode())
    env.service.sync()
    secret = "sk-or-v1-0123456789abcdef0123456789"
    for value in (secret, [f"https://evil.example/{secret}"], ["**"]):
        refused = env.tool("set_path_rules", source=ROOT, ignore=value)
        assert isinstance(refused, str) and secret not in refused, refused
    preview = env.tool("set_path_rules", source=ROOT, ignore=["ban-cu.txt"])
    assert preview["applied"] is False and preview["matches"]["ignored"] == 1
    applied = env.tool("set_path_rules", source=ROOT, ignore=["ban-cu.txt"], confirm=True)
    assert applied["applied"] is True and applied["sync"]["queued"] is True
    sources = env.tool("list_sources")["sources"]
    assert sources[0]["path_rules"]["ignore"] == ["ban-cu.txt"]
    deadline = time.time() + 10  # the server's sync loop runs the queued sync
    while time.time() < deadline and "ban-cu.txt" in stored(env.service):
        time.sleep(0.05)
    assert "ban-cu.txt" not in stored(env.service)
    status = env.tool("sync_status", include_skipped=True)
    assert [f["reason"] for f in status["skipped_files"]] == ["ignored by rule: ban-cu.txt"]


def test_cli_source_rules_ignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from gdrive_rag.cli import main

    for name in CLEARED:
        monkeypatch.delenv(name, raising=False)
    drive = FakeDrive()
    drive.add_root(ROOT, "Kế toán")
    drive.add_folder(OLD, "cu", ROOT)
    drive.put(ROOT, "doc1111111111111", "hoa-don.txt", "Hóa đơn tiền điện".encode())
    drive.put(ROOT, "doc2222222222222", "cu.txt", "Hóa đơn cũ".encode(), parent=OLD)
    service = build_service(tmp_path, drive)
    service.add_sources([ROOT])
    service.sync()
    args = ["--data-dir", str(tmp_path), "--json", "source", "rules", ROOT]
    assert main([*args, "--ignore", folder_link(OLD)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applied"] is True and out["rules"]["ignore"] == [f"id:{OLD}"]
    service.sync()
    assert stored(service) == {"hoa-don.txt"}
    assert main([*args, "--ignore"]) == 0  # no entries: clears the list
    capsys.readouterr()
    service.sync()
    assert stored(service) == {"hoa-don.txt", "cu.txt"}
