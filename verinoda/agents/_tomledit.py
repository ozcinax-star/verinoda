"""A delimited, Verinoda-owned ``[mcp_servers.verinoda]`` table in a Codex config.toml.

The table is written between two marker comments and appended at the end of
the file, so the rest of the file (comments, other ``[mcp_servers.x]`` tables,
formatting) is never rewritten. ``tomllib`` validates the file before and
after every edit; an edit that would change anything but our table is refused.
"""

from __future__ import annotations

import re
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - python 3.10
    import tomli as tomllib

BEGIN = ("# >>> verinoda-managed v1: added by `verinoda install`; "
         "`verinoda uninstall` removes this block")
END = "# <<< verinoda-managed v1"
BLOCK_RE = re.compile(
    r"^# >>> verinoda-managed v1\b[^\r\n]*\r?\n.*?^# <<< verinoda-managed v1\b[^\r\n]*(?:\r?\n|\Z)",
    re.S | re.M,
)
TOMLDecodeError = tomllib.TOMLDecodeError


def loads(text: str) -> dict:
    return tomllib.loads(text)


def toml_str(s: str) -> str:
    """A TOML string literal; literal ('...') form when possible (readable Windows paths)."""
    if "'" not in s and not any(ord(c) < 0x20 or ord(c) == 0x7F for c in s):
        return f"'{s}'"
    out = ['"']
    for ch in s:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def render_block(name: str, entry: dict, nl: str = "\n") -> str:
    lines = [BEGIN, f"[mcp_servers.{name}]", f"command = {toml_str(entry['command'])}",
             "args = [" + ", ".join(toml_str(a) for a in entry["args"]) + "]"]
    if "startup_timeout_sec" in entry:
        lines.append(f"startup_timeout_sec = {int(entry['startup_timeout_sec'])}")
    lines.append(END)
    return nl.join(lines) + nl


def find_block(text: str) -> re.Match | None:
    return BLOCK_RE.search(text)


def block_key(block: str) -> str:
    """Normalised block text used for hashing (line endings and trailing newline ignored)."""
    return block.replace("\r\n", "\n").rstrip("\n")


def without(data: dict, name: str) -> dict:
    """``data`` minus ``mcp_servers.<name>``; an emptied ``mcp_servers`` is dropped too."""
    out = dict(data)
    servers = dict(out.get("mcp_servers") or {})
    servers.pop(name, None)
    if servers:
        out["mcp_servers"] = servers
    else:
        out.pop("mcp_servers", None)
    return out


def append_block(text: str, block: str, nl: str) -> tuple[str, bool, bool]:
    """Append ``block``; returns (new_text, added_eol, added_separator)."""
    added_eol = bool(text) and not text.endswith("\n")
    if added_eol:
        text += nl
    added_sep = bool(text.strip()) and not text.endswith(nl + nl)
    if added_sep:
        text += nl
    return text + block, added_eol, added_sep


def remove_block(text: str, m: re.Match, nl: str, *, added_eol: bool, added_sep: bool) -> str:
    start, end = m.span()
    at_end = end == len(text)
    new = text[:start] + text[end:]
    if at_end and added_sep and new.endswith(nl + nl):
        new = new[: -len(nl)]
    if at_end and added_eol and new.endswith(nl):
        new = new[: -len(nl)]
    return new
