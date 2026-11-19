"""How ``v0_3_0_index.sqlite3`` was made: the v0.3.0 code itself (commit af29add) indexed
synthetic files and stored path rules and skipped files (schema version 4).

Run from an export of that commit (``git archive af29add | tar -x -C /tmp/v030``)::

    python -I tests/fixtures/make_v0_3_0_index.py /tmp/v030 tests/fixtures/v0_3_0_index.sqlite3

``tests/test_upgrade.py`` rebuilds the same fake Drive (``upgrade_drive``) and checks that
migration 5 (the ``ignore`` effect) changes no row and that nothing is downloaded or embedded.
Keep both in step if this file changes.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT, SUB, DEEP = "rootUpgrade03001", "folderAttach0301", "folderDeep000301"
RULES = {"demote": ["attachments/**", "*.bak.txt"], "exclude": ["2025/", "nhap-*.txt"]}


def _long(topic: str, parts: int) -> str:
    """Several numbered paragraphs, long enough for several chunks at the default size."""
    return "\n\n".join(
        f"Mục {n}. {topic} Nội dung chi tiết của mục {n}, ghi rõ số liệu, người phụ trách "
        "và thời hạn hoàn thành. " * 6
        for n in range(1, parts + 1)
    )


FILES = [  # (file id, name, text, parent folder or None for the root)
    ("fileLedger030001", "so-cai.md", _long("Sổ cái doanh thu và đối soát.", 5), None),
    ("filePayroll03001", "luong.txt", "Bảng lương và bảo hiểm xã hội của nhân viên.", None),
    ("fileOldCopy03001", "ban-goc.bak.txt", _long("Bản gốc cũ của sổ cái.", 3), SUB),
    ("fileDeep00030001", "chi-tiet.txt", _long("Chứng từ chi tiết.", 3), DEEP),
    ("fileDraft0030001", "nhap-thang-3.txt", "Bản nháp báo cáo tháng ba.", None),
]
UNSUPPORTED = [  # (file id, name, MIME type, parent): listed, stored in skipped_files
    ("fileZip000030001", "kho.zip", "application/zip", SUB),
    ("fileForm00030001", "khao-sat", "application/vnd.google-apps.form", None),
]


def upgrade_drive(fake_drive: type) -> object:
    drive = fake_drive()
    drive.add_root(ROOT, "Kế toán")
    drive.add_folder(SUB, "attachments", ROOT)
    drive.add_folder(DEEP, "2025", SUB)
    for file_id, name, text, parent in FILES:
        drive.put(ROOT, file_id, name, text.encode(), parent=parent)
    for file_id, name, mime, parent in UNSUPPORTED:
        drive.put(ROOT, file_id, name, b"x", mime=mime, parent=parent)
    return drive


def main(checkout: Path, target: Path) -> None:
    sys.path[:0] = [str(checkout / "src"), str(checkout / "tests")]
    from helpers import FakeDrive, FakeEmbeddings, make_service  # the v0.3.0 helpers

    import gdrive_rag

    assert gdrive_rag.__version__ == "0.3.0", gdrive_rag.__file__
    with tempfile.TemporaryDirectory() as tmp:
        service = make_service(Path(tmp), upgrade_drive(FakeDrive), FakeEmbeddings())
        service.add_sources([ROOT])
        assert service.sync()["indexed"] == len(FILES)
        service.set_path_rules(service.list_sources()[0]["id"], RULES)
        assert service.skipped_files()["total"] == len(UNSUPPORTED)
        connection = sqlite3.connect(str(Path(tmp) / "index.sqlite3"))
        connection.execute("VACUUM INTO ?", (str(target),))
        connection.close()


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
