from pathlib import Path

from helpers import FakeDrive, make_service

from gdrive_rag.drive import DriveSourceError

ROOT_A = "folderAAAAAAAAAAAA"
ROOT_B = "folderBBBBBBBBBBBB"


def names(service, query="hello"):
    return sorted({r["file_name"] for r in service.search(query)["results"]})


def setup_two_sources(tmp_path: Path):
    drive = FakeDrive()
    drive.add_root(ROOT_A, "Phòng A")
    drive.add_root(ROOT_B, "Phòng B")
    drive.put(ROOT_A, "file1aaaaaaaaaa", "a.txt", b"hello from a")
    drive.put(ROOT_A, "shared11111111", "shared.txt", b"hello shared")
    drive.put(ROOT_B, "shared11111111", "shared.txt", b"hello shared")
    service = make_service(tmp_path, drive)
    added = service.add_sources([f"https://drive.google.com/drive/folders/{ROOT_A}", ROOT_B])
    assert [a["name"] for a in added] == ["Phòng A", "Phòng B"]
    return drive, service


def test_add_update_delete(tmp_path: Path) -> None:
    drive, service = setup_two_sources(tmp_path)
    report = service.sync()
    assert report["indexed"] == 2 and report["removed"] == 0
    assert names(service) == ["a.txt", "shared.txt"]
    # unchanged files are not downloaded again
    drive.downloads.clear()
    assert service.sync()["unchanged"] == 2 and drive.downloads == []
    # update
    drive.put(ROOT_A, "file1aaaaaaaaaa", "a.txt", b"goodbye from a", version="2")
    service.sync()
    assert names(service, "goodbye") == ["a.txt"]
    assert names(service, "hello") == ["shared.txt"]
    # delete
    drive.delete(ROOT_A, "file1aaaaaaaaaa")
    assert service.sync()["removed"] == 1
    assert names(service, "goodbye") == []


def test_overlapping_sources_deduplicate_and_survive_removal(tmp_path: Path) -> None:
    drive, service = setup_two_sources(tmp_path)
    service.sync()
    assert drive.downloads.count("shared11111111") == 1
    results = service.search("shared")["results"]
    assert len(results) == 1 and results[0]["sources"] in ("Phòng A, Phòng B", "Phòng B, Phòng A")
    assert service.search("shared", source="Phòng B")["results"]
    assert not service.search("from", source="Phòng B")["results"]
    # file deleted from A only: still reachable through B
    drive.delete(ROOT_A, "shared11111111")
    service.sync()
    assert names(service, "shared") == ["shared.txt"]
    # removing source B drops the file that only B still had
    assert service.remove_source("Phòng B")["documents_removed"] == 1
    assert names(service) == ["a.txt"]


def test_inaccessible_link_is_per_source(tmp_path: Path) -> None:
    drive, service = setup_two_sources(tmp_path)
    added = service.add_sources(["https://drive.google.com/drive/folders/missingCCCCCCCC"])
    assert added[0]["ok"] is False and "not shared" in added[0]["error"]
    report = service.sync()
    assert report["indexed"] == 2
    errors = {s["name"]: s["error"] for s in service.list_sources() if s["status"] == "error"}
    assert list(errors) == ["missingCCCCCCCC"]


def test_root_gone_removes_its_documents(tmp_path: Path) -> None:
    drive, service = setup_two_sources(tmp_path)
    service.sync()
    del drive.roots[ROOT_A]
    service.sync()
    assert names(service) == ["shared.txt"]


def test_auth_failure_keeps_index(tmp_path: Path) -> None:
    drive, service = setup_two_sources(tmp_path)
    service.sync()
    drive.root_errors[ROOT_A] = DriveSourceError("Google sign-in expired")
    drive.root_errors[ROOT_B] = DriveSourceError("Google sign-in expired")
    service.sync()
    assert names(service) == ["a.txt", "shared.txt"]
    assert {s["status"] for s in service.list_sources()} == {"error"}

    def broken(_):
        raise DriveSourceError("Google sign-in expired or was revoked")

    service.backend_factory = broken
    service.sync()
    assert names(service) == ["a.txt", "shared.txt"]


def test_incomplete_listing_never_deletes(tmp_path: Path) -> None:
    drive, service = setup_two_sources(tmp_path)
    service.sync()
    drive.incomplete.add(ROOT_A)
    service.sync()
    assert names(service) == ["a.txt", "shared.txt"]
    assert service.list_sources()[0]["status"] == "partial"


def test_add_without_google_access_is_pending(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    added = service.add_sources([ROOT_A])
    assert added[0]["status"] == "pending" and "not configured" in added[0]["error"]
    assert service.add_sources([ROOT_A])[0]["already_added"] is True
    report = service.sync()
    assert report["sources"][0]["ok"] is False
    status = service.status()
    assert status["documents"]["total"] == 0


def test_extraction_failure_is_per_file(tmp_path: Path) -> None:
    drive, service = setup_two_sources(tmp_path)
    drive.put(
        ROOT_A,
        "badxlsx11111111",
        "broken.xlsx",
        b"not a zip",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    report = service.sync()
    assert report["indexed"] == 2 and report["failed"] == 1
    status = service.status()
    assert status["documents"]["failed"] == 1
    assert status["recent_failures"][0]["file_name"] == "broken.xlsx"
    assert names(service) == ["a.txt", "shared.txt"]


def test_sync_running_never_takes_the_lock(tmp_path: Path, monkeypatch) -> None:
    import fcntl
    import os

    drive, service = setup_two_sources(tmp_path)
    calls: list[int] = []
    real = fcntl.flock
    monkeypatch.setattr(fcntl, "flock", lambda fd, op: (calls.append(op), real(fd, op))[1])
    assert service.sync_running() is False and service.status()["sync"]["running"] is False
    assert calls == []
    with service._sync_lock():
        assert service.sync_running() is True
    assert service.sync_running() is False
    (tmp_path / "sync.running").write_text(str(2**22 + os.getpid()))  # no such process
    assert service.sync_running() is False
