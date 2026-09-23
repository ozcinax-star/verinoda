"""Shared pytest configuration for the Verinoda product tests.

Kept deliberately minimal: every test module defines its own fixtures (copying
examples/orders_app to tmp_path, git init, scan) so modules stay independent.
"""

import os

# project_index reads GRAPHIFY_OUT at import time; point it at .verinoda/index
# before anything imports verinoda.project_index.
os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: end-to-end test that spawns `python -m verinoda` subprocesses")
    config.addinivalue_line("markers", "experiment: runs a real isolated experiment (child process)")
