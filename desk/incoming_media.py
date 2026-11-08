"""Read and authenticate received images; keys never leave the local backend.

Wire format verified against zhinjs/douyin-im, src/services/im/media.ts:
https://github.com/zhinjs/douyin-im/blob/master/src/services/im/media.ts
AES-256-GCM container: nonce(12) + ciphertext + authentication tag(16).
Real account evidence: resource_url.md5 hashes this encrypted container, while
content.md5 is a different source digest. Do not compare resource MD5 to plaintext.
"""
import hashlib
import hmac
import re
import io
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from .profiles import safe_avatar

MAX_IMAGE_BYTES = 20 * 1024 * 1024
FETCH_TIMEOUT = 15


class IncomingMediaError(ValueError):
    pass


def browser_image(data):
    """Convert authenticated HEIC photos into a browser-readable JPEG."""
    if data[4:8] == b'ftyp' and data[8:12] in (b'heic', b'heix', b'hevc', b'hevx', b'mif1', b'msf1'):
        try:
            from PIL import Image, ImageOps
            from pillow_heif import register_heif_opener
            register_heif_opener()
            with Image.open(io.BytesIO(data), formats=['HEIF']) as source:
                if source.width * source.height > 40_000_000:
                    raise IncomingMediaError('图片像素尺寸超过显示限制')
                converted = ImageOps.exif_transpose(source).convert('RGB')
                output = io.BytesIO()
                converted.save(output, format='JPEG', quality=90)
                data = output.getvalue()
        except IncomingMediaError:
            raise
        except Exception:
            raise IncomingMediaError('HEIC 图片转换失败，请核对原图') from None
    return data, image_mime(data)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def image_mime(data):
    """Only browser-displayable raster formats; never trust a CDN content type."""
    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
        raise IncomingMediaError('图片内容为空或超过大小限制')
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data[4:8] == b'ftyp' and data[8:12] in (b'avif', b'avis'):
        return 'image/avif'
    raise IncomingMediaError('图片格式无法显示')


def fetch_image(url, skey='', md5=''):
    """Return (authenticated image bytes, MIME); never log URLs or secrets."""
    if not safe_avatar(url):
        raise IncomingMediaError('图片来源地址无效')
    if not isinstance(skey, str) or (skey and not re.fullmatch(r'[0-9a-fA-F]{64}', skey)):
        raise IncomingMediaError('图片解密信息无效')
    if not isinstance(md5, str) or (md5 and not re.fullmatch(r'[0-9a-fA-F]{32}', md5)):
        raise IncomingMediaError('图片摘要信息无效')
    try:
        request = Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'image/*,application/octet-stream'})
        with build_opener(_NoRedirect()).open(request, timeout=FETCH_TIMEOUT) as response:
            if response.status != 200:
                raise IncomingMediaError('图片资源暂时不可用')
            length = response.headers.get('Content-Length')
            if length is not None and (not length.isdigit() or int(length) > MAX_IMAGE_BYTES):
                raise IncomingMediaError('图片超过大小限制')
            raw = response.read(MAX_IMAGE_BYTES + 1)
    except IncomingMediaError:
        raise
    except Exception:
        raise IncomingMediaError('图片读取失败，请稍后重试') from None
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise IncomingMediaError('图片内容为空或超过大小限制')
    if md5 and not hmac.compare_digest(hashlib.md5(raw).hexdigest(), md5.lower()):
        raise IncomingMediaError('图片摘要校验失败')
    if skey:
        if len(raw) < 28:
            raise IncomingMediaError('图片加密内容不完整')
        try:
            raw = AESGCM(bytes.fromhex(skey)).decrypt(raw[:12], raw[12:], None)
        except Exception:
            raise IncomingMediaError('图片解密校验失败') from None
    return browser_image(raw)
