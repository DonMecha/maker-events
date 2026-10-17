"""日次の公式API取得。LLM・ブラウザ・非公開APIを使わない。

停止: 日次timerを止める。取得失敗は保存済みデータを削除・中止にしない。
1周の上限: 取得元ごと60 HTTP要求、要求間隔5秒、20秒/要求、再試行なし。
キー未設定のAPIにはアクセスしない。秘密や例外の本文をログへ出さない。
"""
import csv
import datetime as dt
import fcntl
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlencode

from core import CITY_SCOPE, JST, REGIONS, Store, canonical_url, normal_event, now

MAX_REQUESTS = 60
MAX_PAGES = 4
KEYWORDS = ['生成AI', 'LLM', '電子工作', 'Arduino', 'メイカー', '作品展示', 'ハッカソン']
PREFECTURES = ['tokyo', 'kanagawa', 'chiba', 'saitama', 'online']
CONNPASS = 'https://connpass.com/api/v2/events/'
DOORKEEPER = 'https://api.doorkeeper.jp'
HOLIDAYS = 'https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv'
TECHPLAY_RSS = 'https://rss.techplay.jp/event/w3c-rss-format/rss.xml'
LUMA_TOKYO_ICS = 'https://api.luma.com/ics/get?entity=discover&id=discplace-9H7asQEvWiv6DA9'


class RequestFailure(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 別ホストへAPIキーを転送しない。変更されたAPI URLは保守で確認する。
        raise RequestFailure('APIの転送先が変わりました。要確認')


class Fetcher:
    def __init__(self, interval=5):
        self.interval, self.last, self.calls = interval, 0, 0

    def get(self, url, headers=None):
        if self.calls >= MAX_REQUESTS:
            raise RequestFailure('1周の要求上限に到達。次回に再確認')
        time.sleep(max(0, self.interval - (time.monotonic() - self.last)))
        self.calls += 1
        self.last = time.monotonic()
        request = urllib.request.Request(url, headers=dict({'User-Agent': 'PersonalMakerEvents/1.0', 'Accept': 'application/json'}, **(headers or {})))
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
                result = response.read(5_000_001)
                if len(result) > 5_000_000:
                    raise RequestFailure('応答サイズ上限を超えました')
                return result
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise RequestFailure('HTTP 404。公開状態は要確認（中止とは判定しない）') from None
            if error.code == 429:
                raise RequestFailure('HTTP 429。制限に達したため今回は停止') from None
            raise RequestFailure(f'HTTP {error.code}。保存済み情報は保持') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RequestFailure('通信失敗。保存済み情報は保持') from None

    def json(self, url, headers):
        try:
            return json.loads(self.get(url, headers))
        except (UnicodeError, json.JSONDecodeError):
            raise RequestFailure('APIの応答形式を確認できません') from None


def credential(name):
    # このアプリへ明示的に渡された値だけ。既存の秘密保管元を探索しない。
    value = os.environ.get(name)
    directory = os.environ.get('CREDENTIALS_DIRECTORY')
    if not value and directory:
        path = Path(directory) / name
        if path.is_file():
            value = path.read_text().strip()
    return value


def connpass_event(raw):
    return {'title': raw['title'], 'url': raw['url'], 'description': raw.get('description'),
            'start_at': raw.get('started_at'), 'end_at': raw.get('ended_at'),
            'venue': raw.get('place'), 'address': raw.get('address'),
            'online': (raw.get('place') or '').strip().lower() in ('オンライン', 'online'),
            'cancelled': raw.get('open_status') == 'cancelled',
            'cancel_evidence': 'connpass公式API open_status=cancelled' if raw.get('open_status') == 'cancelled' else ''}


def doorkeeper_event(raw):
    return {'title': raw['title'], 'url': raw['public_url'], 'description': raw.get('description'),
            'start_at': raw.get('starts_at'), 'end_at': raw.get('ends_at'),
            'venue': raw.get('venue_name'), 'address': raw.get('address'),
            'online': (raw.get('venue_name') or '').strip().lower() in ('オンライン', 'online'),
            # 公開API仕様に中止・申込締切のフィールドはない。推定しない。
            }


def relevant(raw):
    event = normal_event(raw)
    return bool(event['categories']) and (event['region'] in REGIONS + [CITY_SCOPE] or event['online'])


def parse_techplay(data):
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise RequestFailure('TECH PLAY公式RSSを解析できません。保存済みを保持') from None
    if root.tag != 'rss' or root.find('channel') is None:
        raise RequestFailure('TECH PLAYの配信形式が変わりました')
    parsed = []
    ns = '{https://rss.techplay.jp/}'
    for item in root.findall('./channel/item'):
        url = item.findtext('link') or item.findtext('guid') or ''
        if not re.fullmatch(r'https://techplay\.jp/event/\d+/?', url):
            raise RequestFailure('TECH PLAY公式RSSのイベントURLを確認できません')
        place = item.findtext(ns + 'eventPlace') or ''
        raw = {'title': item.findtext('title'), 'url': url,
               'description': item.findtext('{http://purl.org/rss/1.0/modules/content/}encoded') or item.findtext('description'),
               'start_at': item.findtext(ns + 'eventStartTime'), 'end_at': item.findtext(ns + 'eventEndTime'),
               'venue': place, 'address': item.findtext(ns + 'eventAddress'),
               'online': place.strip().lower() in ('オンライン', 'online', 'virtual')}
        creator = item.findtext('{http://purl.org/dc/elements/1.1/}creator')
        if creator:
            raw.update(organizer_name=creator,organizer_evidence='TECH PLAY公式RSS dc:creator（掲載者名。実績の確認とは別）')
        # dc:language=jaは記事の言語。参加時の言語の証拠にはしない。
        parsed.append((canonical_url(url), normal_event(raw)))
    return parsed


def parse_luma(data):
    from icalendar import Calendar
    try:
        calendar = Calendar.from_ical(data)
    except (ValueError, TypeError):
        raise RequestFailure('Luma公式iCalを解析できません。保存済みを保持') from None
    if calendar.name != 'VCALENDAR':
        raise RequestFailure('Lumaの配信形式が変わりました')
    parsed, skipped = [], 0
    for event in calendar.walk('VEVENT'):
        if not str(event.get('SUMMARY') or '').strip():
            skipped += 1
            continue  # 題名のない配信項目は表示せず、他の有効なイベントを取得する。
        if any(event.get(k) for k in ('RRULE', 'RDATE', 'RECURRENCE-ID')) or event.errors:
            skipped += 1
            continue  # 繰返しや不明な日時を単発イベントとして誤表示しない。
        uid = str(event.get('UID') or '')
        if not uid or len(uid) > 500:
            raise RequestFailure('Lumaのイベント識別子を確認できません')
        description = str(event.get('DESCRIPTION') or '')
        url = str(event.get('URL') or '')
        if not url:
            match = re.search(r'https://(?:luma\.com|lu\.ma)/[A-Za-z0-9_-]+', description)
            url = match.group(0) if match else ''
        if not re.fullmatch(r'https://(?:luma\.com|lu\.ma)/[A-Za-z0-9_-]+/?', url):
            raise RequestFailure('Lumaの公式イベントURLを確認できません')
        location = str(event.get('LOCATION') or '')
        if location.startswith(('https://luma.com/', 'https://lu.ma/')):
            location = ''  # URLは会場の住所ではない。
        start, end = event.decoded('DTSTART', None), event.decoded('DTEND', None)
        all_day = isinstance(start, dt.date) and not isinstance(start, dt.datetime)
        def stamp(value):
            if isinstance(value, dt.datetime):
                if value.tzinfo is None:
                    raise RequestFailure('Lumaのタイムゾーンが未指定です。要確認')
                return value.astimezone(JST).isoformat()
            if isinstance(value, dt.date):
                return dt.datetime.combine(value, dt.time(), JST).isoformat()
            return None
        online = location.strip().lower() in ('オンライン', 'online', 'virtual')
        status = str(event.get('STATUS') or '').upper()
        raw = {'title': str(event.get('SUMMARY') or ''), 'url': url, 'description': description,
               'start_at': stamp(start), 'end_at': stamp(end), 'all_day': all_day,
               'venue': location, 'address': location, 'online': online, 'region': CITY_SCOPE}
        organizer = event.get('ORGANIZER')
        if organizer is not None and organizer.params.get('CN'):
            raw.update(organizer_name=str(organizer.params['CN']),organizer_evidence='Luma公式iCal ORGANIZER CN（配信上の名称。実績の確認とは別）')
        if status in ('CANCELLED', 'CONFIRMED'):
            raw['cancelled'] = status == 'CANCELLED'
            raw['cancel_evidence'] = 'Luma公式iCal STATUS=CANCELLED' if raw['cancelled'] else ''
        # TENTATIVEやSTATUS無しで、本人が確認した中止を解除しない。
        parsed.append((uid, normal_event(raw), 'cancelled' in raw))
    return parsed, skipped


def sync_techplay(store, fetch):
    parsed = parse_techplay(fetch.get(TECHPLAY_RSS, {'Accept': 'application/rss+xml, text/xml'}))
    tracked, count, seen = set(store.tracked_ids('TECH PLAY')), 0, set()
    for external_id, event in parsed:
        if external_id in tracked or relevant(event):
            event.pop('cancelled', None);event.pop('cancel_evidence', None);event.pop('deadline_at', None)
            store.upsert('TECH PLAY', external_id, event)
            count += 1;seen.add(external_id)
    missing = len(tracked - seen)
    detail = f'公式RSSの新着{len(parsed)}件から対象{count}件を取得。締切・中止は配信未提供'
    if missing:
        detail += f'。保存済み{missing}件は新着配信の範囲外。詳細は公式で要確認'
    return count, 'partial' if missing else 'ok', detail


def sync_luma(store, fetch):
    parsed, skipped = parse_luma(fetch.get(LUMA_TOKYO_ICS, {'Accept': 'text/calendar'}))
    tracked, count, seen = set(store.tracked_ids('Luma')), 0, set()
    for external_id, event, has_status in parsed:
        if external_id in tracked or relevant(event):
            event.pop('deadline_at', None)
            if not has_status:
                event.pop('cancelled', None);event.pop('cancel_evidence', None)
            store.upsert('Luma', external_id, event)
            count += 1;seen.add(external_id)
    missing = len(tracked - seen)
    detail = f'公式Tokyo都市別iCalの{len(parsed)}件から対象{count}件を取得。日時と明示された中止を追跡'
    if missing or skipped:
        detail += f'。配信にない保存済み{missing}件・繰返し/不明な形式{skipped}件は要確認。消失だけで中止にしません'
    return count, 'partial' if missing or skipped else 'ok', detail


def sync_connpass(store, fetch, key):
    headers = {'X-API-Key': key}
    count, capped, seen = 0, False, set()
    today = dt.datetime.now(JST)
    months = sorted({(today.date() + dt.timedelta(days=i)).strftime('%Y%m') for i in range(91)})
    for page in range(MAX_PAGES):
        query = [('keyword_or', k) for k in KEYWORDS] + [('prefecture', p) for p in PREFECTURES] + [('ym', m) for m in months]
        query += [('count', 100), ('start', page * 100 + 1), ('order', 2)]
        data = fetch.json(CONNPASS + '?' + urlencode(query), headers)
        if not isinstance(data, dict) or not isinstance(data.get('events'), list):
            raise RequestFailure('connpassの応答形式が変わりました')
        for raw in data['events']:
            event = connpass_event(raw)
            if relevant(event):
                store.upsert('connpass', str(raw['id']), event)
                seen.add(str(raw['id']))
                count += 1
        if page * 100 + len(data['events']) >= data.get('results_available', 0):
            break
        if page == MAX_PAGES - 1:
            capped = True
    # 保存済みは検索範囲から消えても追跡。最大100件ずつバッチ取得。
    tracked = [i for i in store.tracked_ids('connpass') if i not in seen]
    for offset in range(0, len(tracked), 100):
        ids = tracked[offset:offset + 100]
        query = [('event_id', i) for i in ids] + [('count', 100)]
        data = fetch.json(CONNPASS + '?' + urlencode(query), headers)
        returned = set()
        for raw in data['events']:
            store.upsert('connpass', str(raw['id']), connpass_event(raw))
            returned.add(str(raw['id']))
            count += 1
        if set(ids) - returned:
            capped = True  # 消えたイベントは保持し、要確認を表示。
    return count, 'partial' if capped else 'ok', ('検索上限または保存済みIDの未取得あり。中止にはしません' if capped else '公式API取得完了')


def sync_doorkeeper(store, fetch, key):
    headers = {'Authorization': 'Bearer ' + key}
    count, capped, seen = 0, False, set()
    # 同じ地域の全イベントから興味のある分野を決定的に選別。
    for prefecture in PREFECTURES[:-1]:
        for page in range(1, MAX_PAGES + 1):
            query = urlencode({'prefecture': prefecture, 'page': page, 'locale': 'ja', 'sort': 'starts_at',
                               'until': (dt.datetime.now(JST).date() + dt.timedelta(days=90)).isoformat()})
            data = fetch.json(DOORKEEPER + '/events?' + query, headers)
            if not isinstance(data, list):
                raise RequestFailure('Doorkeeperの応答形式が変わりました')
            for entry in data:
                raw = entry['event']
                event = doorkeeper_event(raw)
                if relevant(event) and str(raw['id']) not in seen:
                    store.upsert('doorkeeper', str(raw['id']), event)
                    seen.add(str(raw['id']))
                    count += 1
            if len(data) < 25:
                break
            if page == MAX_PAGES:
                capped = True
    tracked = [i for i in store.tracked_ids('doorkeeper') if i not in seen]
    # 古い登録も公平に再照合する。毎回同じ先頭IDで上限を使い切らない。
    with store.connect() as db:
        row = db.execute("SELECT value FROM metadata WHERE key='doorkeeper_cursor'").fetchone()
        cursor = int(row[0]) if row else 0
    if tracked:
        cursor %= len(tracked)
        rotated = tracked[cursor:] + tracked[:cursor]
        allowance = max(0, MAX_REQUESTS - fetch.calls)
        for external_id in rotated[:allowance]:
            try:
                data = fetch.json(DOORKEEPER + '/events/' + external_id, headers)
                store.upsert('doorkeeper', external_id, doorkeeper_event(data['event']))
                count += 1
            except RequestFailure as error:
                if not str(error).startswith('HTTP 404'):
                    raise
                capped = True
        with store.connect() as db:
            db.execute("INSERT INTO metadata VALUES('doorkeeper_cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (str(cursor + min(allowance, len(tracked))),))
        capped = capped or len(tracked) > allowance
    return count, 'partial' if capped else 'ok', ('検索または追跡に上限/未取得あり。保存済みを保持' if capped else '公式API取得完了。言語・中止・締切は未提供なら要確認')


def sync_holidays(store, fetch):
    text = fetch.get(HOLIDAYS, {'Accept': 'text/csv'}).decode('cp932')
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or rows[0][0] != '国民の祝日・休日月日':
        raise RequestFailure('祝日CSVの形式が変わりました')
    holidays = [(dt.datetime.strptime(r[0], '%Y/%m/%d').date().isoformat(), r[1]) for r in rows[1:] if len(r) == 2]
    if len(holidays) < 10:
        raise RequestFailure('祝日CSVが不完全です。前回データを保持')
    years = sorted({int(d[:4]) for d, _ in holidays})
    with store.connect() as db:
        db.execute('DELETE FROM holidays')
        db.executemany('INSERT INTO holidays VALUES(?,?)', holidays)
        for key, value in [('holiday_years', json.dumps(years)), ('holidays_checked_at', now())]:
            db.execute('INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
    return len(holidays), 'ok', '内閣府の公開祝日CSVを取得'


def daily(store):
    lock_path = store.path.parent / 'daily.lock'
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'busy', 'detail': '収集はすでに実行中'}
        results = []
        for source, key_name, sync in [('祝日', None, sync_holidays),
                                      ('TECH PLAY', None, sync_techplay),
                                      ('Luma', None, sync_luma),
                                      ('connpass', 'EVENTS_CONNPASS_API_KEY', sync_connpass),
                                      ('Doorkeeper', 'EVENTS_DOORKEEPER_API_TOKEN', sync_doorkeeper)]:
            started = now()
            key = credential(key_name) if key_name else None
            count = 0
            try:
                if key_name and not key:
                    status, detail = 'unconfigured', '未接続：公式APIの認証情報が必要です'
                else:
                    fetch = Fetcher()
                    count, status, detail = sync(store, fetch, key) if key_name else sync(store, fetch)
            except RequestFailure as error:
                status, detail = 'error', str(error)
            except (ValueError, KeyError, TypeError, UnicodeError):
                status, detail = 'error', '取得データの形式を確認できません。保存済みデータは保持'
            store.record_run(source, started, status, count, detail)
            results.append({'source': source, 'status': status, 'count': count, 'detail': detail})
        evidence_file = Path(__file__).with_name('organizer-evidence.json')
        try:
            if evidence_file.exists():store.apply_organizer_evidence(json.loads(evidence_file.read_text()))
        except (OSError,ValueError,KeyError,TypeError):
            store.record_run('主催者確認',now(),'error',0,'確認根拠ファイルを適用できません。未確認のまま保持します')
        backup = store.backup(store.path.parent / 'backups' / (dt.datetime.now(JST).strftime('%Y%m%d-%H%M%S-%f') + '.sqlite3'))
        for old in sorted(backup.parent.glob('*.sqlite3'))[:-14]:
            old.unlink()
        return {'status': 'done', 'results': results, 'backup': backup.name}
