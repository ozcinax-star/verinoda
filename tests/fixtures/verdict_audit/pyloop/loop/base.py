"""A tiny event loop: callbacks queued with call_soon run on the next pass of run_once."""

import heapq
import time

TIMER_RESOLUTION = 0.001  # seconds; timers due within this window run in the same pass


class BaseLoop:
    def __init__(self):
        self._ready = []
        self._timers = []
        self._stopping = False

    def call_soon(self, callback, *args):
        self._ready.append((callback, args))
        return len(self._ready)

    def call_later(self, delay, callback, *args):
        heapq.heappush(self._timers, (time.monotonic() + delay, id(callback), callback, args))

    def _run_timers(self):
        now = time.monotonic() + TIMER_RESOLUTION
        while self._timers and self._timers[0][0] <= now:
            _when, _key, callback, args = heapq.heappop(self._timers)
            self.call_soon(callback, *args)

    def run_once(self):
        self._run_timers()
        ready, self._ready = self._ready, []
        for callback, args in ready:
            callback(*args)

    def stop(self):
        self.call_soon(self._set_stopping)

    def _set_stopping(self):
        self._stopping = True
