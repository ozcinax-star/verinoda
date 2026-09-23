"""Version text <-> tag matching and ordering (docs/DESIGN.md D11).

A version the user wrote (``2.31``, ``v2.31.0``, ``2.31 sürümü``) is matched
against a repository's tags after normalising the usual prefixes (``v``,
``release-``, ``rel-``, ``<name>-``, ``<name>@``, ``<name>/v``):

1. exact: the normalised tag equals the version (PEP 440 equality when the
   optional ``packaging`` library is present, so ``2.31`` == ``2.31.0``);
2. prefix: tags inside the version's series (``2.31`` -> ``2.31.1``, ``2.31.2``);
   the highest one is chosen and every candidate is reported;
3. none: mismatch M10 (version not found) - never a fallback to a branch.

Ordering uses PEP 440 when ``packaging`` is importable and a numeric
fallback otherwise; pre-releases sort before their release and are skipped
when the newest *release* is wanted.
"""

from __future__ import annotations

import re

try:  # optional: a pure-Python wheel that is usually present; the fallback below covers its absence
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import InvalidVersion, Version
except ImportError:  # pragma: no cover - exercised only without packaging
    Version = None  # type: ignore[assignment]
    SpecifierSet = None  # type: ignore[assignment]

_PRE = re.compile(r"(?:a|b|rc|alpha|beta|pre|preview|dev)\.?\d*$|-(?:alpha|beta|rc|pre|dev)", re.I)
_PREFIXES = ("release-", "release_", "rel-", "version-", "ver-", "v", "V")


def normalize_tag(tag: str, name: str | None = None) -> str:
    """``v2.31.0`` -> ``2.31.0``; ``requests-2.31.0`` / ``pkg@1.2`` / ``sub/v1.2.0`` -> the version part."""
    t = tag.strip()
    if t.startswith("refs/tags/"):
        t = t[len("refs/tags/"):]
    if name:
        for sep in ("-", "@", "_", "/", "/v", "-v", "@v"):
            if t.lower().startswith((name + sep).lower()):
                t = t[len(name) + len(sep):]
                break
    if "/" in t and re.search(r"/v?\d", t):  # Go submodule tags: sub/v1.2.0
        t = t.rsplit("/", 1)[1]
    for p in _PREFIXES:
        if t.startswith(p) and len(t) > len(p) and t[len(p)].isdigit():
            return t[len(p):]
    return t


def normalize_version(text: str) -> str:
    t = text.strip().strip("'’")
    t = re.sub(r"\.x$", "", t, flags=re.I)
    return t[1:] if t[:1] in ("v", "V") and t[1:2].isdigit() else t


def is_prerelease(v: str) -> bool:
    nv = normalize_version(v)
    if Version is not None:
        try:
            return Version(nv).is_prerelease
        except InvalidVersion:
            pass
    return bool(_PRE.search(nv))


def sort_key(v: str):
    nv = normalize_version(v)
    if Version is not None:
        try:
            return (1, Version(nv), "")
        except InvalidVersion:
            pass
    parts = re.split(r"[.\-+_]", nv)
    nums = tuple(int(p) if p.isdigit() else -1 for p in parts)
    return (0, nums, nv)


def equal(a: str, b: str) -> bool:
    na, nb = normalize_version(a), normalize_version(b)
    if na == nb:
        return True
    if Version is not None:
        try:
            return Version(na) == Version(nb)
        except InvalidVersion:
            return False
    strip = lambda x: re.sub(r"(\.0)+$", "", x)  # noqa: E731
    return strip(na) == strip(nb)


def in_series(version: str, series: str) -> bool:
    """``2.31.2`` is in the ``2.31`` series; ``2.310`` is not."""
    nv, ns = normalize_version(version), normalize_version(series)
    return nv.startswith(ns + ".") or nv.startswith(ns + "-") or nv.startswith(ns + "+")


def match_tags(version_text: str, tags: list[str], *, name: str | None = None) -> dict:
    """Tags for a version the user wrote -> ``{how, chosen, exact, candidates}``.

    ``how`` is ``exact``, ``prefix`` (the highest tag in the series is chosen)
    or ``none``. Tags are compared after :func:`normalize_tag`.
    """
    want = normalize_version(version_text)
    exact = [t for t in tags if equal(normalize_tag(t, name), want)]
    if exact:
        # prefer the literal spelling, then v-prefixed, then the rest (stable order)
        exact.sort(key=lambda t: (normalize_tag(t, name) != want, not t.startswith("v"), t))
        return {"how": "exact", "chosen": exact[0], "exact": exact, "candidates": exact}
    series = [t for t in tags if in_series(normalize_tag(t, name), want)]
    if series:
        series.sort(key=lambda t: sort_key(normalize_tag(t, name)))
        releases = [t for t in series if not is_prerelease(normalize_tag(t, name))] or series
        return {"how": "prefix", "chosen": releases[-1], "exact": [], "candidates": series}
    return {"how": "none", "chosen": None, "exact": [], "candidates": []}


def newest(versions: list[str], *, name: str | None = None, include_pre: bool = False) -> str | None:
    """The newest version/tag; pre-releases only when nothing else exists or ``include_pre``."""
    vs = [v for v in versions if re.search(r"\d", normalize_tag(v, name))]
    if not vs:
        return None
    rel = vs if include_pre else ([v for v in vs if not is_prerelease(normalize_tag(v, name))] or vs)
    return max(rel, key=lambda v: sort_key(normalize_tag(v, name)))


def _npm_range(spec: str, version: str) -> bool | None:
    """Caret/tilde/x-ranges of npm/cargo, enough for 'max satisfying' checks."""
    try:
        v = [int(x) for x in normalize_version(version).split("-")[0].split(".")[:3]]
    except ValueError:
        return None
    v += [0] * (3 - len(v))
    s = spec.strip()
    m = re.fullmatch(r"([\^~]?)v?(\d+)(?:\.(\d+|x|\*))?(?:\.(\d+|x|\*))?", s)
    if not m:
        return None
    op = m.group(1)
    base = [int(x) if x and x.isdigit() else None for x in (m.group(2), m.group(3), m.group(4))]
    lo = [x or 0 for x in base]
    if v < lo:
        return False
    if op == "^":
        if lo[0] > 0:
            return v[0] == lo[0]
        if lo[1] > 0:
            return v[0] == 0 and v[1] == lo[1]
        return v[:3] == lo[:3]
    if op == "~":
        return v[0] == lo[0] and (base[1] is None or v[1] == lo[1])
    # bare x-range / exact
    return all(b is None or vv == b for vv, b in zip(v, base))


def satisfies(spec: str | None, version: str, *, ecosystem: str = "pypi") -> bool | None:
    """Does ``version`` satisfy the constraint ``spec``? None when it cannot be decided."""
    if not spec or spec in ("*", ""):
        return True
    if ecosystem in ("npm", "cargo"):
        return _npm_range(spec, version)
    if SpecifierSet is not None:
        try:
            return SpecifierSet(spec).contains(normalize_version(version), prereleases=True)
        except (InvalidSpecifier, InvalidVersion):
            return None
    m = re.fullmatch(r"==\s*v?([\w.+-]+)", spec.strip())
    return equal(m.group(1), version) if m else None
