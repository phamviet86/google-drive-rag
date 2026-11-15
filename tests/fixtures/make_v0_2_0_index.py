"""How ``v0_2_0_index.sqlite3`` was made: the v0.2.0 code itself indexed synthetic files.

Run from a checkout of the ``v0.2.0`` tag (``git worktree add /tmp/v020 v0.2.0``)::

    python -I tests/fixtures/make_v0_2_0_index.py /tmp/v020 tests/fixtures/v0_2_0_index.sqlite3

``tests/test_upgrade.py`` rebuilds the same fake Drive (``upgrade_drive``) and checks that the
upgrade re-extracts and re-embeds nothing. Keep both in step if this file changes.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT, SUB, DEEP = "rootUpgrade00001", "folderAttach0001", "folderDeep000001"


def _long(topic: str, parts: int) -> str:
    """Several numbered paragraphs, long enough for several chunks at the default size."""
    return "\n\n".join(
        f"Mục {n}. {topic} Nội dung chi tiết của mục {n}, ghi rõ số liệu, người phụ trách "
        "và thời hạn hoàn thành. " * 6
        for n in range(1, parts + 1)
    )


FILES = [  # (file id, name, text, parent folder or None for the root)
    ("fileLedger000001", "so-cai.md", _long("Sổ cái doanh thu và đối soát.", 6), None),
    ("filePayroll00001", "luong.txt", "Bảng lương và bảo hiểm xã hội của nhân viên.", None),
    ("fileOldCopy00001", "ban-goc.txt", _long("Bản gốc cũ của sổ cái.", 4), SUB),
    ("fileDeep00000001", "chi-tiet.txt", _long("Chứng từ chi tiết.", 3), DEEP),
]


def upgrade_drive(fake_drive: type) -> object:
    drive = fake_drive()
    drive.add_root(ROOT, "Kế toán")
    drive.add_folder(SUB, "attachments", ROOT)
    drive.add_folder(DEEP, "2025", SUB)
    for file_id, name, text, parent in FILES:
        drive.put(ROOT, file_id, name, text.encode(), parent=parent)
    return drive


def main(checkout: Path, target: Path) -> None:
    sys.path[:0] = [str(checkout / "src"), str(checkout / "tests")]
    from helpers import FakeDrive, FakeEmbeddings, make_service  # the v0.2.0 helpers

    import gdrive_rag

    assert gdrive_rag.__version__ == "0.2.0", gdrive_rag.__file__
    with tempfile.TemporaryDirectory() as tmp:
        service = make_service(Path(tmp), upgrade_drive(FakeDrive), FakeEmbeddings())
        service.add_sources([ROOT])
        assert service.sync()["indexed"] == len(FILES)
        connection = sqlite3.connect(str(Path(tmp) / "index.sqlite3"))
        connection.execute("VACUUM INTO ?", (str(target),))
        connection.close()


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
