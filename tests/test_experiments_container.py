"""Container isolation, run for real (docs/DESIGN.md D63).

Skipped unless VERINODA_CONTAINER_TESTS=1 (the CI job `container` sets it, with docker and a test image that has
pytest, VERINODA_TEST_IMAGE); with the flag set and no working docker/podman the tests fail instead of skipping.
They check the promises of a container run - no network, only the copy writable, not root, no capabilities, no
secrets, the timeout stops the container - and that an image without pytest is inconclusive, never a pass. The
project is not trusted, so an allowlisted pytest run goes to the container by itself; the image comes from the
user-level config (a project's own config cannot choose it).
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import experiments, paths  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ENABLED = os.environ.get("VERINODA_CONTAINER_TESTS") == "1"
pytestmark = [pytest.mark.experiment,
              pytest.mark.skipif(not ENABLED, reason="set VERINODA_CONTAINER_TESTS=1 with docker/podman and a test "
                                                     "image with pytest (VERINODA_TEST_IMAGE); CI job 'container'")]

PROMISES = '''
import os, socket, pathlib, pytest

def test_cwd_is_the_mounted_copy():
    assert os.getcwd() == "/work"
    assert os.environ["VERINODA_ARTIFACTS"] == "/artifacts" and os.environ["HOME"] == "/tmp"

def test_no_network():
    with pytest.raises(OSError):
        socket.create_connection(("1.1.1.1", 53), timeout=3)

def test_not_root_and_no_capabilities():
    assert os.getuid() != 0
    status = pathlib.Path("/proc/self/status").read_text()
    eff = next(ln for ln in status.splitlines() if ln.startswith("CapEff:")).split()[1]
    assert int(eff, 16) == 0
    nnp = next((ln for ln in status.splitlines() if ln.startswith("NoNewPrivs:")), "NoNewPrivs: 1")
    assert nnp.split()[1] == "1"

def test_only_the_copy_and_tmp_are_writable():
    with pytest.raises(OSError):
        pathlib.Path("/usr/verinoda-probe").write_text("x")
    pathlib.Path("/tmp/probe").write_text("x")
    pathlib.Path("/work/written-by-the-test.txt").write_text("x")
    (pathlib.Path("/artifacts") / "seen.txt").write_text("ok")

def test_no_secret_reaches_the_container():
    assert not [k for k in os.environ if "ANTHROPIC" in k or "TOKEN" in k]
'''


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture(scope="module")
def runtime():
    rt = experiments.container_runtime()
    if not rt:
        pytest.fail("VERINODA_CONTAINER_TESTS=1 but no working docker/podman was found")
    return rt


@pytest.fixture
def proj(tmp_path, monkeypatch, runtime):
    """An untrusted project with a test file, and a user config that names the image."""
    user = tmp_path / "user-config"
    user.mkdir()
    image = os.environ.get("VERINODA_TEST_IMAGE") or experiments.DEFAULT_CONTAINER_IMAGE
    (user / "config.json").write_text(json.dumps({"experiments": {"container_image": image}}), encoding="utf-8")
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(user))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-FAKE-not-a-real-key")
    repo = tmp_path / "p"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_promises.py").write_text(PROMISES, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    assert not paths.is_trusted(repo)
    st = open_store(repo)
    yield repo, st, user
    st.close()


def test_a_container_run_keeps_its_promises(proj):
    repo, st, _ = proj
    res = experiments.run(st, repo, ["python", "-m", "pytest", "-q", "tests"], hypothesis="promises",
                          timeout=180)
    assert res["isolation"] == "container" and res["trusted"] is False
    g = res["guarantees"]
    assert g["network_isolated"] and g["fs_writes_confined"] and g["fs_reads_confined"]
    assert res["outcome"] == "pass", (res["summary"], Path(res["logs"]["stdout"]).read_text(encoding="utf-8"))
    assert Path(res["artifacts"]["seen.txt"]).read_text(encoding="utf-8") == "ok"
    # what the tests wrote in the copy belongs to the user: the throw-away copy was deleted
    assert not any("could not be deleted" in lim for lim in res.get("limits") or [])


def test_an_image_without_pytest_is_inconclusive_never_a_pass(proj):
    repo, st, user = proj
    (user / "config.json").write_text(json.dumps({"experiments": {"container_image":
                                                                  experiments.DEFAULT_CONTAINER_IMAGE}}),
                                      encoding="utf-8")
    res = experiments.run(st, repo, ["python", "-m", "pytest", "-q", "tests"], hypothesis="h", timeout=180)
    assert res["isolation"] == "container"
    assert res["outcome"] == "inconclusive", res["summary"]
    assert "container_image" in res["next_step"]


def test_the_timeout_stops_the_container(proj, runtime):
    repo, st, _ = proj
    (repo / "tests" / "test_slow.py").write_text("import time\n\ndef test_slow():\n    time.sleep(120)\n",
                                                 encoding="utf-8")
    res = experiments.run(st, repo, ["python", "-m", "pytest", "-q", "tests/test_slow.py"], hypothesis="h",
                          timeout=15)
    assert res["outcome"] == "timeout"
    deadline = time.monotonic() + 20  # --rm removes a killed container in the daemon, after `kill` returns
    while True:
        left = subprocess.run([runtime, "ps", "-a", "-q", "--filter", f"name=verinoda-{res['id']}"],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if not left.stdout.strip() or time.monotonic() > deadline:
            break
        time.sleep(1)
    assert left.stdout.strip() == "", left.stdout


def test_a_projects_own_config_cannot_choose_the_image(proj):
    repo, st, _ = proj
    (repo / ".verinoda").mkdir(exist_ok=True)
    (repo / ".verinoda" / "config.json").write_text(json.dumps({"experiments": {"container_image":
                                                                              "example.invalid/evil:1"}}),
                                                    encoding="utf-8")
    res = experiments.run(st, repo, ["python", "-m", "pytest", "-q", "tests"], hypothesis="h", timeout=180)
    assert st.get("experiments", res["id"])["environment"]["image"] != "example.invalid/evil:1"
    assert any("experiments.container_image" in lim for lim in res["limits"])
