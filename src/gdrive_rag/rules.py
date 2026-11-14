"""Per-source path rules for search: ``demote`` moves a file's hits ``DEMOTE_PLACES`` places
down the ranked list (its score is lowered to just below the hit now above it), ``exclude``
leaves it out of search results. Rules are applied at query time only; the file stays indexed
and readable by its ID, and changing rules never re-extracts or re-embeds anything.

A pattern is a glob over the file's ``path`` as search results show it (inside the source
folder), compared without case or Vietnamese accents:

- ``*`` matches within one folder or file name, ``?`` one character, ``**`` any number of
  folders (``**/`` also none);
- a pattern without a leading ``/`` may start at any folder depth, so ``attachments/**`` matches
  ``Kế toán/attachments/a.docx``; a leading ``/`` anchors it at the top of the source;
- a trailing ``/`` means everything below that folder.

A rule applies to the documents of the source it is set on. A document held by several sources
gets the rules of all of them; ``exclude`` wins over ``demote``. Files named in search's
``files`` filter are never excluded.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable
from typing import Any

from .db import fts_fold

EFFECTS = ("demote", "exclude")
DEMOTE_PLACES = 10  # a demoted hit moves this many places down the fused order
MAX_PATTERNS = 20  # per effect and source (keeps list_sources under the wire cap)
MAX_PATTERN_CHARS = 100


def fold(text: str) -> str:
    """Lower-case and accent-free, the way patterns and paths are compared."""
    decomposed = unicodedata.normalize("NFD", fts_fold(text).lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def clean(patterns: Any, name: str) -> list[str]:
    """Validated patterns (stripped, duplicates dropped, order kept). Messages never repeat a
    value."""
    if not isinstance(patterns, list | tuple):
        raise ValueError(f"{name} must be a list of path patterns")
    result: list[str] = []
    for value in patterns:
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a list of strings")
        pattern = value.strip()
        if not pattern or pattern in {"/", "**", "/**"}:
            raise ValueError(f"{name}: an empty or match-everything pattern is not allowed")
        if len(pattern) > MAX_PATTERN_CHARS:
            raise ValueError(f"{name}: a pattern is longer than {MAX_PATTERN_CHARS} characters")
        if any(unicodedata.category(c) == "Cc" for c in pattern):
            raise ValueError(f"{name}: a pattern contains a control character")
        if pattern not in result:
            result.append(pattern)
    if len(result) > MAX_PATTERNS:
        raise ValueError(f"{name}: at most {MAX_PATTERNS} patterns")
    return result


def compile_pattern(pattern: str) -> re.Pattern[str]:
    text = fold(pattern)
    anchored = text.startswith("/")
    text = text.lstrip("/")
    if text.endswith("/"):
        text += "**"
    out: list[str] = []
    i = 0
    while i < len(text):
        if text.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif text.startswith("**", i):
            out.append(".*")
            i += 2
        elif text[i] == "*":
            out.append("[^/]*")
            i += 1
        elif text[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(text[i]))
            i += 1
    prefix = "" if anchored else "(?:.*/)?"
    return re.compile(prefix + "".join(out), re.DOTALL)


def demote(order: list[int], demoted: set[int], scores: dict[int, float]) -> list[int]:
    """Move each demoted id ``DEMOTE_PLACES`` places down ``order`` (best first) and lower its
    score in ``scores`` to just below the id now above it, so scores stay in order. A fixed
    shift, not a multiplier: an old copy whose name matches the query keeps far more signals
    than the canonical file, and any factor that fixes one query is arbitrary for the next."""
    shifted = sorted(
        range(len(order)), key=lambda i: i + (DEMOTE_PLACES + 0.5 if order[i] in demoted else 0)
    )
    result = [order[i] for i in shifted]
    for above, chunk_id in zip(result, result[1:], strict=False):
        if chunk_id in demoted and scores[chunk_id] >= scores[above]:
            scores[chunk_id] = math.nextafter(scores[above], 0.0)
    return result


def matches(pattern: str, path: str) -> bool:
    return compile_pattern(pattern).fullmatch(fold(path)) is not None


def effects(
    rules: Iterable[tuple[int, str, str]], documents: Iterable[tuple[int, str, int]]
) -> dict[int, tuple[str, str]]:
    """``{document id: (effect, pattern)}`` for documents a rule hits. ``rules`` are
    ``(source id, effect, pattern)``, ``documents`` ``(document id, path, source id)`` with one
    row per source membership. ``exclude`` wins over ``demote``; the first matching pattern of
    an effect is reported."""
    compiled: dict[int, list[tuple[str, str, re.Pattern[str]]]] = {}
    for source_id, effect, pattern in rules:
        compiled.setdefault(source_id, []).append((effect, pattern, compile_pattern(pattern)))
    output: dict[int, tuple[str, str]] = {}
    for document_id, path, source_id in documents:
        folded = fold(path)
        for effect, pattern, regex in compiled.get(source_id, ()):
            if output.get(document_id, ("",))[0] == "exclude":
                break
            if effect == "demote" and document_id in output:
                continue
            if regex.fullmatch(folded):
                output[document_id] = (effect, pattern)
    return output
