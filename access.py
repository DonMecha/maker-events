"""Cloudflare Accessの本人認証を要求する。停止は公開設定を外して再起動。

秘密を保管せず、署名検証に必要な公開鍵だけ取得する。ネットワーク障害は拒否。
"""
import ipaddress
import json
import re
from pathlib import Path

import jwt


class AccessGate:
    ip = dns = None
    error = '本人認証を確認できません。アプリのURLからログインしてください'

    def __init__(self, path):
        config = json.loads(Path(path).read_text())
        self.host = config['host']
        team, self.audience = config['team'], config['audience']
        if not re.fullmatch(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', self.host):
            raise ValueError('公開先ホストを確認してください')
        if not re.fullmatch(r'[a-z0-9-]+', team) or not re.fullmatch(r'[a-f0-9]{64}', self.audience):
            raise ValueError('Accessの設定を確認してください')
        # 別アプリのaudienceや、メールヘッダだけで本人と扱わない。
        self.owners = frozenset(s.strip().lower() for s in config['owner_emails'])
        if not self.owners or any(not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', s) for s in self.owners):
            raise ValueError('本人の許可設定を確認してください')
        self.origin = 'https://' + self.host
        self.issuer = 'https://' + team + '.cloudflareaccess.com'
        self.keys = jwt.PyJWKClient(self.issuer + '/cdn-cgi/access/certs', timeout=5, lifespan=300)

    def allowed(self, ip, on_loopback=False, token=''):
        try:
            # Tunnelはloopbackへ接続。ローカルでも認証を省略しない。
            if not on_loopback or not ipaddress.ip_address(ip).is_loopback or not token or len(token) > 12000:
                return False
            key = self.keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=['RS256'], audience=self.audience,
                                issuer=self.issuer, options={'require': ['exp', 'iat', 'iss', 'aud', 'sub', 'email']})
            return (isinstance(claims['sub'], str) and bool(claims['sub']) and
                    claims.get('type') == 'app' and isinstance(claims['email'], str) and
                    claims['email'].lower() in self.owners)
        except (jwt.PyJWTError, OSError, ValueError, TypeError, KeyError):
            return False
