"""A transport hands protocol events to the loop instead of calling the protocol directly."""


class Transport:
    def __init__(self, loop, protocol):
        self._loop = loop
        self._protocol = protocol
        self._paused = False
        self._loop.call_soon(self._protocol.connection_made, self)

    def close(self):
        self._loop.call_soon(self._call_connection_lost, None)

    def pause_reading(self):
        self._paused = True

    def resume_reading(self):
        self._paused = False
        self._loop.call_soon(self._protocol.resume_reading)

    def _call_connection_lost(self, exc):
        self._protocol.connection_lost(exc)
