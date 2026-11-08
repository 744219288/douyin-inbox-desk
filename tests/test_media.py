import unittest
from desk.media import decode_content

class MediaTests(unittest.TestCase):
    def test_actual_sticker_shape(self):
        m=decode_content(5,{'url':{'uri':'abc','url_list':['https://p11-sign.douyinpic.com/a.webp']}})
        self.assertEqual(m,{'text':'[表情包]','media_kind':'sticker','media_url':'https://p11-sign.douyinpic.com/a.webp'})

    def test_bad_urls_and_missing_image_keep_sticker_identity(self):
        for url in ('javascript:alert(1)','http://p11-sign.douyinpic.com/a','https://douyinpic.com.evil.com/a','https://user@p11-sign.douyinpic.com/a','https://127.0.0.1/a'):
            m=decode_content(5,{'url':{'url_list':[url]}})
            self.assertEqual(m['media_kind'],'sticker')
            self.assertEqual(m['media_url'],'')

    def test_unsupported_media_still_requires_human(self):
        self.assertEqual(decode_content(17,{})['text'],'[非文字消息，请人工处理]')

    def test_pc_image_uses_original_resource_not_other_cover(self):
        m = decode_content(27, {'cover': 'https://p11-sign.douyinpic.com/wrong.png',
            'resource_url': {'origin_url_list': ['https://p11-sign.douyinpic.com/photo.png']}})
        self.assertEqual(m['media_kind'], 'image')
        self.assertEqual(m['media_url'], 'https://p11-sign.douyinpic.com/photo.png')

    def test_sticker_prefers_own_url_over_author_avatar(self):
        m = decode_content(5, {'author_avatar': 'https://p11-sign.douyinpic.com/avatar.png',
            'url': {'url_list': ['https://p11-sign.douyinpic.com/emoji.gif']}})
        self.assertEqual(m['media_url'], 'https://p11-sign.douyinpic.com/emoji.gif')
