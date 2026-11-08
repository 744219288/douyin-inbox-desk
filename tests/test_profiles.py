import unittest
from desk.profiles import safe_avatar, parse_profile


class ProfileTests(unittest.TestCase):
    def test_rejects_other_contact_identity(self):
        body={'status_code':0,'user':{'uid':'2','sec_uid':'MS4wOther','nickname':'wrong'}}
        with self.assertRaises(ValueError):
            parse_profile(body,'1','MS4wRight')

    def test_avatar_hosts_and_schemes(self):
        for url in ('javascript:alert(1)','http://p3.douyinpic.com/a','https://douyinpic.com.evil.test/a',
                    'https://user:pass@p3.douyinpic.com/a','https://127.0.0.1/a'):
            self.assertEqual('',safe_avatar(url))
        self.assertEqual('https://p3.douyinpic.com/a',safe_avatar('https://p3.douyinpic.com/a'))

    def test_valid_name_without_avatar(self):
        result=parse_profile({'status_code':0,'user':{'uid':'1','sec_uid':'MS4wX','nickname':' 张三 '}},'1','')
        self.assertEqual('张三',result['name'])
        self.assertEqual('',result['avatar_url'])

    def test_platform_failure_does_not_become_profile(self):
        with self.assertRaises(ValueError):
            parse_profile({'status_code':8},'1','')
