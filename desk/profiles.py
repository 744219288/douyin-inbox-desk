"""Best-effort contact metadata, independent of message delivery."""
from urllib.parse import urlsplit


def safe_avatar(value):
    if not isinstance(value, str) or len(value) > 4096:
        return ''
    try:
        url = urlsplit(value)
        host = url.hostname or ''
        allowed = any(host == domain or host.endswith('.' + domain) for domain in
                      ('douyinpic.com', 'douyinstatic.com', 'byteimg.com', 'ibytedtos.com'))
        return value if url.scheme == 'https' and allowed and not url.username and not url.password and url.port in (None, 443) else ''
    except ValueError:
        return ''


def parse_profile(payload, uid, sec_uid):
    user = payload.get('user', {})
    if payload.get('status_code') != 0 or not isinstance(user, dict):
        raise ValueError('profile unavailable')
    if str(user.get('uid')) != str(uid) or (sec_uid and user.get('sec_uid') != sec_uid):
        raise ValueError('profile identity mismatch')
    name = user.get('nickname')
    if not isinstance(name, str) or not name.strip():
        raise ValueError('profile name missing')
    avatar = user.get('avatar_thumb') or {}
    urls = avatar.get('url_list', []) if isinstance(avatar, dict) else []
    return {'name': name.strip()[:200], 'avatar_url': next((safe_avatar(u) for u in urls if safe_avatar(u)), ''),
            'sec_uid': user.get('sec_uid', '')}


def fetch_profile(data_dir, uid, sec_uid):
    if not str(uid).isdigit() or (sec_uid and (not isinstance(sec_uid, str) or not sec_uid.startswith('MS4w') or not all(c.isalnum() or c in '-_=' for c in sec_uid))):
        raise ValueError('invalid contact identity')
    from .transport import load_vendor, read_session
    Auth, API, *_ = load_vendor()
    session = read_session(data_dir)
    auth = Auth.from_cookie(session['cookie'], ticket=session.get('ticket'), ts_sign=session.get('ts_sign'),
        client_cert=session.get('client_cert'), private_key=session.get('private_key'), bootstrap_creator=False)
    if sec_uid:
        payload = API.get_user_info(auth, 'https://www.douyin.com/user/' + sec_uid)
    else:
        # Same upstream profile request, using numeric user_id for old sessions
        # whose message parser did not retain sec_sender. Verified with this account.
        from builder.header import HeaderBuilder, HeaderType
        from builder.params import Params
        from utils import http_client
        referer = 'https://www.douyin.com/'
        headers = HeaderBuilder.build(HeaderType.GET)
        headers.set_referer(referer)
        headers.with_uifid(auth)
        params = Params()
        for key, value in {'device_platform':'webapp','aid':'6383','channel':'channel_pc_web',
            'publish_video_strategy_type':'2','source':'channel_pc_web','user_id':str(uid),
            'personal_center_strategy':'1','profile_other_record_enable':'1','land_to':'1'}.items():
            params.add_param(key,value)
        params.with_platform(round_trip_time='0')
        params.with_web_id(auth,referer)
        params.with_uifid(auth)
        params.add_param('msToken',auth.msToken)
        params.with_a_bogus()
        params.with_verify_fp(auth)
        response = http_client.get('https://www.douyin.com/aweme/v1/web/user/profile/other/',
            headers=headers.get(),cookies=auth.cookie,params=params.get(),timeout=15,verify=True,allow_redirects=False)
        response.raise_for_status()
        payload = response.json()
    return parse_profile(payload, uid, sec_uid)
