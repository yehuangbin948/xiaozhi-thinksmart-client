import asyncio,types,sys
import numpy as np
import pytest
import test_barge_in
from thinksmart_client import music,mcp
from thinksmart_client.audio import Audio,Player

def player(blocked=lambda:False):
    audio=types.SimpleNamespace(music_player=Player(),music_enabled=False)
    events=[]
    return music.Music(audio,lambda *args:events.append(args),blocked),events

@pytest.mark.parametrize('url',['file:///dev/zero','http://127.0.0.1/','http://m.music.126.net.evil/a',
                                'http://user:pass@m.music.126.net/a','http://m.music.126.net:23/a'])
def test_music_rejects_non_netease_urls(url):assert not music.valid_url(url)

def test_music_holds_without_discarding_buffer_and_view_never_contains_url():
    held=[False]
    p,events=player(lambda:held[0]);p.playing=True;p.track={'title':'测试','url':'should-not-be-in-view'}
    p.audio.music_player.push(np.ones((4800,2),dtype=np.float32))
    p.refresh();assert p.audio.music_enabled
    held[0]=True;p.refresh();assert not p.audio.music_enabled
    assert events[-1][0]=='music' and events[-1][1]['held']
    assert p.audio.music_player.buffered()==.1
    assert 'url' not in p.view()['track']

@pytest.mark.asyncio
async def test_decoder_streams_pcm_and_reports_completion(monkeypatch):
    p,events=player();p.playing=True;p.track={'id':1,'title':'测试'}
    reader=asyncio.StreamReader();reader.feed_data(np.ones((120,2),dtype='<f4').tobytes());reader.feed_eof()
    class Sink:
        def buffered(self):return 0
        def push(self,pcm):assert pcm.shape==(120,2)
    p.audio.music_player=Sink()
    async def spawn(*args,**kwargs):
        assert '-protocol_whitelist' in args and kwargs['stderr']==asyncio.subprocess.DEVNULL
        async def wait():return 0
        return types.SimpleNamespace(stdout=reader,returncode=0,wait=wait)
    monkeypatch.setattr(music.asyncio,'create_subprocess_exec',spawn)
    await p._pump('ffmpeg','https://m.music.126.net/test.mp3')
    assert p.samples==120 and not p.playing and p.ended and not p.error and events

@pytest.mark.asyncio
async def test_stop_cancels_decoder_and_clears_only_music_buffer():
    p,_=player();p.playing=True;p.track={'id':1};p.task=asyncio.create_task(asyncio.sleep(60))
    old=p.task
    await p.stop()
    assert old.cancelled() and p.track is None and not p.playing
    assert p.audio.music_player.buffered()==0

@pytest.mark.asyncio
@pytest.mark.parametrize('held',[False,True])
async def test_stop_with_full_decoder_pipe_returns_mcp_reply(monkeypatch,held):
    p,_=player(lambda:held)
    original=asyncio.create_subprocess_exec
    children=[]
    async def spawn(*args,**kwargs):
        proc=await original(sys.executable,'-c',
            'import os,signal;signal.signal(signal.SIGTERM,signal.SIG_IGN)\nwhile True:os.write(1,b"\\0"*65536)',
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,limit=4096)
        children.append(proc);return proc
    monkeypatch.setattr(music.asyncio,'create_subprocess_exec',spawn)
    monkeypatch.setattr(music,'EXIT_TIMEOUT',.1)
    p.playing=True;p.track={'id':1};p.task=asyncio.create_task(p._pump('fake','unused'))
    pump=p.task
    try:
        async def full_pipe():
            while not children or not children[0].stdout._paused: await asyncio.sleep(.01)
        await asyncio.wait_for(full_pipe(),2)
        result=await asyncio.wait_for(mcp.handle({'jsonrpc':'2.0','id':78,'method':'tools/call',
            'params':{'name':'self.music.transport','arguments':{'action':'stop'}}},types.SimpleNamespace(music=p)),2)
        assert not result['result']['isError']
        assert p.track is None and p.proc is None and pump.done()
        assert children[0].returncode is not None
        assert not p.playing and p.audio.music_player.buffered()==0
    finally:
        for proc in children:
            if proc.returncode is None:proc.kill()
            await asyncio.wait_for(proc.stdout.read(),2)
            await asyncio.wait_for(proc.wait(),2)
        if not pump.done():pump.cancel()
        await asyncio.gather(pump,return_exceptions=True)

def test_output_mixes_music_and_sends_actual_output_to_echo_canceller():
    a=Audio.__new__(Audio);a.player=Player();a.music_player=Player();a.music_enabled=True
    a.player.push(np.full((20,2),.25,np.float32));a.music_player.push(np.full((20,2),.5,np.float32))
    captured=[];a.aec=types.SimpleNamespace(feed_far=lambda data:captured.append(data.copy()))
    a.last_out=0
    out=np.zeros((20,2),np.float32)
    a._out_cb(out,20,None,None)
    np.testing.assert_allclose(out,.75);np.testing.assert_array_equal(out,captured[0])

@pytest.mark.asyncio
async def test_internal_transport_delegates_to_music():
    p,_=player()
    result=await mcp.handle({'jsonrpc':'2.0','id':77,'method':'tools/call','params':{'name':'self.music.transport','arguments':{'action':'status'}}},types.SimpleNamespace(music=p))
    assert result['result']['isError'] is False

@pytest.mark.asyncio
async def test_music_button_preserves_active_conversation_state():
    from unittest.mock import MagicMock,AsyncMock
    from thinksmart_client.web import make_app
    client=MagicMock()
    client.state='speaking';client.cfg={'device_id':'test-device'}
    client.ensure_connected=AsyncMock(return_value=True)
    client.tools_listed=asyncio.Event();client.tools_listed.set()
    client._hub_http=AsyncMock(return_value={'ok':True})
    app=make_app(client)
    handler=next(r.handler for r in app.router.routes() if r.resource.canonical=='/api/music')
    request=types.SimpleNamespace(json=AsyncMock(return_value={'action':'pause'}))
    response=await handler(request)
    assert response.status==200
    client.set_state.assert_not_called()
    client.state='connecting'
    await handler(request)
    client.set_state.assert_called_once_with('idle')
