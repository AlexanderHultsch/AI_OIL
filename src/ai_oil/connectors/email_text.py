"""Email text normalisation: HTML to text, quoted history and signatures removed (DM-53)."""

import re
import unicodedata
from collections.abc import Sequence

from bs4 import BeautifulSoup, NavigableString

_DROPPED_TAGS = ("script", "style", "head")
_BLOCK_TAGS = (
    "p",
    "div",
    "li",
    "tr",
    "table",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "hr",
)
_CELL_TAGS = ("td", "th")
_HTML_WHITESPACE = re.compile(r"[ \t\r\n\f]+")
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍﻿"))
_SPACE_RUN = re.compile(r" {2,}")
_BLANK_RUNS = re.compile(r"\n{3,}")
_AROUND_TAB = re.compile(r" *\t *")
_LEADING_SPACES = re.compile(r"(?m)^ +")
_BLOCK_BREAK = "\x00"
_BLOCK_BREAK_RUN = re.compile(r" *\x00[ \x00]*")

_FROM_PREFIXES = ("from:", "von:")
_SENT_PREFIXES = ("sent:", "gesendet:", "date:", "datum:")
_TO_PREFIXES = ("to:", "an:", "subject:", "betreff:")
_SENT_WINDOW = 4
_TO_WINDOW = 5
_SEPARATOR_LINE = re.compile(
    r"^-{3,}\s*(?:original message|ursprüngliche nachricht|forwarded message"
    r"|weitergeleitete nachricht)\s*-{3,}$",
    re.IGNORECASE,
)
_WROTE_LINE = re.compile(r"^(?:on .+ wrote:|am .+ schrieb .+:)$", re.IGNORECASE)
_UNDERSCORE_LINE = re.compile(r"^_{10,}$")
_UNDERSCORE_LOOKAHEAD = 2

_SIGNATURE_PREFIXES = ("sent from my ", "gesendet von meinem ")
_GERMAN_SIGNATURE_PREFIX = "von meinem "
_GERMAN_SIGNATURE_WORD = "gesendet"

_SUBJECT_PREFIX = re.compile(
    r"^\s*(?:(?:re|aw|wg|fwd?|antw(?:ort)?)(?:\[\d+\])?\s*:|\[[^\]]*\])\s*", re.IGNORECASE
)
_WHITESPACE_RUN = re.compile(r"\s+")


def html_to_text(html: str) -> str:
    """Plain text of an HTML mail body, normalised (DM-53)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(_DROPPED_TAGS):
        tag.decompose()
    for node in soup.find_all(string=True):
        if type(node) is NavigableString and not node.find_parent("pre"):
            node.replace_with(NavigableString(_HTML_WHITESPACE.sub(" ", str(node))))
    for br in soup.find_all("br"):
        br.replace_with(NavigableString("\n"))
    for block in soup.find_all(_BLOCK_TAGS):
        block.insert_before(NavigableString(_BLOCK_BREAK))
        block.insert_after(NavigableString(_BLOCK_BREAK))
    for cell in soup.find_all(_CELL_TAGS):
        if cell.find_previous_sibling(_CELL_TAGS) is not None:
            cell.insert_before(NavigableString("\t"))
    text = _BLOCK_BREAK_RUN.sub(_BLOCK_BREAK, soup.get_text())
    text = text.replace(f"\n{_BLOCK_BREAK}", "\n").replace(_BLOCK_BREAK, "\n")
    text = _LEADING_SPACES.sub("", _AROUND_TAB.sub("\t", text.replace("\r", "")))
    return normalise_body(text)


def normalise_body(text: str) -> str:
    """Unified line endings and spaces, zero-width characters removed, blank lines collapsed."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(
        " " if unicodedata.category(char) == "Zs" else char for char in text.translate(_ZERO_WIDTH)
    )
    text = _SPACE_RUN.sub(" ", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return _BLANK_RUNS.sub("\n\n", text).strip("\n")


def _header_key(line: str) -> str:
    return line.lstrip("*> \t").lower()


def _starts_with_any(line: str, prefixes: tuple[str, ...]) -> bool:
    return _header_key(line).startswith(prefixes)


def _is_outlook_header(lines: list[str], index: int) -> bool:
    if not _starts_with_any(lines[index], _FROM_PREFIXES):
        return False
    sent = lines[index + 1 : index + 1 + _SENT_WINDOW]
    recipients = lines[index + 1 : index + 1 + _TO_WINDOW]
    return any(_starts_with_any(line, _SENT_PREFIXES) for line in sent) and any(
        _starts_with_any(line, _TO_PREFIXES) for line in recipients
    )


def _is_wrote_line(lines: list[str], index: int) -> bool:
    line = lines[index].strip()
    if _WROTE_LINE.match(line):
        return True
    if index + 1 >= len(lines) or not line.lower().startswith(("on ", "am ")):
        return False
    return bool(_WROTE_LINE.match(f"{line} {lines[index + 1].strip()}"))


def _is_underscore_marker(lines: list[str], index: int) -> bool:
    if not _UNDERSCORE_LINE.match(lines[index].strip()):
        return False
    following = [line for line in lines[index + 1 :] if line.strip()][:_UNDERSCORE_LOOKAHEAD]
    return any(_starts_with_any(line, _FROM_PREFIXES) for line in following)


def _quote_block_start(lines: list[str]) -> int | None:
    start = None
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index].lstrip()
        if not line:
            continue
        if not line.startswith(">"):
            break
        start = index
    return start


def _drop_trailing_blank(lines: list[str]) -> list[str]:
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    return lines[:end]


def strip_quoted(text: str) -> str:
    """The part before the first reply or forward marker, English and German (DM-53)."""
    lines = text.split("\n")
    cut = _quote_block_start(lines)
    for index, line in enumerate(lines):
        if cut is not None and index >= cut:
            break
        if (
            _is_outlook_header(lines, index)
            or _SEPARATOR_LINE.match(line.strip())
            or _is_wrote_line(lines, index)
            or _is_underscore_marker(lines, index)
        ):
            cut = index
            break
    if cut is None:
        return text
    return "\n".join(_drop_trailing_blank(lines[:cut]))


def _is_signature_line(line: str, extra_markers: Sequence[str]) -> bool:
    if line.rstrip() == "--":
        return True
    stripped = line.lstrip()
    lowered = stripped.lower()
    if lowered.startswith(_SIGNATURE_PREFIXES):
        return True
    if (
        lowered.startswith(_GERMAN_SIGNATURE_PREFIX)
        and _GERMAN_SIGNATURE_WORD in lowered[len(_GERMAN_SIGNATURE_PREFIX) :]
    ):
        return True
    return any(marker and stripped.startswith(marker) for marker in extra_markers)


def strip_signature(text: str, extra_markers: Sequence[str] = ()) -> str:
    """The text cut at the first signature marker (DM-53)."""
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if _is_signature_line(line, extra_markers):
            return "\n".join(_drop_trailing_blank(lines[:index]))
    return text


def new_text(text: str, *, extra_markers: Sequence[str] = ()) -> str | None:
    """The body without quoted history and signature, or None if nothing is left (DM-53)."""
    result = strip_signature(strip_quoted(normalise_body(text)), extra_markers).strip()
    return result or None


def subject_root(subject: str | None) -> str:
    """The subject without reply, forward and external-tag prefixes (CON-25)."""
    root = subject or ""
    while True:
        stripped = _SUBJECT_PREFIX.sub("", root, count=1)
        if stripped == root:
            break
        root = stripped
    return _WHITESPACE_RUN.sub(" ", root).strip()
