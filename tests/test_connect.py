"""One connection to the hub: a second wake (button, voice, a report to read) while connecting
waits for the first connect instead of opening another.

Run like test_barge_in.py (it stubs the audio stack; importing it first does that here too).
"""
import asyncio
import json

import pytest

import test_barge_in


def make_client():
    c = test_barge_in.make_client()
    c.tools_listed = asyncio.Event()
    c.play = lambda name: None
    return c


class SlowHub:
    features = {}

    def __init__(self, fail=False):
        self.ws = None
        self.connects = 0
        self.fail = fail
        self.sent = []

    @property
    def connected(self):
        return self.ws is not None

    async def connect(self):
        self.connects += 1
        await asyncio.sleep(0.05)              # handshake and the hub's hello
        if self.fail:
            raise OSError('refused')
        self.ws = object()

    async def send(self, msg):
        self.sent.append(msg)
        return True


@pytest.mark.asyncio
async def test_concurrent_wakes_open_one_connection():
    c = make_client()
    c.hub = SlowHub()
    results = await asyncio.gather(*(c.ensure_connected() for _ in range(3)))
    assert results == [True, True, True]
    assert c.hub.connects == 1


@pytest.mark.asyncio
async def test_a_failed_connect_does_not_block_the_next_try():
    c = make_client()
    c.hub = SlowHub(fail=True)
    assert not await c.ensure_connected()
    assert c.state == 'idle'
    c.hub.fail = False
    assert await c.ensure_connected()
    assert c.hub.connects == 2


class FakeWs:
    def __init__(self, msgs):
        self.msgs = msgs
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.msgs:
            raise StopAsyncIteration
        return self.msgs.pop(0)

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_a_handler_error_closes_the_connection_it_gives_up_on():
    from thinksmart_client.hub import Hub
    closed = []

    async def on_json(msg):
        raise KeyError('label')

    async def on_closed():
        closed.append(True)
    hub = Hub('ws://x', 'd', 'c', 't', on_json, lambda b: None, on_closed)
    ws = hub.ws = FakeWs(['{"type": "server"}'])
    await hub._read(ws)
    assert ws.closed and closed and not hub.connected


@pytest.mark.asyncio
@pytest.mark.parametrize('prefixes', [(), ('小麦小麦', '你好小智')])
async def test_hello_announces_only_configured_wake_prefixes(monkeypatch, prefixes):
    from thinksmart_client import hub as module
    sent = []
    class Ws(FakeWs):
        async def send(self, message): sent.append(json.loads(message))
        async def recv(self): return '{"type":"hello","session_id":"test"}'
    ws = Ws([])
    async def connect(*args, **kwargs): return ws
    async def closed(): pass
    monkeypatch.setattr(module, 'connect', connect)
    hub = module.Hub('ws://x', 'd', 'c', 't', closed, lambda b: None, closed,
                     wake_prefixes=prefixes)
    await hub.connect()
    if prefixes:
        assert sent[0]['features']['wake_prefixes'] == list(prefixes)
    else:
        assert 'wake_prefixes' not in sent[0]['features']
    await hub._reader
