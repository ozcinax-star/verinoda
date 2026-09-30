"""Minecraft translation keys: the lang files of each locale against the default one, and the code that uses them.

A mod's text lives in ``assets/<ns>/lang/<locale>.json`` (``.lang`` with ``key=value`` lines before 1.13): one
file per locale, ``en_us`` the one the others are translated from. Nothing checks that they agree: a key added to
``en_us`` and never translated shows English in German, a key written twice keeps only its last value, a ``%s``
dropped from a translation loses its argument, and a key the code asks for (``Component.translatable("...")``)
that no file defines shows the raw key in game.

:func:`lang_files` finds the files, :func:`read_lang` reads one with every key's line (duplicates kept),
:func:`check` compares every locale with the default one of its namespace and the code with the default files.
Each finding cites both files it rests on (the default locale's line and the other locale's, the call site and
the default file). A comparison of two files is ``statically_verified``; a key no code names (``unused_key``) is
a search that found nothing, so ``strong_inference`` at most: a key built at run time from a registry id or a
prefix is counted as used, and one built any other way is reported.
"""
from __future__ import annotations

import bisect
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_LOCALE = "en_us"
_LANG_PATH = re.compile(r"(?:^|/)assets/([^/]+)/lang/([A-Za-z]{2,3}_[A-Za-z]{2,3}|[A-Za-z_]+)\.(json|lang)$")
_SKIP = {"build", ".gradle", "out", "bin", "node_modules", ".git", ".verinoda", "run", ".idea", "graphify-out"}
# where a key is named: source code and the other resources (advancements, text components, datapack functions)
_USE_SUFFIXES = (".java", ".kt", ".kts", ".scala", ".groovy", ".js", ".ts", ".json", ".mcfunction", ".snbt",
                 ".toml", ".yml", ".yaml", ".properties")
_STRING = re.compile(r"\"((?:[^\"\\\n]|\\.)*)\"")
# KubeJS and Groovy name keys in single quotes too
_SQ_STRING = re.compile(r"'((?:[^'\\\n]|\\.)*)'")
_SQ_SUFFIXES = (".js", ".ts", ".groovy")
# the comments of a C-family source, matched past its string and char literals so a `//` in a string is kept
_COMMENT = re.compile(r"(\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*')|//[^\n]*|/\*.*?\*/", re.S)
# the calls whose first argument is a translation key, across mappings and loaders; the literal must be the whole
# argument (`"tooltip.mod." + n` and Kotlin's `"tooltip.mod.$n"` are built at run time)
_TRANSLATE_CALL = re.compile(
    r"\b(?:Component\.translatable(?:WithFallback)?|Text\.translatable(?:WithFallback)?|"
    r"new\s+(?:TranslatableText|TranslationTextComponent|TranslatableComponent|ChatComponentTranslation)|"
    r"I18n\.(?:get|format|translate|translateToLocal|translateToLocalFormatted)|"
    r"StatCollector\.translateToLocal(?:Formatted)?|Language\.getInstance\(\)\.getOrDefault)\s*\(\s*\"([^\"\\\n$]+)\""
    r"(?=\s*[,)])")
# `%s`, `%d`, `%2$s`; `%%` is a literal percent sign
_PLACEHOLDER = re.compile(r"%(?:(\d+)\$)?([a-zA-Z%])")
# the game rewrites every `%d` and `%f` (with a width or precision: `%.1f`, `%5d`) to `%s` when it loads a lang file
_NUMERIC = re.compile(r"%(\d+\$)?[\d.]*[df]")
# the registry kinds whose key is `<kind>.<ns>.<path>`, built by the game from the registered id
_REGISTRY_KINDS = {"item", "block", "entity", "effect", "enchantment", "biome", "fluid", "sound", "subtitles",
                   "container", "itemGroup", "itemgroup", "stat", "painting", "attribute", "potion",
                   "death", "advancements", "key", "gamerule", "dimension", "structure", "trim_material",
                   "trim_pattern", "jukebox_song", "instrument", "banner_pattern", "villager", "entity_type",
                   "creativetab", "creative_tab", "config", "tag", "upgrade", "filled_map"}


@dataclass
class LangFile:
    path: str
    namespace: str
    locale: str
    keys: dict[str, list[tuple[int, str]]] = field(default_factory=dict)   # key -> [(line, value)], in order
    error: str | None = None
    error_line: int | None = None

    def line(self, key: str) -> int:
        return self.keys[key][0][0]


def _line(text: str, i: int) -> int:
    return text.count("\n", 0, i) + 1


def _newlines(text: str) -> list[int]:
    return [m.start() for m in re.finditer("\n", text)]


def _line_at(newlines: list[int], i: int) -> int:
    """The line of offset ``i`` from the offsets of the text's newlines (counting from 0 for each key is
    quadratic in the file's size)."""
    return bisect.bisect_left(newlines, i) + 1


def _blank_comments(text: str) -> str:
    """``text`` with its comments turned into spaces, newlines kept, so every offset keeps its line."""
    return _COMMENT.sub(lambda m: m.group(0) if m.group(1) else re.sub(r"[^\n]", " ", m.group(0)), text)


def lang_files(repo: Path) -> list[tuple[str, str, str]]:
    """``(path, namespace, locale)`` of every ``assets/<ns>/lang/<locale>.json|.lang`` file."""
    from verinoda.snapshot import listed_files

    out = []
    for rel in listed_files(Path(repo)):
        rel = Path(rel).as_posix()
        m = _LANG_PATH.search(rel)
        if m and not any(x in _SKIP for x in rel.split("/")[:-1]):
            out.append((rel, m.group(1), m.group(2).lower()))
    return sorted(out)


def _parse_json(text: str, lf: LangFile) -> None:
    """A flat JSON object read key by key, so a key written twice keeps both lines (``json.loads`` keeps the
    last). A number or boolean is kept as its text; an object, array or null fails the whole file, as in game."""
    dec = json.JSONDecoder()
    ws = re.compile(r"\s*")
    newlines = _newlines(text)
    i = ws.match(text, 0).end()
    if text.startswith("\ufeff", i):
        i = ws.match(text, i + 1).end()
    if text[i:i + 1] != "{":
        raise ValueError(i, "expected {")
    i = ws.match(text, i + 1).end()
    if text[i:i + 1] == "}":
        return
    while True:
        if text[i:i + 1] != "\"":
            raise ValueError(i, "expected a key")
        key, j = json.decoder.scanstring(text, i + 1)
        at = _line_at(newlines, i)
        j = ws.match(text, j).end()
        if text[j:j + 1] != ":":
            raise ValueError(j, "expected :")
        j = ws.match(text, j + 1).end()
        try:
            value, end = dec.raw_decode(text, j)
        except RecursionError:
            raise ValueError(j, f"the value of {key} is nested too deeply") from None
        if value is None or isinstance(value, (dict, list)):
            kind = "null" if value is None else "an object" if isinstance(value, dict) else "an array"
            raise ValueError(j, f"the value of {key} is {kind}, not a string")
        lf.keys.setdefault(key, []).append((at, value if isinstance(value, str) else json.dumps(value)))
        j = end
        j = ws.match(text, j).end()
        if text[j:j + 1] == ",":
            i = ws.match(text, j + 1).end()
            continue
        if text[j:j + 1] == "}":
            return
        raise ValueError(j, "expected , or }")


def read_lang(repo: Path, path: str, namespace: str = "", locale: str = "") -> LangFile:
    lf = LangFile(path, namespace, locale)
    try:
        text = (Path(repo) / path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        lf.error = str(e)
        return lf
    if path.endswith(".lang"):
        for n, ln in enumerate(text.removeprefix("﻿").splitlines(), 1):
            s = ln.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            lf.keys.setdefault(k.strip(), []).append((n, v))
        return lf
    try:
        _parse_json(text, lf)
    except (ValueError, json.JSONDecodeError) as e:
        pos = e.pos if isinstance(e, json.JSONDecodeError) else e.args[0]
        lf.error = e.msg if isinstance(e, json.JSONDecodeError) else e.args[1]
        lf.error_line = _line(text, pos)
    return lf


def placeholders(value: str) -> dict[int, str]:
    """``{argument index: conversion}`` of a value's format placeholders: ``%s %s`` -> ``{1: "s", 2: "s"}``,
    ``%2$s`` -> ``{2: "s"}``. ``%d`` and ``%.1f`` are ``s``, as the game loads them."""
    out, nxt = {}, 1
    for m in _PLACEHOLDER.finditer(_NUMERIC.sub(r"%\1s", value.replace("%%", ""))):
        if m.group(1):
            out[int(m.group(1))] = m.group(2)
        else:
            out[nxt] = m.group(2)
            nxt += 1
    return out


def _fmt(ph: dict[int, str]) -> str:
    return " ".join(f"%{k}${v}" for k, v in sorted(ph.items())) or "none"


def _uses(repo: Path, lang_paths: set[str]) -> tuple[set[str], dict[str, list[str]], set[str], int]:
    """The string literals of the code and resources, the translate calls (key -> sites), the resource file stems
    (a model or blockstate named after the id), and how many files were read."""
    from verinoda.snapshot import listed_files

    literals: set[str] = set()
    calls: dict[str, list[str]] = {}
    stems: set[str] = set()
    n = 0
    for rel in listed_files(Path(repo)):
        rel = Path(rel).as_posix()
        if rel in lang_paths or any(x in _SKIP for x in rel.split("/")[:-1]):
            continue
        p = Path(rel)
        if p.suffix.lower() not in _USE_SUFFIXES:
            continue
        if "/assets/" in f"/{rel}" or "/data/" in f"/{rel}":
            stems.add(p.stem)
        try:
            text = (Path(repo) / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        n += 1
        literals.update(m.group(1) for m in _STRING.finditer(text))
        if p.suffix.lower() in _SQ_SUFFIXES:
            literals.update(m.group(1) for m in _SQ_STRING.finditer(text))
        if p.suffix.lower() in (".java", ".kt", ".scala", ".groovy"):
            code = _blank_comments(text)   # a commented-out call asks for nothing
            newlines = _newlines(code)
            for m in _TRANSLATE_CALL.finditer(code):
                calls.setdefault(m.group(1), []).append(f"{rel}:{_line_at(newlines, m.start())}")
    return literals, calls, stems, n


def _used(key: str, literals: set[str], prefixes: list[str], stems: set[str]) -> str | None:
    """How the code reaches a key, or None: named in full, built from a literal prefix (``"tooltip.mod." + x``,
    Kotlin's ``"tooltip.mod.$x"``), or built by the game from a registered id (``item.mod.ruby`` from ``"ruby"`` /
    ``"mod:ruby"``, ``item.mod.tools.ruby_pick`` from ``"tools/ruby_pick"``, ``itemGroup.mod`` from ``"mod"``, the
    1.12 ``tile.mod.ruby_ore.name`` from ``setTranslationKey("mod.ruby_ore")``)."""
    if key in literals:
        return "literal"
    if any(key.startswith(p) for p in prefixes):
        return "prefix"
    parts = key.split(".")
    if len(parts) >= 3 and parts[-1] == "name" and parts[0] in ("tile", "item", "entity", "fluid"):
        if ".".join(parts[1:-1]) in literals or parts[-2] in literals:
            return "registry"
    if len(parts) == 2 and parts[0] in _REGISTRY_KINDS and parts[1] in literals:
        return "registry"
    if len(parts) >= 3 and parts[0] in _REGISTRY_KINDS:
        ns, path, slashed = parts[1], parts[2], "/".join(parts[2:])
        if (path in literals or f"{ns}:{path}" in literals or path in stems or ".".join(parts[2:]) in literals
                or slashed in literals or f"{ns}:{slashed}" in literals):
            return "registry"
    return None


def check(repo: Path, default_locale: str = DEFAULT_LOCALE, *, unused: bool = True) -> dict:
    """Every finding of the lang files of ``repo``: ``{"kind", "key", "status", "at", "other_at", "why"}``."""
    repo = Path(repo)
    default_locale = default_locale.lower()
    found = lang_files(repo)
    files = [read_lang(repo, p, ns, loc) for p, ns, loc in found]
    by_ns: dict[str, list[LangFile]] = {}
    for lf in files:
        by_ns.setdefault(lf.namespace, []).append(lf)
    findings: list[dict] = []
    # namespace -> key -> the default file that defines it (the first by path). The game merges every pack's
    # file of a locale, so a multi-loader layout (common/ and fabric/ each with an en_us) is one default locale.
    defaults: dict[str, dict[str, LangFile]] = {}

    def add(kind, key, status, at, other_at, why):
        findings.append({"kind": kind, "key": key, "status": status, "at": at, "other_at": other_at, "why": why})

    def merged(group: list[LangFile]) -> dict[str, LangFile]:
        out: dict[str, LangFile] = {}
        for lf in group:
            for key in lf.keys:
                out.setdefault(key, lf)
        return out

    for lf in files:
        if lf.error:
            add("invalid_file", None, "statically_verified", f"{lf.path}:{lf.error_line or 1}", None,
                f"{lf.path} is not read as a flat lang file: {lf.error}")
    for ns, group in sorted(by_ns.items()):
        for lf in group:
            for key, occ in lf.keys.items():
                if len(occ) > 1:
                    first, last = occ[0], occ[-1]
                    add("duplicate_key", key, "statically_verified", f"{lf.path}:{last[0]}", f"{lf.path}:{first[0]}",
                        f"{key} is written {len(occ)} times in {lf.path}; the game keeps the value of line {last[0]}")
        dfiles = [lf for lf in group if lf.locale == default_locale and not lf.error]
        if not dfiles:
            continue
        default = defaults[ns] = merged(dfiles)
        many = f" ({len(dfiles)} files)" if len(dfiles) > 1 else ""
        locales: dict[str, list[LangFile]] = {}
        for lf in group:
            if lf.locale != default_locale and not lf.error:
                locales.setdefault(lf.locale, []).append(lf)
        for locale, lfs in sorted(locales.items()):
            have = merged(lfs)
            for key, d in default.items():
                if key not in have:
                    add("missing_in_locale", key, "statically_verified", f"{d.path}:{d.line(key)}", lfs[0].path,
                        f"{key} is in {default_locale}, not in {locale}"
                        f"{f' ({len(lfs)} files)' if len(lfs) > 1 else ''} (shown in {default_locale} there)")
            for lf in lfs:
                for key, occ in lf.keys.items():
                    d = default.get(key)
                    if d is None:
                        add("extra_in_locale", key, "statically_verified", f"{lf.path}:{occ[0][0]}", dfiles[0].path,
                            f"{key} is in {locale}, not in the default locale {default_locale}{many}")
                        continue
                    want, got = placeholders(d.keys[key][-1][1]), placeholders(occ[-1][1])
                    if want != got:
                        add("placeholder_mismatch", key, "statically_verified", f"{lf.path}:{occ[-1][0]}",
                            f"{d.path}:{d.keys[key][-1][0]}",
                            f"{key}: {_fmt(got)} in {locale}, {_fmt(want)} in {default_locale}")
    searched = 0
    if defaults:
        literals, calls, stems, searched = _uses(repo, {lf.path for lf in files})
        known = {k for d in defaults.values() for k in d} | {k for lf in files for k in lf.keys}
        for key, sites in sorted(calls.items()):
            if key in known:
                continue
            # a key with no dot segment naming a namespace of the project is vanilla's or another mod's; an
            # assets/minecraft override repeats only the vanilla keys it changes, so it names no project key
            ns = next((n for n in key.split(".") if n in defaults and n != "minecraft"), None)
            if ns is None:
                continue
            add("missing_key", key, "statically_verified", sites[0], min(d.path for d in defaults[ns].values()),
                f"{key} is asked for at {', '.join(sites[:3])}{' ...' if len(sites) > 3 else ''}, "
                f"and no lang file of the project defines it")
        if unused:
            prefixes = [lit for lit in literals if len(lit) >= 4 and lit.endswith((".", "_")) and "." in lit[:-1]]
            # a Kotlin or Groovy template ("tooltip.mod.$level", "tooltip.mod.${x}") builds keys from its head
            prefixes += [h for h in (lit.partition("$")[0] for lit in literals if "$" in lit)
                         if len(h) >= 4 and "." in h]
            for ns, default in sorted(defaults.items()):
                if ns == "minecraft":
                    continue   # the project overrides vanilla text: the game names these keys, not the code
                for key, d in default.items():
                    if _used(key, literals, prefixes, stems) is None:
                        add("unused_key", key, "strong_inference", f"{d.path}:{d.line(key)}", None,
                            f"{key} is named by no string of the {searched} code and resource file(s) read, and "
                            f"no literal prefix or registered id builds it")
    order = {"invalid_file": 0, "missing_key": 1, "placeholder_mismatch": 2, "duplicate_key": 3,
             "missing_in_locale": 4, "extra_in_locale": 5, "unused_key": 6}

    def place(at: str) -> tuple[str, int]:
        path, _, line = at.rpartition(":")
        return (path, int(line)) if line.isdigit() else (at, 0)

    findings.sort(key=lambda f: (order[f["kind"]], place(f["at"])))
    return {"files": [{"path": lf.path, "namespace": lf.namespace, "locale": lf.locale, "keys": len(lf.keys)}
                      for lf in files],
            "default_locale": default_locale, "namespaces": sorted(by_ns),
            "no_default": sorted(ns for ns in by_ns if ns not in defaults),
            "searched_files": searched, "findings": findings}


def lookup(repo: Path, default_locale: str = DEFAULT_LOCALE, *, unused: bool = True) -> dict:
    """``verinoda lang``: the check; ``no_lang`` when the project has no lang file, ``no_default`` when no
    namespace has the default locale (a typo in ``--default``): nothing was compared, which is not a clean pass."""
    res = check(repo, default_locale, unused=unused)
    if not res["files"]:
        return {"status": "no_lang", **res,
                "note": "no assets/<namespace>/lang/<locale>.json or .lang file in the project"}
    if len(res["no_default"]) == len(res["namespaces"]):
        locales = sorted({f["locale"] for f in res["files"]})
        return {"status": "no_default", **res,
                "note": f"no namespace has a {res['default_locale']} file, so no locale was compared "
                        f"(locales found: {', '.join(locales)})"}
    return {"status": "found", **res}


def render(res: dict) -> str:
    if res["status"] == "no_lang":
        return res["note"]
    counts: dict[str, int] = {}
    for f in res["findings"]:
        counts[f["kind"]] = counts.get(f["kind"], 0) + 1
    out = [f"{len(res['files'])} lang file(s) in {len(res['namespaces'])} namespace(s), default {res['default_locale']}"
           + (f"; {sum(counts.values())} finding(s): " + ", ".join(f"{v} {k}" for k, v in counts.items())
              if counts else "; nothing disagrees")]
    if res["status"] == "no_default":
        out.append(f"  {res['note']}")
    elif res["no_default"]:
        out.append(f"  no {res['default_locale']} file in: {', '.join(res['no_default'])} (locales not compared)")
    for f in res["findings"]:
        other = f" <- {f['other_at']}" if f["other_at"] else ""
        out.append(f"  {f['kind']} [{f['status']}] {f['at']}{other} - {f['why']}")
    return "\n".join(out)
