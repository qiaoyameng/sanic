"""Optional NFC normalization for URL paths.

When ``NFC_PATHS`` is enabled on the application config, Sanic normalizes
both registered route URIs and incoming request paths to a canonical
Unicode NFC form so that equivalent Unicode spellings (for example ``é``
as U+00E9 or as ``e`` + U+0301) resolve to the same route.

The normalization is deliberately conservative:

- ``%2F`` and ``%5C`` are never decoded while normalizing a request path,
  so an encoded separator cannot smuggle additional path hierarchy into a
  request.
- Percent-encoding is decoded exactly once. Double-encoded sequences such
  as ``%252F`` normalize to ``%2F`` and stay that way.
- Segments that are not valid UTF-8 are left untouched (and therefore will
  simply not match a normalized route), instead of raising an error.
"""

from __future__ import annotations

import re

from typing import Any
from unicodedata import normalize
from urllib.parse import quote, unquote


__all__ = (
    "decode_match_params",
    "normalize_request_path",
    "normalize_route_uri",
)

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

# Octets that must never be decoded while normalizing a request path.
# Decoding them would allow an encoded separator (%2F => "/", %5C => "\")
# to introduce new path hierarchy after normalization.
_PROTECTED_OCTETS = {
    0x2F: "%2F",
    0x5C: "%5C",
}

# Split a route URI on "/" while leaving <...> parameter declarations
# (which may themselves contain "/") intact. Mirrors the expression used
# by sanic_routing.utils.path_to_parts.
_ROUTE_SEGMENT_SPLIT = re.compile(r"/(?=[^>]*(?:<(?<!\?<)|$))")


def _unquote_loose(value: str) -> str:
    """Decode percent-encoding once, keeping invalid UTF-8 as-is."""
    if "%" not in value:
        return value
    try:
        return unquote(value, errors="strict")
    except UnicodeDecodeError:
        return value


def normalize_route_uri(uri: str) -> str:
    """Normalize a route URI to NFC at registration time.

    Parameter declarations (``<...>``) are left untouched. Static parts
    are percent-decoded once (matching what sanic_routing already does)
    and normalized to NFC so that two equivalent spellings of the same
    route collapse into a single route (raising ``RouteExists`` instead of
    silently coexisting).
    """
    if not uri:
        return uri
    parts = _ROUTE_SEGMENT_SPLIT.split(uri)
    return "/".join(
        part
        if part.startswith("<")
        else normalize("NFC", _unquote_loose(part))
        for part in parts
    )


def _canonicalize_segment(segment: str) -> str:
    if not segment or ("%" not in segment and segment.isascii()):
        return segment

    out: list[str] = []
    text: list[str] = []
    octets = bytearray()

    def flush_octets() -> bool:
        nonlocal octets
        if not octets:
            return True
        try:
            text.append(octets.decode("utf-8"))
        except UnicodeDecodeError:
            return False
        octets = bytearray()
        return True

    def flush_text() -> None:
        if text:
            out.append(quote(normalize("NFC", "".join(text)), safe=""))
            text.clear()

    idx = 0
    size = len(segment)
    while idx < size:
        char = segment[idx]
        if (
            char == "%"
            and idx + 3 <= size
            and segment[idx + 1] in _HEX_DIGITS
            and segment[idx + 2] in _HEX_DIGITS
        ):
            octet = int(segment[idx + 1 : idx + 3], 16)
            protected = _PROTECTED_OCTETS.get(octet)
            if protected is None:
                octets.append(octet)
            else:
                if not flush_octets():
                    return segment
                flush_text()
                out.append(protected)
            idx += 3
        else:
            if not flush_octets():
                return segment
            text.append(char)
            idx += 1

    if not flush_octets():
        return segment
    flush_text()
    return "".join(out)


def normalize_request_path(path: str) -> str:
    """Normalize a request path to its canonical NFC, percent-encoded form.

    Each segment is percent-decoded once (except ``%2F`` and ``%5C``, which
    stay encoded), normalized to NFC, and re-encoded. Segments containing
    invalid UTF-8 are returned unchanged.
    """
    if "%" not in path and path.isascii():
        return path
    return "/".join(_canonicalize_segment(part) for part in path.split("/"))


def decode_match_params(params: dict[str, Any]) -> dict[str, Any]:
    """Decode matched path parameters into their NFC Unicode form.

    Values are decoded exactly once; anything that is not valid UTF-8 is
    left percent-encoded so the raw value is still recoverable.
    """
    return {
        key: _unquote_loose(value) if isinstance(value, str) else value
        for key, value in params.items()
    }
