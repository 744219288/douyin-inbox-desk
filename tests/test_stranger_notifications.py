import json
import unittest
from desk.transport import load_vendor, parse_incoming
from desk.stranger import _field


class StrangerNotificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        *_, cls.Live, cls.Response = load_vendor()

    def frame(self, sender=12, kind=7, conversation_type=1, identity=123):
        msg = self.Response.MessageBody(conversation_id='0:1:99:12',
            conversation_type=conversation_type, sender=sender,
            server_message_id=identity, message_type=kind,
            content=json.dumps({'text':'hello'}))
        payload = _field(1,1099) + _field(6,_field(503,_field(2,msg.SerializeToString())))
        return self.Live.PushFrame(payloadType='pb',payload=payload).SerializeToString()

    def test_stranger_body_503_is_received(self):
        result = parse_incoming(self.frame(),'99',self.Live,self.Response)
        self.assertEqual(result['text'],'hello')
        self.assertEqual(result['name'],'陌生人私信')
        self.assertEqual(result['message_id'],'123')

    def test_stranger_group_receipt_and_unstable_id_ignored(self):
        for options in ({'conversation_type':2},{'kind':50001},{'identity':0}):
            self.assertIsNone(parse_incoming(self.frame(**options),'99',self.Live,self.Response))

    def test_stranger_media_goes_to_manual(self):
        self.assertEqual(parse_incoming(self.frame(kind=27),'99',self.Live,self.Response)['text'],
                         '[图片]')
