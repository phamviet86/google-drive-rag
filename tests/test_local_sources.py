"""Local folder sources: confinement to GDRAG_LOCAL_ROOT, sync/search/read/folder scope,
deletion safety, independence from Google access, the always-on source tools and the CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import anyio
import pytest
from helpers import FakeDrive, FakeEmbeddings, make_service

from gdrive_rag.admin import ADMIN_ENV, Admin, AdminRefused
from gdrive_rag.cli import main
from gdrive_rag.config import load_settings
from gdrive_rag.drive import Download, DriveItem, DriveSourceError
from gdrive_rag.local import LocalBackend, local_mime
from gdrive_rag.service import Service

DRIVE_ROOT = "folderDriveRoot000"
DRIVE_FILE = "fileDriveDoc00000"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GDRAG_LOCAL_ROOT", "GDRAG_DRIVE_LINKS", "GDRAG_DATA_DIR", ADMIN_ENV):
        monkeypatch.delenv(name, raising=False)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def tree(root: Path) -> Path:
    """local/ hợp-đồng/2025/thue.txt, hợp-đồng/mau.md, ghi-chu.txt, plus things never indexed."""
    write(root / "hop-dong" / "2025" / "thue.txt", "Hợp đồng thuê kho năm 2025")
    write(root / "hop-dong" / "mau.md", "# Mẫu hợp đồng\n\nĐiều khoản thanh toán")
    write(root / "ghi-chu.txt", "Ghi chú họp giao ban")
    write(root / "bang.xyz", "unsupported type")
    write(root / ".an" / "bi-mat.txt", "hidden folder text")
    write(root / ".bi-mat.txt", "hidden file text")
    write(root / "~$khoa.docx", "office lock file")
    return root


def local_service(
    tmp_path: Path,
    drive: FakeDrive | None = None,
    embeddings: FakeEmbeddings | None = None,
    **settings: Any,
) -> tuple[Path, Service]:
    root = tree(tmp_path / "local")
    return root, make_service(tmp_path / "data", drive, embeddings, local_root=root, **settings)


def names(service: Service, query: str, **scope: Any) -> set[str]:
    return {hit["file_name"] for hit in service.search(query, limit=50, **scope)["results"]}


def touch_later(path: Path, text: str) -> None:
    stat = path.stat()
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


# -- adding -----------------------------------------------------------------------------------
def test_add_inside_root_by_id_or_absolute_path(tmp_path: Path) -> None:
    root, service = local_service(tmp_path)
    [folder] = service.add_sources(["local:hop-dong"])
    assert folder["ok"] and folder["kind"] == "local_folder" and folder["name"] == "hop-dong"
    assert folder["root_id"] == folder["link"] == "local:hop-dong"
    [same] = service.add_sources([str(root / "hop-dong") + "/"])
    assert same["already_added"] and same["id"] == folder["id"]
    [again] = service.add_sources(["local:/hop-dong/./"])
    assert again["already_added"]
    [file] = service.add_sources([str(root / "ghi-chu.txt")])
    assert file["kind"] == "local_file" and file["root_id"] == "local:ghi-chu.txt"
    [whole] = service.add_sources(["local:"])
    assert whole["root_id"] == "local:." and whole["name"] == "local"
    # the folder root is known at once, so a folder-scoped search works during the first sync
    listing = service.list_folders(parent="local:hop-dong")
    assert listing["parent"]["drive_link"] == "local:hop-dong"


def test_add_refuses_outside_root_and_never_echoes(tmp_path: Path) -> None:
    root, service = local_service(tmp_path)
    secret = write(tmp_path / "outside" / "secret-name.txt", "outside the root")
    (root / "link-out").symlink_to(secret.parent)
    (root / "file-out.txt").symlink_to(secret)
    admin = Admin(service)
    for value in (
        str(secret),
        "local:../outside/secret-name.txt",
        "local:link-out",
        "local:link-out/secret-name.txt",
        "local:file-out.txt",
        "/etc",
        "local:missing-folder",
        str(root / "missing-folder"),
    ):
        with pytest.raises(AdminRefused) as refused:
            admin.add_source(value)
        message = str(refused.value)
        assert "no such file or folder inside the local folder" in message
        assert "secret-name" not in message and value not in message
    with pytest.raises(AdminRefused, match="hidden"):
        admin.add_source("local:.an")
    assert service.list_sources() == []


def test_symlink_inside_root_is_resolved_to_its_target(tmp_path: Path) -> None:
    root, service = local_service(tmp_path)
    (root / "lien-ket").symlink_to(root / "hop-dong")
    [added] = service.add_sources(["local:lien-ket"])
    assert added["root_id"] == "local:hop-dong"  # one canonical ID, no alias source


def test_unset_root_gives_a_clear_error(tmp_path: Path) -> None:
    service = make_service(tmp_path / "data")
    [result] = service.add_sources(["local:hop-dong"])
    assert result["ok"] is False and "GDRAG_LOCAL_ROOT is not set" in result["error"]
    with pytest.raises(AdminRefused, match="GDRAG_LOCAL_ROOT"):
        Admin(service).add_source("/local/hop-dong")
    assert service.list_sources() == []
    assert service.status()["local_folder"]["configured"] is False


def test_local_root_comes_from_the_environment_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"local_root": "/etc"}))
    assert load_settings(data).local_root is None
    monkeypatch.setenv("GDRAG_LOCAL_ROOT", str(tmp_path / "local"))
    assert load_settings(data).local_root == tmp_path / "local"
    from gdrive_rag.config import save_config

    with pytest.raises(ValueError, match="unknown config keys"):
        save_config(data, {"local_root": "/etc"})


# -- sync, search, read, folders ---------------------------------------------------------------
def test_sync_search_read_and_folder_scope(tmp_path: Path) -> None:
    root, service = local_service(tmp_path)
    (root / "lien-ket.txt").symlink_to(root / "ghi-chu.txt")  # a link to a file inside
    (root / "ra-ngoai.txt").symlink_to(write(tmp_path / "x" / "o.txt", "outside text"))
    (root / "thu-muc-lien-ket").symlink_to(root / "hop-dong")
    service.add_sources(["local:."])
    report = service.sync()
    assert report["failed"] == 0 and report["indexed"] == 4, report
    assert names(service, "đồng") == {"thue.txt", "mau.md"}
    assert names(service, "dong", folder="local:hop-dong/2025") == {"thue.txt"}
    assert names(service, "giao ban") == {"ghi-chu.txt", "lien-ket.txt"}
    assert names(service, "giao ban", files=["local:ghi-chu.txt"]) == {"ghi-chu.txt"}
    assert names(service, "outside text") == set()
    assert names(service, "hidden") == set()
    hit = service.search("thuê kho")["results"][0]
    assert hit["document_id"] == "local:hop-dong/2025/thue.txt"
    assert hit["drive_link"] == hit["document_id"]
    assert hit["path"] == "hop-dong/2025/thue.txt"
    assert hit["parent_folder_id"] == "local:hop-dong/2025"
    read = service.read(hit["document_id"])
    assert read["chunks"][0]["text"] == "Hợp đồng thuê kho năm 2025"
    assert service.read("local:/hop-dong/2025/thue.txt")["file_name"] == "thue.txt"
    top = service.list_folders()
    assert [f["folder_id"] for f in top["folders"]] == ["local:."]
    inner = service.list_folders(parent="local:hop-dong", include_documents=True)
    assert [f["name"] for f in inner["folders"]] == ["2025"]
    assert [d["document_id"] for d in inner["documents"]] == ["local:hop-dong/mau.md"]
    skipped = {f["path"]: f["reason"] for f in service.skipped_files()["files"]}
    assert set(skipped) == {"bang.xyz", "ra-ngoai.txt", "thu-muc-lien-ket"}
    assert "outside" in skipped["ra-ngoai.txt"] and "folder" in skipped["thu-muc-lien-ket"]
    status = service.status()
    assert status["local_folder"] == {"configured": True, "ok": True, "error": None}
    assert status["sources"][0]["first_sync_done"] is True
    assert not list((tmp_path / "data" / "tmp").iterdir())  # extraction copies cleaned up


def test_modify_delete_and_rename_without_reembedding(tmp_path: Path) -> None:
    embeddings = FakeEmbeddings()
    root, service = local_service(tmp_path, embeddings=embeddings)
    service.add_sources(["local:."])
    service.sync()
    embedded = len(embeddings.embedded)
    assert service.sync()["unchanged"] == 3  # nothing changed: nothing read again

    touch_later(root / "ghi-chu.txt", "Ghi chú họp giao ban tháng sáu")
    report = service.sync()
    assert report["indexed"] == 1 and report["unchanged"] == 2
    assert service.search("tháng sáu")["results"][0]["file_name"] == "ghi-chu.txt"

    (root / "hop-dong" / "mau.md").unlink()
    assert service.sync()["removed"] == 1
    with pytest.raises(ValueError):
        service.read("local:hop-dong/mau.md")

    embedded = len(embeddings.embedded)
    (root / "hop-dong").rename(root / "hop-dong-cu")  # a folder rename: new IDs below it
    report = service.sync()
    assert report["indexed"] == 1 and report["removed"] == 1
    hit = service.search("thuê kho")["results"][0]
    assert hit["document_id"] == "local:hop-dong-cu/2025/thue.txt"
    assert hit["path"] == "hop-dong-cu/2025/thue.txt"
    assert len(embeddings.embedded) == embedded  # same chunk text: the vectors are reused
    with pytest.raises(ValueError):
        service.read("local:hop-dong/2025/thue.txt")


def test_empty_or_missing_root_never_deletes(tmp_path: Path) -> None:
    root, service = local_service(tmp_path)
    service.add_sources(["local:.", "local:hop-dong"])
    service.sync()
    before = service.status()["documents"]["indexed"]
    assert before == 3

    moved = tmp_path / "moved"
    root.rename(moved)  # the mount is gone
    report = service.sync()
    assert report["removed"] == 0
    assert all(not s["ok"] and "missing" in s["error"] for s in report["sources"])
    assert service.status()["documents"]["indexed"] == before
    assert service.status()["local_folder"]["ok"] is False

    root.mkdir()  # an empty folder where the mount should be (Docker creates one)
    write(root / ".DS_Store", "only hidden entries")
    report = service.sync()
    assert all("empty" in s["error"] for s in report["sources"])
    assert service.status()["documents"]["indexed"] == before
    assert names(service, "thuê kho", folder="local:hop-dong/2025") == {"thue.txt"}

    (root / ".DS_Store").unlink()
    root.rmdir()
    moved.rename(root)
    report = service.sync()
    assert all(s["ok"] for s in report["sources"]) and report["removed"] == 0


def test_missing_source_path_clears_that_source(tmp_path: Path) -> None:
    root, service = local_service(tmp_path)
    service.add_sources(["local:hop-dong", "local:ghi-chu.txt"])
    service.sync()
    assert service.status()["documents"]["indexed"] == 3
    for path in sorted((root / "hop-dong").rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    (root / "hop-dong").rmdir()
    report = service.sync()
    by_name = {s["name"]: s for s in report["sources"]}
    assert by_name["hop-dong"]["ok"] is False
    assert "no longer exists" in by_name["hop-dong"]["error"]
    assert report["removed"] == 2
    assert names(service, "ghi chú") == {"ghi-chu.txt"}


def test_unreadable_file_is_a_per_file_download_error(tmp_path: Path) -> None:
    class Vanishing(LocalBackend):
        def download(self, item: DriveItem) -> Download:
            if item.id == "local:ghi-chu.txt":
                (Path(self.configured) / "ghi-chu.txt").unlink()  # gone between list and read
            return super().download(item)

    root = tree(tmp_path / "local")
    service = make_service(tmp_path / "data", local_root=root)
    service.local_factory = lambda settings: Vanishing(root)
    service.add_sources(["local:."])
    report = service.sync()
    assert report["failed"] == 1 and report["indexed"] == 2 and "stopped" not in report
    failure = service.status()["recent_failures"][0]
    assert failure["error"].startswith("download: ") and failure["path"] == "ghi-chu.txt"


# -- independence from Google -----------------------------------------------------------------
def drive_tree() -> FakeDrive:
    drive = FakeDrive()
    drive.add_root(DRIVE_ROOT, "Drive công ty")
    drive.put(DRIVE_ROOT, DRIVE_FILE, "bao-cao.txt", "Báo cáo doanh thu quý".encode())
    drive.put(DRIVE_ROOT, "fileDriveOld00000", "cu.txt", "Tài liệu cũ".encode())
    return drive


def test_local_sources_sync_without_google_access(tmp_path: Path) -> None:
    _, service = local_service(tmp_path)  # Google access not configured: its factory raises
    service.add_sources(["local:hop-dong"])
    report = service.sync()
    assert report["indexed"] == 2 and all(s["ok"] for s in report["sources"])
    assert names(service, "đồng") == {"thue.txt", "mau.md"}
    [drive] = service.add_sources([DRIVE_ROOT])  # stored pending, as before
    report = service.sync()
    by_name = {s["name"]: s for s in report["sources"]}
    assert by_name["hop-dong"]["ok"] and not by_name[DRIVE_ROOT]["ok"]
    assert "not configured" in by_name[DRIVE_ROOT]["error"] and drive["kind"] == "unknown"
    assert names(service, "đồng") == {"thue.txt", "mau.md"}


def test_google_failure_does_not_stop_local_and_the_other_way_round(tmp_path: Path) -> None:
    drive = drive_tree()
    root, service = local_service(tmp_path, drive)
    service.add_sources([DRIVE_ROOT, "local:."])
    service.sync()
    assert names(service, "báo cáo doanh thu") == {"bao-cao.txt"}
    assert service.status()["documents"]["indexed"] == 5

    # Google fails (network): Drive documents stay, local changes still apply
    drive.root_errors[DRIVE_ROOT] = DriveSourceError("Drive connection failed: TimeoutError")
    drive.delete(DRIVE_ROOT, "fileDriveOld00000")
    (root / "ghi-chu.txt").unlink()
    report = service.sync()
    assert report["removed"] == 1  # the local file only
    assert names(service, "tài liệu cũ") == {"cu.txt"}
    del drive.root_errors[DRIVE_ROOT]

    # a Drive download that loses the sign-in stops Drive only; local files are still read
    touch_later(root / "hop-dong" / "mau.md", "# Mẫu mới\n\nĐiều khoản phạt")
    drive.put(DRIVE_ROOT, DRIVE_FILE, "bao-cao.txt", "Báo cáo mới".encode(), version="2")
    drive.download_errors[DRIVE_FILE] = DriveSourceError("Google sign-in was rejected (HTTP 401)")
    report = service.sync()
    assert "HTTP 401" in report["stopped"]
    assert names(service, "điều khoản phạt") == {"mau.md"}
    assert names(service, "tài liệu cũ") == {"cu.txt"}  # Drive membership kept: pass stopped
    del drive.download_errors[DRIVE_FILE]

    # the local mount goes away: local documents stay, Drive changes still apply
    moved = tmp_path / "moved"
    root.rename(moved)
    report = service.sync()
    assert report["removed"] == 1 and names(service, "tài liệu cũ") == set()
    assert names(service, "thuê kho") == {"thue.txt"} and names(service, "phạt") == {"mau.md"}
    assert service.read(DRIVE_FILE)["chunks"][0]["text"] == "Báo cáo mới"


# -- tools and CLI ---------------------------------------------------------------------------
async def tool_names(server: Any) -> set[str]:
    return {tool.name for tool in await server.list_tools()}


def test_source_tools_are_registered_with_the_switch_off(tmp_path: Path) -> None:
    from gdrive_rag.server import create_server

    _, service = local_service(tmp_path)
    server = create_server(service)  # GDRAG_ADMIN_TOOLS off: no Admin given
    found = anyio.run(tool_names, server)
    assert {"add_source", "remove_source", "set_path_rules"} <= found
    assert not found & {"set_embedding", "google_auth_start", "remove_google_auth"}
    with_admin = anyio.run(tool_names, create_server(service, Admin(service)))
    assert {"set_embedding", "google_auth_start", "remove_google_auth"} <= with_admin

    async def call(name: str, arguments: dict[str, Any]) -> Any:
        return await server.call_tool(name, arguments)

    added = anyio.run(call, "add_source", {"link": "local:hop-dong"})
    assert not added.is_error and added.structured_content["applied"] is True
    assert added.structured_content["sync"]["queued"] is True
    assert service.list_sources()[0]["root_id"] == "local:hop-dong"
    assert service.take_request() is not None  # a sync is queued for the loop
    outside = str(tmp_path / "x")
    refused = anyio.run(call, "add_source", {"link": outside})
    assert refused.is_error and outside not in refused.content[0].text
    assert "no such file or folder inside the local folder" in refused.content[0].text
    rules = anyio.run(call, "set_path_rules", {"source": "hop-dong", "demote": ["2025/**"]})
    assert rules.structured_content["confirm_needed"] is True
    preview = anyio.run(call, "remove_source", {"source": "local:hop-dong"})
    assert "local files are not changed" in preview.structured_content["would"]
    assert service.list_sources()
    not_bool = anyio.run(call, "remove_source", {"source": "local:hop-dong", "confirm": "yes"})
    assert not_bool.is_error and service.list_sources()
    anyio.run(call, "remove_source", {"source": "local:hop-dong", "confirm": True})
    assert service.list_sources() == []


def test_remove_by_absolute_path_and_env_listed_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, service = local_service(tmp_path)
    service.add_sources(["local:hop-dong", "local:ghi-chu.txt"])
    service.sync()
    admin = Admin(service)
    monkeypatch.setenv("GDRAG_DRIVE_LINKS", "local:ghi-chu.txt")
    with pytest.raises(AdminRefused, match="GDRAG_DRIVE_LINKS"):
        admin.remove_source(str(root / "ghi-chu.txt"), True)
    preview = admin.remove_source(str(root / "hop-dong"), False)
    assert preview["documents_removed"] == 2 and preview["applied"] is False
    assert admin.remove_source(str(root / "hop-dong"), True)["documents_removed"] == 2
    assert [s["root_id"] for s in service.list_sources()] == ["local:ghi-chu.txt"]


def test_cli_source_add_goes_through_admin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tree(tmp_path / "local")
    data = str(tmp_path / "data")
    monkeypatch.setenv("GDRAG_LOCAL_ROOT", str(root))
    monkeypatch.delenv("GDRAG_GOOGLE_AUTH", raising=False)
    code = main(["--data-dir", data, "--json", "source", "add", "local:hop-dong", "/etc"])
    assert code == 2
    output = json.loads(capsys.readouterr().out)
    assert output[0]["ok"] is True and output[0]["source"]["kind"] == "local_folder"
    assert "gdrive-rag sync" in output[0]["message"] and "sync" not in output[0]
    assert output[1]["ok"] is False and "inside the local folder" in output[1]["error"]
    # a Drive link Google cannot confirm (no access set up) is no longer stored
    assert main(["--data-dir", data, "--json", "source", "add", DRIVE_ROOT]) == 2
    assert "did not confirm" in json.loads(capsys.readouterr().out)[0]["error"]
    assert main(["--data-dir", data, "--json", "sync"]) == 0
    assert json.loads(capsys.readouterr().out)["indexed"] == 2
    search = ["search", "thuê kho", "--folder", "local:hop-dong"]
    assert main(["--data-dir", data, "--json", *search]) == 0
    assert json.loads(capsys.readouterr().out)["results"][0]["file_name"] == "thue.txt"
    assert main(["--data-dir", data, "--json", "source", "remove", "local:hop-dong"]) == 0
    capsys.readouterr()
    assert main(["--data-dir", data, "--json", "source", "list"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_local_mime_is_deterministic() -> None:
    assert local_mime("a.PDF") == "application/pdf"
    assert local_mime("a.md") == "text/markdown"
    assert local_mime("a.markdown") == "text/markdown"
    assert local_mime("a.docx").endswith("wordprocessingml.document")
    assert local_mime("a.xlsb") == "application/vnd.ms-excel.sheet.binary.macroenabled.12"
    assert local_mime("a.unknownext") == "application/octet-stream"
