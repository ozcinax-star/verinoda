"""Shared pytest configuration for the Verinoda product tests.

Kept deliberately minimal: every test module defines its own fixtures (copying
examples/orders_app to tmp_path, git init, scan) so modules stay independent.
"""

import os
import shutil
import tempfile

# project_index reads GRAPHIFY_OUT at import time; point it at .verinoda/index
# before anything imports verinoda.project_index.
os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

# the name check keeps an environment's word index in the user's cache directory
# (codecheck_rank.user_cache_dir): tests write to a directory of their own, never the real one
_CACHE = tempfile.mkdtemp(prefix="verinoda-test-cache-")
os.environ["VERINODA_CACHE_DIR"] = _CACHE
# the first build of that index must finish on a loaded machine too (tests that need a cut build set their own)
os.environ.setdefault("VERINODA_NAME_INDEX_BUDGET_S", "1800")


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: end-to-end test that spawns `python -m verinoda` subprocesses")
    config.addinivalue_line("markers", "experiment: runs a real isolated experiment (child process)")


def pytest_unconfigure(config):
    shutil.rmtree(_CACHE, ignore_errors=True)
