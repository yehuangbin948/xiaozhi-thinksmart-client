"""Device tools the hub's models may call (MCP over the xiaozhi channel): speaker volume, sleep hours,
screen brightness, countdowns / alarms / reminders and their volume.

For alarms the model passes what the user meant (a duration, or a clock time with a date, a repeat
rule, what it is about); the device works out the time point and answers with it for the model to
repeat (alarms.py)."""
import asyncio
import json
import logging
import re

log = logging.getLogger(__name__)

TOOLS = [
    {
        'name': 'self.audio_speaker.set_volume',
        'description': '设置设备扬声器的音量。用户说「声音大一点」「音量调到 50」这类话时调用；'
                       '相对调整时先用 get_volume 读当前音量。',
        'inputSchema': {'type': 'object',
                        'properties': {'volume': {'type': 'integer', 'minimum': 0, 'maximum': 100}},
                        'required': ['volume']},
    },
    {
        'name': 'self.audio_speaker.get_volume',
        'description': '读取设备扬声器当前的音量（0-100）。',
        'inputSchema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'self.sleep_mode.set',
        'description': '设置设备的睡眠时段：时段内后台任务做完的汇报不主动念，只在屏幕上提示，'
                       '等用户点一下或唤醒再念；屏幕调暗；唤醒和对话照常，音量不变。'
                       '用户说「晚上十点到早上七点别念了」时 enabled=true, start="22:00", end="07:00"；'
                       '说「关掉睡眠模式」时 enabled=false。时间用 24 小时制 HH:MM，可以跨午夜；只改开关时不用传时间。',
        'inputSchema': {'type': 'object',
                        'properties': {'enabled': {'type': 'boolean'},
                                       'start': {'type': 'string', 'description': '开始时间 HH:MM'},
                                       'end': {'type': 'string', 'description': '结束时间 HH:MM'}},
                        'required': ['enabled']},
    },
    {
        'name': 'self.sleep_mode.get',
        'description': '读取睡眠时段的设置，以及现在是否在睡眠时段内。',
        'inputSchema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'self.screen.set_brightness',
        'description': '调节设备屏幕亮度，两个参数只传一个。亮度 0-100：0 是最暗（仍看得清），100 最亮。'
                       '「屏幕亮一点」change=20，「暗一点」change=-20（「再亮一点」同样传 change，不用先读）；'
                       '「调到最亮」brightness=100，「调到最暗」brightness=0，「亮度调到 60」brightness=60。'
                       '设备没有自动亮度。调过之后一直保持；睡眠时段屏幕会自动调暗，时段内调的亮度用到这次时段结束。',
        'inputSchema': {'type': 'object',
                        'properties': {'brightness': {'type': 'integer', 'minimum': 0, 'maximum': 100},
                                       'change': {'type': 'integer', 'minimum': -100, 'maximum': 100,
                                                  'description': '在当前亮度上加减'}}},
    },
    {
        'name': 'self.screen.get_brightness',
        'description': '读取屏幕亮度：level 当前亮度 0-100，dimmed 是否因睡眠时段调暗。',
        'inputSchema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'self.alarm.create',
        'description': '新建倒计时、闹钟或提醒，设备到点响铃。只传用户的意思，具体几点由设备算好返回，'
                       '你照返回的 when（和 repeat）跟用户复述一遍，例如「好的，明天早上 7 点叫你」。\n'
                       'kind：「倒计时 5 分钟」「计时 3 分钟」是 timer；「明早 7 点叫我」「定个 6 点半的闹钟」是 alarm；'
                       '「10 分钟后提醒我晾衣服」「下午 3 点提醒我开会」是 reminder（到点念 say）。\n'
                       '时间二选一：多久以后传 hours/minutes/seconds（「一个半小时后」hours=1, minutes=30）；'
                       '某个钟点传 time（24 小时制 HH:MM，「早上 7 点」07:00、「晚上 7 点」19:00），'
                       '是哪天用 days_from_today（今天 0、明天 1、后天 2）或 date（YYYY-MM-DD），都不传就是最近的那个钟点。'
                       '用户没说早上还是晚上、又猜不出时先问清楚。\n'
                       '循环用 repeat：daily 每天；workdays 工作日（只按周一到周五算，不管法定节假日和调休）；'
                       'weekly 每周几，配 weekdays（1 是周一……7 是周日，可多选）；monthly 每月几号，配 month_day'
                       '（这个月没有这一天时在当月最后一天响）。循环的必须传 time，不传日期。\n'
                       'label 是屏幕上显示的简短内容（「晾衣服」「开会」），没说就不传；'
                       'say 是提醒到点念的一句话（「该晾衣服了」），只用于 reminder，不传就念「提醒你，<label>」。',
        'inputSchema': {'type': 'object',
                        'properties': {'kind': {'type': 'string', 'enum': ['timer', 'alarm', 'reminder']},
                                       'hours': {'type': 'number'}, 'minutes': {'type': 'number'},
                                       'seconds': {'type': 'number'},
                                       'time': {'type': 'string', 'description': 'HH:MM，24 小时制'},
                                       'days_from_today': {'type': 'integer', 'minimum': 0},
                                       'date': {'type': 'string', 'description': 'YYYY-MM-DD'},
                                       'repeat': {'type': 'string', 'enum': ['daily', 'workdays', 'weekly', 'monthly']},
                                       'weekdays': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1, 'maximum': 7}},
                                       'month_day': {'type': 'integer', 'minimum': 1, 'maximum': 31},
                                       'label': {'type': 'string'}, 'say': {'type': 'string'}},
                        'required': ['kind']},
    },
    {
        'name': 'self.alarm.list',
        'description': '列出设备上还没响的倒计时、闹钟和提醒（「我有哪些闹钟」「明天几点叫我」），'
                       '每条有 id、type、when（下次响的时间）、in（还有多久）、repeat、label；还有最近错过的条数。'
                       '取消某一条前先用它找到 id。',
        'inputSchema': {'type': 'object',
                        'properties': {'kind': {'type': 'string', 'enum': ['timer', 'alarm', 'reminder'],
                                                'description': '只看这一种，不传就全部'}}},
    },
    {
        'name': 'self.alarm.cancel',
        'description': '取消倒计时、闹钟或提醒：传 id（先用 self.alarm.list 找到）；用户明确说「全部取消」时传 all=true，'
                       '可以配 kind 只取消这一种。用户说的那条不确定是哪个时先问清楚，不要猜。正在响的也能取消。',
        'inputSchema': {'type': 'object',
                        'properties': {'id': {'type': 'integer'}, 'all': {'type': 'boolean'},
                                       'kind': {'type': 'string', 'enum': ['timer', 'alarm', 'reminder']}}},
    },
    {
        'name': 'self.alarm.snooze',
        'description': '再睡一会儿：用户叫停刚响的闹钟后说「再睡 10 分钟」「过 5 分钟再叫我」时调用，'
                       '让正在响或 15 分钟内响过的那一条过 minutes 分钟（默认 10）再响一次，内容和循环都不变。'
                       '返回 success=false 表示没有这样的一条，这时问用户要不要设一个几分钟后的闹钟。',
        'inputSchema': {'type': 'object',
                        'properties': {'minutes': {'type': 'integer', 'minimum': 1, 'maximum': 60}}},
    },
    {
        'name': 'self.alarm.set_volume',
        'description': '设置闹钟音量（倒计时、闹钟、提醒响铃的音量，和平时的设备音量分开），两个参数只传一个：'
                       '「闹钟声音大一点」change=10，「闹钟音量调到 80」volume=80。平时说话的音量用 self.audio_speaker.set_volume。',
        'inputSchema': {'type': 'object',
                        'properties': {'volume': {'type': 'integer', 'minimum': 0, 'maximum': 100},
                                       'change': {'type': 'integer', 'minimum': -100, 'maximum': 100}}},
    },
    {
        'name': 'self.alarm.get_volume',
        'description': '读取闹钟音量（0-100）。',
        'inputSchema': {'type': 'object', 'properties': {}},
    },
]

ALARM_WHEN = ('hours', 'minutes', 'seconds', 'time', 'date', 'days_from_today')


def _alarm_args(args):
    """self.alarm.create's arguments as Client.create_alarm takes them."""
    rule = args.get('repeat')
    repeat = rule and {'type': rule, 'days': args.get('weekdays'), 'day': args.get('month_day')}
    return {'kind': args.get('kind'), 'label': args.get('label') or '', 'say': args.get('say') or '',
            'repeat': repeat, **{k: args[k] for k in ALARM_WHEN if args.get(k) is not None}}


def _brief(view):
    return {k: view[k] for k in ('id', 'type', 'label', 'when', 'in', 'repeat') if view.get(k) not in (None, '')}


async def _wpctl(*args):
    p = await asyncio.create_subprocess_exec('wpctl', *args, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.DEVNULL)
    out, _ = await p.communicate()
    return out.decode()


async def get_volume():
    m = re.search(r'([\d.]+)', await _wpctl('get-volume', '@DEFAULT_AUDIO_SINK@'))
    return round(float(m.group(1)) * 100) if m else None


async def set_volume(volume):
    await _wpctl('set-volume', '@DEFAULT_AUDIO_SINK@', f'{max(0, min(100, int(volume))) / 100:.2f}')


def _dumps(value):
    return json.dumps(value, ensure_ascii=False)


async def handle(payload, client=None):
    """One JSON-RPC request from the hub; returns the reply payload (None for notifications)."""
    method, req_id, params = payload.get('method'), payload.get('id'), payload.get('params') or {}
    if req_id is None:
        return None

    def result(value):
        return {'jsonrpc': '2.0', 'id': req_id, 'result': value}

    def error(message):
        return {'jsonrpc': '2.0', 'id': req_id, 'error': {'code': -32601, 'message': message}}

    if method == 'initialize':
        return result({'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                       'serverInfo': {'name': 'thinksmart-client', 'version': '0.1.0'}})
    if method == 'tools/list':
        return result({'tools': TOOLS + [{'name': 'self.music.transport',
            'description': 'Internal transport for the managed NetEase plugin.',
            'inputSchema': {'type': 'object', 'properties': {'action': {'type': 'string'},
                           'track': {'type': 'object'}}, 'required': ['action']}}]})
    if method == 'tools/call':
        name, args = params.get('name'), params.get('arguments') or {}
        try:
            if name == 'self.music.transport' and client:
                text = _dumps(await client.music.control(args))
            elif name == 'self.audio_speaker.set_volume' and client:
                text = json.dumps({'success': True, 'volume': await client.set_volume(args['volume'], 'voice')})
            elif name == 'self.audio_speaker.get_volume' and client:
                text = json.dumps({'volume': await client.device_volume()})
            elif name == 'self.sleep_mode.set' and client:
                text = json.dumps({'success': True, 'sleep': await client.set_sleep(
                    enabled=args.get('enabled'), start=args.get('start'), end=args.get('end'))},
                    ensure_ascii=False)
            elif name == 'self.sleep_mode.get' and client:
                text = json.dumps({**client.sleep, 'active': client.sleeping}, ensure_ascii=False)
            elif name == 'self.screen.set_brightness' and client:
                text = json.dumps({'success': True, **await client.set_brightness(
                    level=args.get('brightness'), change=args.get('change'))})
            elif name == 'self.screen.get_brightness' and client:
                text = json.dumps(client.brightness())
            elif name == 'self.alarm.create' and client:
                text = _dumps({'success': True, **_brief(await client.create_alarm(**_alarm_args(args)))})
            elif name == 'self.alarm.list' and client:
                view = client.alarm_view()
                items = [_brief(i) for i in view['items'] if not args.get('kind') or i['kind'] == args['kind']]
                text = _dumps({'items': items, 'missed_recently': len(view['missed']),
                               **({} if view['synced'] else {'note': '设备时间还没同步，闹钟暂停中'})})
            elif name == 'self.alarm.cancel' and client:
                if args.get('all'):
                    ids = [i['id'] for i in client.alarm_view()['items'] if not args.get('kind') or i['kind'] == args['kind']]
                elif args.get('id') is not None:
                    ids = [int(args['id'])]
                else:
                    raise ValueError('要传 id，或者 all=true')
                gone = [_brief(v) for v in [await client.delete_alarm(i) for i in ids] if v]
                text = _dumps({'success': True, 'cancelled': gone} if gone or args.get('all') else
                              {'success': False, 'reason': f"没有 id 为 {args['id']} 的，先用 self.alarm.list 查"})
            elif name == 'self.alarm.snooze' and client:
                minutes = int(args.get('minutes') or 10)
                found = await client.snooze_alarm(minutes)
                text = _dumps({'success': True, 'minutes': minutes, 'snoozed': [_brief(v) for v in found]} if found else
                              {'success': False, 'reason': '现在没有正在响、也没有 15 分钟内响过的闹钟',
                               'suggest': f'问用户要不要设一个 {minutes} 分钟后的闹钟（self.alarm.create）'})
            elif name == 'self.alarm.set_volume' and client:
                text = json.dumps({'success': True, 'volume': await client.set_alarm_volume(
                    volume=args.get('volume'), change=args.get('change'))})
            elif name == 'self.alarm.get_volume' and client:
                text = json.dumps({'volume': client.alarm_volume})
            else:
                return error(f'Unknown tool: {name}')
        except Exception as e:
            log.exception('tool %s failed', name)
            return result({'content': [{'type': 'text', 'text': str(e)}], 'isError': True})
        return result({'content': [{'type': 'text', 'text': text}], 'isError': False})
    return error(f'Method not implemented: {method}')
