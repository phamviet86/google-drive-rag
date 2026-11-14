"""Offline ranking eval: a corpus shaped like the owner's Drive (large spreadsheet chunks full of
dates and numbers, a few short prose chunks) and Vietnamese questions of the kinds people ask.

Each query names the chunk it expects by a marker word in the chunk text. The test records the
rank of that chunk for FTS-only and for hybrid search (with a bag-of-words fake embedding that,
like a real one, finds common words such as "tháng", "là", "bao nhiêu" everywhere) and checks
thresholds. Run ``.venv/bin/python tests/test_ranking_eval.py`` to print the per-query table.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from helpers import FakeDrive, make_service, make_xlsx

from gdrive_rag.service import Service

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ROOT = "rootEval000000000"
LIMIT = 8

MEETING = (
    "Biên bản họp giao ban văn phòng. Thành phần gồm trưởng phòng hành chính, kế toán trưởng và "
    "đại diện các tổ. Nội dung thứ nhất: rà soát kế hoạch tuyển dụng quý một, phòng nhân sự báo "
    "cáo đã nhận hồ sơ và sẽ phỏng vấn trong hai tuần tới. Nội dung thứ hai: thống nhất mẫu báo "
    "cáo tuần, mỗi tổ gửi trước chiều thứ sáu. Nội dung thứ ba: kiểm tra thiết bị phòng cháy, "
    "thay bình chữa cháy hết hạn ở tầng ba. Nội dung thứ tư: chuẩn bị tất niên, dự kiến tổ chức "
    "tại nhà hàng gần văn phòng, mỗi người đóng góp ý kiến về thực đơn. Nội dung thứ năm: nhắc "
    "mọi người tắt điều hoà khi ra về và khoá cửa sổ. Ý kiến khác: đề xuất mua thêm máy in màu "
    "cho phòng thiết kế, giao kế toán xem báo giá. Cuộc họp kết thúc lúc mười một giờ. "
)
PROSE = {
    # the live case: one appended line inside a longer Google Doc chunk
    "GIUXE": MEETING + "Dòng thêm khi sửa file: mã kiểm tra đồng bộ KT-DONGBO-7391, phí giữ xe "
    "tháng mười hai là 120 nghìn đồng.",
    "NGHIPHEP": "Quy định nghỉ phép: mỗi nhân viên chính thức được nghỉ 12 ngày phép có lương "
    "mỗi năm, báo trước ít nhất ba ngày.",
    "VANPHONG": "Hợp đồng thuê văn phòng tại Đà Nẵng hết hạn vào tháng sáu năm 2026, cần gia hạn "
    "trước ba tháng.",
    "WIFI": "Mật khẩu wifi phòng họp tầng hai là HOP-2026-XANH, đổi mỗi quý.",
    "THANGMAY": "Lịch bảo trì thang máy: thứ hai đầu tiên của mỗi tháng, từ 8 giờ đến 10 giờ.",
    "DIENNUOC": "Tiền điện nước tháng mười một là bao nhiêu thì kế toán ghi vào sổ chi phí "
    "chung, hạn nộp ngày 15 hằng tháng.",
}
MONTHS = ["một", "hai", "ba", "tư", "năm", "sáu", "bảy", "tám", "chín", "mười", "mười một"]
STAFF = ["Nguyễn Văn An", "Trần Thị Bình", "Lê Hoàng Cường", "Phạm Thu Dung", "Võ Minh Em"]

# (query, marker of the expected chunk)
QUERIES: list[tuple[str, str]] = [
    ("KT-DONGBO-7391", "GIUXE"),
    ("phí giữ xe", "GIUXE"),
    ("giữ xe bao nhiêu tiền", "GIUXE"),
    ("phí giữ xe tháng mười hai", "GIUXE"),
    ("phí giữ xe tháng mười hai là bao nhiêu", "GIUXE"),
    ("phi giu xe thang muoi hai la bao nhieu", "GIUXE"),
    ("phí giữ xe là bao nhiêu", "GIUXE"),
    ("120 nghìn đồng", "GIUXE"),
    ("mã kiểm tra đồng bộ", "GIUXE"),
    ("nghỉ phép được bao nhiêu ngày một năm", "NGHIPHEP"),
    ("nghi phep bao nhieu ngay", "NGHIPHEP"),
    ("hợp đồng thuê văn phòng hết hạn khi nào", "VANPHONG"),
    ("hop dong thue van phong da nang", "VANPHONG"),
    ("mật khẩu wifi phòng họp là gì", "WIFI"),
    ("HOP-2026-XANH", "WIFI"),
    ("lịch bảo trì thang máy vào ngày nào trong tháng", "THANGMAY"),
    ("tiền điện nước tháng mười một là bao nhiêu", "DIENNUOC"),
    ("doanh thu tháng mười hai năm 2025", "DTTHANG12"),
    ("tổng doanh thu cả năm 2025 là bao nhiêu", "DTTONG"),
    ("lương tháng 12 của Võ Minh Em", "LUONGEM"),
    ("phụ cấp gửi xe tháng 12 của Trần Thị Bình", "PCBINH"),
    # paraphrases: no rare query word is in the expected chunk, only the vectors can find it
    ("tiền đỗ xe một tháng", "GIUXE"),
    ("ngày được vắng mặt có hưởng lương", "NGHIPHEP"),
    # question words that appear in no chunk at all ("gì", "ở đâu") must not dilute the match
    ("mật khẩu wifi ở đâu và gì", "WIFI"),
    ("thang máy bảo trì lúc nào vậy nhỉ", "THANGMAY"),
]


def ledger_rows() -> list[list[Any]]:
    """Revenue ledger: many rows with dates, months and amounts; two rows are query targets."""
    rows: list[list[Any]] = [["Ngày", "Diễn giải", "Số tiền", "Ghi chú"]]
    for month in range(1, 13):
        for day in (3, 9, 15, 21, 27):
            name = MONTHS[month - 1] if month <= 11 else "mười hai"
            rows.append(
                [
                    f"{day:02d}/{month:02d}/2025",
                    f"Phí dịch vụ tháng {name} là {month * day * 1000} đồng",
                    month * day * 10_000,
                    f"Thu tháng {name}, khách số {day}, còn nợ bao nhiêu ghi sau, xe {day}",
                ]
            )
        if month == 12:
            rows.append(["31/12/2025", "DTTHANG12 Doanh thu tháng mười hai năm 2025", 182_000_000])
    rows.append(["", "DTTONG Tổng doanh thu cả năm 2025", 2_150_000_000, "đã kiểm toán"])
    return rows


def payroll_rows() -> list[list[Any]]:
    rows: list[list[Any]] = [["Tháng", "Nhân viên", "Lương", "Phụ cấp gửi xe", "Thực lĩnh"]]
    for month in range(1, 13):
        for person in STAFF:
            marker = ""
            if month == 12 and person == "Võ Minh Em":
                marker = "LUONGEM "
            if month == 12 and person == "Trần Thị Bình":
                marker = "PCBINH "
            rows.append(
                [f"{marker}tháng {month}/2025", person, 12_000_000 + month, 100_000, 12_100_000]
            )
    return rows


def timesheet_rows() -> list[list[Any]]:
    rows: list[list[Any]] = [["Ngày", "Nhân viên", "Giờ vào", "Giờ ra", "Ghi chú"]]
    for day in range(1, 29):
        for person in STAFF[:3]:
            rows.append([f"{day:02d}/12/2025", person, "08:00", "17:30", "tháng mười hai, ca hai"])
    return rows


class EvalEmbeddings:
    """Bag of lowercased words and word pairs hashed into 1024 dims, length-normalised.

    It shares the weakness the live index shows: a long table chunk full of "tháng", "là",
    numbers and dates looks similar to any question that uses those words."""

    model = "eval-bag"
    dimensions = 1024

    @property
    def identity(self) -> str:
        return "eval-bag-1024"

    # a real model maps these to the same meaning; the fake does it with a small synonym table
    SYNONYMS = {"đỗ": "giữ", "gửi": "giữ", "vắng": "nghỉ", "hưởng": "có", "mặt": "phép"}

    def _vector(self, text: str) -> list[float]:
        words = [self.SYNONYMS.get(w, w) for w in re.findall(r"\w+", text.lower())]
        values = [0.0] * self.dimensions
        for feature in [*words, *(f"{a} {b}" for a, b in zip(words, words[1:], strict=False))]:
            values[int(hashlib.md5(feature.encode()).hexdigest(), 16) % self.dimensions] += 1.0
        norm = math.sqrt(sum(v * v for v in values)) or 1.0
        return [v / norm for v in values]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text.split("\nQuery:", 1)[-1])


def build(tmp_path: Path) -> Service:
    drive = FakeDrive()
    drive.add_root(ROOT, "Công ty")
    for number, (marker, text) in enumerate(PROSE.items()):
        drive.put(ROOT, f"prose{number:011d}", f"{marker.lower()}.txt", f"{marker} {text}".encode())
    drive.put(ROOT, "ledger00000000000", "So doanh thu", make_xlsx(ledger_rows()), mime=XLSX)
    drive.put(ROOT, "payroll0000000000", "Bang luong", make_xlsx(payroll_rows()), mime=XLSX)
    drive.put(ROOT, "timesheet00000000", "Cham cong", make_xlsx(timesheet_rows()), mime=XLSX)
    service = make_service(tmp_path, drive, EvalEmbeddings())  # type: ignore[arg-type]
    service.add_sources([ROOT])
    service.sync()
    return service


def rank_of(service: Service, query: str, marker: str) -> int | None:
    results = service.search(query, limit=LIMIT)["results"]
    for rank, hit in enumerate(results, 1):
        chunk = service.read(hit["document_id"], start_chunk=hit["chunk"], max_chunks=1)
        if marker in chunk["chunks"][0]["text"]:
            return rank
    return None


def evaluate(service: Service) -> dict[str, list[int | None]]:
    embeddings = service.embedding_factory
    hybrid = [rank_of(service, q, m) for q, m in QUERIES]
    service.embedding_factory = lambda _: None
    lexical = [rank_of(service, q, m) for q, m in QUERIES]
    service.embedding_factory = embeddings
    return {"fts": lexical, "hybrid": hybrid}


def top(ranks: list[int | None], k: int) -> int:
    return sum(1 for rank in ranks if rank is not None and rank <= k)


# Measured on 2026-11-14 (25 queries). Before (1bd1a25): rank-only RRF over raw bm25 order.
#   fts    top1 22 / top3 23 / top8 25      hybrid top1 15 / top3 17 / top8 25
# After: rarity-weighted coverage + adjacency, score fusion with LEXICAL_WEIGHT 0.7.
#   fts    top1 23 / top3 24 / top8 25      hybrid top1 21 / top3 24 / top8 25
THRESHOLDS = {"fts": {1: 23, 3: 24, 8: 25}, "hybrid": {1: 21, 3: 24, 8: 25}}
MUST_BE_FIRST = ("KT-DONGBO-7391", "HOP-2026-XANH", "phí giữ xe", "120 nghìn đồng")


def test_ranking_eval(tmp_path: Path) -> None:
    service = build(tmp_path)
    assert service.search("x", limit=1)["mode"] == "hybrid"
    ranks = evaluate(service)
    for mode, wanted in THRESHOLDS.items():
        for k, at_least in wanted.items():
            assert top(ranks[mode], k) >= at_least, (mode, k, ranks[mode])
        for query in MUST_BE_FIRST:  # exact codes and short keywords must not regress
            index = [q for q, _ in QUERIES].index(query)
            assert ranks[mode][index] == 1, (mode, query, ranks[mode][index])


def test_narrow_scope_keeps_the_strong_match_first(tmp_path: Path) -> None:
    """Under a two-file scope the vector list has a handful of chunks; scaling it must not turn
    small cosine gaps into a full swing (min-max did, putting these targets last)."""
    service = build(tmp_path)
    files = ["prose00000000000", "ledger00000000000"]
    for query in (
        "phí giữ xe",
        "phí giữ xe là bao nhiêu",
        "phi giu xe thang muoi hai la bao nhieu",
    ):
        hit = service.search(query, files=files)["results"][0]
        text = service.read(hit["document_id"], start_chunk=hit["chunk"], max_chunks=1)
        assert "GIUXE" in text["chunks"][0]["text"], query


def test_lexical_score_prefers_rare_terms_and_phrases() -> None:
    from gdrive_rag.ranking import idf, lexical_score, tokens

    assert tokens("Phí GIỮ xe, Đà Nẵng") == ["phi", "giu", "xe", "da", "nang"]
    assert idf(1, 100) > idf(50, 100) > 0 == idf(0, 100)
    query = tokens("phí giữ xe tháng mười hai")
    weights = {"phi": 2.0, "giu": 4.0, "xe": 2.0, "thang": 0.1, "muoi": 0.2, "hai": 0.2}
    target = lexical_score(query, weights, "phí giữ xe tháng mười hai là 120 nghìn")
    table = lexical_score(query, weights, "tháng mười hai | phí | xe " * 40)  # repetition
    shuffled = lexical_score(query, weights, "xe hai giữ tháng phí mười")
    assert target == 1.0 and table < 0.6 and shuffled < target


def test_cli_explain_shows_both_lists(tmp_path: Path, monkeypatch, capsys) -> None:
    import json

    import gdrive_rag.cli as cli
    from gdrive_rag.cli import main as cli_main

    service = build(tmp_path)
    monkeypatch.setattr(cli, "_service", lambda args: service)
    assert cli_main(["--json", "search", "phí giữ xe", "--explain", "--limit", "3"]) == 0
    hits = json.loads(capsys.readouterr().out)["results"]
    explain = hits[0]["explain"]
    assert set(explain) == {"fts_rank", "fts_score", "bm25", "vector_rank", "vector_score", "fused"}
    assert explain["fts_rank"] == 1 and explain["fused"] == hits[0]["score"]
    assert "explain" not in service.search("phí giữ xe")["results"][0]


def main() -> None:  # pragma: no cover - prints the table for the report
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        ranks = evaluate(build(Path(directory)))
    print(f"{'query':45} {'fts':>4} {'hybrid':>6}")
    for (query, _), lexical, hybrid in zip(QUERIES, ranks["fts"], ranks["hybrid"], strict=True):
        print(f"{query:45} {lexical or '-':>4} {hybrid or '-':>6}")
    for mode in ("fts", "hybrid"):
        print(mode, {k: top(ranks[mode], k) for k in (1, 3, 8)}, "of", len(QUERIES))


if __name__ == "__main__":
    main()
