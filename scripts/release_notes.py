"""Print the version in pyproject.toml, or the CHANGELOG section of a version.

Used by .github/workflows/release.yml, which publishes a GitHub release for each new version
that reaches main. Standard library only, so the workflow needs no install step.

    python scripts/release_notes.py version            -> 0.5.0
    python scripts/release_notes.py notes 0.5.0        -> the text under "## [0.5.0] - <date>"

``notes`` exits with status 1 when CHANGELOG.md has no section for that version or the section is
empty, so a version bump without release notes never becomes a release.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def version(pyproject: Path = ROOT / "pyproject.toml") -> str:
    with pyproject.open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def notes(text: str, wanted: str) -> str:
    """The body of the ``## [<wanted>]`` section, up to the next ``## `` heading."""
    heading = re.compile(r"^## \[(?P<version>[^\]]+)\]")
    lines: list[str] = []
    inside = False
    for line in text.splitlines():
        match = heading.match(line)
        if match:
            if inside:
                break
            inside = match["version"] == wanted
            continue
        if inside:
            lines.append(line)
    if not inside and not lines:
        raise LookupError(f"CHANGELOG.md has no section for {wanted}")
    body = "\n".join(lines).strip()
    if not body:
        raise LookupError(f"the CHANGELOG.md section for {wanted} is empty")
    return body + "\n"


def main(argv: list[str]) -> int:
    if argv[:1] == ["version"] and len(argv) == 1:
        print(version())
        return 0
    if argv[:1] == ["notes"] and len(argv) == 2:
        try:
            sys.stdout.write(notes((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), argv[1]))
        except LookupError as exc:
            print(exc, file=sys.stderr)
            return 1
        return 0
    print("usage: release_notes.py version | notes <version>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
