"""認証の迂回・別アプリJWT・期限切れ・本人以外・書込元の実HTTP試験。"""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from waitress.server import create_server

from access import AccessGate
from core import Store
from tests import sample
from wsgi import App


class AccessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name)/'access.json'
        self.config.write_text(json.dumps({'host':'app.example.com','team':'fixture',
                                          'audience':'a'*64,'owner_emails':['owner@example.test']}))
        self.gate = AccessGate(self.config)
        self.gate.keys = SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=self.key.public_key()))

    def tearDown(self):
        self.tmp.cleanup()

    def token(self, changes=None, key=None):
        now = int(time.time())
        claims = {'iss':self.gate.issuer, 'aud':['a'*64], 'exp':now+300, 'iat':now-1,
                  'sub':'fixture-owner','email':'owner@example.test','type':'app'}
        claims.update(changes or {})
        return jwt.encode(claims, key or self.key, algorithm='RS256', headers={'kid':'fixture'})

    def test_identity_signature_claims_and_no_local_bypass(self):
        self.assertTrue(self.gate.allowed('127.0.0.1', True, self.token()))
        for claims in ({'aud':['b'*64]}, {'iss':'https://other.cloudflareaccess.com'},
                       {'exp':int(time.time())-1}, {'iat':int(time.time())+300},
                       {'email':'other@example.test'}, {'type':'service'}, {'sub':''}, {'email':None}):
            self.assertFalse(self.gate.allowed('127.0.0.1', True, self.token(claims)))
        self.assertFalse(self.gate.allowed('127.0.0.1',True,self.token(key=self.other_key)))
        self.assertFalse(self.gate.allowed('127.0.0.1',True,jwt.encode({'sub':'fixture'},None,algorithm='none')))
        self.assertFalse(self.gate.allowed('127.0.0.1',True,''))
        self.assertFalse(self.gate.allowed('100.64.0.1',True,self.token()))
        self.assertFalse(self.gate.allowed('127.0.0.1',False,self.token()))
        with patch.object(self.gate.keys,'get_signing_key_from_jwt',side_effect=jwt.PyJWKClientConnectionError('offline')):
            self.assertFalse(self.gate.allowed('127.0.0.1',True,self.token()))

    def test_https_host_origin_authorization_persistence_and_restore(self):
        store = Store(Path(self.tmp.name)/'events.sqlite3')
        app = App(store,self.gate,1)
        server = create_server(app,host='127.0.0.1',port=0,threads=2,asyncore_loop_timeout=.05,
                               clear_untrusted_proxy_headers=True)
        thread = threading.Thread(target=server.run);thread.start()
        base = 'http://127.0.0.1:'+str(server.effective_port)
        token = self.token()
        def request(path, payload=None, **headers):
            fields = {'Host':self.gate.host,'Cf-Access-Jwt-Assertion':token,**headers}
            return urllib.request.urlopen(urllib.request.Request(base+path,
                data=json.dumps(payload).encode() if payload is not None else None,headers=fields),timeout=5)
        try:
            for headers in ({'Cf-Access-Jwt-Assertion':''}, {'Host':'127.0.0.1'},
                            {'Cf-Access-Jwt-Assertion':'invalid','Cf-Access-Authenticated-User-Email':'owner@example.test',
                             'X-Forwarded-For':'127.0.0.1','X-Forwarded-Proto':'https'}):
                for path in ('/','/api/events','/api/export','/health'):
                    with self.assertRaises(urllib.error.HTTPError) as error:request(path,**headers)
                    self.assertEqual(error.exception.code,403)
            with request('/') as response:self.assertEqual(response.status,200)
            for origin in ('http://app.example.com','https://evil.example.test',''):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request('/api/events',sample(),**{'Origin':origin,'Content-Type':'application/json'})
                self.assertEqual(error.exception.code,403)
            headers = {'Origin':self.gate.origin,'Content-Type':'application/json'}
            with request('/api/events',sample(),**headers) as response:event_id=json.load(response)['id']
            with request('/api/events/'+event_id+'/state',{'version':0,'note':'隔離保存','status':'気になる'},**headers) as response:
                self.assertEqual(response.status,200)
            with request('/api/events') as response:self.assertEqual(json.load(response)['events'][0]['state']['note'],'隔離保存')
            backup = store.backup(Path(self.tmp.name)/'restore.sqlite3')
            self.assertEqual(Store(backup).export(),Store(store.path).export())
            # 同じDBをローカルモードへ戻す復元。本人編集は消さない。
            from server import Gate
            restored = App(Store(backup),Gate(),1)
            self.assertIsNone(restored.public_origin)
            self.assertEqual(restored.store.events()[0]['state']['note'],'隔離保存')
        finally:
            server.task_dispatcher.shutdown()
            for channel in list(server._map.values()):channel.close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())


if __name__=='__main__':unittest.main(verbosity=2)
