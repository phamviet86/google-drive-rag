import json
import stat
from pathlib import Path

import pytest

from gdrive_rag.cli import main
from gdrive_rag.config import load_settings

FAKE_KEY = {
    "type": "service_account",
    "client_email": "reader@example-project.iam.gserviceaccount.com",
    "private_key": "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "GDRAG_DATA_DIR",
        "GDRAG_EMBEDDING_API_KEY",
        "GDRAG_EMBEDDING_BASE_URL",
        "GDRAG_EMBEDDING_MODEL",
        "GDRAG_GOOGLE_AUTH",
        "GDRAG_GOOGLE_CREDENTIALS",
        "GDRAG_DRIVE_LINKS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_setup_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key_file = tmp_path / "key.json"
    key_file.write_text(json.dumps(FAKE_KEY))
    data = tmp_path / "data"
    monkeypatch.setenv("GDRAG_EMBEDDING_API_KEY", "sk-test-secret-value")
    monkeypatch.setenv("GDRAG_GOOGLE_AUTH", "service_account")
    monkeypatch.setenv("GDRAG_GOOGLE_CREDENTIALS", str(key_file))
    monkeypatch.setenv(
        "GDRAG_DRIVE_LINKS", "https://drive.google.com/drive/folders/folderAAAAAAAAAAAA"
    )
    assert main(["--data-dir", str(data), "--json", "setup", "--yes"]) == 0
    output = capsys.readouterr().out
    assert "sk-test-secret-value" not in output and "not-a-real-key" not in output
    report = json.loads(output)
    assert report["share_folders_with"] == FAKE_KEY["client_email"]
    assert report["sources"][0]["ok"] is True  # added; resolved on the first sync
    settings = load_settings(data, use_env=False)
    assert settings.embedding_api_key == "sk-test-secret-value"
    assert settings.google_auth == "service_account"
    for path in (settings.config_path, settings.service_account_file):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    assert main(["--data-dir", str(data), "--json", "status"]) == 0
    status = capsys.readouterr().out
    assert "sk-test-secret-value" not in status
    assert json.loads(status)["sources"][0]["root_id"] == "folderAAAAAAAAAAAA"

    assert main(["--data-dir", str(data), "source", "list"]) == 0
    assert "folderAAAAAAAAAAAA" in capsys.readouterr().out
    assert main(["--data-dir", str(data), "--json", "source", "remove", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["documents_removed"] == 0


def test_bad_inputs_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = str(tmp_path / "d")
    assert main(["--data-dir", data, "--json", "source", "add", "https://example.com/x"]) == 2
    assert main(["--data-dir", data, "--json", "source", "remove", "nothing"]) == 2
    bad = tmp_path / "bad.json"
    bad.write_text('{"type": "authorized_user"}')
    assert (
        main(["--data-dir", data, "--json", "auth", "google", "--service-account", str(bad)]) == 2
    )
    assert "not a service-account key" in capsys.readouterr().out
