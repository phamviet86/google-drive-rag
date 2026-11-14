from pathlib import Path

from helpers import FakeDrive, make_service

from gdrive_rag.drive import DriveSourceError
from gdrive_rag.extract import Extractor

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


def test_old_ocr_text_is_re_extracted_once_and_text_files_are_left_alone(tmp_path: Path) -> None:
    from helpers import FakeEmbeddings, FakeOCR, make_scanned_pdf

    drive = FakeDrive()
    drive.add_root(ROOT_A, "Phòng A")
    drive.put(ROOT_A, "fileText00000000", "a.txt", b"hello from a")
    drive.put(
        ROOT_A, "fileScan00000000", "scan.pdf", make_scanned_pdf(None), mime="application/pdf"
    )
    embeddings = FakeEmbeddings()
    old_ocr = FakeOCR("HP DONG cu", name="pp-ocrv6")
    service = make_service(tmp_path, drive, embeddings, ocr=old_ocr)
    service.add_sources([ROOT_A])
    service.sync()
    with service.db.transaction() as connection:  # data written at ced9af1 has no ocr_engine
        connection.execute(
            "UPDATE documents SET metadata_json=json_remove(metadata_json, '$.ocr_engine')"
        )
    embedded = list(embeddings.embedded)
    new_ocr = FakeOCR("Hợp đồng thuê mặt bằng")
    service.extractor_factory = lambda s: Extractor(new_ocr, ocr_enabled=True, work_dir=s.work_dir)
    drive.downloads.clear()
    report = service.sync()
    assert drive.downloads == ["fileScan00000000"]  # only the OCR'd file is fetched again
    assert report["indexed"] == 1 and report["unchanged"] == 1
    assert embeddings.embedded[len(embedded) :] == ["Hợp đồng thuê mặt bằng"]
    assert service.search("hợp đồng")["results"][0]["file_name"] == "scan.pdf"
    drive.downloads.clear()
    assert service.sync()["unchanged"] == 2 and drive.downloads == []


def test_scan_indexed_without_ocr_is_retried_when_ocr_becomes_available(tmp_path: Path) -> None:
    from helpers import FakeOCR, make_scanned_pdf

    drive = FakeDrive()
    drive.add_root(ROOT_A, "Phòng A")
    drive.put(
        ROOT_A, "fileScan00000000", "scan.pdf", make_scanned_pdf(None), mime="application/pdf"
    )
    ocr = FakeOCR("Biên bản nghiệm thu")
    ocr.ready = False  # tesseract not installed yet
    service = make_service(tmp_path, drive, ocr=ocr)
    service.add_sources([ROOT_A])
    service.sync()
    assert service.status()["documents"]["ocr_unavailable"] == 1
    drive.downloads.clear()
    service.sync()
    assert drive.downloads == []  # still unavailable: no pointless re-extraction
    ocr.ready = True
    service.sync()
    assert drive.downloads == ["fileScan00000000"]
    assert service.status()["documents"]["ocr_unavailable"] == 0
    assert names(service, "nghiệm thu") == ["scan.pdf"]


def test_ocr_languages_setting(tmp_path: Path, monkeypatch) -> None:
    import pytest

    from gdrive_rag.config import load_settings

    assert load_settings(tmp_path, use_env=False).ocr_langs == "vie+eng"
    monkeypatch.setenv("GDRAG_OCR_LANGS", " vie + eng + fra ")
    assert load_settings(tmp_path).ocr_langs == "vie+eng+fra"
    monkeypatch.setenv("GDRAG_OCR_LANGS", "vie; rm -rf /")
    with pytest.raises(ValueError, match="ocr_langs"):
        load_settings(tmp_path)


def test_sync_of_one_source_leaves_the_others(tmp_path: Path) -> None:
    drive, service = setup_two_sources(tmp_path)
    service.sync()
    before = {s["name"]: s["last_sync_at"] for s in service.list_sources()}
    drive.put(ROOT_B, "fileB0000000000", "b.txt", b"hello from b")
    import time

    time.sleep(1.1)  # last_sync_at has one-second resolution
    source_b = next(s["id"] for s in service.list_sources() if s["name"] == "Phòng B")
    report = service.sync("requested", source_b)
    assert report["trigger"] == "requested" and len(report["sources"]) == 1
    after = {s["name"]: s["last_sync_at"] for s in service.list_sources()}
    assert after["Phòng A"] == before["Phòng A"] and after["Phòng B"] != before["Phòng B"]
    assert names(service, "from") == ["a.txt", "b.txt"]
    assert service.status()["sync"]["trigger"] == "requested"


def test_scheduled_cycle_is_retried_not_skipped_and_keeps_its_deadline(
    tmp_path: Path, monkeypatch
) -> None:
    import time

    import gdrive_rag.server as server_module
    from gdrive_rag.server import SyncLoop

    drive, service = setup_two_sources(tmp_path)
    monkeypatch.setattr(server_module, "BUSY_RETRY_SECONDS", 0.05)
    triggers: list[str] = []
    real_sync = service.sync

    def recording(trigger: str = "manual", source_id: int | None = None):
        report = real_sync(trigger, source_id)  # SyncBusy propagates and is not recorded
        triggers.append(trigger)
        return report

    monkeypatch.setattr(service, "sync", recording)
    loop = SyncLoop(service, run_at_start=True)
    monkeypatch.setattr(loop, "_next_due", lambda: time.monotonic() + 0.6)
    lock = service._sync_lock()
    lock.__enter__()  # another process (a CLI sync) holds the lock at start
    loop.start()
    try:
        time.sleep(0.3)
        assert service.db.get_meta("last_sync_trigger") is None  # nothing could run yet
        lock.__exit__(None, None, None)
        deadline = time.time() + 5
        while triggers != ["scheduled"]:  # the busy cycle was retried, not skipped
            assert time.time() < deadline, triggers
            time.sleep(0.02)
        first_done = time.monotonic()
        assert service.request_sync()["started"] is True  # an on-demand run in between ...
        while len(triggers) < 3:  # ... and the next scheduled cycle still comes on time
            assert time.time() < deadline, triggers
            time.sleep(0.02)
        assert triggers[:3] == ["scheduled", "requested", "scheduled"]
        assert time.monotonic() - first_done < 1.5
    finally:
        loop.close()
        loop.thread.join(timeout=5)
    assert not loop.thread.is_alive()
