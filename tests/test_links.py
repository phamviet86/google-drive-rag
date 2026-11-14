import pytest

from gdrive_rag.drive import item_suffix, parse_drive_link

FOLDER = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"


@pytest.mark.parametrize(
    "link",
    [
        f"https://drive.google.com/drive/folders/{FOLDER}",
        f"https://drive.google.com/drive/folders/{FOLDER}?usp=sharing",
        f"https://drive.google.com/drive/u/1/folders/{FOLDER}",
        f"https://docs.google.com/document/d/{FOLDER}/edit#heading=h.1",
        f"https://docs.google.com/spreadsheets/d/{FOLDER}/edit?gid=0",
        f"https://docs.google.com/presentation/d/{FOLDER}/edit",
        f"https://drive.google.com/file/d/{FOLDER}/view?usp=drive_link",
        f"https://drive.google.com/open?id={FOLDER}",
        f"https://drive.google.com/uc?id={FOLDER}&export=download",
        f"  {FOLDER}  ",
    ],
)
def test_parse_drive_link(link: str) -> None:
    assert parse_drive_link(link) == FOLDER


def test_shared_drive_link() -> None:
    assert parse_drive_link("https://drive.google.com/drive/folders/0AAbCdEfGhIjKUk9PVA") == (
        "0AAbCdEfGhIjKUk9PVA"
    )


@pytest.mark.parametrize(
    "link",
    ["https://example.com/folders/abcdefghijklmnop", "not a link", "https://drive.google.com/"],
)
def test_bad_links(link: str) -> None:
    with pytest.raises(ValueError):
        parse_drive_link(link)


def test_item_suffix_uses_mime_then_name() -> None:
    assert item_suffix("application/vnd.google-apps.document", "x") == ".md"
    assert item_suffix("application/vnd.google-apps.spreadsheet", "x") == ".xlsx"
    assert item_suffix("application/vnd.ms-excel", "a") == ".xls"
    assert item_suffix("application/msword", "a") == ".doc"
    assert item_suffix("application/octet-stream", "Bao cao.XLSB") == ".xlsb"
    assert item_suffix("application/octet-stream", "photo.raw") == ""
    assert item_suffix("application/vnd.google-apps.form", "form") == ""
