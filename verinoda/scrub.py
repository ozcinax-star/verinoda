"""Secrets and personal data taken out of what Verinoda stores or hands on: logs and exports.

What Verinoda keeps of a run it did not write itself - an experiment's stdout and stderr, an agent-reported run's
output, a log copied for ``verinoda trace-log`` and the excerpts cited from them - and the HTML file of
``verinoda ui --export`` pass through :func:`redact` before they are written. ``verinoda secret-scan`` runs the same
rules over files (by default those stored files and the export) and reports what still matches; ``--fix`` redacts
the files in place, for what an earlier version stored.

A match is a pattern (a token format, a key block, a ``password=...`` value, an e-mail address, the value of a
secret-looking variable of this environment): it is a heuristic, so a finding is ``strong_inference`` at most, and a
secret with no recognisable shape is not found. A redaction keeps the line structure (a key block becomes one marker
and its empty lines), so the line numbers a claim cites stay right. The marker names the rule, never the value.

Speed: a run's log is up to 5 MB, so a rule is looked for only where its literal anchor (``AKIA``, ``ghp_``, ``://``,
``@``, ``password`` ...) occurs, found by ``str.find``; the rule's pattern is then matched there.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

MARK = "<redacted:{}>"
# a value with one of these in it is a placeholder in a template or a doc, not a secret
_PLACEHOLDER = re.compile(r"example|placeholder|your[_-]|changeme|dummy|redacted|x{6}|\*{3}|\.\.\.", re.I)
# an e-mail-shaped name that is not an address: a Kotlin label, a git remote, a file at a scale
_NOT_MAIL_LOCAL = frozenset({"git", "this", "super", "return", "break", "continue"})
_NOT_MAIL_TLD = frozenset({"png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "js", "mjs", "ts", "css", "json", "java",
                           "kt", "py", "class", "jar", "txt", "md", "html", "xml", "yml", "yaml", "toml", "lock",
                           "test", "invalid", "localhost", "example", "local"})
_NOT_MAIL_DOMAIN = re.compile(r"(?:^|\.)example\.(?:com|org|net)$", re.I)
_SECRET_ENV_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH", re.I)
_NOT_SECRET_ENV = frozenset({"PWD", "OLDPWD", "SSH_AUTH_SOCK", "XAUTHORITY", "GPG_AGENT_INFO"})
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")  # keeps every index


@dataclass(frozen=True)
class Rule:
    name: str
    anchors: tuple[str, ...]  # literal text every match contains (lower case when ``fold``)
    rx: re.Pattern            # matched at the anchor, or searched around it when ``back``
    check: object = None      # a function of the match: False drops it (a placeholder, an ordinary name)
    fold: bool = False        # the anchors are looked for case-insensitively (ASCII)
    back: int = 0             # the match starts up to this many characters before its anchor


def _value_ok(m: re.Match) -> bool:
    v = m.group("v")
    if len(v) < 8 or _PLACEHOLDER.search(v) or v[0] in "$<{%*(" or len(set(v)) < 4:
        return False
    if m.group("q"):  # quoted: anything but a sentence or a constant's name ('NEO4J_PASSWORD')
        return " " not in v and not re.fullmatch(r"[A-Z][A-Z0-9_]*", v)
    if m.string[m.end():m.end() + 1] in ("(", "["):  # unquoted and called or indexed: code, mx.per_1k(...)
        return False
    return bool(re.search(r"[A-Za-z]", v) and re.search(r"\d", v))  # unquoted: not a name such as getpass


def _url_ok(m: re.Match) -> bool:
    v = m.group("v")
    return not (_PLACEHOLDER.search(v) or v[0] in "$<{%*"
                or v.lower() in ("password", "pass", "passwd", "pwd", "secret", "token"))


def _mail_ok(m: re.Match) -> bool:
    local, domain = m.group("local"), m.group("domain")
    before = m.string[max(0, m.start() - 200):m.start()]
    if "://" in re.split(r"\s", before)[-1]:  # the user (or password) and host of a URL, not an address
        return False
    return (local.lower() not in _NOT_MAIL_LOCAL and domain.rsplit(".", 1)[-1].lower() not in _NOT_MAIL_TLD
            and not _NOT_MAIL_DOMAIN.search(domain))


_KEYWORDS = ("password", "passwd", "secret", "token", "api_key", "apikey", "access_key", "accesskey", "private_key",
             "privatekey", "credential")

RULES: tuple[Rule, ...] = (
    # a key block: the BEGIN line, its base64 lines and the END line when it is there
    Rule("private-key", ("-----BEGIN",),
         re.compile(r"-----BEGIN[A-Z0-9 ]{0,30}PRIVATE KEY(?: BLOCK)?-----(?:\r?\n[ \t]*(?:[A-Za-z0-9+/=]{1,120}|"
                    r"[A-Za-z-]+:[^\r\n]*))*(?:\r?\n[ \t]*-----END[A-Z0-9 ]{0,30}PRIVATE KEY(?: BLOCK)?-----)?")),
    Rule("aws-access-key", ("AKIA", "ASIA", "ABIA", "ACCA"),
         re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}(?![A-Za-z0-9])")),
    Rule("github-token", ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
         re.compile(r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})(?!\w)")),
    Rule("gitlab-token", ("glpat-",), re.compile(r"(?<![A-Za-z0-9_])glpat-[A-Za-z0-9_-]{20,}")),
    Rule("slack-token", ("xox",), re.compile(r"(?<![A-Za-z0-9_])xox[abposr]-[A-Za-z0-9-]{10,}")),
    Rule("slack-webhook", ("https://hooks.slack.com/services/",),
         re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]{20,}")),
    Rule("api-key", ("sk-",),
         re.compile(r"(?<![A-Za-z0-9_])sk-(?:ant-|proj-|svcacct-|admin-)?[A-Za-z0-9_-]*[A-Za-z0-9]{20}[A-Za-z0-9_-]*")),
    Rule("stripe-key", ("sk_live_", "sk_test_", "rk_live_", "rk_test_", "pk_live_", "pk_test_"),
         re.compile(r"(?<![A-Za-z0-9_])[srp]k_(?:live|test)_[0-9A-Za-z]{16,}")),
    Rule("google-api-key", ("AIza",), re.compile(r"(?<![A-Za-z0-9_])AIza[0-9A-Za-z_-]{35}")),
    Rule("npm-token", ("npm_",), re.compile(r"(?<![A-Za-z0-9_])npm_[A-Za-z0-9]{36}(?!\w)")),
    Rule("pypi-token", ("pypi-AgE",), re.compile(r"(?<![A-Za-z0-9_])pypi-AgE[A-Za-z0-9_-]{50,}")),
    Rule("jwt", ("eyJ",),
         re.compile(r"(?<![A-Za-z0-9_])eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    # only the password of a URL (scheme://user:PASSWORD@host)
    Rule("url-password", ("://",),
         re.compile(r"(?<=[A-Za-z0-9])://[^\s:/@'\"<>]{1,64}:(?P<v>[^\s/@'\"<>]{1,128})@"), _url_ok),
    # only the value of an Authorization header
    Rule("auth-header", ("authorization", "bearer"),
         re.compile(r"(?i)(?:authorization\s*[:=]\s*[\"']?\s*(?:bearer|basic|token)|bearer)\s+"
                    r"(?P<v>[A-Za-z0-9._~+/-]{16,}=*)"), fold=True),
    # only the value of password = "...", api_key: ..., "client_secret": "..."
    Rule("assigned-secret", _KEYWORDS,
         re.compile(r"(?i)(?:password|passwd|secret|token|api_?key|access_?key|private_?key|credential)[\w-]{0,20}"
                    r"[\"']?\s{0,3}(?:=|:|=>)\s{0,3}(?P<q>[\"']?)(?P<v>[^\s\"'`,;<>(){}\[\]]{1,200})(?P=q)"),
         _value_ok, fold=True),
    Rule("email", ("@",),
         re.compile(r"(?<![\w.%+\\-])(?P<local>[A-Za-z0-9][\w.%+-]{0,63})@(?P<domain>(?:[A-Za-z0-9-]{1,63}\.){1,8}"
                    r"[A-Za-z]{2,24})(?![\w-])"), _mail_ok, back=64),
)


def _env_secrets(env) -> list[tuple[str, str]]:
    """``(name, value)`` of this environment's secret-looking variables (a name with KEY, TOKEN, SECRET ...), longest
    value first; a short value, a path or a flag is left out."""
    out = []
    for k, v in (env if env is not None else os.environ).items():
        if not v or k.upper() in _NOT_SECRET_ENV or not _SECRET_ENV_NAME.search(k):
            continue
        v = v.strip()
        if len(v) < 8 or "/" in v or "\\" in v or " " in v or v.lower() in ("true", "false"):
            continue
        out.append((k, v))
    return sorted(out, key=lambda kv: -len(kv[1]))


def _finds(hay: str, needle: str):
    i = hay.find(needle)
    while i >= 0:
        yield i
        i = hay.find(needle, i + 1)


def _matches(rule: Rule, text: str, folded: str):
    hay = folded if rule.fold else text
    seen = set()
    for anchor in rule.anchors:
        for i in _finds(hay, anchor):
            if not rule.back:
                m = rule.rx.match(text, i)
                if m:
                    yield m
                continue
            for m in rule.rx.finditer(text, max(0, i - rule.back), i + len(anchor) + 300):
                if m.start() <= i < m.end() and m.span() not in seen:  # the match this anchor is in
                    seen.add(m.span())
                    yield m


def _spans(text: str, env=None) -> list[tuple[int, int, str]]:
    """``(start, end, rule)`` of every part of ``text`` to take out, in order, overlaps merged (the rule of the
    earliest span names it)."""
    found = []
    for name, value in _env_secrets(env):
        found += [(i, i + len(value), f"env:{name}") for i in _finds(text, value)]
    folded = text.translate(_ASCII_LOWER)
    for rule in RULES:
        for m in _matches(rule, text, folded):
            if rule.check is not None and not rule.check(m):
                continue
            g = "v" if "v" in rule.rx.groupindex else 0
            found.append((m.start(g), m.end(g), rule.name))
    found.sort(key=lambda s: (s[0], -s[1]))
    out: list[tuple[int, int, str]] = []
    for s, e, name in found:
        if out and s < out[-1][1]:
            if e > out[-1][1]:
                out[-1] = (out[-1][0], e, out[-1][2])
            continue
        out.append((s, e, name))
    return out


def redact(text: str, env=None) -> str:
    """``text`` with every match replaced by ``<redacted:RULE>``; its line breaks are kept. ``env`` (default: this
    process's environment) supplies the secret variables whose values are taken out wherever they occur."""
    if not text:
        return text
    spans = _spans(text, env)
    if not spans:
        return text
    parts, at = [], 0
    for s, e, name in spans:
        parts.append(text[at:s])
        parts.append(MARK.format(name) + "".join(re.findall(r"\r?\n", text[s:e])))
        at = e
    parts.append(text[at:])
    return "".join(parts)


def scan(text: str, env=None) -> list[dict]:
    """What :func:`redact` would take out: the rule, the line and column (1-based) and the length; never the value."""
    out = []
    for s, e, name in _spans(text, env):
        line = text.count("\n", 0, s) + 1
        col = s - (text.rfind("\n", 0, s) + 1) + 1
        out.append({"rule": name, "line": line, "column": col, "length": e - s})
    return out


# -- files: the stored logs and the export -------------------------------------------------------------------------

MAX_FILE_BYTES = 64 * 1024 * 1024
# a debug attempt's change.patch is code the ledger applies again, and runs/blobs/ holds files by content id: not logs
_TEXT_SUFFIXES = (".txt", ".log", ".out", ".err")


def stored_files(repo: Path) -> list[Path]:
    """What Verinoda keeps of runs and logs (``.verinoda/runs/**``, ``.verinoda/logs/**``: text files) and the export
    of ``verinoda ui --export`` at its default place, when they are there."""
    from verinoda.paths import atlas_dir, runs_dir

    out = []
    for base in (runs_dir(repo), atlas_dir(repo) / "logs"):
        if base.is_dir():
            out += sorted(p for p in base.rglob("*") if p.is_file() and p.suffix.lower() in _TEXT_SUFFIXES)
    from verinoda.ui.export import default_path

    exp = default_path(repo)
    if exp.is_file():
        out.append(exp)
    return out


def scan_files(repo: Path, paths: list[Path] | None = None, *, fix: bool = False, env=None) -> dict:
    """Findings in ``paths`` (default :func:`stored_files`), each with its ``file:line`` as evidence; with ``fix``
    the files are rewritten redacted (the rest of their bytes as they were: line endings stay)."""
    repo = Path(repo).resolve()
    files = [Path(p) for p in paths] if paths else stored_files(repo)
    findings, skipped, fixed = [], [], []
    for p in files:
        shown = _shown(repo, p)
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                skipped.append({"file": shown, "why": f"larger than {MAX_FILE_BYTES // (1024 * 1024)} MB"})
                continue
            text = p.read_bytes().decode("utf-8", "replace")
        except OSError as exc:
            skipped.append({"file": shown, "why": f"cannot read: {exc.strerror or exc}"})
            continue
        hits = scan(text, env)
        for h in hits:
            findings.append({**h, "file": shown, "evidence": f"{shown}:{h['line']}", "status": "strong_inference",
                             "basis": "the text has the shape of a secret or of personal data (a pattern, not a "
                                      "check of the value)"})
        if fix and hits:
            p.write_bytes(redact(text, env).encode("utf-8"))
            fixed.append(shown)
    return {"scanned": [_shown(repo, p) for p in files], "findings": findings, "fixed": fixed, "skipped": skipped,
            "clean": not findings or fix,
            "note": "pattern matches: a secret with no recognisable shape is not found"}


def _shown(repo: Path, p: Path) -> str:
    try:
        return p.resolve().relative_to(repo).as_posix()
    except ValueError:
        return str(p)


def render(res: dict) -> str:
    out = [f"scanned {len(res['scanned'])} file(s): {len(res['findings'])} finding(s)"
           + (f", {len(res['fixed'])} file(s) redacted" if res["fixed"] else "")]
    for f in res["findings"]:
        out.append(f"  {f['evidence']}:{f['column']}  {f['rule']} ({f['length']} chars) [{f['status']}]")
    for s in res["skipped"]:
        out.append(f"  skipped {s['file']}: {s['why']}")
    if res["findings"] and not res["fixed"]:
        out.append("redact them in place with --fix (line numbers are kept)")
    return "\n".join(out)
