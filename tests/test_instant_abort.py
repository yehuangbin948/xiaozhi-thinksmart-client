"""Interrupting while the hub recognises or thinks: at once, and nothing of the old turn comes back.

Run like test_barge_in.py (it stubs the audio stack; importing it first does that here too).
"""
import asyncio

import numpy as np
import pytest

from test_barge_in import PACKET, ask, make_client


class FakeKws:
    def __init__(self):
        self.fed = 0
        self.hit = None

    def feed(self, block):
        self.fed += 1
        return self.hit

    def recent(self):
        return np.zeros(16000, np.float32)


async def said_and_recognising(c):
    """The user woke it and spoke; the hub's VAD says they stopped and it is recognising."""
    await c.start_listening('realtime')
    await c._on_json({'type': 'vad', 'state': 'end'})
    assert c.status()['recognising'] and c.uploading and c.in_turn()


def block():
    return np.zeros(320, np.float32)


@pytest.mark.asyncio
async def test_wake_word_is_listened_for_once_the_hub_is_recognising():
    c = make_client()
    c.kws, c.backlog = FakeKws(), None
    c.loop = asyncio.get_running_loop()
    c._send_q = asyncio.Queue(100)
    c.encoder = type('E', (), {'encode': lambda self, pcm: b'opus'})()
    woke = []

    async def voice_wake(label, audio):
        woke.append(label)
    c.voice_wake = voice_wake
    await c.start_listening('realtime')
    c._on_block(block(), block())
    assert c.kws.fed == 0                    # listening: the hub hears it all, no wake word needed
    await c._on_json({'type': 'vad', 'state': 'end'})
    c._on_block(block(), block())
    assert c.kws.fed == 1
    c.kws.hit = '小麦小麦'
    c._on_block(block(), block())
    assert not c.uploading and len(c.backlog) == 3  # also keep speech before the delayed hit
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert woke == ['小麦小麦']
    c.kws.hit = None
    c._on_block(block(), block())
    assert len(c.backlog) == 4


@pytest.mark.asyncio
async def test_delayed_wake_preserves_one_second_and_sends_each_frame_once():
    c = make_client()
    c.kws, c.backlog = FakeKws(), None
    c.loop = asyncio.get_running_loop()
    c._send_q = asyncio.Queue(100)
    encoded = []
    c.encoder = type('E', (), {'encode': lambda self, pcm: encoded.append(pcm.copy()) or b'opus'})()

    async def wake(*args): pass
    c.voice_wake = wake
    for n in range(1, 62):
        if n == 61: c.kws.hit = '小麦小麦'
        # wake audio retains the voice even when the ordinary ASR path suppresses it
        c._on_block(block(), np.full(320, n / 128, np.float32))
    assert len(c.backlog) == 50
    c.kws.hit = None
    c.uploading = True
    c._on_block(np.full(320, 62 / 128, np.float32), block())
    assert len(encoded) == 51
    assert [int(pcm[0]) for pcm in encoded] == [n * 256 for n in range(12, 63)]
    assert c.backlog is None
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize('closed_mic', [False, True])
async def test_wake_only_recognition_keeps_listening_for_the_command(closed_mic):
    c = make_client()
    await said_and_recognising(c)
    if closed_mic:
        c.uploading, c.awaiting_text = False, True
        c.set_state('thinking')
    await c._on_json({'type': 'wake_ready'})
    assert c.uploading and c.state == 'listening'
    assert not c.recognising and not c.awaiting_text and not c.hub_pending
    assert c.deadline and c.threads == {}
    await c._on_json({'type': 'stt', 'text': '播放孙燕姿的歌'})
    assert c.state == 'thinking' and not c.uploading
    await c._on_json({'type': 'wake_ready'})  # late acknowledgement cannot reopen a real turn
    assert c.state == 'thinking' and not c.uploading


@pytest.mark.asyncio
async def test_wake_word_while_recognising_drops_the_question_and_listens_again():
    c = make_client()
    await said_and_recognising(c)
    await c.wake('voice')
    assert c.hub.kinds()[-2:] == [('abort', None), ('listen', 'start')]
    assert c.state == 'listening' and c.uploading and not c.status()['recognising']
    assert c.aborted_at is None              # logged when listening started again
    # the old sentence was recognised before the hub saw the abort: its text must not show up
    await c._on_json({'type': 'stt', 'text': '旧的问题'})
    await c._on_json({'type': 'tts', 'state': 'start'})
    assert c.threads == {} and c.state == 'listening' and c.uploading
    # the hub confirms the abort; the next question is a normal turn
    await c._on_json({'type': 'tts', 'state': 'stop', 'interrupted': True})
    assert c.state == 'listening' and c.uploading
    await c._on_json({'type': 'stt', 'text': '新的问题'})
    await c._on_json({'type': 'tts', 'state': 'start'})
    c._on_audio(PACKET)
    assert [m['text'] for m in c.threads[None]] == ['新的问题']
    assert c.state == 'thinking' and not c.mute_audio and c.audio.player.buffered() > 0


@pytest.mark.asyncio
async def test_button_while_recognising_goes_idle():
    c = make_client()
    await said_and_recognising(c)
    await c.abort()
    assert c.state == 'idle' and not c.uploading and not c.status()['recognising']
    assert c.hub.kinds()[-1] == ('abort', None)


@pytest.mark.asyncio
async def test_wake_word_while_waiting_for_the_text_interrupts():
    c = make_client()
    await c.start_listening('realtime')
    c.uploading, c.awaiting_text = False, True   # long sentence: mic closed, the text not back yet
    c.set_state('thinking')
    await c.wake('voice')
    assert c.hub.kinds()[-2:] == [('abort', None), ('listen', 'start')]
    assert c.state == 'listening' and not c.awaiting_text


@pytest.mark.asyncio
async def test_interrupted_while_thinking_marks_the_question():
    c = make_client()
    await c.start_listening('realtime')
    await c._on_json({'type': 'stt', 'text': '帮我写个程序'})
    await c._on_json({'type': 'tts', 'state': 'start'})
    await c.wake('voice')
    q = c.threads[None][-1]
    assert q['role'] == 'user' and q['interrupted']
    # a sentence of the old reply sent before the hub saw the abort: not shown, not played
    await c._on_json({'type': 'tts', 'state': 'sentence_start', 'text': '好的'})
    c._on_audio(PACKET)
    assert len(c.threads[None]) == 1 and c.state == 'listening'
    assert c.audio.player.buffered() == 0


@pytest.mark.asyncio
async def test_question_is_not_marked_once_the_reply_started():
    c = make_client()
    await ask(c)
    await c.wake('voice')
    user, bot = c.threads[None]
    assert not user.get('interrupted') and bot['interrupted']


@pytest.mark.asyncio
async def test_lost_confirmation_does_not_block_later_turns(monkeypatch):
    c = make_client()
    await said_and_recognising(c)
    await c.wake('voice')
    import thinksmart_client.app as app
    later = app.time.monotonic() + 4
    monkeypatch.setattr(app.time, 'monotonic', lambda: later)
    await c._on_json({'type': 'stt', 'text': '新的问题'})
    assert [m['text'] for m in c.threads[None]] == ['新的问题']
