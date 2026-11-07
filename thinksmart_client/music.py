"""Optional NetEase playback transport. Decodes online MP3 into the existing PipeWire/AEC output.
No sound device is opened here, and no media or authenticated URL is persisted.
"""
import asyncio
import shutil
from urllib.parse import urlsplit

import numpy as np

RATE = 48000
CHUNK = 4800 * 2 * 4

def valid_url(url):
    try:
        p = urlsplit(url)
        host = (p.hostname or '').lower()
        return (p.scheme in ('http', 'https') and not p.username and not p.password and p.port in (None, 80, 443)
                and any(host.endswith('.' + d) for d in ('music.126.net', 'music.163.com', 'music.127.net')))
    except (ValueError, TypeError): return False

class Music:
    def __init__(self, audio, publish, blocked):
        self.audio, self.publish, self.blocked = audio, publish, blocked
        self.track = None
        self.playing = False
        self.task = self.proc = None
        self.samples = 0
        self.error = ''
        self.ended = False

    def view(self):
        queued = self.audio.music_player.buffered()
        return {'track': {k: self.track[k] for k in ('id','title','artist','duration') if k in self.track} if self.track else None,
                'playing': self.playing, 'held': bool(self.playing and self.blocked()),
                'position': max(0, self.samples / RATE - queued), 'error': self.error,
                'supported': bool(shutil.which('ffmpeg')), 'ended': self.ended}

    def emit(self): self.publish('music', self.view())

    def refresh(self):
        self.audio.music_enabled = bool(self.playing and not self.blocked())

    async def stop(self):
        self.playing = False
        self.audio.music_enabled = False
        task, self.task = self.task, None
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        from .audio import Player
        self.audio.music_player = Player()
        self.track, self.samples, self.error = None, 0, ''
        self.ended = False
        self.emit()

    async def control(self, payload):
        action = payload.get('action')
        if action == 'play':
            track = payload.get('track') or {}
            if not isinstance(track, dict) or not valid_url(track.get('url')):
                return {'ok': False, 'error': '只接受网易云在线音源'}
            binary = shutil.which('ffmpeg')
            if not binary: return {'ok': False, 'error': '设备缺少 ffmpeg 解码组件'}
            await self.stop()
            self.track = {k: track[k] for k in ('id','title','artist','duration') if k in track}
            self.playing = True
            self.task = asyncio.create_task(self._pump(binary, track['url']))
        elif action == 'stop': await self.stop()
        elif action in ('pause', 'resume'):
            if not self.track or not self.task or self.task.done():
                return {'ok': False, 'error': '当前没有可继续播放的歌曲，请重新点歌'}
            self.playing = action == 'resume'
        elif action != 'status': return {'ok': False, 'error': '不支持的播放操作'}
        self.refresh(); self.emit()
        return {'ok': True, **self.view()}

    async def _pump(self, binary, url):
        proc = None
        last_emit = 0
        try:
            proc = self.proc = await asyncio.create_subprocess_exec(
                binary, '-nostdin', '-loglevel', 'error', '-rw_timeout', '15000000',
                '-protocol_whitelist', 'http,https,tcp,tls', '-f', 'mp3', '-i', url,
                '-f', 'f32le', '-acodec', 'pcm_f32le', '-ar', str(RATE), '-ac', '2', 'pipe:1',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            while True:
                self.refresh()
                if not self.audio.music_enabled or self.audio.music_player.buffered() > .3:
                    await asyncio.sleep(.025)
                    continue
                try: raw = await proc.stdout.readexactly(CHUNK)
                except asyncio.IncompleteReadError as end: raw = end.partial
                if raw:
                    if len(raw) % 8: raise ValueError('Invalid PCM frame')
                    pcm = np.frombuffer(raw, dtype='<f4').reshape(-1, 2).copy()
                    if not np.isfinite(pcm).all(): raise ValueError('Invalid PCM data')
                    self.samples += len(pcm)
                    self.audio.music_player.push(pcm)
                if len(raw) < CHUNK:
                    if await proc.wait() != 0 or self.samples == 0: raise ValueError('Decoder failed')
                    while self.audio.music_player.buffered() > 0:
                        self.refresh(); await asyncio.sleep(.025)
                    self.playing = False
                    self.ended = True
                    break
                now = asyncio.get_running_loop().time()
                if now-last_emit > .5: self.emit(); last_emit = now
        except asyncio.CancelledError: raise
        except (OSError, ValueError):
            self.error = '音乐播放失败，请重新点歌或检查网络'
            self.playing = False
        finally:
            if proc and proc.returncode is None:
                try: proc.terminate()
                except ProcessLookupError: pass
                try: await asyncio.wait_for(proc.wait(), 3)
                except asyncio.TimeoutError:
                    try: proc.kill()
                    except ProcessLookupError: pass
                    await proc.wait()
            self.proc = None
            self.refresh(); self.emit()
