"""画面とAPIを1プロセスで配信する個人用サーバー。

正本: data/events.sqlite3。停止: このserverのserviceだけを停止する。
LAN公開は行わない。Cloudflare設定は別ファイル、秘密は配信しない。
"""
import argparse
import ipaddress
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from core import Store, dumps

BASE = Path(__file__).resolve().parent
DEFAULT_DB = BASE / 'data/events.sqlite3'

def tailscale_json(*args):
    result = subprocess.run(['tailscale', *args], capture_output=True, text=True, timeout=5, check=True)
    return json.loads(result.stdout)


def node_user_matches(record, owner_id):
    node = record.get('Node') or {}
    profile = record.get('UserProfile') or {}
    return bool(owner_id and node.get('User') and profile.get('ID') and
                str(node['User']) == str(owner_id) == str(profile['ID']) and
                node.get('MachineAuthorized') is True)


class Gate:
    error = '本人のTailscale端末から接続してください'
    def __init__(self, tailscale=False):
        self.owner_id, self.ip, self.dns = None, None, None
        self.cache, self.lock = {}, threading.Lock()
        if tailscale:
            status = tailscale_json('status', '--json')
            own = status.get('Self') or {}
            if status.get('BackendState') != 'Running' or not own.get('UserID') or not own.get('Online'):
                raise RuntimeError('Tailscaleの本人ログインを確認できないため接続を開始しません')
            self.owner_id = own['UserID']
            self.ip = next((s for s in own.get('TailscaleIPs', []) if ipaddress.ip_address(s).version == 4), None)
            self.dns = own.get('DNSName', '').rstrip('.')
            if not self.ip or not node_user_matches(tailscale_json('whois', '--json', self.ip), self.owner_id):
                raise RuntimeError('Tailscaleの本人端末を照合できません')

    def allowed(self, ip, on_loopback=False, token=''):
        address = ipaddress.ip_address(ip)
        # ローカル開発接続はloopbackで受けた要求に限定。
        if address.is_loopback:
            return on_loopback
        if not self.owner_id or address.version != 4 or address not in ipaddress.ip_network('100.64.0.0/10'):
            return False
        with self.lock:
            cached = self.cache.get(ip)
            if cached and cached[0] > time.monotonic():
                return cached[1]
        try:
            allowed = node_user_matches(tailscale_json('whois', '--json', ip), self.owner_id)
        except (subprocess.SubprocessError, OSError, ValueError, TypeError):
            allowed = False
        with self.lock:
            self.cache[ip] = (time.monotonic() + 5, allowed)
        return allowed


def serve(store, port, use_tailscale, access_config=None):
    from waitress.server import create_server
    from wsgi import App
    if access_config:
        from access import AccessGate
        gate = AccessGate(access_config)
    else:
        gate = Gate(use_tailscale)
    hosts = ['127.0.0.1'] + ([gate.ip] if gate.ip else [])
    application = App(store, gate, port)
    server = create_server(application, listen=' '.join(f'{host}:{port}' for host in hosts),
                           threads=4, connection_limit=32, channel_timeout=30,
                           cleanup_interval=5, max_request_header_size=8192,
                           max_request_body_size=150000, clear_untrusted_proxy_headers=True,
                           expose_tracebacks=False, ident='MakerEvents')
    try:
        for host in hosts:
            print(f'http://{host}:{port}/', flush=True)
        print('Waitress / 本人認証 / SQLite保存', flush=True)
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='個人用AI・メイカーイベント管理')
    parser.add_argument('--db', default=str(DEFAULT_DB))
    sub = parser.add_subparsers(dest='command', required=True)
    server = sub.add_parser('serve')
    server.add_argument('--port', type=int, default=8798)
    mode = server.add_mutually_exclusive_group()
    mode.add_argument('--tailscale', action='store_true')
    mode.add_argument('--access-config', help='Cloudflare Accessの非秘密設定JSON')
    sub.add_parser('daily')
    sub.add_parser('check')
    sub.add_parser('export')
    args = parser.parse_args()
    store = Store(args.db)
    if args.command == 'serve':
        serve(store, args.port, args.tailscale, args.access_config)
    elif args.command == 'daily':
        from collect import daily
        print(dumps(daily(store)))
    elif args.command == 'check':
        with store.connect() as db:
            print(db.execute('PRAGMA integrity_check').fetchone()[0])
    elif args.command == 'export':
        print(dumps(store.export()))


if __name__ == '__main__':
    main()
