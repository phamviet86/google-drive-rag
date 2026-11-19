"""Local files as sources (``local:<path>``) and file / folder exclusions by ID: both work
without any admin switch, through the same service, search, read and MCP tools as Drive."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from helpers import FakeDrive, FakeEmbeddings, make_service
from test_admin import ROOT, Env, wait_idle
from test_path_rules import names

from gdrive_rag.admin import Admin, AdminRefused
from gdrive_rag.drive import parse_drive_link
from gdrive_rag.local import RoutingBackend
from gdrive_rag.service import Service

pytest_plugins = ["test_admin"]  # the `env` fixture: a live server


def tree(root: Path) -> Path:
    """files/ with docs/ (a note, a nested contract), photos/ and things that must not be read."""
    files = root / "files"
    (files / "docs" / "hop-dong").mkdir(parents=True)
    (files / "photos").mkdir()
    (files / "docs" / "ghi-chu.txt").write_text("Ghi chú về hóa đơn tiền điện", encoding="utf-8")
    (files / "docs" / "hop-dong" / "thue-nha.md").write_text(
        "# Hợp đồng thuê nhà\n\nTiền thuê nhà trả vào ngày 5 hằng tháng.", encoding="utf-8"
    )
    (files / "root.txt").write_text("Tệp ở thư mục gốc về doanh thu", encoding="utf-8")
    (files / "docs" / ".secret.txt").write_text("hidden hóa đơn", encoding="utf-8")
    (files / "docs" / "~$lock.txt").write_text("lock hóa đơn", encoding="utf-8")
    (files / "photos" / "archive.zip").write_bytes(b"PK\x03\x04")
    return files


def local_service(
    tmp_path: Path, drive: FakeDrive | None = None, embeddings: FakeEmbeddings | None = None
) -> tuple[Service, Path]:
    files = tree(tmp_path)
    return make_service(tmp_path / "data", drive, embeddings, local_root=files), files


def test_local_ids_are_normalized_and_confined() -> None:
    assert parse_drive_link("local:docs/ghi-chu.txt") == "local:docs/ghi-chu.txt"
    assert parse_drive_link(" local:/docs//./hop-dong/ ") == "local:docs/hop-dong"
    assert parse_drive_link("local:") == parse_drive_link("local:/") == "local:"
    for bad in ("local:../etc", "local:docs/../../x", "local:a\\b", "local:a\nb"):
        with pytest.raises(ValueError):
            parse_drive_link(bad)


def test_local_folder_is_indexed_searched_and_read_without_google(tmp_path: Path) -> None:
    service, _ = local_service(tmp_path)  # no Drive configured at all
    added = service.add_sources(["local:docs"])[0]
    assert added["ok"] and added["kind"] == "folder" and added["name"] == "docs"
    assert added["link"] == "local:docs"
    report = service.sync()
    assert (report["indexed"], report["failed"]) == (2, 0)  # hidden and ~$ files are not read
    hit = service.search("hợp đồng thuê nhà")["results"][0]
    assert hit["file_name"] == "thue-nha.md" and hit["path"] == "hop-dong/thue-nha.md"
    assert hit["drive_link"] == "local:docs/hop-dong/thue-nha.md"
    assert names(service.search("hóa đơn")) == ["ghi-chu.txt"]  # .secret.txt / ~$ stay out
    # the same ID scopes a search and reads the document
    assert names(service.search("nhà", folder="local:docs/hop-dong")) == ["thue-nha.md"]
    assert names(service.search("nhà", files=["local:docs/hop-dong/thue-nha.md"])) == [
        "thue-nha.md"
    ]
    assert service.search("hóa đơn", folder="local:docs/hop-dong")["results"] == []
    read = service.read("local:docs/hop-dong/thue-nha.md")
    assert "ngày 5" in read["chunks"][0]["text"]
    folders = service.list_folders(parent="local:docs")
    assert [f["folder_id"] for f in folders["folders"]] == ["local:docs/hop-dong"]
    assert all(f["drive_link"].startswith("local:") for f in folders["folders"])


def test_whole_local_folder_file_source_and_overlap(tmp_path: Path) -> None:
    service, _ = local_service(tmp_path)
    everything, one = service.add_sources(["local:", "local:docs/ghi-chu.txt"])
    assert everything["name"] == "Local files" and one["kind"] == "file"
    report = service.sync()
    assert report["failed"] == 0
    # root.txt, ghi-chu and thue-nha (the note once for both sources); archive.zip is skipped
    assert report["indexed"] == 3
    assert names(service.search("doanh thu")) == ["root.txt"]
    assert service.status()["skipped"]["total"] == 1  # archive.zip: no extractor
    # the note belongs to both sources, so removing one keeps it
    service.remove_source("Local files")
    assert names(service.search("hóa đơn")) == ["ghi-chu.txt"]


def test_changes_are_picked_up_and_deletions_applied(tmp_path: Path) -> None:
    service, files = local_service(tmp_path)
    service.add_sources(["local:docs"])
    service.sync()
    assert service.sync()["unchanged"] == 2  # nothing read again
    note = files / "docs" / "ghi-chu.txt"
    note.write_text("Ghi chú mới về bảo hiểm xã hội và nhiều hơn nữa", encoding="utf-8")
    assert service.sync()["indexed"] == 1
    assert names(service.search("bảo hiểm")) == ["ghi-chu.txt"]
    note.rename(files / "docs" / "doi-ten.txt")  # a rename is a new ID: read once more
    report = service.sync()
    assert report["indexed"] == 1 and report["removed"] == 1
    assert names(service.search("bảo hiểm")) == ["doi-ten.txt"]
    (files / "docs" / "doi-ten.txt").unlink()
    service.sync()
    assert service.search("bảo hiểm")["results"] == []


def test_an_unavailable_local_folder_deletes_nothing(tmp_path: Path) -> None:
    service, files = local_service(tmp_path)
    service.add_sources(["local:docs"])
    service.sync()
    moved = files.with_name("files-away")
    files.rename(moved)  # the volume is gone, e.g. not mounted after a reboot
    report = service.sync()
    assert report["sources"][0]["ok"] is False
    assert service.list_sources()[0]["status"] == "error"
    assert names(service.search("hóa đơn")) == ["ghi-chu.txt"]  # still searchable
    moved.rename(files)
    service.sync()
    assert service.list_sources()[0]["status"] == "ok"


def test_paths_outside_the_local_folder_are_refused(tmp_path: Path) -> None:
    service, files = local_service(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("zebraquartz", encoding="utf-8")
    (files / "docs" / "link-out").symlink_to(outside, target_is_directory=True)
    (files / "docs" / "link-file.txt").symlink_to(outside / "secret.txt")
    admin = Admin(service)
    for link in ("local:link-out", "local:docs/link-out", "local:docs/link-file.txt"):
        service.add_sources(["local:docs"])
        refused = admin.service.add_sources([link])[0]
        if refused["ok"]:  # a link that stays inside is fine; these all leave the folder
            admin.service.discard_source(refused["id"])
            pytest.fail(f"{link} was accepted")
    assert service.add_sources(["local:../outside"])[0]["ok"] is False
    service.sync()
    assert service.search("zebraquartz")["results"] == []  # symlinks are never followed


def test_add_source_tool_rules_for_local(tmp_path: Path) -> None:
    service, _ = local_service(tmp_path)
    admin = Admin(service)  # no secrets folder: sources need none
    added = admin.add_source("local:docs")
    assert added["applied"] and added["source"]["name"] == "docs"
    assert "already added" in admin.add_source("local:/docs/")["message"]
    with pytest.raises(AdminRefused, match="no such folder or file"):
        admin.add_source("local:nope")
    assert len(service.list_sources()) == 1  # nothing kept for the refused path
    with pytest.raises(AdminRefused, match="could not add"):
        admin.add_source("local:../x")
    bare = make_service(tmp_path / "bare", FakeDrive())
    bare.backend_factory = lambda _: RoutingBackend(lambda: FakeDrive(), None)  # no GDRAG_LOCAL_DIR
    with pytest.raises(AdminRefused, match="GDRAG_LOCAL_DIR"):
        Admin(bare).add_source("local:docs")


def test_local_and_drive_sources_live_together(tmp_path: Path) -> None:
    drive = FakeDrive()
    drive.add_root(ROOT, "Kế toán")
    drive.put(ROOT, "fileDrive0000001", "drive.txt", "Báo cáo hóa đơn trên Drive".encode())
    service, _ = local_service(tmp_path, drive)
    service.add_sources([ROOT, "local:docs"])
    assert service.sync()["failed"] == 0
    assert sorted(names(service.search("hóa đơn"))) == ["drive.txt", "ghi-chu.txt"]
    assert names(service.search("hóa đơn", source="docs")) == ["ghi-chu.txt"]
    service.remove_source("docs")
    assert names(service.search("hóa đơn")) == ["drive.txt"]


# -- exclusions -------------------------------------------------------------------------------
def test_excluding_a_file_and_a_folder_by_id(tmp_path: Path) -> None:
    service, _ = local_service(tmp_path)
    service.add_sources(["local:"])
    service.sync()
    assert sorted(names(service.search("nhà hóa đơn doanh thu"))) == [
        "ghi-chu.txt",
        "root.txt",
        "thue-nha.md",
    ]
    result = service.set_exclusions(["local:docs/hop-dong", "local:root.txt"], [])
    assert [e["kind"] for e in result["added"]] == ["folder", "file"]
    assert result["added"][0]["documents_hidden"] == 1
    assert names(service.search("nhà hóa đơn doanh thu")) == ["ghi-chu.txt"]
    # still indexed and readable by ID; naming the file in `files` finds it again
    assert service.read("local:root.txt")["chunks"]
    assert names(service.search("doanh thu", files=["local:root.txt"])) == ["root.txt"]
    again = service.set_exclusions(["local:root.txt"], ["local:docs/hop-dong", "local:never"])
    assert again["already_excluded"] == ["local:root.txt"]
    assert again["removed"] == ["local:docs/hop-dong"] and again["not_excluded"] == ["local:never"]
    assert sorted(names(service.search("nhà hóa đơn doanh thu"))) == ["ghi-chu.txt", "thue-nha.md"]
    assert [e["id"] for e in service.exclusions()["exclusions"]] == ["local:root.txt"]


def test_exclusion_works_for_drive_ids_before_the_first_sync(tmp_path: Path) -> None:
    drive = FakeDrive()
    drive.add_root(ROOT, "Kế toán")
    drive.add_folder("folderOld0000001", "cu", ROOT)
    drive.put(ROOT, "fileNew000000001", "moi.txt", "Báo cáo doanh thu mới".encode())
    drive.put(
        ROOT,
        "fileOld000000001",
        "cu.txt",
        "Báo cáo doanh thu cũ".encode(),
        parent="folderOld0000001",
    )
    service = make_service(tmp_path, drive, FakeEmbeddings())
    pending = service.set_exclusions(
        ["https://drive.google.com/drive/folders/folderOld0000001"], []
    )
    assert pending["added"][0]["kind"] == "not_indexed_yet"  # a typo would look the same
    service.add_sources([ROOT])
    service.sync()
    assert names(service.search("doanh thu")) == ["moi.txt"]
    listed = service.exclusions()["exclusions"][0]
    assert listed["kind"] == "folder" and listed["name"] == "cu" and listed["documents_hidden"] == 1
    service.set_exclusions([], ["folderOld0000001"])
    assert sorted(names(service.search("doanh thu"))) == ["cu.txt", "moi.txt"]


def test_exclusion_limits_and_bad_ids(tmp_path: Path) -> None:
    service, _ = local_service(tmp_path)
    with pytest.raises(ValueError):
        service.set_exclusions(["not an id"], [])
    with pytest.raises(ValueError, match="at most 50"):
        service.set_exclusions([f"local:f{i}" for i in range(51)], [])
    for chunk in range(4):
        service.set_exclusions([f"local:f{chunk}-{i}" for i in range(50)], [])
    with pytest.raises(ValueError, match="at most 200"):
        service.set_exclusions(["local:one-more"], [])


# -- MCP: no admin switch needed ---------------------------------------------------------------
def test_source_and_exclusion_tools_over_mcp(env: Env, tmp_path: Path) -> None:
    from test_mcp_http import call

    files = tree(tmp_path)
    from gdrive_rag.local import LocalBackend, RoutingBackend

    routed = RoutingBackend(lambda: env.drive, LocalBackend(files))
    env.service.backend_factory = lambda _: routed
    added: Any = env.tool("add_source", link="local:docs")
    assert added["source"]["name"] == "docs"
    wait_idle(env.service)
    tools = {t.name: t for t in call(env.url, "tools/list").tools}
    assert tools["set_exclusions"].annotations.read_only_hint is False
    assert tools["list_exclusions"].annotations.read_only_hint is True
    first = call(env.url, "set_exclusions", {"add": ["local:docs/hop-dong"]})
    assert not first.is_error and first.structured_content["total"] == 1
    listed = call(env.url, "list_exclusions", {})
    assert listed.structured_content["exclusions"][0]["documents_hidden"] == 1
    found = call(env.url, "search", {"query": "thuê nhà"})
    assert found.structured_content["results"] == []
    bad = call(env.url, "set_exclusions", {"add": ["nope nope"]})
    assert bad.is_error
    call(env.url, "set_exclusions", {"remove": ["local:docs/hop-dong"]})
    assert call(env.url, "search", {"query": "thuê nhà"}).structured_content["results"]
