"""Pure text normalization for taxonomy matching.

No I/O, no database, no config. Everything downstream (the alias index, the resolver,
the seeder, the candidate queue) agrees on exactly one definition of "the same string",
and it lives here.

Rules:

  * NFKC, then lowercase. Curly quotes and the various unicode dashes are folded to
    their ASCII equivalents first, so smart-quoted text agrees with plain text.
  * ``&`` becomes the word ``and``. Job descriptions write both "Data Structures &
    Algorithms" and "data structures and algorithms" and they must collide.
  * Apostrophes are deleted rather than turned into a space, so "bachelor's degree"
    becomes "bachelors degree" and not "bachelor s degree".
  * ``+`` and ``#`` are word characters, so ``c++`` and ``c#`` survive as single tokens
    and neither can be matched by the alias ``c``.
  * ``.`` is a word character only when it is *internal* (``node.js``, ``asp.net``) or
    *leading* (``.net``). A trailing sentence period is dropped, so "We use Go." still
    tokenizes to ``("we", "use", "go")``.
  * Everything else non-alphanumeric collapses to a single space.

Normalization is idempotent: ``normalize(normalize(s)) == normalize(s)``.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["EXTRA_WORD_CHARS", "normalize", "tokenize"]

#: characters that belong to a token even though ``str.isalnum()`` says otherwise
EXTRA_WORD_CHARS = frozenset("+#.")

_FOLD = str.maketrans(
    {
        "‘": "'",  # left single quote
        "’": "'",  # right single quote
        "ʼ": "'",  # modifier letter apostrophe
        "‛": "'",
        "“": " ",  # left double quote
        "”": " ",  # right double quote
        "‐": "-",  # hyphen
        "‑": "-",  # non-breaking hyphen
        "‒": "-",  # figure dash
        "–": "-",  # en dash
        "—": "-",  # em dash
        "―": "-",  # horizontal bar
        "−": "-",  # minus sign
        " ": " ",  # non-breaking space
        "​": "",  # zero-width space
    }
)

_APOSTROPHE_RE = re.compile(r"'")
_DOT_RE = re.compile(r"\.")
_WS_RE = re.compile(r"\s+")


def _dot_replacement(match: re.Match[str]) -> str:
    """Keep a dot only when it glues a token together (``node.js``) or leads one (``.net``)."""
    text = match.string
    i = match.start()
    nxt = text[i + 1] if i + 1 < len(text) else " "
    if not nxt.isalnum():
        return " "
    prev = text[i - 1] if i else " "
    if prev.isalnum() or prev.isspace():
        return "."
    return " "


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch in EXTRA_WORD_CHARS


def normalize(text: str | None) -> str:
    """Lowercase, punctuation-folded, whitespace-collapsed form used for all matching."""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text)).translate(_FOLD).lower()
    s = s.replace("&", " and ")
    s = _APOSTROPHE_RE.sub("", s)
    s = _DOT_RE.sub(_dot_replacement, s)
    s = "".join(ch if _is_word_char(ch) else " " for ch in s)
    return _WS_RE.sub(" ", s).strip()


def tokenize(text: str | None) -> tuple[str, ...]:
    """Normalize and split into whitespace-delimited tokens.

    Token equality *is* the word-boundary rule: ``"go"`` can never be found inside
    ``"django"``, ``"mongo"`` or ``"ongoing"`` because those are single distinct tokens.
    """
    norm = normalize(text)
    if not norm:
        return ()
    return tuple(norm.split(" "))
