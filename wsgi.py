"""HTTP解析はWaitressへ任せ、アプリの認可と操作だけを扱う。

外部HTMLやコードは実行しない。秘密・本文・メモをログに出さない。
"""
import ipaddress
import json
import logging
import re
from http import HTTPStatus
from pathlib import Path

from core import Conflict, LANGUAGES, STATUSES, canonical_url, dumps, now

BASE = Path(__file__).resolve().parent
ASSETS = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/connpass-application': ('connpass-application.html', 'text/html; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/style.css': ('style.css', 'text/css; charset=utf-8')}


class App:
    def __init__(self, store, gate, port):
        self.store, self.gate = store, gate
        self.public_origin = getattr(gate, 'origin', None)
        self.hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        if gate.ip:
            self.hosts.add(f'{gate.ip}:{port}')
        if gate.dns:
            self.hosts.add(f'{gate.dns}:{port}')
        if self.public_origin:
            self.hosts = {gate.host}

    def __call__(self, env, start_response):
        attachment = None
        mime = 'application/json; charset=utf-8'
        try:
            ip = env.get('REMOTE_ADDR', '')
            try:
                local = ipaddress.ip_address(ip).is_loopback
            except ValueError:
                local = False
            if not self.gate.allowed(ip, local, env.get('HTTP_CF_ACCESS_JWT_ASSERTION', '')):
                status, value = 403, {'error': self.gate.error}
            elif env.get('HTTP_HOST') not in self.hosts:
                status, value = 403, {'error': '接続先ホストを確認してください'}
            elif env['REQUEST_METHOD'] in ('GET', 'HEAD'):
                status, value, mime, attachment = self.read(env['PATH_INFO'])
            elif env['REQUEST_METHOD'] == 'POST':
                if env.get('HTTP_ORIGIN') != (self.public_origin or 'http://' + env['HTTP_HOST']) or env.get('CONTENT_TYPE', '').split(';')[0] != 'application/json':
                    status, value = 403, {'error': 'アプリ画面から操作してください'}
                else:
                    size = int(env.get('CONTENT_LENGTH') or 0)
                    if not 0 < size <= 150000:
                        raise ValueError('入力サイズを確認してください')
                    payload = json.loads(env['wsgi.input'].read(size))
                    if not isinstance(payload, dict):
                        raise ValueError('入力形式を確認してください')
                    status, value = self.write(env['PATH_INFO'], payload)
            else:
                status, value = 405, {'error': 'この操作は利用できません'}
        except Conflict as error:
            status, value = 409, {'error': str(error)}
        except (ValueError, TypeError) as error:
            status, value = 400, {'error': str(error) if type(error) is ValueError else '入力形式を確認してください'}
        except KeyError:
            status, value = 404, {'error': '対象が見つかりません'}
        except Exception as error:
            # DBや内部例外の内容を外へ出さない。エラーそのものはHTTP 500で明示。
            logging.getLogger('maker-events').error('request failed (%s)', type(error).__name__)
            status, value = 500, {'error': '保存先・内部処理の確認が必要です。変更は再送せず更新して確認してください'}
        data = value if isinstance(value, bytes) else dumps(value).encode()
        headers = [('Content-Type', mime), ('Content-Length', str(len(data))), ('Cache-Control', 'no-store'),
                   ('X-Content-Type-Options', 'nosniff'), ('Referrer-Policy', 'no-referrer'),
                   ('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
                   ('X-Frame-Options', 'DENY')]
        if attachment:
            headers.append(('Content-Disposition', f'attachment; filename="{attachment}"'))
        start_response(f'{status} {HTTPStatus(status).phrase}', headers)
        return [] if env['REQUEST_METHOD'] == 'HEAD' else [data]

    def read(self, path):
        store = self.store
        mime, attachment = 'application/json; charset=utf-8', None
        if path in ASSETS:
            file, mime = ASSETS[path]
            value = (BASE / 'web' / file).read_bytes()
        elif path == '/api/events':
            value = {'events': store.events(), 'statuses': STATUSES, 'languages': LANGUAGES,
                     'server_time': now(), 'overview': store.overview()}
        elif re.fullmatch(r'/api/events/[a-f0-9]{24}/history', path):
            value = {'history': store.get_history(path.split('/')[3])}
        elif path == '/api/export':
            value, attachment = {'exported_at': now(), 'data': store.export()}, 'maker-events-export.json'
        elif path == '/health':
            with store.connect() as db:
                db.execute('SELECT 1').fetchone()
            value = {'status': 'ok'}
        else:
            return 404, {'error': '見つかりません'}, mime, None
        return 200, value, mime, attachment

    def write(self, path, payload):
        if path == '/api/events':
            canonical = canonical_url(payload.get('url', ''))
            event_id = self.store.upsert('manual', canonical, payload, actor='owner', create_only=True)
            return 200, {'id': event_id}
        if re.fullmatch(r'/api/events/[a-f0-9]{24}/state', path):
            return 200, {'state': self.store.update_state(path.split('/')[3], payload)}
        if re.fullmatch(r'/api/events/[a-f0-9]{24}/organizer', path):
            return 200, {'review': self.store.review_organizer(path.split('/')[3], payload)}
        if re.fullmatch(r'/api/events/[a-f0-9]{24}/visibility', path):
            return 200, {'visibility': self.store.update_visibility(path.split('/')[3], payload)}
        if re.fullmatch(r'/api/events/[a-f0-9]{24}/facts', path):
            self.store.edit_facts(path.split('/')[3], payload)
            return 200, {'status': 'ok'}
        return 404, {'error': '見つかりません'}
