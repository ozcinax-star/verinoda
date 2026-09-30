"""Ownership and knowledge (verinoda/ownership.py): CODEOWNERS resolution with the rule as evidence, and from git
blame the authors, main author, bus factor and knowledge loss of a file, folder, line range or symbol."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, ownership  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str, author: str = "Bob", date: str | None = None) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": f"{author.lower()}@example.org",
           "GIT_COMMITTER_NAME": author, "GIT_COMMITTER_EMAIL": f"{author.lower()}@example.org"}
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    return subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "init.defaultBranch=main",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()


def _commit(repo: Path, files: dict[str, str | bytes], msg: str, *, author: str, date: str) -> str:
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg, author=author, date=date)
    return _git(repo, "rev-parse", "HEAD")


SVC_ALICE = "".join(f"a{i} = {i}\n" for i in range(6))
SVC = SVC_ALICE + "def run():\n    return a0 + a1\n\n\ndef stop():\n    return None\n"


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> tuple[Path, list[str]]:
    """Alice (last commit 2024) wrote six lines of src/svc.py; Bob added run() and stop() in 2026 and a binary
    file; Carol wrote docs/guide.md; CODEOWNERS owns src/ by @core, *.md by @docs, and leaves src/gen/ unowned."""
    r = tmp_path_factory.mktemp("owners") / "proj"
    r.mkdir()
    _git(r, "init", "-q")
    shas = [
        _commit(r, {"src/svc.py": SVC_ALICE}, "Alice's constants", author="Alice", date="2024-01-10T10:00:00+00:00"),
        _commit(r, {"src/svc.py": SVC, "src/logo.bin": b"\x00\x01\x02binary\x00", "src/gen/out.py": "x = 1\n"},
                "Bob adds run and stop", author="Bob", date="2026-03-01T10:00:00+00:00"),
        _commit(r, {"docs/guide.md": "# Guide\n\nRead me.\n",
                    ".github/CODEOWNERS": "# owners\n*.md @docs-team\n/src/ @core @alice\n/src/gen/\n"
                                          "!/src/keep.py @nobody\n"},
                "Carol writes the guide", author="Carol", date="2026-05-01T10:00:00+00:00"),
    ]
    return r, shas


def _check(r: Path, res: dict) -> None:
    from verinoda.claims import check_status

    for c in res["claims"]:
        evs = [{**e, "relation": "supports", "id": f"evd_{i}"} for i, e in enumerate(c["evidence"])]
        assert check_status(c["status"], evs, claim={"kind": c["kind"], "text": c["text"]}, repo=r) is None, c


def test_a_file_has_its_main_author_bus_factor_and_owner(repo):
    r, shas = repo
    res = ownership.owners(r, "src/svc.py")
    assert res["status"] == "found" and res["files"] == 1 and res["head"] == shas[2]
    # a tie is ordered by e-mail
    assert [(a["name"], a["lines"]) for a in res["authors"]] == [("Alice", 6), ("Bob", 6)]
    assert res["main_author"]["name"] == "Alice"
    assert res["credited_lines"] == 12 and res["bus_factor"]["value"] == 2
    main, bus, loss, owner = res["claims"]
    assert main["status"] == bus["status"] == loss["status"] == "strong_inference"
    assert main["evidence"][0]["source_type"] == "git_history" and main["evidence"][0]["commit_sha"] == shas[2]
    assert shas[2][:10] in main["text"] and "of 12 lines (50%)" in main["text"]
    assert "bus factor of `src/svc.py`" in bus["text"] and " is 2:" in bus["text"]
    assert owner["status"] == "statically_verified"
    assert owner["evidence"][0]["locator"] == ".github/CODEOWNERS:3"
    assert owner["text"].endswith("contains: /src/ @core @alice") and "owned by @core @alice" in owner["text"]
    _check(r, res)


def test_knowledge_loss_counts_the_lines_of_inactive_authors(repo):
    r, _ = repo
    res = ownership.owners(r, "src/svc.py")
    loss = res["knowledge_loss"]
    assert loss["lines"] == 6 and loss["share"] == 0.5 and loss["since"] == "2025-05-01"
    assert [a["name"] for a in loss["authors"]] == ["Alice"] and loss["authors"][0]["last_commit"] == "2024-01-10"
    claim = next(c for c in res["claims"] if "no commit since" in c["text"])
    assert "Alice (6, last commit 2024-01-10)" in claim["text"]
    assert {a["name"]: a["active"] for a in res["authors"]} == {"Alice": False, "Bob": True}
    wide = ownership.owners(r, "src/svc.py", days=1000)
    assert wide["knowledge_loss"]["lines"] == 0 and not any("no commit since" in c["text"] for c in wide["claims"])


def test_a_folder_is_blamed_file_by_file_binary_files_left_out(repo):
    r, _ = repo
    res = ownership.owners(r, "src")
    assert res["files"] == 2 and not res["truncated"]  # svc.py and gen/out.py; logo.bin is binary
    assert [f["path"] for f in res["per_file"]] == ["src/svc.py", "src/gen/out.py"]
    co = res["codeowners"]
    assert co["unowned"] == 1 and co["unowned_files"] == ["src/gen/out.py"]  # a rule with no owners
    assert [(o["line"], o["files"]) for o in co["owned"]] == [(3, 1)]
    assert co["skipped"] and co["skipped"][0]["line"] == 5
    owner = next(c for c in res["claims"] if "CODEOWNERS" in c["text"])
    assert "1 of the 2 file(s) of `src` are owned by @core @alice" in owner["text"]  # the count once
    cut = ownership.owners(r, "src", max_files=1)
    assert cut["truncated"] and cut["files"] == 1
    assert "the first 1 of the 2 file(s) of `src` (in path order)" in cut["claims"][0]["text"]
    cut = ownership.owners(r, max_files=2)  # .github/CODEOWNERS and docs/guide.md
    cut_owner = next(c for c in cut["claims"] if "CODEOWNERS" in c["text"])
    assert "1 of the first 2 of the 4 file(s) of `.` (in path order) are owned by @docs-team" in cut_owner["text"]
    _check(r, res)


def test_the_whole_project_by_default(repo):
    r, _ = repo
    res = ownership.owners(r)
    assert res["target"] == "." and res["files"] == 4  # CODEOWNERS, guide.md, gen/out.py, svc.py
    assert {a["name"] for a in res["authors"]} == {"Alice", "Bob", "Carol"}
    lines = {o["line"]: o["files"] for o in res["codeowners"]["owned"]}
    assert lines == {2: 1, 3: 1}  # *.md -> guide.md; /src/ -> svc.py
    _check(r, res)


def test_a_line_range_and_a_symbol(repo):
    r, _ = repo
    rng = ownership.owners(r, "src/svc.py:1-3")
    assert rng["lines"] == [1, 3] and [a["name"] for a in rng["authors"]] == ["Alice"]
    assert rng["bus_factor"]["value"] == 1 and "src/svc.py:1-3" in rng["claims"][0]["text"]
    sym = ownership.owners(r, "src/svc.py#run")
    assert sym["symbol"] == "run" and sym["lines"] == [7, 8]
    assert [a["name"] for a in sym["authors"]] == ["Bob"] and "`run` (src/svc.py:7-8)" in sym["claims"][0]["text"]
    _check(r, rng)
    _check(r, sym)


def test_uncommitted_lines_are_credited_to_nobody(repo, tmp_path):
    r, _ = repo
    w = tmp_path / "w"
    shutil.copytree(r, w)
    (w / "src" / "svc.py").write_text(SVC + "EXTRA = 1\n", encoding="utf-8")
    res = ownership.owners(w, "src/svc.py")
    assert res["uncommitted_lines"] == 1 and res["credited_lines"] == 12


def test_a_work_tree_that_differs_from_head_is_not_called_head(repo, tmp_path):
    r, shas = repo
    clean = ownership.owners(r, "src/svc.py:7-8")
    ev = clean["claims"][0]["evidence"][0]
    assert ev["locator"] == f"git blame -w -L7,8 {shas[2]} -- src/svc.py" and not clean["modified_files"]
    assert f"at HEAD {shas[2][:10]}" in clean["claims"][0]["text"]
    assert ownership.owners(r, "src")["claims"][0]["evidence"][0]["locator"] == \
        f"git blame -w {shas[2]} -- <each text file git tracks under src>"
    w = tmp_path / "w"
    shutil.copytree(r, w)
    # two lines above Bob's run(): lines 7-8 on disk are Alice's a4, a5 now, not Bob's lines 7-8 at HEAD
    (w / "src" / "svc.py").write_text("NEW = 1\nNEW2 = 2\n" + SVC, encoding="utf-8")
    (w / ".github" / "CODEOWNERS").write_text("/src/ @someone-else\n", encoding="utf-8")
    res = ownership.owners(w, "src/svc.py:7-8")
    assert res["modified_files"] == ["src/svc.py"] and res["main_author"]["name"] == "Alice"
    main = res["claims"][0]
    assert "at HEAD" not in main["text"] and f"in the work tree on HEAD {shas[2][:10]}" in main["text"]
    ev = main["evidence"][0]
    assert ev["locator"] == "git blame -w -L7,8 -- src/svc.py" and ev["meta"]["work_tree"]
    assert any("work tree" in u for u in main["uncertainties"])
    owner = next(c for c in res["claims"] if "CODEOWNERS" in c["text"])
    assert not res["codeowners"]["committed"] and not owner["evidence"][0].get("commit_sha")
    assert any("CODEOWNERS differs from HEAD" in x for x in res["coverage"]["limits"])
    _check(w, res)


def test_a_tie_has_one_main_author_alone_and_in_a_folder(tmp_path):
    r = tmp_path / "t"
    r.mkdir()
    _git(r, "init", "-q")
    _commit(r, {"f.py": "a = 1\nb = 2\n", "g.py": "z = 0\n"}, "one", author="Alice", date="2026-01-01T10:00:00+00:00")
    _commit(r, {"f.py": "a = 1\nb = 2\nc = 3\nd = 4\n"}, "two", author="Bob", date="2026-01-02T10:00:00+00:00")
    alone = ownership.owners(r, "f.py")["main_author"]["name"]
    listed = next(f for f in ownership.owners(r)["per_file"] if f["path"] == "f.py")["main_author"]
    assert alone == listed == "Alice"  # the smaller e-mail, both ways


def test_a_huge_days_value_does_not_crash(repo):
    r, _ = repo
    for days in (100_000, 99_999_999_999):
        res = ownership.owners(r, "src/svc.py", days=days)
        assert res["knowledge_loss"]["lines"] == 0 and res["knowledge_loss"]["since"] == "1970-01-01"
    assert ownership._day(-5) == "1970-01-01"


def test_a_shallow_clone_says_its_oldest_commit_may_not_have_written_the_lines(repo, tmp_path):
    r, _ = repo
    c = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", r.as_uri(), str(c)], check=True, capture_output=True,
                   stdin=subprocess.DEVNULL)
    res = ownership.owners(c, "src/svc.py")
    assert res["claims"][0]["status"] == "strong_inference"
    assert any("shallow clone" in u for u in res["claims"][0]["uncertainties"])


def test_mailmap_joins_an_authors_two_addresses(tmp_path):
    r = tmp_path / "m"
    r.mkdir()
    _git(r, "init", "-q")
    _commit(r, {"a.py": "x = 1\n"}, "one", author="Dan", date="2026-01-01T10:00:00+00:00")
    _commit(r, {"a.py": "x = 1\ny = 2\n", ".mailmap": "Dan <dan@example.org> <dan2@example.org>\n"}, "two",
            author="Dan2", date="2026-01-02T10:00:00+00:00")
    res = ownership.owners(r, "a.py")
    assert [(a["name"], a["lines"]) for a in res["authors"]] == [("Dan", 2)]


def test_codeowners_patterns_follow_githubs_rules():
    def m(pat: str, path: str) -> bool:
        return bool(ownership.pattern_matcher(pat)(path))

    assert m("*", "a/b/c.py") and m("*.js", "x/y/z.js") and not m("*.js", "x/y/z.ts")
    assert m("/build/logs/", "build/logs/a.txt") and not m("/build/logs/", "x/build/logs/a.txt")
    assert m("docs/*", "docs/getting-started.md") and not m("docs/*", "docs/build-app/troubleshooting.md")
    assert m("apps/", "apps/a.py") and m("apps/", "x/apps/a.py") and not m("apps/", "apps")
    assert m("/docs/", "docs/a/b.md") and m("docs", "x/docs/a.md") and m("/scripts", "scripts/x.sh")
    assert m("**/logs", "logs/a") and m("**/logs", "deep/down/logs/a") and m("/apps/**", "apps/x/y")
    assert m("src/?.py", "src/a.py") and not m("src/?.py", "src/ab.py")
    assert m("my dir/", "my dir/a")
    assert ownership._split("my\\ dir/ @a # note") == ["my dir/", "@a"]
    # a word that starts with an unescaped # starts the comment, whatever follows; an escaped one is a word
    assert ownership._split("* @all #note @notowner") == ["*", "@all"]
    assert ownership._split("\\#x @a") == ["#x", "@a"] and ownership._split("a@b#c @d") == ["a@b#c", "@d"]
    assert m("/apps/**", "apps/x") and not m("/apps/**", "apps") and m("src/**.py", "src/a.py")


def test_codeowners_patterns_cannot_make_matching_slow():
    import time

    t0 = time.perf_counter()
    assert not ownership.pattern_matcher("a/**/**/**/**/**/**/**/**/**/**/**/b")("/".join(["a"] * 28))
    assert not ownership.pattern_matcher("*a*a*a*a*a*a*a*a*a*a*a*a*a*a*b")("a" * 28)
    assert not ownership.pattern_matcher("*a*a*a*a*a*a*a*a*a*a*a*a*a*a*b")("/".join(["a" * 28] * 20))
    assert time.perf_counter() - t0 < 1.0  # a backtracking regex took 96 s and 9 s on the first two


def test_an_inline_comment_is_not_an_owner(tmp_path):
    (tmp_path / "CODEOWNERS").write_text("* @all #note @notowner\n", encoding="utf-8")
    co = ownership.read_codeowners(tmp_path)
    assert co["rules"][0]["owners"] == ["@all"]


def test_the_last_matching_rule_wins(tmp_path):
    (tmp_path / "CODEOWNERS").write_text("* @all\n/src/ @core\n/src/ui/ @ui\n", encoding="utf-8")
    co = ownership.read_codeowners(tmp_path)
    assert co["file"] == "CODEOWNERS"
    assert ownership.owner_rule(co, "src/ui/a.ts")["owners"] == ["@ui"]
    assert ownership.owner_rule(co, "src/b.py")["owners"] == ["@core"]
    assert ownership.owner_rule(co, "README.md")["owners"] == ["@all"]


def test_gitlab_sections_lower_the_owner_claim(tmp_path):
    r = tmp_path / "g"
    r.mkdir()
    _git(r, "init", "-q")
    _commit(r, {"a.py": "x = 1\n", "CODEOWNERS": "[Backend] @be\n*.py @py\n"}, "one", author="Eve",
            date="2026-01-01T10:00:00+00:00")
    res = ownership.owners(r, "a.py")
    owner = next(c for c in res["claims"] if "CODEOWNERS" in c["text"])
    assert owner["status"] == "strong_inference" and res["codeowners"]["sections"]
    _check(r, res)


def test_a_project_in_a_folder_matches_codeowners_by_the_repository_path(tmp_path):
    top = tmp_path / "mono"
    top.mkdir()
    _git(top, "init", "-q")
    _commit(top, {"svc a/app.py": "x = 1\n", "other/b.py": "y = 2\n",
                  "CODEOWNERS": "/svc\\ a/ @svc-team\n/other/ @other\n"}, "one", author="Fay",
            date="2026-01-01T10:00:00+00:00")
    proj = top / "svc a"
    res = ownership.owners(proj)
    assert res["files"] == 1 and res["authors"][0]["name"] == "Fay"
    assert [o["owners"] for o in res["codeowners"]["owned"]] == [["@svc-team"]]
    ev = next(c for c in res["claims"] if "CODEOWNERS" in c["text"])["evidence"][0]
    assert ev["path"] == "CODEOWNERS" and ev["meta"]["root"] == str(top)
    assert any("folder 'svc a/'" in x for x in res["coverage"]["limits"])


def test_no_codeowners_is_an_unknown(tmp_path):
    r = tmp_path / "n"
    r.mkdir()
    _git(r, "init", "-q")
    _commit(r, {"a.py": "x = 1\n"}, "one", author="Gil", date="2026-01-01T10:00:00+00:00")
    res = ownership.owners(r, "a.py")
    assert res["codeowners"] is None and res["status"] == "found"
    assert any("CODEOWNERS" in u["question"] for u in res["unknowns"])


def test_not_git_and_bad_targets(repo, tmp_path):
    r, _ = repo
    assert ownership.owners(tmp_path)["status"] == "not_git"
    with pytest.raises(ValueError):
        ownership.owners(r, "no/such/file.py")
    with pytest.raises(ValueError):
        ownership.owners(r, "../outside")
    with pytest.raises(ValueError, match="outside the project"):
        ownership.owners(r, "../proj/src/svc.py:1-2".replace("proj", "elsewhere"))
    assert ownership.owners(r, "./src/")["path"] == "src"
    with pytest.raises(ValueError):
        ownership.owners(r, "src/svc.py#nothing_here")
    assert ownership.owners(r, "src/logo.bin")["status"] == "not_found"
    for bad in ("src/svc.py:0-1", "src/svc.py:13-20", "src/svc.py:5-99"):  # svc.py has 12 lines
        with pytest.raises(ValueError, match="has 12 line"):
            ownership.owners(r, bad)
    assert ownership.owners(r, "src/svc.py:12-11")["lines"] == [11, 12]


def test_cli(repo, capsys, tmp_path):
    r, _ = repo
    assert cli.main(["owners", "src/svc.py", "--repo", str(r), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["bus_factor"]["value"] == 2
    assert cli.main(["owners", "src", "--repo", str(r)]) == 0
    text = capsys.readouterr().out
    assert "bus factor of `src`" in text and "CODEOWNERS (.github/CODEOWNERS" in text and "(inactive)" in text
    assert cli.main(["owners", "src/logo.bin", "--repo", str(r)]) == 2
    capsys.readouterr()
    assert cli.main(["owners", "nothing.py", "--repo", str(r)]) == 1
    assert cli.main(["owners", "src/svc.py:5-99", "--repo", str(r)]) == 1
    assert cli.main(["owners", "src/svc.py", "--days", "100000", "--repo", str(r)]) == 0
    capsys.readouterr()
    assert cli.main(["owners", "--repo", str(tmp_path)]) == 2
