"""Per-source path rules for search: ``demote`` puts all of a file's hits below the next
``DEMOTE_PLACES`` other files in the ranked list (their scores are lowered to just below that
file's best hit), ``exclude`` leaves it out of search results. Rules are applied at query time
only; the file stays indexed and readable by its ID, and changing rules never re-extracts or
re-embeds anything.

A pattern is a glob over the file's ``path`` as search results show it (inside the source
folder), compared without case or Vietnamese accents:

- ``*`` matches within one folder or file name, ``?`` one character, ``**`` any number of
  folders (``**/`` also none);
- a pattern without a leading ``/`` may start at any folder depth, so ``attachments/**`` matches
  ``Kế toán/attachments/a.docx``; a leading ``/`` anchors it at the top of the source;
- a trailing ``/`` means everything below that folder.

A rule applies to the documents of the source it is set on. A document held by several sources
gets the rules of all of them; ``ignore`` wins over ``exclude``, which wins over ``demote``.
Files named in search's ``files`` filter are never excluded (but an ignored file is not shown).

``ignore`` keeps files out of the index altogether: at sync, after listing and before any
download, ``ignore_listing`` drops matching files and folders from the listing (they are stored
as skipped files with the reason ``ignored by rule: <entry>``), so the normal membership
replacement removes what was indexed. An ignore entry is a glob like the others, or a file or
folder ID stored as ``id:<ID>`` (from a Drive link, a bare ID, or ``id:`` written out; any
backend's opaque ID works the same way). A folder entry covers everything below it through the
listing's parent IDs, so it survives renames and moves. A file listed by several sources is
ignored when any of them ignores it. Until the next sync removes them, search hides ignored
documents like ``exclude`` does.
"""

from __future__ import annotations

import bisect
import functools
import math
import re
import unicodedata
from collections.abc import Callable, Collection, Iterable
from dataclasses import replace
from typing import Any

from .db import fts_fold
from .drive import (
    Listing,
    SkippedFile,
    display_segment,
    is_local_id,
    normalize_local_id,
    parse_drive_link,
)

EFFECTS = ("demote", "exclude", "ignore")
RANK = {"ignore": 0, "exclude": 1, "demote": 2}  # the lower rank wins on one document
ID_PREFIX = "id:"  # an ignore entry naming a file or folder by ID, not a path
IGNORED_REASON = "ignored by rule: "  # skipped_files.reason prefix (+ the entry), own rule
OTHER_SOURCE_REASON = "ignored by a rule of another source that lists this file"
IGNORED_GLOB = "ignored by rule: *"  # SQL GLOB for rows ignored by their own source's rule
_DRIVE_ID = re.compile(r"[A-Za-z0-9_-]{19,}")
DEMOTE_PLACES = 10  # a demoted file goes below this many other files of the fused order
MAX_PATTERNS = 20  # per effect and source (keeps list_sources under the wire cap)
MAX_PATTERN_CHARS = 100
# A pattern matching all of these matches any path, so it would demote or hide everything.
# Every name is longer than a pattern can be, so "?" runs ("**??", "?*?") that only skip very
# short names are caught too; an exact length ("**/??") still matches none of them.
_PROBE = 2 * 60  # > MAX_PATTERN_CHARS
MATCH_ALL_PROBES = tuple(
    "/".join(letter * _PROBE + suffix for letter, suffix in shape)
    for shape in (
        (("a", ""),),  # a Google Doc at the top: no extension
        (("b", ".txt"),),
        (("c", ""), ("d", "")),
        (("e", ""), ("f", ""), ("g", ".docx")),
        (("h", ""), ("i", ""), ("j", ""), ("k", ".pdf")),
        (("l", ".m"), ("n", "")),  # a folder name with a dot
    )
)


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
        pattern = _check_glob(value.strip(), name)
        if pattern not in result:
            result.append(pattern)
    if len(result) > MAX_PATTERNS:
        raise ValueError(f"{name}: at most {MAX_PATTERNS} patterns")
    return result


def _check_glob(pattern: str, name: str) -> str:
    if not pattern or pattern in {"/", "**", "/**"}:
        raise ValueError(f"{name}: an empty or match-everything pattern is not allowed")
    if len(pattern) > MAX_PATTERN_CHARS:
        raise ValueError(f"{name}: a pattern is longer than {MAX_PATTERN_CHARS} characters")
    if any(unicodedata.category(c) == "Cc" for c in pattern):
        raise ValueError(f"{name}: a pattern contains a control character")
    if all(matches(pattern, probe) for probe in MATCH_ALL_PROBES):
        raise ValueError(
            f"{name}: a pattern matches every file path (like * or **/*); "
            "name a folder or a file type instead"
        )
    return pattern


def is_id(entry: str) -> bool:
    return entry.startswith(ID_PREFIX)


def looks_like_drive_id(value: str) -> bool:
    """A bare Drive ID: 19+ ID characters mixing upper case, lower case and digits (a folder
    name written like ``bao-cao-2025`` is not one; ``id:`` forces an ID)."""
    return (
        _DRIVE_ID.fullmatch(value) is not None
        and any(c.isupper() for c in value)
        and any(c.islower() for c in value)
        and any(c.isdigit() for c in value)
    )


def clean_ignore(values: Any, known: Callable[[str], bool] = lambda _: False) -> list[str]:
    """Validated ignore entries in their stored form: ``id:<ID>`` for a Drive link, an
    ``id:`` entry, a ``local:`` ID, a value ``known`` says is an ID, or a bare Drive ID; a glob
    otherwise (same checks as ``clean``). Messages never repeat a value."""
    if not isinstance(values, list | tuple):
        raise ValueError("ignore must be a list of path patterns, Drive links or IDs")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("ignore must be a list of strings")
        entry = value.strip()
        if any(unicodedata.category(c) == "Cc" for c in entry):
            raise ValueError("ignore: an entry contains a control character")
        if is_id(entry):
            ident = entry[len(ID_PREFIX) :].strip()
            if is_local_id(ident):
                ident = normalize_local_id(ident)  # a path: spaces are allowed
            elif not ident or any(c.isspace() for c in ident):
                raise ValueError("ignore: an id: entry needs one ID after id:")
            stored = ID_PREFIX + ident
        elif is_local_id(entry):  # local:<path inside the local folder>, file or folder
            stored = ID_PREFIX + normalize_local_id(entry)
        elif "://" in entry:
            try:
                stored = ID_PREFIX + parse_drive_link(entry)
            except ValueError:
                raise ValueError(
                    "ignore: a link is not a Google Drive folder or file link"
                ) from None
        elif entry and (known(entry) or looks_like_drive_id(entry)):
            stored = ID_PREFIX + entry
        else:
            stored = _check_glob(entry, "ignore")
        if len(stored) > MAX_PATTERN_CHARS:
            raise ValueError(f"ignore: an entry is longer than {MAX_PATTERN_CHARS} characters")
        if stored not in result:
            result.append(stored)
    if len(result) > MAX_PATTERNS:
        raise ValueError(f"ignore: at most {MAX_PATTERNS} entries")
    return result


class Glob:
    """One pattern as a small NFA run in step with the path: time is linear in the path length
    times the pattern length, so no pattern (``**a**a**a…``) can backtrack exponentially."""

    def __init__(self, pattern: str):
        text = fold(pattern)
        anchored = text.startswith("/")
        text = text.lstrip("/")
        if text.endswith("/"):
            text += "**"
        tokens: list[tuple[str, str | int]] = []

        def any_folders() -> None:  # "**/": nothing, or anything ending in "/"
            start = len(tokens)
            tokens.extend([("skip", 0), ("dstar", ""), ("lit", "/")])
            tokens[start] = ("skip", len(tokens))

        if not anchored:
            any_folders()
        i = 0
        while i < len(text):
            if text.startswith("**/", i):
                any_folders()
                i += 3
            elif text.startswith("**", i):
                tokens.append(("dstar", ""))
                i += 2
            elif text[i] == "*":
                tokens.append(("star", ""))
                i += 1
            elif text[i] == "?":
                tokens.append(("one", ""))
                i += 1
            else:
                tokens.append(("lit", text[i]))
                i += 1
        self.tokens = tokens
        self._closures: dict[int, frozenset[int]] = {}
        self._below: dict[frozenset[int], bool] = {}
        # The longest run of literal characters every match must contain: a cheap substring
        # test before running the NFA over many paths. Tokens a skip can jump over are
        # optional, and a run breaks where a skip lands.
        optional: set[int] = set()
        targets: set[int] = set()
        for index, (kind, value) in enumerate(tokens):
            if kind == "skip":
                optional.update(range(index + 1, int(value)))
                targets.add(int(value))
        runs, run = [""], ""
        for index, (kind, value) in enumerate(tokens):
            if kind != "lit" or index in optional or index in targets:
                runs.append(run)
                run = ""
            if kind == "lit" and index not in optional:
                run += str(value)
        self.needle = max([*runs, run], key=len)

    def _closure(self, state: int) -> frozenset[int]:
        known = self._closures.get(state)
        if known is not None:
            return known
        seen: set[int] = set()
        stack = [state]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            if current < len(self.tokens):
                kind, value = self.tokens[current]
                if kind == "skip":
                    stack += [current + 1, int(value)]
                elif kind in {"star", "dstar"}:
                    stack.append(current + 1)
        result = frozenset(seen)
        self._closures[state] = result
        return result

    def start(self) -> frozenset[int]:
        return self._closure(0)

    def advance(self, states: frozenset[int], folded: str) -> frozenset[int]:
        """The states after reading ``folded`` (already ``fold``-ed) from ``states``; empty
        once no state survives."""
        end = len(self.tokens)
        for char in folded:
            following: set[int] = set()
            for state in states:
                if state == end:
                    continue
                kind, value = self.tokens[state]
                if kind == "lit" and char == value or kind == "one" and char != "/":
                    following |= self._closure(state + 1)
                elif kind == "dstar" or kind == "star" and char != "/":
                    following |= self._closure(state)
            if not following:
                return frozenset()
            states = frozenset(following)
        return states

    def accepts(self, states: frozenset[int]) -> bool:
        return len(self.tokens) in states

    def below(self, states: frozenset[int]) -> bool:
        """With ``states`` read up to a folder path and its ``/``: does the pattern match
        every path below that folder (the ``MATCH_ALL_PROBES`` test, from there on)?"""
        known = self._below.get(states)
        if known is None:
            known = bool(states) and all(
                self.accepts(self.advance(states, probe)) for probe in MATCH_ALL_PROBES
            )
            self._below[states] = known
        return known

    def match(self, folded_path: str) -> bool:
        """``folded_path`` is already ``fold``-ed; the whole path must match."""
        return self.accepts(self.advance(self.start(), folded_path))


@functools.lru_cache(maxsize=1024)
def compile_pattern(pattern: str) -> Glob:
    return Glob(pattern)


def demote(
    order: list[int],
    document_of: dict[int, int],
    demoted: set[int],
    scores: dict[int, float],
) -> list[int]:
    """Put every hit of each demoted document below the best hit of the next
    ``DEMOTE_PLACES`` other (not demoted) documents after its own best hit (below all of them
    when fewer follow), and return the order by the new scores. ``order`` is chunk ids, best
    first; ``document_of`` maps them to document ids; ``demoted`` holds document ids.

    A demoted hit's score is lowered to just below that anchor hit, or kept when it is already
    lower, so its weaker hits stay where their own score puts them instead of moving with the
    best one past stronger hits of other files. Other hits keep their scores and order; ties
    keep the old order. Scores are therefore in order, and the result does not depend on how
    many hits are shown.

    By document, not by chunk: a file with many matching chunks must drop as a whole. A fixed
    shift, not a multiplier: an old copy whose name matches the query keeps far more signals
    than the canonical file, and any factor that fixes one query is arbitrary for the next."""
    first: dict[int, int] = {}
    for position, chunk_id in enumerate(order):
        first.setdefault(document_of.get(chunk_id, -chunk_id), position)
    others = sorted(p for d, p in first.items() if d not in demoted)
    kept = [c for c in order if document_of.get(c, -c) not in demoted]
    if not kept:
        return order
    lowest = scores[kept[-1]]
    for chunk_id in order:
        document = document_of.get(chunk_id, -chunk_id)
        if document not in demoted:
            continue
        after = bisect.bisect_right(others, first[document])
        if after + DEMOTE_PLACES <= len(others):
            anchor = scores[order[others[after + DEMOTE_PLACES - 1]]]
        else:
            anchor = lowest
        scores[chunk_id] = min(scores[chunk_id], math.nextafter(anchor, 0.0))
    place = {chunk_id: index for index, chunk_id in enumerate(order)}
    return sorted(order, key=lambda chunk_id: (-scores[chunk_id], place[chunk_id]))


def matches(pattern: str, path: str) -> bool:
    return compile_pattern(pattern).match(fold(path))


def effects(
    rules: Iterable[tuple[int, str, str]], documents: Iterable[tuple[int, str, int]]
) -> dict[int, tuple[str, str]]:
    """``{document id: (effect, pattern)}`` for documents a path pattern hits. ``rules`` are
    ``(source id, effect, pattern)``, ``documents`` ``(document id, path, source id)`` with one
    row per source membership. ``ignore`` wins over ``exclude`` over ``demote``; the first
    matching pattern of an effect is reported. ``id:`` ignore entries are not paths: see
    ``ignore_listing`` and ``SearchEngine._rule_effects``."""
    by_source = group(rules)
    return combine(
        (document_id, row_effect(by_source.get(source_id, []), path))
        for document_id, path, source_id in documents
    )


def group(rules: Iterable[tuple[int, str, str]]) -> dict[int, list[tuple[str, str]]]:
    """``{source id: [(effect, pattern), ...]}`` in rule order."""
    grouped: dict[int, list[tuple[str, str]]] = {}
    for source_id, effect, pattern in rules:
        grouped.setdefault(source_id, []).append((effect, pattern))
    return grouped


def row_effect(rules: list[tuple[str, str]], path: str) -> tuple[str, str] | None:
    """The effect of one source's path patterns on one path: the first matching pattern of the
    strongest effect that matches (ignore, then exclude, then demote), else None. ``id:``
    ignore entries are skipped (they name IDs, not paths)."""
    folded = fold(path)
    best: tuple[str, str] | None = None
    for effect, pattern in rules:
        if effect == "ignore" and is_id(pattern):
            continue
        if best is not None and RANK[effect] >= RANK[best[0]]:
            continue
        if compile_pattern(pattern).match(folded):
            best = (effect, pattern)
            if effect == "ignore":
                break
    return best


def combine(
    rows: Iterable[tuple[int, tuple[str, str] | None]],
) -> dict[int, tuple[str, str]]:
    """Per document over its source memberships: the strongest effect (ignore, exclude,
    demote), the first one seen of that effect."""
    output: dict[int, tuple[str, str]] = {}
    for document_id, effect in rows:
        if effect is None:
            continue
        known = output.get(document_id)
        if known is None or RANK[effect[0]] < RANK[known[0]]:
            output[document_id] = effect
    return output


def ignore_listing(
    listing: Listing, entries: Iterable[str], ids: Collection[str] = ()
) -> tuple[Listing, set[str]]:
    """``listing`` without the files and folders that ignore ``entries`` (or ``ids``: IDs
    another source ignores) hit, and the IDs it left out (files, skipped files and folders).

    Works on any backend's listing: items and skipped files by ``id``, ``path`` and
    ``parent_id``, folders by ``id``, ``name`` and ``parent_id``. A folder is ignored when an
    ID names it or a folder above it, or when a glob matches every path below it (``old/``,
    ``**/attachments/**``); its files and subfolders go with it. A file is ignored when an ID
    names it, its folder is ignored, or a glob matches its path. Left-out files become skipped
    files with the reason ``ignored by rule: <entry>``. Glob states are carried down the folder
    tree, so a file costs the length of its name per glob, not of its whole path."""
    entries = list(entries)
    named = {e[len(ID_PREFIX) :]: e for e in entries if is_id(e)}
    for ident in ids:
        named.setdefault(ident, "")
    globs = [(e, compile_pattern(e)) for e in entries if not is_id(e)]
    if not named and not globs:
        return listing, set()
    folders = {f.id: f for f in listing.folders}
    state: dict[str, tuple[str, list[frozenset[int]]]] = {}  # folder -> (path, glob states)
    why: dict[str, str] = {}  # ignored folder -> entry ("" when another source ignores it)

    def visit(folder_id: str) -> None:
        chain: list[str] = []
        current = folder_id
        while current in folders and current not in state and current not in chain:
            chain.append(current)
            current = folders[current].parent_id
        for folder in reversed(chain):
            parent = folders[folder].parent_id
            if parent in state:  # below a listed folder: one more path segment
                above, states = state[parent]
                segment = display_segment(folders[folder].name)
                path = f"{above}/{segment}" if above else segment
                step = fold(segment) + "/"
                states = [g.advance(s, step) for (_, g), s in zip(globs, states, strict=True)]
            else:  # the listing's top folder (the source root): paths start below it
                path, states = "", [g.start() for _, g in globs]
            state[folder] = (path, states)
            if folder in named:
                why[folder] = named[folder]
            elif parent in why:
                why[folder] = why[parent]
            elif path:
                pairs = zip(globs, states, strict=True)
                hit = next((e for (e, g), s in pairs if g.below(s)), None)
                if hit is not None:
                    why[folder] = hit

    for folder_id in folders:
        visit(folder_id)

    def entry_for(item_id: str, path: str, parent_id: str) -> str | None:
        if item_id in named:
            return named[item_id]
        if parent_id in why:
            return why[parent_id]
        folded = fold(path)
        above, states = state.get(parent_id, ("", []))
        if parent_id in state and (not above or path.startswith(f"{above}/")):
            rest = fold(path[len(above) + 1 :] if above else path)
            pairs = zip(globs, states, strict=True)
            return next(
                (e for (e, g), s in pairs if g.needle in folded and g.accepts(g.advance(s, rest))),
                None,
            )
        return next((e for e, g in globs if g.needle in folded and g.match(folded)), None)

    def reason(entry: str) -> str:
        return IGNORED_REASON + entry if entry else OTHER_SOURCE_REASON

    left_out: set[str] = set(why)
    items = []
    skipped: list[SkippedFile] = []
    for item in listing.items:
        entry = entry_for(item.id, item.path, item.parent_id)
        if entry is None:
            items.append(item)
        else:
            left_out.add(item.id)
            skipped.append(
                SkippedFile(
                    item.id, item.path, item.mime_type, item.size, reason(entry), item.parent_id
                )
            )
    for file in listing.skipped:
        entry = entry_for(file.id, file.path, file.parent_id)
        if entry is None:
            skipped.append(file)
        else:
            left_out.add(file.id)
            ignored_before = file.reason.startswith((IGNORED_REASON, OTHER_SOURCE_REASON))
            skipped.append(file if ignored_before else replace(file, reason=reason(entry)))
    kept_folders = [f for f in listing.folders if f.id not in why]
    return replace(listing, items=items, folders=kept_folders, skipped=skipped), left_out
