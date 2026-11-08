import tempfile
import unittest
import time
import json
from unittest.mock import patch
from pathlib import Path
from desk.service import DeskService


class Wire:
    def connect(self, settings, message, status):
        status('connected')
    def disconnect(self):
        pass
    def send(self, raw, text):
        self.sent.append(text)
    def __init__(self):
        self.sent = []


class MediaFlowTests(unittest.TestCase):
    def test_clock_skew_cannot_repeat_answer_on_reconnect(self):
        self.service.receive('c','peer','时钟测试','skew',created_at_ms=(time.time()+5)*1000)
        self.service.pool.shutdown(wait=True)
        self.assertEqual(len(self.wire.sent),1)
        self.service.disconnect()
        self.service.connect()
        self.assertEqual(len(self.wire.sent),1)

    def test_live_transport_waits_for_history_before_resuming(self):
        self.wire.requires_history_before_resume = True
        self.service.receive('c','peer','旧请求','old',_schedule_reply=False)
        self.service.disconnect()
        self.service.connect()
        self.assertEqual(self.wire.sent,[])
        self.service.recover_batch([dict(conversation_id='c',sender_id='peer',text='旧请求',message_id='old',created_at_ms=int(time.time()*1000),history_only=True)])
        self.assertEqual(self.wire.sent,[])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wire = Wire()
        self.service = DeskService(Path(self.tmp.name), lambda *_: '收到，有什么可以帮您？', self.wire)
        self.service._queue_profile = lambda _: None
        self.service.update_settings({'mode': 'live', 'deepseek_api_key': 'fake'})
        self.service.connect()

    def tearDown(self):
        self.service.pool.shutdown(wait=True)
        self.service.close()
        self.tmp.cleanup()

    def test_history_fills_empty_image_url_in_place_without_reply(self):
        stamp = int(time.time()*1000)
        self.service.takeover(self.service.receive('c','peer','人工','first'))
        self.service.receive('c','peer','[非文字消息，请人工处理]','photo',created_at_ms=stamp,media_kind='image')
        before = self.service.conversation('live:c')['messages']
        self.service.recover_batch([dict(conversation_id='c',sender_id='peer',text='[图片]',message_id='photo',created_at_ms=stamp,media_kind='image',media_url='https://p26-sign.douyinpic.com/test.png')])
        after = self.service.conversation('live:c')['messages']
        self.assertEqual([m['id'] for m in before], [m['id'] for m in after])
        self.assertEqual(next(m for m in after if m['media_kind']=='image')['media_url'], 'https://p26-sign.douyinpic.com/test.png')
        self.assertEqual(self.wire.sent, [])

    def test_display_only_media_does_not_change_reception_or_send(self):
        self.service.recover_batch([dict(conversation_id='c',sender_id='peer',text='[图片]',message_id='old',created_at_ms=1000,media_kind='image',media_url='https://p26-sign.douyinpic.com/test.png',history_only=True)])
        self.service.pool.shutdown(wait=True)
        view = self.service.conversation('live:c')
        self.assertEqual(len(view['messages']),1)
        self.assertEqual(view['conversation']['status'],'ai')
        self.assertEqual(self.wire.sent,[])

    def test_reconnected_sticker_alone_gets_one_reply(self):
        e=dict(conversation_id='c',sender_id='peer',text='[表情包：微笑]',message_id='offline',created_at_ms=int(time.time()*1000),media_kind='sticker',media_url='https://p26-sign.douyinpic.com/test.png')
        self.service.recover_batch([e])
        self.service.pool.shutdown(wait=True)
        self.service.recover_batch([e])
        self.assertEqual(len(self.wire.sent),1)

    def test_encrypted_media_key_stays_server_side_and_cache_is_mode_scoped(self):
        secret='01'*32
        self.service.receive('c','peer','[图片]','encrypted',media_kind='image',media_url='https://p26-sign.douyinpic.com/test.png',media_secret=secret,media_md5='02'*16,_schedule_reply=False)
        view=self.service.conversation('live:c')
        mid=view['messages'][0]['id']
        self.assertEqual(view['messages'][0]['media_url'],'/api/incoming-media/'+mid)
        self.assertNotIn(secret,json.dumps(view))
        png=b'\x89PNG\r\n\x1a\n'+b'x'*32
        with patch('desk.incoming_media.fetch_image',return_value=(png,'image/png')) as fetch:
            self.assertEqual(self.service.incoming_image(mid),(png,'image/png'))
            self.assertEqual(self.service.incoming_image(mid),(png,'image/png'))
            fetch.assert_called_once()
        self.service.update_settings({'mode':'demo'})
        with self.assertRaises(ValueError):self.service.incoming_image(mid)

    def test_image_reply_does_not_claim_vision_or_force_handoff(self):
        captured=[]
        self.service.responder=lambda settings,*args: captured.append(settings['persona']) or '请问想咨询什么？'
        self.service.receive('c','peer','[图片]','image',media_kind='image')
        self.service.pool.shutdown(wait=True)
        self.assertEqual(self.service.conversation('live:c')['conversation']['status'],'ai')
        self.assertEqual(len(self.wire.sent),1)
        self.assertIn('未获得图片像素',captured[0])
