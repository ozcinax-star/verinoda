"""Secrets and personal data out of stored logs and the HTML export; `verinoda secret-scan` (verinoda.scrub)."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, scrub, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# made-up values in the shapes the rules know; none of them is a real credential
GH = "ghp_" + "Ab3dE" * 7 + "x"
AWS = "AKIA" + "IOSFODNN7EXAMPL0"
PEM = ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun\n"
       "VTLw7onLRnrq0/IzW7yWR7QkrmBL7jTKEn5u+qKhbwKfBstIs+bMY2Zkp18gnTxK\n-----END RSA PRIVATE KEY-----")
ANT = "sk-ant-api03-" + "Q7w9E2r4T6y8U1i3O5p7A9s2D4f6" + "-AA"
MAIL = "jane.doe@corp-mail.net"
NO_ENV: dict = {}


# -- the rules -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,rule", [
    (f"push with {GH} now", "github-token"),
    (f"aws_access_key_id={AWS}", "aws-access-key"),
    (PEM, "private-key"),
    (f"ANTHROPIC_API_KEY is {ANT}", "api-key"),
    ("xoxb-" + "1234567890-abcdefghij", "slack-token"),
    ("glpat-" + "a1B2c3D4e5F6g7H8i9J0k", "gitlab-token"),
    ("AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q", "google-api-key"),
    ("sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc", "stripe-key"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U", "jwt"),
    ("connecting to postgres://app:s3cr3tPw9@db.internal:5432/orders", "url-password"),
    ("Authorization: Bearer abcDEF123456ghiJKL789", "auth-header"),
    ('db_password = "Tr0ub4dor&3xyz"', "assigned-secret"),
    ('{"client_secret": "9f8e7d6c5b4a3f2e1d0c"}', "assigned-secret"),
    (f"reported by {MAIL}", "email"),
])
def test_each_rule_takes_out_its_shape_and_names_itself(text, rule):
    out = scrub.redact(text, env=NO_ENV)
    assert f"<redacted:{rule}>" in out
    assert scrub.scan(out, env=NO_ENV) == []  # a marker is never a finding
    hits = scrub.scan(text, env=NO_ENV)
    assert [h["rule"] for h in hits] == [rule]
    assert set(hits[0]) == {"rule", "line", "column", "length"}  # where and what kind, never the value


def test_only_the_secret_part_goes_and_the_line_structure_stays():
    text = (f"line one\nurl postgres://app:s3cr3tPw9@db/x\n{PEM}\nAuthorization: Bearer abcDEF123456ghiJKL789\n"
            "last line\r\nwindows line\r\n")
    out = scrub.redact(text, env=NO_ENV)
    assert out.count("\n") == text.count("\n") and out.count("\r\n") == text.count("\r\n")
    lines = out.splitlines()
    assert lines[1] == "url postgres://app:<redacted:url-password>@db/x"   # user and host stay
    assert lines[2] == "<redacted:private-key>" and lines[3] == lines[4] == ""
    assert lines[6] == "Authorization: Bearer <redacted:auth-header>"
    assert lines[7] == "last line"
    hit = scrub.scan(text, env=NO_ENV)[0]
    assert (hit["line"], hit["column"]) == (2, 20)


def test_the_value_of_a_secret_variable_of_this_environment_goes_wherever_it_is():
    env = {"DEPLOY_TOKEN": "q9w8e7r6t5y4", "HOME": "/home/me", "PWD": "/work/proj", "SHORT_KEY": "abc",
           "SSH_AUTH_SOCK": "/tmp/agent.sock"}
    out = scrub.redact("echo q9w8e7r6t5y4 into /home/me from /work/proj", env=env)
    assert out == "echo <redacted:env:DEPLOY_TOKEN> into /home/me from /work/proj"


@pytest.mark.parametrize("text", [
    "api_key = os.environ.get('API_KEY')",
    "password=getpass()",
    'password = "${DB_PASSWORD}"',
    "token: <your-token-here>",
    "result = push(uri='NEO4J_URI', password='NEO4J_PASSWORD')",
    '"facts_per_1k_tokens": mx.per_1k(found, toks),',
    "the lexer yields a token = next_token",
    "git@github.com:org/repo.git",
    "return this@Outer.value",
    "icon@2x.png and logo@3x.webp",
    "mail me at someone@example.com",
    '"import pytest\\n\\n@pytest.fixture\\ndef x(): pass"',
    "java.lang.Object@6d06d69c",
    "postgres://user:PASSWORD@host/db",
    "task-scheduler-configuration-and-more",
    "-----BEGIN CERTIFICATE-----",
    "AKIA is a prefix, AKIAshort is not a key",
])
def test_ordinary_code_placeholders_and_non_addresses_stay(text):
    assert scrub.redact(text, env=NO_ENV) == text


def test_the_ui_page_itself_passes_the_scan():
    from importlib import resources

    for name in ("index.html", "app.js", "app.css", "graph3d.js"):
        text = resources.files("verinoda.ui").joinpath("static", name).read_text(encoding="utf-8")
        assert scrub.scan(text, env=NO_ENV) == [], name


def test_a_large_log_is_redacted_quickly():
    import time

    line = "2026-09-30 12:00:01 INFO [main] com.example.Service - request id=12345 user=bob path=/api/v1 took 12ms\n"
    text = line * 40_000 + f"leaked {GH}\n" + line * 10_000  # about 5 MB, the most a run's log keeps
    t0 = time.monotonic()
    out = scrub.redact(text, env=NO_ENV)
    assert GH not in out and out.count("\n") == text.count("\n")
    assert time.monotonic() - t0 < 5


# -- `verinoda secret-scan` ------------------------------------------------------------------------------------------

def _stored(repo: Path) -> tuple[Path, Path]:
    run = repo / ".verinoda" / "runs" / "exp_1" / "stdout.txt"
    run.parent.mkdir(parents=True)
    run.write_bytes(f"collected 3 items\r\nlogin as {MAIL}\r\ntoken {GH}\r\n3 passed\r\n".encode())
    log = repo / ".verinoda" / "logs" / "latest-0123456789.log"
    log.parent.mkdir(parents=True)
    log.write_text(f"[12:00] start\n{PEM}\n[12:01] stop\n", encoding="utf-8")
    patch = repo / ".verinoda" / "runs" / "dba_1" / "change.patch"  # code the ledger applies again: not a log
    patch.parent.mkdir(parents=True)
    patch.write_text(f"+KEY = '{GH}'\n", encoding="utf-8")
    return run, log


def test_secret_scan_finds_what_older_versions_stored_and_fix_redacts_it(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(scrub, "_env_secrets", lambda env: [])
    repo = tmp_path / "proj"
    run, log = _stored(repo)
    before = {p: p.read_bytes() for p in (run, log)}
    assert cli.main(["secret-scan", "--repo", str(repo), "--json"]) == 1
    res = json.loads(capsys.readouterr().out)
    assert res["scanned"] == [".verinoda/runs/exp_1/stdout.txt", ".verinoda/logs/latest-0123456789.log"]
    assert {(f["evidence"], f["rule"]) for f in res["findings"]} == {
        (".verinoda/runs/exp_1/stdout.txt:2", "email"), (".verinoda/runs/exp_1/stdout.txt:3", "github-token"),
        (".verinoda/logs/latest-0123456789.log:2", "private-key")}
    assert all(f["status"] == "strong_inference" for f in res["findings"])
    out = json.dumps(res)
    assert GH not in out and MAIL not in out and "MIIE" not in out  # a finding never carries the value
    assert cli.main(["secret-scan", "--repo", str(repo)]) == 1
    text = capsys.readouterr().out
    assert ".verinoda/runs/exp_1/stdout.txt:3:7  github-token" in text and "--fix" in text
    assert cli.main(["secret-scan", "--repo", str(repo), "--fix"]) == 0
    for p, old in before.items():
        new = p.read_bytes()
        assert new.count(b"\n") == old.count(b"\n") and new.count(b"\r\n") == old.count(b"\r\n")
    assert run.read_bytes().startswith(b"collected 3 items\r\nlogin as <redacted:email>\r\n")
    capsys.readouterr()
    assert cli.main(["secret-scan", "--repo", str(repo)]) == 0
    assert "0 finding(s)" in capsys.readouterr().out


def test_secret_scan_of_named_files(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(scrub, "_env_secrets", lambda env: [])
    clean, dirty = tmp_path / "a.txt", tmp_path / "b.txt"
    clean.write_text("nothing here\n", encoding="utf-8")
    dirty.write_text(f"x\n{AWS}\n", encoding="utf-8")
    assert cli.main(["secret-scan", str(clean), "--repo", str(tmp_path)]) == 0
    capsys.readouterr()
    assert cli.main(["secret-scan", str(clean), str(dirty), "--repo", str(tmp_path), "--json"]) == 1
    res = json.loads(capsys.readouterr().out)
    assert [f["evidence"] for f in res["findings"]] == ["b.txt:2"]
    with pytest.raises(SystemExit, match="not a file"):
        cli.main(["secret-scan", str(tmp_path / "missing.txt"), "--repo", str(tmp_path)])


# -- stored logs: written redacted -----------------------------------------------------------------------------------

def test_a_log_copied_for_trace_log_is_kept_redacted_with_its_lines(tmp_path):
    from verinoda import trace_log

    repo = tmp_path / "proj"
    repo.mkdir()
    log = tmp_path / "elsewhere" / "latest.log"
    log.parent.mkdir()
    log.write_text(f"[12:00] boot\n[12:00] auth with {GH} for {MAIL}\n[12:01] done\n", encoding="utf-8")
    rel, lines = trace_log._log_in_repo(repo, log)
    kept = repo / rel
    assert rel.startswith(".verinoda/logs/") and len(lines) == 3
    assert lines[1] == "[12:00] auth with <redacted:github-token> for <redacted:email>"
    assert kept.read_text(encoding="utf-8").splitlines() == lines
    assert scrub.scan_files(repo, env=NO_ENV)["findings"] == []
    assert GH in log.read_text(encoding="utf-8")  # the user's own file is read, never changed


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def orders(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = tmp_path / "orders_app"
    shutil.copytree(ROOT / "examples" / "orders_app", repo,
                    ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", ".pytest_cache", "*.db"))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    st = open_store(repo)
    yield repo, st
    st.close()


@pytest.mark.experiment
def test_an_experiments_logs_and_evidence_carry_no_secret_it_printed(orders):
    from verinoda import experiments

    repo, st = orders
    (repo / "tests" / "test_leak.py").write_text(
        "def test_leak():\n"
        f"    print('connecting with {GH} as {MAIL}')\n"
        "    assert False\n", encoding="utf-8")
    res = experiments.run(st, repo, ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_leak.py"],
                          hypothesis="it leaks", expect="fail")
    logs = Path(res["logs"]["stdout"]).read_text(encoding="utf-8") + Path(res["logs"]["stderr"]).read_text(
        encoding="utf-8")
    assert "<redacted:github-token>" in logs and GH not in logs and MAIL not in logs
    rows = json.dumps([dict(r) for r in st.all("SELECT * FROM evidence")] + [dict(r) for r in st.all(
        "SELECT * FROM experiments")], default=str)
    assert GH not in rows and MAIL not in rows
    assert scrub.scan_files(repo, env=NO_ENV)["findings"] == []


@pytest.mark.experiment
def test_an_agent_reported_output_is_kept_redacted(orders, monkeypatch):
    from verinoda import debug, experiments

    monkeypatch.setattr(experiments, "container_runtime", lambda: None)
    repo, st = orders
    out = f"BUILD FAILED\nusing {ANT}\n    at com.example.Foo.run(Foo.java:3)\n"
    s = debug.start(st, repo, "gradle fails", ["gradlew", "test"], observed_output=out, exit_code=1)
    kept = next((repo / ".verinoda" / "runs").rglob("observed_output.txt"))
    text = kept.read_text(encoding="utf-8")
    assert ANT not in text and text.splitlines()[1] == "using <redacted:api-key>"
    assert ANT not in json.dumps(dict(st.evidence(s["evidence_id"])), default=str)
    assert scrub.scan_files(repo, env=NO_ENV)["findings"] == []
    # the same output without a secret is kept byte for byte
    s2 = debug.attempt(st, repo, hypothesis="again", observed_output="BUILD SUCCESSFUL\r\n", exit_code=0,
                       command=["gradlew", "test"])
    kept_all = (repo / ".verinoda" / "runs").rglob("observed_output.txt")
    assert any(p.read_bytes() == b"BUILD SUCCESSFUL\r\n" for p in kept_all)
    assert s2["outcome"] == "pass"


# -- the HTML export --------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def glow(tmp_path_factory):
    repo = tmp_path_factory.mktemp("scrub") / "glow"
    shutil.copytree(ROOT / "examples" / "glow_mod", repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__"))
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "DEPLOY.md").write_text(
        "# Deploying the mod\n\n"
        f"Ask {MAIL} for access; the release job reads its token.\n\n"
        f"## Upload with {GH}\n\n"
        "```\n"
        f"export CF_API_KEY={AWS}\n"
        "```\n", encoding="utf-8")
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
        from verinoda.claims import Claims

        Claims(st, repo).create(f"the upload is reviewed by {MAIL}", project=str(repo), snapshot=st.latest_snapshot(),
                                subjects=["docs/DEPLOY.md"])
    finally:
        st.close()
    return repo


def test_the_html_export_passes_a_secret_scan(glow, tmp_path, monkeypatch):
    from verinoda.ui import export

    monkeypatch.setattr(scrub, "_env_secrets", lambda env: [])
    with monkeypatch.context() as m:  # without the redaction the export would carry them
        m.setattr(export, "redact", lambda text: text)
        raw = json.dumps(export.build(glow), ensure_ascii=False)
    assert GH in raw and MAIL in raw
    out = export.write(glow, tmp_path / "graph.html")
    html = Path(out["path"]).read_text(encoding="utf-8")
    assert GH not in html and MAIL not in html and "<redacted:github-token>" in html.replace("\\u003c", "<").replace(
        "\\u003e", ">")
    res = scrub.scan_files(glow, [Path(out["path"])], env=NO_ENV)
    assert res["findings"] == [] and res["clean"]
    # the export at its default place is one of the files `secret-scan` checks by default
    default = export.write(glow)
    assert Path(default["path"]) in scrub.stored_files(glow)
