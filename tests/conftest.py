"""Shared pytest configuration for the Verinoda product tests.

Kept deliberately minimal: every test module defines its own fixtures (copying
examples/orders_app to tmp_path, git init, scan) so modules stay independent.
"""

import os
import shutil
import tempfile

import pytest

# project_index reads GRAPHIFY_OUT at import time; point it at .verinoda/index
# before anything imports verinoda.project_index.
os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

# the name check keeps an environment's word index in the user's cache directory
# (codecheck_rank.user_cache_dir): tests write to a directory of their own, never the real one
_CACHE = tempfile.mkdtemp(prefix="verinoda-test-cache-")
os.environ["VERINODA_CACHE_DIR"] = _CACHE
# the first build of that index must finish on a loaded machine too (tests that need a cut build set their own)
os.environ.setdefault("VERINODA_NAME_INDEX_BUDGET_S", "1800")


@pytest.fixture(scope="session", autouse=True)
def _user_config_and_trust(tmp_path_factory):
    """Verinoda's per-user directory (user config, trust.json) points at a temporary folder for the whole
    session - the tests never read or write the real one - and the session's temporary folder is trusted with
    its subfolders: the temporary projects the tests build are trusted explicitly, as a user would with
    `verinoda trust`, so their tests run with process isolation (docs/DESIGN.md D63). Set in os.environ, not
    with monkeypatch, so that `python -m verinoda` subprocesses inherit it. Tests of an untrusted project
    point VERINODA_CONFIG_DIR at a fresh folder of their own."""
    from verinoda import paths

    old = os.environ.get(paths.CONFIG_DIR_ENV)
    os.environ[paths.CONFIG_DIR_ENV] = str(tmp_path_factory.mktemp("verinoda-user-config"))
    paths.set_trust(tmp_path_factory.getbasetemp(), subfolders=True)
    yield
    if old is None:
        os.environ.pop(paths.CONFIG_DIR_ENV, None)
    else:
        os.environ[paths.CONFIG_DIR_ENV] = old


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: end-to-end test that spawns `python -m verinoda` subprocesses")
    config.addinivalue_line("markers", "experiment: runs a real isolated experiment (child process)")


def pytest_unconfigure(config):
    shutil.rmtree(_CACHE, ignore_errors=True)
