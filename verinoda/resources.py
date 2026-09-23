"""Resource ids in code and data files, and the files they name.

Game data packs and resource packs (Minecraft's, and anything laid out the same
way) name their files by *resource location*: ``ns:path`` is the file
``data/<ns>/<registry>/<path>.<ext>`` or ``assets/<ns>/<kind>/<path>.<ext>``.
``mymod:wisp_death`` is ``data/mymod/function/wisp_death.mcfunction``,
``mymod:item/lantern_staff`` is ``assets/mymod/models/item/lantern_staff.json``,
``#mymod:glow_ore`` is the tag ``data/mymod/tags/block/glow_ore.json`` and
``mymod:ember_vein`` in a biome file is
``data/mymod/worldgen/placed_feature/ember_vein.json``. Code and data refer to
each other only through these strings, so a question about one layer ("where
does the wisp heart drop?") often has its answer in the other.

* :func:`resource_keys` - the ids a repository file defines, with its registry;
* :func:`refs_in_line` - the ids one line refers to, with the registries the
  context allows: ``function ns:x`` (functions), ``advancement ... ns:x``,
  ``"parent": "ns:x"`` (models), ``"feature": "ns:x"`` (worldgen) ...;
  translation keys ``"item.ns.x"``; ``Identifier.of("ns", "x")``; unquoted ids
  in data files; and bare quoted names (``"wisp_death"``) only as an argument of
  an id constructor (``Identifier.of(MOD_ID, "x")``) or of a project helper
  whose name says what it loads (``fonksiyon(p, pos, "wisp_death")``);
* :class:`KeyMap` - resolution of a reference to the files that define it.

Only repositories that are packs or mods get keys at all (a ``pack.mcmeta``, or
a Fabric/Quilt/Forge/NeoForge manifest), and inside them only files under a
known registry or asset folder: ``data/processed/train/labels.csv`` in a machine
learning project is not a resource.

A link is a lead for ranking and rendering. A claim built from one cites the
line that names the id. It is an inference, reported as such, when the
namespace is assumed (a bare name) or when the line does not say which kind of
resource it names and files of several kinds carry that id (``sure=False``).
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field

RES_PATH = re.compile(r"(?:^|/)(data|assets)/([a-z0-9_.-]+)/([a-z0-9_]+)/(.+)\.([A-Za-z0-9]+)$")
MANIFESTS = frozenset({"pack.mcmeta", "fabric.mod.json", "quilt.mod.json", "mods.toml", "neoforge.mods.toml",
                       "mcmod.info"})
# Registry folders of data packs (1.20 plural and 1.21 singular names) and asset kinds of resource packs.
DATA_REGISTRIES = frozenset("""function functions tags recipe recipes loot_table loot_tables advancement advancements
    predicate predicates item_modifier item_modifiers structure structures worldgen dimension dimension_type
    damage_type chat_type trim_material trim_pattern banner_pattern painting_variant wolf_variant enchantment
    enchantment_provider jukebox_song instrument trial_spawner test_instance test_environment timeline
    cat_variant pig_variant frog_variant cow_variant chicken_variant wolf_sound_variant""".split())
ASSET_KINDS = frozenset("""models textures blockstates sounds lang shaders particles font atlases items equipment
    texts post_effect waypoint_style""".split())
# Resource kinds whose files an id names under a sub-folder (``item/x`` for models/item/x.json)
# while plain item/block ids name ``x``: both keys are registered.
_SUBFOLDER_KINDS = {"models", "textures"}
_SUBFOLDERS = ("item/", "block/", "entity/")
FUNCTION_KINDS = ("function", "functions")
ITEM_KINDS = ("items", "models", "textures")
DATA_REF_SUFFIXES = (".mcfunction", ".json", ".mcmeta", ".snbt")   # unquoted ids are read in these

_ID = r"(#?)([a-z0-9_.-]+):([a-z0-9_./-]*[a-z_][a-z0-9_./-]*)"
FUNCTION_RX = re.compile(r"\bfunction\s+" + _ID)
QUOTED_ID_RX = re.compile(r"[\"']" + _ID + r"[\"']")
UNQUOTED_ID_RX = re.compile(r"(?<![\w:./-])" + _ID + r"(?![\w:])")
LANG_RX = re.compile(r"[\"'](item|block|entity|effect|enchantment|biome|container|advancements?|death|subtitles?)"
                     r"\.([a-z0-9_]+)\.([a-z0-9_./]+)[\"']")
BARE_RX = re.compile(r"[\"']([a-z0-9_]+(?:/[a-z0-9_]+)*(?:\.(?:png|json|ogg|mcfunction|nbt))?)[\"']")
# Identifier.of("ns", "path"), new ResourceLocation("ns", "path"), fromNamespaceAndPath("ns", "path")
NS_PATH_CALL_RX = re.compile(r"\b(?:of|fromNamespaceAndPath|Identifier|ResourceLocation|tryBuild)\s*\(\s*"
                             r"\"([a-z0-9_.-]+)\"\s*,\s*\"([a-z0-9_./-]+)\"")
# calls that turn a bare name into an id: the constructors, and helpers taking the mod id
ID_CALLS = frozenset("of fromnamespaceandpath withdefaultnamespace identifier resourcelocation id trybuild "
                     "trydefault location key modid".split())
COLLECTION_RECEIVERS = frozenset("set list map enumset stream arrays immutablelist immutableset immutablemap "
                                 "optional collections objects string pattern".split())
MOD_ID_ARG = re.compile(r"\b(?:MOD_?ID|MODID|NAMESPACE|NS)\b")
# a project helper's name says which kind of resource its string argument names
HELPER_KINDS: list[tuple[re.Pattern, tuple[str, ...]]] = [
    (re.compile(r"(function|fonksiyon|mcfunction|datapack|runfunc|calistir|tetikle)"), FUNCTION_KINDS),
    (re.compile(r"(dimension|boyut)"), ("dimension", "dimension_type")),
    (re.compile(r"(sound|ses)"), ("sounds",)),
    (re.compile(r"(texture|doku)"), ("textures",)),
    (re.compile(r"(model)"), ("models", "items")),
    (re.compile(r"(recipe|tarif)"), ("recipe", "recipes")),
    (re.compile(r"(loot|ganimet)"), ("loot_table", "loot_tables")),
    (re.compile(r"(advancement|basarim)"), ("advancement", "advancements")),
    (re.compile(r"(structure|yapi)"), ("structure", "structures", "worldgen/structure")),
]
# mcfunction command words -> the registries of the id that follows them in the same command
COMMAND_KINDS: list[tuple[re.Pattern, tuple[str, ...]]] = [
    (re.compile(r"\badvancement\s+(?:grant|revoke)\b[^;]*$"), ("advancement", "advancements")),
    (re.compile(r"\bloot\b.*\bloot\s*$"), ("loot_table", "loot_tables")),
    (re.compile(r"\brecipe\s+(?:give|take)\b[^;]*$"), ("recipe", "recipes")),
    (re.compile(r"\b(?:give|clear)\s+\S+\s*$"), ITEM_KINDS),
    (re.compile(r"\bitem\s+(?:replace|modify)\b.*\b(?:with|modify)?\s*$"), ITEM_KINDS + ("item_modifier",
                                                                                          "item_modifiers")),
    (re.compile(r"\b(?:id|Item|item)\s*:\s*[\"']?$"), ITEM_KINDS),
    (re.compile(r"\bexecute\b.*\bin\s+$"), ("dimension",)),
    (re.compile(r"\bplace\s+feature\s+$"), ("worldgen/configured_feature",)),
    (re.compile(r"\bplace\s+(?:structure|jigsaw)\s+$"), ("worldgen/structure", "worldgen/template_pool")),
    (re.compile(r"\bplace\s+template\s+$"), ("structure", "structures")),
    (re.compile(r"\bplaysound\s+$"), ("sounds",)),
    (re.compile(r"\bparticle\s+$"), ("particles",)),
    (re.compile(r"\bdamage\b.*\s$"), ("damage_type",)),
    (re.compile(r"\bif\s+predicate\s+$|\bunless\s+predicate\s+$"), ("predicate", "predicates")),
]
# JSON keys -> registries of the id value
JSON_KEY_KINDS: dict[str, tuple[str, ...]] = {
    "function": FUNCTION_KINDS, "functions": FUNCTION_KINDS,
    "parent": ("models", "advancement", "advancements"), "model": ("models", "items"),
    "texture": ("textures",), "particle": ("textures",), "all": ("textures",), "layer0": ("textures",),
    "layer1": ("textures",), "layer2": ("textures",), "side": ("textures",), "top": ("textures",),
    "bottom": ("textures",), "front": ("textures",), "end": ("textures",),
    "recipe_id": ("recipe", "recipes"), "recipe": ("recipe", "recipes"), "recipes": ("recipe", "recipes"),
    "loot_table": ("loot_table", "loot_tables"), "table": ("loot_table", "loot_tables"),
    "feature": ("worldgen/placed_feature", "worldgen/configured_feature"),
    "features": ("worldgen/placed_feature", "worldgen/configured_feature"),
    "biome": ("worldgen/biome",), "biomes": ("worldgen/biome",),
    "noise_settings": ("worldgen/noise_settings",), "settings": ("worldgen/noise_settings",),
    "structure": ("worldgen/structure", "structure", "structures"),
    "processors": ("worldgen/processor_list",), "pool": ("worldgen/template_pool",),
    "start_pool": ("worldgen/template_pool",), "fallback": ("worldgen/template_pool",),
    "carvers": ("worldgen/configured_carver",), "noise": ("worldgen/noise",),
    "density_function": ("worldgen/density_function",),
    "item": ITEM_KINDS, "result": ITEM_KINDS, "icon": ITEM_KINDS,
    "sound": ("sounds",), "sounds": ("sounds",), "sound_event": ("sounds",),
    "advancement": ("advancement", "advancements"), "predicate": ("predicate", "predicates"),
    "modifier": ("item_modifier", "item_modifiers"), "damage_type": ("damage_type",),
}
_JSON_KEY_BEFORE = re.compile(r"[\"']([A-Za-z0-9_]+)[\"']\s*:\s*(?:\[\s*)?$")
MAX_TARGETS = 6          # files one reference may resolve to
MIN_BARE = 6             # a bare name without '_' or '/' needs this many letters (not "tick")


@dataclass(frozen=True)
class Ref:
    line: int
    ns: str                 # "" for a bare name; "#ns" for a tag reference
    path: str
    form: str               # function | id | lang | bare
    kinds: tuple = ()       # registries the context allows (empty: unknown)

    @property
    def rid(self) -> str:
        return f"{self.ns}:{self.path}" if self.ns else self.path


def _split(rel: str):
    m = RES_PATH.search(rel)
    if not m:
        return None
    root, ns, kind, rest, ext = m.groups()
    return root, ns, kind, rest, ext


def resource_keys(rel: str) -> list[tuple[str, str, str]]:
    """``(namespace, path, registry)`` ids the file ``rel`` defines (empty for other files).

    Tags are keyed under ``#ns`` with registry ``tags/<registry>``; worldgen files under
    their worldgen registry (``worldgen/biome``); asset files also by their full path
    (``textures/entity/x.png``), the form code passes to an id constructor.
    """
    parts = _split(rel)
    if not parts:
        return []
    root, ns, kind, rest, ext = parts
    if root == "data":
        if kind not in DATA_REGISTRIES:
            return []
        if kind == "tags":
            reg, _, path = rest.partition("/")
            if reg == "worldgen":
                sub, _, path = path.partition("/")
                reg = f"worldgen/{sub}"
            return [(f"#{ns}", path, f"tags/{reg}")] if path else []
        if kind == "worldgen":
            reg, _, path = rest.partition("/")
            return [(ns, path, f"worldgen/{reg}")] if path else []
        return [(ns, rest, kind)]
    if kind not in ASSET_KINDS:
        return []
    keys = [(ns, rest, kind), (ns, f"{kind}/{rest}.{ext}", kind)]
    if kind in _SUBFOLDER_KINDS and rest.startswith(_SUBFOLDERS):
        keys.append((ns, rest.split("/", 1)[1], kind))
    return keys


def _kinds_from_context(text: str, start: int, data_file: bool) -> tuple[str, ...]:
    before = text[:start]
    m = _JSON_KEY_BEFORE.search(before)
    if m:
        return JSON_KEY_KINDS.get(m.group(1), ())
    if data_file:
        cmd = before.rsplit(" run ", 1)[-1]
        for rx, kinds in COMMAND_KINDS:
            if rx.search(cmd):
                return kinds
    return ()


def _enclosing_call(text: str, pos: int) -> tuple[str, str, str] | None:
    """``(callee, receiver, argument text)`` of the call whose parentheses hold ``pos``."""
    depth = 0
    for i in range(pos - 1, -1, -1):
        c = text[i]
        if c == ")":
            depth += 1
        elif c == "(":
            if depth == 0:
                m = re.search(r"(?:([A-Za-z_][\w]*)\s*\.\s*)?([A-Za-z_][\w]*)\s*$", text[:i])
                if not m:
                    return None
                close = text.find(")", pos)
                return m.group(2), (m.group(1) or ""), text[i + 1:close if close > 0 else len(text)]
            depth -= 1
    return None


def _bare_kinds(text: str, start: int) -> tuple[str, ...] | None:
    """The registries a bare quoted name at ``start`` names, or None when it is not an id at all."""
    call = _enclosing_call(text, start)
    if call is None:
        return None
    callee, receiver, args = call
    low, rec = callee.lower(), receiver.lower()
    if rec in COLLECTION_RECEIVERS:
        return None
    for rx, kinds in HELPER_KINDS:  # fonksiyon(p, pos, "x"), Datapack.run(server, "x")
        if rx.search(low) or rx.search(rec):
            return kinds
    if low in ID_CALLS or MOD_ID_ARG.search(args):
        return ()
    if low.startswith("register") or low in ("kaydet", "registerItem", "registerblock"):
        return ITEM_KINDS + ("blockstates",)
    return None


def refs_in_line(text: str, line: int, *, data_file: bool) -> list[Ref]:
    """Ids ``text`` (line ``line``) refers to, in order, without duplicates."""
    out: list[Ref] = []
    seen: set[tuple[str, str]] = set()

    def add(ns: str, path: str, form: str, kinds: tuple = ()) -> None:
        path = path.rstrip("./")
        if path and (ns, path) not in seen:
            seen.add((ns, path))
            out.append(Ref(line, ns, path, form, kinds))

    for m in FUNCTION_RX.finditer(text):
        tag = m.group(1)
        add(tag + m.group(2), m.group(3), "function", ("tags/function", "tags/functions") if tag else FUNCTION_KINDS)
    for m in LANG_RX.finditer(text):
        add(m.group(2), m.group(3), "lang", ITEM_KINDS if m.group(1) in ("item", "block") else ())
    for m in NS_PATH_CALL_RX.finditer(text):
        add(m.group(1), m.group(2), "id")
    for m in QUOTED_ID_RX.finditer(text):
        add(m.group(1) + m.group(2), m.group(3), "id", _kinds_from_context(text, m.start(), data_file))
    if data_file:
        for m in UNQUOTED_ID_RX.finditer(text):
            add(m.group(1) + m.group(2), m.group(3), "id", _kinds_from_context(text, m.start(), True))
    else:
        for m in BARE_RX.finditer(text):
            name = m.group(1)
            if not (len(name) >= 4 and ("_" in name or "/" in name or len(name) >= MIN_BARE)):
                continue
            if any(r.path == name for r in out):  # Identifier.of("ns", "x"): already read with its namespace
                continue
            kinds = _bare_kinds(text, m.start())
            if kinds is not None:
                add("", name, "bare", kinds)
    return out


@dataclass
class KeyMap:
    """Defined ids -> files, built from a repository's file list."""

    by_key: dict[tuple[str, str], list[tuple[str, str]]] = field(default_factory=dict)
    by_path: dict[str, set[str]] = field(default_factory=dict)      # path -> namespaces defining it
    namespaces: set[str] = field(default_factory=set)

    @classmethod
    def build(cls, files: list[str]) -> "KeyMap":
        """Keys for the files of a pack or mod repository; an empty map for any other repository."""
        if not any(f.rsplit("/", 1)[-1] in MANIFESTS for f in files):
            return cls()
        by_key: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        by_path: dict[str, set[str]] = defaultdict(set)
        for f in files:
            for ns, path, kind in resource_keys(f):
                by_key[(ns, path)].append((f, kind))
                if not ns.startswith("#"):
                    by_path[path].add(ns)
        return cls({k: sorted(v) for k, v in by_key.items()}, dict(by_path), {ns for ns, _ in by_key})

    def signature(self) -> str:
        h = hashlib.sha1()
        for (ns, path), fs in sorted(self.by_key.items()):
            h.update(f"{ns}:{path}={','.join(f for f, _ in fs)}\n".encode("utf-8"))
        return h.hexdigest()

    def resolve(self, ref: Ref) -> list[tuple[str, str]]:
        """``(file, registry)`` pairs a reference names; a bare name only when one namespace defines it.

        When the context names registries (:attr:`Ref.kinds`), only files of those registries
        count; an id naming none of them (a vanilla item, an entity) resolves to nothing.
        """
        if ref.form == "bare":
            nss = self.by_path.get(ref.path) or set()
            if len(nss) != 1:
                return []
            key = (next(iter(nss)), ref.path)
        else:
            key = (ref.ns, ref.path)
        targets = self.by_key.get(key) or []
        if ref.kinds:
            targets = [t for t in targets if t[1] in ref.kinds]
        return targets[:MAX_TARGETS]

    def sure(self, ref: Ref, targets: list[tuple[str, str]]) -> bool:
        """The line pins what it names: the namespace is written and the registry is known or unique."""
        if ref.form == "bare":
            return False
        return bool(ref.kinds) or len({kind for _f, kind in targets}) <= 1
