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
# the calls whose first argument is a translation key, across mappings and loaders
_TRANSLATE_CALL = re.compile(
    r"\b(?:Component\.translatable(?:WithFallback)?|Text\.translatable(?:WithFallback)?|"
    r"new\s+(?:TranslatableText|TranslationTextComponent|TranslatableComponent|ChatComponentTranslation)|"
    r"I18n\.(?:get|format|translate|translateToLocal|translateToLocalFormatted)|"
    r"StatCollector\.translateToLocal(?:Formatted)?|Language\.getInstance\(\)\.getOrDefault)\s*\(\s*\"([^\"\\\n]+)\"")
# `%s`, `%d`, `%2$s`; `%%` is a literal percent sign
_PLACEHOLDER = re.compile(r"%(?:(\d+)\$)?([a-zA-Z%])")
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
    last); a nested value is kept as its JSON text."""
    dec = json.JSONDecoder()
    ws = re.compile(r"\s*")
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
        at = _line(text, i)
        j = ws.match(text, j).end()
        if text[j:j + 1] != ":":
            raise ValueError(j, "expected :")
        value, j = dec.raw_decode(text, ws.match(text, j + 1).end())
        lf.keys.setdefault(key, []).append((at, value if isinstance(value, str) else json.dumps(value)))
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
        for n, ln in enumerate(text.splitlines(), 1):
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
    ``%2$s`` -> ``{2: "s"}``."""
    out, nxt = {}, 1
    for m in _PLACEHOLDER.finditer(value.replace("%%", "")):
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
        if p.suffix.lower() in (".java", ".kt", ".scala", ".groovy"):
            for m in _TRANSLATE_CALL.finditer(text):
                calls.setdefault(m.group(1), []).append(f"{rel}:{_line(text, m.start())}")
    return literals, calls, stems, n


def _used(key: str, literals: set[str], prefixes: list[str], stems: set[str]) -> str | None:
    """How the code reaches a key, or None: named in full, built from a literal prefix (``"tooltip.mod." + x``),
    or built by the game from a registered id (``item.mod.ruby`` from ``"ruby"`` / ``"mod:ruby"``)."""
    if key in literals:
        return "literal"
    if any(key.startswith(p) for p in prefixes):
        return "prefix"
    parts = key.split(".")
    if len(parts) >= 3 and parts[0] in _REGISTRY_KINDS:
        ns, path = parts[1], parts[2]
        if path in literals or f"{ns}:{path}" in literals or path in stems or ".".join(parts[2:]) in literals:
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
    defaults: dict[str, LangFile] = {}

    def add(kind, key, status, at, other_at, why):
        findings.append({"kind": kind, "key": key, "status": status, "at": at, "other_at": other_at, "why": why})

    for lf in files:
        if lf.error:
            add("invalid_file", None, "statically_verified", f"{lf.path}:{lf.error_line or 1}", None,
                f"{lf.path} is not read as a flat lang file: {lf.error}")
    for ns, group in sorted(by_ns.items()):
        default = next((lf for lf in group if lf.locale == default_locale and not lf.error), None)
        if default:
            defaults[ns] = default
        for lf in group:
            for key, occ in lf.keys.items():
                if len(occ) > 1:
                    first, last = occ[0], occ[-1]
                    add("duplicate_key", key, "statically_verified", f"{lf.path}:{last[0]}", f"{lf.path}:{first[0]}",
                        f"{key} is written {len(occ)} times in {lf.path}; the game keeps the value of line {last[0]}")
        if default is None:
            continue
        for lf in group:
            if lf.locale == default_locale or lf.error:   # a second default file (datagen output) is not a locale
                continue
            for key in default.keys:
                if key not in lf.keys:
                    add("missing_in_locale", key, "statically_verified", f"{default.path}:{default.line(key)}",
                        lf.path, f"{key} is in {default_locale}, not in {lf.locale} (shown in {default_locale} there)")
            for key, occ in lf.keys.items():
                if key not in default.keys:
                    add("extra_in_locale", key, "statically_verified", f"{lf.path}:{occ[0][0]}", default.path,
                        f"{key} is in {lf.locale}, not in the default locale {default_locale}")
                    continue
                want, got = placeholders(default.keys[key][-1][1]), placeholders(occ[-1][1])
                if want != got:
                    add("placeholder_mismatch", key, "statically_verified", f"{lf.path}:{occ[-1][0]}",
                        f"{default.path}:{default.keys[key][-1][0]}",
                        f"{key}: {_fmt(got)} in {lf.locale}, {_fmt(want)} in {default_locale}")
    searched = 0
    if defaults:
        literals, calls, stems, searched = _uses(repo, {lf.path for lf in files})
        known = {k for d in defaults.values() for k in d.keys} | {k for lf in files for k in lf.keys}
        for key, sites in sorted(calls.items()):
            if key in known:
                continue
            ns = next((n for n in key.split(".") if n in defaults), None)
            if ns is None:
                continue   # no dot segment names a namespace of the project: a vanilla or another mod's key
            add("missing_key", key, "statically_verified", sites[0], defaults[ns].path,
                f"{key} is asked for at {', '.join(sites[:3])}{' ...' if len(sites) > 3 else ''}, "
                f"and no lang file of the project defines it")
        if unused:
            prefixes = [lit for lit in literals if len(lit) >= 4 and lit.endswith((".", "_")) and "." in lit[:-1]]
            for ns, d in sorted(defaults.items()):
                if ns == "minecraft":
                    continue   # the project overrides vanilla text: the game names these keys, not the code
                for key in d.keys:
                    if _used(key, literals, prefixes, stems) is None:
                        add("unused_key", key, "strong_inference", f"{d.path}:{d.line(key)}", None,
                            f"{key} is named by no string of the {searched} code and resource file(s) read, and "
                            f"no literal prefix or registered id builds it")
    order = {"invalid_file": 0, "missing_key": 1, "placeholder_mismatch": 2, "duplicate_key": 3,
             "missing_in_locale": 4, "extra_in_locale": 5, "unused_key": 6}
    findings.sort(key=lambda f: (order[f["kind"]], f["at"]))
    return {"files": [{"path": lf.path, "namespace": lf.namespace, "locale": lf.locale, "keys": len(lf.keys)}
                      for lf in files],
            "default_locale": default_locale, "namespaces": sorted(by_ns),
            "no_default": sorted(ns for ns in by_ns if ns not in defaults),
            "searched_files": searched, "findings": findings}


def lookup(repo: Path, default_locale: str = DEFAULT_LOCALE, *, unused: bool = True) -> dict:
    """``verinoda lang``: the check, or ``no_lang`` when the project has no lang file."""
    res = check(repo, default_locale, unused=unused)
    if not res["files"]:
        return {"status": "no_lang", **res,
                "note": "no assets/<namespace>/lang/<locale>.json or .lang file in the project"}
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
    if res["no_default"]:
        out.append(f"  no {res['default_locale']} file in: {', '.join(res['no_default'])} (locales not compared)")
    for f in res["findings"]:
        other = f" <- {f['other_at']}" if f["other_at"] else ""
        out.append(f"  {f['kind']} [{f['status']}] {f['at']}{other} - {f['why']}")
    return "\n".join(out)
