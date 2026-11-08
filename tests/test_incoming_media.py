import hashlib
import io
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from desk.incoming_media import fetch_image, image_mime, IncomingMediaError, _NoRedirect


class Response(io.BytesIO):
    status = 200
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}


class IncomingMediaTests(unittest.TestCase):
    url = 'https://p26-sign.douyinpic.com/obj/test?signature=private'
    key = bytes(range(32))
    nonce = bytes(range(12))
    plain = b'\xff\xd8\xff\xe1' + b'fixture image bytes' * 10

    def encrypted(self):
        return self.nonce + AESGCM(self.key).encrypt(self.nonce, self.plain, None)

    def fetch(self, raw, key=None, md5='', response=None):
        with patch('desk.incoming_media.build_opener') as factory:
            factory.return_value.open.return_value = response or Response(raw)
            result = fetch_image(self.url, self.key.hex() if key is None else key, md5)
            request = factory.return_value.open.call_args.args[0]
            self.assertFalse(request.has_header('Cookie'))
            self.assertEqual(factory.return_value.open.call_args.kwargs['timeout'], 15)
            return result

    def test_encrypted_container_digest_then_authenticated_jpeg(self):
        raw = self.encrypted()
        self.assertEqual(self.fetch(raw, md5=hashlib.md5(raw).hexdigest()), (self.plain, 'image/jpeg'))

    def test_wrong_key_and_tamper_are_rejected(self):
        raw = self.encrypted()
        for changed, key in ((raw, 'ff' * 32), (raw[:-1] + bytes([raw[-1] ^ 1]), self.key.hex())):
            with self.subTest(key_changed=key != self.key.hex()), self.assertRaises(IncomingMediaError) as raised:
                self.fetch(changed, key)
            self.assertNotIn(key, str(raised.exception))
            self.assertNotIn('private', str(raised.exception))

    def test_truncated_or_wrong_digest_rejected(self):
        for raw, digest in ((b'short', ''), (self.encrypted(), '0' * 32)):
            with self.assertRaises(IncomingMediaError):
                self.fetch(raw, md5=digest)

    def test_plain_raster_and_mime_not_http_header(self):
        samples = [(b'\x89PNG\r\n\x1a\nrest', 'image/png'), (b'GIF89arest', 'image/gif'),
                   (b'GIF87arest', 'image/gif'), (b'RIFF\x00\x00\x00\x00WEBPrest', 'image/webp')]
        for raw, expected in samples:
            self.assertEqual(self.fetch(raw, key='', response=Response(raw, {'Content-Type': 'text/html'})), (raw, expected))
        for raw in (b'<html>not an image</html>', b'<svg></svg>', b'not an image'):
            with self.assertRaises(IncomingMediaError):
                image_mime(raw)

    def test_limits_enforced_with_and_without_content_length(self):
        with patch('desk.incoming_media.MAX_IMAGE_BYTES', 32):
            for response in (Response(b'x'*33), Response(b'x', {'Content-Length': '33'})):
                with self.assertRaises(IncomingMediaError):
                    self.fetch(b'', key='', response=response)

    def test_invalid_url_key_digest_make_no_request(self):
        with patch('desk.incoming_media.build_opener') as factory:
            for url in ('http://p26-sign.douyinpic.com/a', 'https://douyinpic.com.evil.test/a', 'https://127.0.0.1/a', 'https://user@p26-sign.douyinpic.com/a'):
                with self.assertRaises(IncomingMediaError):
                    fetch_image(url)
            for key, digest in (('x'*64, ''), ('ab', ''), ('', 'invalid')):
                with self.assertRaises(IncomingMediaError):
                    fetch_image(self.url, key, digest)
            factory.assert_not_called()

    def test_redirect_and_network_errors_are_redacted(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, '', {}, 'https://127.0.0.1'))
        response = Response(self.plain); response.status = 302
        with self.assertRaises(IncomingMediaError):
            self.fetch(b'', key='', response=response)
        with patch('desk.incoming_media.build_opener') as factory:
            factory.return_value.open.side_effect = OSError('private signed URL and key')
            with self.assertRaises(IncomingMediaError) as raised:
                fetch_image(self.url)
            self.assertNotIn('private', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
