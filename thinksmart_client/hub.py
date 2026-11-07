"""The xiaozhi WebSocket protocol, as spoken by xiaozhi-hub-server.

Client → hub: hello (features mcp, Opus 16 kHz mono), listen start/stop, abort, mcp
replies, binary Opus frames. Hub → client: hello (session_id, its audio parameters), stt,
llm, tts start/sentence_start/stop, mcp requests, server, binary Opus frames.
"""
import asyncio
import json
import logging

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)


class Hub:
    def __init__(self, url, device_id, client_id, token, on_json, on_audio, on_closed, wake_prefixes=()):
        self.url = url
        self.headers = {'Authorization': f'Bearer {token}', 'Protocol-Version': '1',
                        'Device-Id': device_id, 'Client-Id': client_id}
        self.on_json, self.on_audio, self.on_closed = on_json, on_audio, on_closed
        self.ws = None
        self.session_id = None
        self.audio_params = {'sample_rate': 24000, 'frame_duration': 60}
        self._reader = None
        self.features = {}
        self.wake_prefixes = [p for p in wake_prefixes if isinstance(p,str) and 0<len(p)<=24][:10]

    @property
    def connected(self):
        return self.ws is not None

    async def connect(self):
        """Open the channel and exchange hellos; raises on failure."""
        ws = await connect(self.url, additional_headers=self.headers, open_timeout=5,
                           ping_interval=20, ping_timeout=20, close_timeout=3,
                           max_size=10 * 1024 * 1024, compression=None)
        await ws.send(json.dumps({
            'type': 'hello', 'version': 1, 'transport': 'websocket',
            # no 'aec': with it the hub cuts a reply off as soon as its VAD hears anything. Only the
            # wake word or the button interrupts here (app.py), and the mic isn't uploaded during a reply
            # vad_end: the hub says when its VAD decides the user finished (the page shows "recognising")
            # ready_on_tts_start: messages and audio are handled in order, so audio right after "tts start"
            # plays (the hub need not wait before a cached reply such as "我在")
            'features': {'mcp': True, 'vad_end': True, 'ready_on_tts_start': True,
                         **({'wake_prefixes': self.wake_prefixes} if self.wake_prefixes else {})},
            'audio_params': {'format': 'opus', 'sample_rate': 16000, 'channels': 1, 'frame_duration': 20},
        }))
        try:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=10)
                if isinstance(msg, str):
                    data = json.loads(msg)
                    if data.get('type') == 'hello':
                        break
        except Exception:
            await ws.close()
            raise
        self.session_id = data.get('session_id')
        self.audio_params.update(data.get('audio_params') or {})
        self.features = data.get('features') or {}   # vad_end: the hub tells when it recognised nothing
        self.ws = ws
        self._reader = asyncio.create_task(self._read(ws))
        log.info('connected to hub, session %s, audio %s', self.session_id, self.audio_params)

    async def _read(self, ws):
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    self.on_audio(msg)
                else:
                    try:
                        data = json.loads(msg)
                    except ValueError:
                        continue
                    await self.on_json(data)
        except ConnectionClosed:
            pass
        except Exception:
            log.exception('hub reader failed')
        finally:
            if self.ws is ws:
                self.ws = None
                log.info('hub connection closed')
                await self.on_closed()
            await ws.close()   # after a handler error it is still open: don't leave the hub's session hanging

    async def send(self, msg):
        if self.ws is None:
            return False
        msg.setdefault('session_id', self.session_id)
        try:
            await self.ws.send(json.dumps(msg, ensure_ascii=False))
            return True
        except ConnectionClosed:
            return False

    async def send_audio(self, packet):
        if self.ws is not None:
            try:
                await self.ws.send(packet)
            except ConnectionClosed:
                pass

    async def close(self):
        ws = self.ws
        if ws is not None:
            await ws.close()
