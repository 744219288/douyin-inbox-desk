"""Decode display-only media without passing image content to the text model."""
import json
from .profiles import safe_avatar


def decode_content(kind, content):
    try:
        payload = json.loads(content) if isinstance(content, str) else content
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if kind in (7, 10001):
        text = payload.get('text', '')
        return {'text': text[:4000] if isinstance(text, str) else ''}
    media_kind = 'sticker' if kind in (5, 10002) else 'image' if kind in (2, 27, 10003) else 'unsupported'
    # Only trusted HTTPS image CDN URLs can become image elements.
    def find_url(value, depth=0):
        if depth > 8:
            return ''
        if isinstance(value, str):
            return safe_avatar(value)
        children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else []
        for child in list(children)[:64]:
            found = find_url(child, depth+1)
            if found:
                return found
        return ''
    # These fields belong to the message itself; generic recursive traversal
    # can accidentally choose an author's avatar or an unrelated card cover.
    resource = payload.get('resource_url', {})
    source = payload.get('url', {}) if media_kind == 'sticker' else resource
    if isinstance(source, dict):
        source = source.get('origin_url_list') or source.get('url_list') or source
    url = find_url(source)
    if not url:
        url = find_url(payload.get('url_list', []))
    label = payload.get('display_name', '')
    label = label.strip()[:80] if isinstance(label, str) else ''
    text = ('[表情包：' + label + ']' if label else '[表情包]') if media_kind == 'sticker' else '[图片]' if media_kind == 'image' else '[非文字消息，请人工处理]'
    result = {'text': text,
            'media_kind': media_kind,
            'media_url': url if media_kind in ('sticker', 'image') else ''}
    if media_kind == 'image' and isinstance(resource, dict) and resource.get('skey'):
        result['media_secret'] = resource['skey']
        result['media_md5'] = resource.get('md5', '')
    return result
