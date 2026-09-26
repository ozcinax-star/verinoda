"""Excerpt of the loop this project replaced, kept for comparison; it is not imported."""

import os

TIMER_RESOLUTION = float(os.environ.get("OLDLOOP_TIMER_RESOLUTION", "0.01"))
DEBUG = os.environ.get("OLDLOOP_DEBUG") == "1"


class OldLoop:
    def __init__(self):
        self._callbacks = []

    def call_soon(self, callback, *args):
        self._callbacks.append((callback, args))

    def run_once(self):
        for callback, args in self._callbacks:
            callback(*args)
        self._callbacks = []
