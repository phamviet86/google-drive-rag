"""scripts/release_notes.py: the version and CHANGELOG notes the release workflow publishes."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release_notes.py"

spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
assert spec is not None and spec.loader is not None
release_notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_notes)

CHANGELOG = """# Changelog

## [Unreleased]

- coming

## [0.5.0] - 2026-11-21

Tóm tắt: bản mới.

### Added

- `read` takes `source`.

## [0.4.0] - 2026-11-19

- older
"""


def test_notes_take_only_that_section() -> None:
    text = release_notes.notes(CHANGELOG, "0.5.0")
    assert text.startswith("Tóm tắt: bản mới.")
    assert "### Added" in text and "`read` takes `source`." in text
    assert "older" not in text and "coming" not in text and "## [" not in text


def test_last_section_runs_to_the_end() -> None:
    assert release_notes.notes(CHANGELOG, "0.4.0") == "- older\n"


@pytest.mark.parametrize("missing", ["0.6.0", "0.5", "Unreleased-x"])
def test_a_version_without_a_section_is_refused(missing: str) -> None:
    with pytest.raises(LookupError, match="no section"):
        release_notes.notes(CHANGELOG, missing)


def test_an_empty_section_is_refused() -> None:
    with pytest.raises(LookupError, match="empty"):
        release_notes.notes("## [1.0.0] - 2026-01-01\n\n## [0.9.0]\n- x\n", "1.0.0")


def test_the_repository_version_has_release_notes() -> None:
    # every version bump must come with a CHANGELOG section, or the release workflow fails
    version = release_notes.version()
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert release_notes.notes(changelog, version).strip()


def test_command_line() -> None:
    run = [sys.executable, str(SCRIPT)]
    out = subprocess.run([*run, "version"], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == release_notes.version()
    bad = subprocess.run([*run, "notes", "9.9.9"], capture_output=True, text=True)
    assert bad.returncode == 1 and "no section for 9.9.9" in bad.stderr
