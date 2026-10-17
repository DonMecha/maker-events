"""個人の状態と公式情報を分けて永続保存する。

正本: SQLite。停止: server/dailyの実行を止める。停止でデータは削除しない。
LLMや作品の採点は使わず、日時と明記された根拠だけで計算する。
"""
import contextlib
import datetime as dt
import hashlib
import html
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

JST = ZoneInfo('Asia/Tokyo')
STATUSES = ['気になる', '申込済み', '結果待ち', '参加確定', '参加済み', '見送り']
LANGUAGES = ['要確認', '日本語', '日本語対応あり', '日本語対応なし']
ORGANIZER_DECISIONS = ('pending', 'allowed', 'hidden')
FIELDS = ('title', 'url', 'start_at', 'end_at', 'deadline_at', 'venue', 'address',
          'description', 'categories', 'region', 'online', 'cancelled',
          'cancel_evidence', 'language', 'language_evidence', 'all_day')
REGIONS = ['東京都', '神奈川県', '千葉県', '埼玉県']
CITY_SCOPE = '東京・近郊（公式都市配信）'
TOPICS = {'AI開発': r'(?<![A-Za-z0-9])AI(?![A-Za-z0-9])|生成AI|人工知能|LLM|機械学習|ディープラーニング|エージェント|Vibe Cod|Claude|ChatGPT',
          'AI創作': r'AI.{0,8}(創作|画像|映像|音楽)|生成.{0,5}(画像|映像|音楽)',
          '電子工作': r'電子工作|ESP32|Arduino|Raspberry Pi|ロボット|IoT|メイカー|Maker',
          '作品展示': r'展示|展覧|展覧会|上映|Maker Faire|作品発表',
          '制作者交流': r'交流|もくもく|ハッカソン|ミートアップ|meetup|hackathon|LT会'}


class Conflict(ValueError):
    pass


def now():
    return dt.datetime.now(JST).isoformat(timespec='seconds')


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip += 1
        elif tag in ('p', 'br', 'div', 'li', 'h1', 'h2', 'h3', 'tr'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip - 1)
        elif tag in ('p', 'div', 'li', 'h1', 'h2', 'h3', 'tr'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def plain(value, limit=30000):
    parser = PlainText()
    parser.feed(str(value or '')[:200000])
    parser.close()  # 末尾の「&DRIP」等を未完のHTML参照としてバッファに残さない。
    return re.sub(r'\n[ \t]*\n+', '\n', html.unescape(''.join(parser.parts))).strip()[:limit]


def canonical_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('公式URLを確認してください')
    p = urlsplit(value.strip())
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('公式URLはhttp/httpsの通常URLを指定してください')
    if p.port not in (None, 80, 443):
        raise ValueError('公式URLのポートを確認してください')
    host = p.hostname.lower()
    if host in ('lu.ma', 'luma.com') and re.fullmatch(r'/[A-Za-z0-9_-]+/?', p.path):
        host = 'luma.com'
    m = re.fullmatch(r'/event/(\d+)/?', p.path)
    if (host == 'connpass.com' or host.endswith('.connpass.com')) and m:
        return f'https://connpass.com/event/{m[1]}/'
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')]
    return urlunsplit((p.scheme.lower(), host, p.path or '/', urlencode(sorted(query)), ''))


def timestamp(value):
    if not value:
        return None
    if not isinstance(value, str):
        raise ValueError('日時の形式を確認してください')
    try:
        date = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('日時の形式を確認してください') from None
    if date.tzinfo is None:
        date = date.replace(tzinfo=JST)
    return date.astimezone(JST).isoformat(timespec='seconds')


def language_evidence(text):
    # 日本語の文章というだけでは判定しない。曖昧・否定・条件文も確定しない。
    ja = re.search(r'(?:使用言語|開催言語|講演言語|発表言語|言語|Language)\s*[:：]\s*(?:日本語|Japanese)(?:\s*[・/,、]\s*(?:英語|English))?(?=$|[。\n\r])|(?:本イベント|本勉強会|本セッション)(?:は|の講演は)日本語で(?:開催|実施|進行)します', text, re.I)
    en = re.search(r'(?:使用言語|開催言語|講演言語|発表言語|言語|Language)\s*[:：]\s*(?:英語のみ|English only)(?=$|[。\n\r])', text, re.I)
    support = re.search(r'日本語(?:通訳|字幕)(?:あり|付き|を提供します)(?=$|[。\n\r])', text)
    if en and (ja or support):
        return '要確認', '言語の記載が混在。公式で確認してください'
    if support:
        return '日本語対応あり', support.group(0)
    if ja:
        return '日本語', ja.group(0)
    if en:
        return '日本語対応なし', en.group(0)
    return '要確認', ''


def normal_event(raw):
    for key in ('online', 'cancelled', 'all_day'):
        if key in raw and type(raw[key]) is not bool:
            raise ValueError('オンライン・中止の指定を確認してください')
    title = plain(raw.get('title'), 300)
    if not title:
        raise ValueError('イベント名を入力してください')
    url = canonical_url(raw.get('url', ''))
    desc = plain(raw.get('description'))
    lang, evidence = language_evidence(desc)
    if raw.get('language') in LANGUAGES and raw.get('language_evidence'):
        lang, evidence = raw['language'], plain(raw['language_evidence'], 1000)
    text = title + '\n' + desc
    address, venue = plain(raw.get('address'), 500), plain(raw.get('venue'), 500)
    online = bool(raw.get('online'))
    region = next((r for r in REGIONS if r in address), '地域要確認')
    if raw.get('region') in REGIONS + [CITY_SCOPE]:
        region = raw['region']
    if online:
        region = 'オンライン'
    event = {'title': title, 'url': url, 'description': desc, 'venue': venue,
             'address': address, 'start_at': timestamp(raw.get('start_at')),
             'end_at': timestamp(raw.get('end_at')), 'deadline_at': timestamp(raw.get('deadline_at')),
             'categories': [name for name, pattern in TOPICS.items() if re.search(pattern, text, re.I)],
             'region': region, 'online': online, 'cancelled': bool(raw.get('cancelled')),
             'cancel_evidence': plain(raw.get('cancel_evidence'), 500),
             'language': lang, 'language_evidence': evidence, 'all_day': bool(raw.get('all_day'))}
    if event['cancelled'] and not event['cancel_evidence']:
        raise ValueError('中止には公式の根拠が必要です')
    for key, limit in [('organizer_name',300),('organizer_evidence',1000)]:
        if raw.get(key):event[key] = plain(raw[key],limit)
    if raw.get('organizer_url'):event['organizer_url'] = canonical_url(raw['organizer_url'])
    if event['start_at'] and event['end_at'] and event['end_at'] < event['start_at']:
        raise ValueError('終了日時が開始日時より前です')
    return event


SCHEMA = '''
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, canonical_url TEXT NOT NULL UNIQUE, facts TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1, first_seen TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
 source TEXT NOT NULL, external_id TEXT NOT NULL, event_id TEXT NOT NULL REFERENCES events(id),
 snapshot TEXT NOT NULL, last_seen TEXT NOT NULL, PRIMARY KEY(source, external_id)
);
CREATE TABLE IF NOT EXISTS user_state (
 event_id TEXT PRIMARY KEY REFERENCES events(id), status TEXT,
 note TEXT NOT NULL DEFAULT '', travel_minutes INTEGER, language TEXT NOT NULL DEFAULT '要確認',
 language_evidence TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 0, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS history (
 id INTEGER PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id), actor TEXT NOT NULL,
 kind TEXT NOT NULL, before_json TEXT, after_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS history_event ON history(event_id, id);
CREATE INDEX IF NOT EXISTS events_start ON events(json_extract(facts, '$.start_at'));
CREATE TABLE IF NOT EXISTS collection_runs (
 id INTEGER PRIMARY KEY, source TEXT NOT NULL, started_at TEXT NOT NULL,
 finished_at TEXT NOT NULL, status TEXT NOT NULL, count INTEGER NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holidays (date TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS organizer_reviews (
 event_id TEXT PRIMARY KEY REFERENCES events(id), decision TEXT NOT NULL,
 name TEXT NOT NULL, url TEXT NOT NULL, evidence TEXT NOT NULL, identity TEXT NOT NULL,
 actor TEXT NOT NULL, version INTEGER NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_visibility (
 event_id TEXT PRIMARY KEY REFERENCES events(id), hidden INTEGER NOT NULL CHECK(hidden IN (0,1)),
 version INTEGER NOT NULL, updated_at TEXT NOT NULL
);
PRAGMA user_version = 1;
'''


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] > 1:
                raise RuntimeError('このプログラムより新しい版のDBです。旧版で書き換えず停止します')
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(SCHEMA)
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def history(db, event_id, actor, kind, before, after):
        db.execute('INSERT INTO history(event_id,actor,kind,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)',
                   (event_id, actor, kind, dumps(before) if before is not None else None, dumps(after), now()))

    def upsert(self, source, external_id, raw, actor='collector', expected_version=None, create_only=False):
        facts = normal_event(raw)
        canonical = facts['url']
        stamp = now()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old_source = db.execute('SELECT event_id FROM sources WHERE source=? AND external_id=?',
                                    (source, str(external_id))).fetchone()
            row = db.execute('SELECT * FROM events WHERE ' + ('id=?' if old_source else 'canonical_url=?'),
                             (old_source['event_id'] if old_source else canonical,)).fetchone()
            if expected_version is not None and (not row or row['version'] != expected_version):
                raise Conflict('別の更新があります。再読込みして確認してください')
            if row:
                event_id = row['id']
                before = json.loads(row['facts'])
                if create_only:
                    facts = before
                # この取得元が提供しない締切の補足を消さない。
                if actor == 'collector' and 'deadline_at' not in raw and before.get('deadline_at'):
                    facts['deadline_at'] = before['deadline_at']
                if actor == 'collector' and 'cancelled' not in raw:
                    facts['cancelled'] = before['cancelled']
                    facts['cancel_evidence'] = before['cancel_evidence']
                for key in ('organizer_name','organizer_url','organizer_evidence'):
                    if key not in raw and key in before:facts[key] = before[key]
                if facts != before:
                    collision = db.execute('SELECT id FROM events WHERE canonical_url=? AND id<>?', (canonical, event_id)).fetchone()
                    if collision:
                        raise Conflict('URLが別の保存済みイベントと競合しています。要確認')
                    db.execute('UPDATE events SET canonical_url=?,facts=?,version=version+1,updated_at=? WHERE id=?',
                               (canonical, dumps(facts), stamp, event_id))
                    kind = '中止' if facts['cancelled'] and not before['cancelled'] else '公式情報変更'
                    self.history(db, event_id, actor, kind, before, facts)
            else:
                event_id = hashlib.sha256(canonical.encode()).hexdigest()[:24]
                db.execute('INSERT INTO events(id,canonical_url,facts,first_seen,updated_at) VALUES(?,?,?,?,?)',
                           (event_id, canonical, dumps(facts), stamp, stamp))
                self.history(db, event_id, actor, '登録', None, facts)
            db.execute('INSERT INTO sources(source,external_id,event_id,snapshot,last_seen) VALUES(?,?,?,?,?) '
                       'ON CONFLICT(source,external_id) DO UPDATE SET snapshot=excluded.snapshot,last_seen=excluded.last_seen',
                       (source, str(external_id), event_id, dumps(facts), stamp))
        return event_id

    def edit_facts(self, event_id, payload):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
            if not row:
                raise KeyError(event_id)
            if type(payload.get('version')) is not int or payload['version'] != row['version']:
                raise Conflict('公式情報に新しい更新があります。再読込みしてください')
            raw = payload.get('facts')
            if not isinstance(raw, dict):
                raise ValueError('イベント情報を確認してください')
            before = json.loads(row['facts'])
            facts = normal_event(dict(before, **raw))
            other = db.execute('SELECT id FROM events WHERE canonical_url=? AND id<>?', (facts['url'], event_id)).fetchone()
            if other:
                raise Conflict('このURLのイベントはすでに保存されています')
            if facts != before:
                db.execute('UPDATE events SET canonical_url=?,facts=?,version=version+1,updated_at=? WHERE id=?',
                           (facts['url'], dumps(facts), now(), event_id))
                self.history(db, event_id, 'owner', '公式情報の手動更新', before, facts)

    def export(self):
        with self.connect() as db:
            return {table: [dict(r) for r in db.execute('SELECT * FROM ' + table)]
                    for table in ('events', 'sources', 'user_state', 'history', 'collection_runs', 'holidays', 'metadata', 'organizer_reviews', 'event_visibility')}

    @staticmethod
    def empty_visibility():
        return {'hidden':False,'version':0,'updated_at':None}

    def update_visibility(self,event_id,payload):
        if set(payload)!={'hidden','version'} or type(payload.get('hidden')) is not bool:
            raise ValueError('興味なしの指定を確認してください')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM events WHERE id=?',(event_id,)).fetchone():raise KeyError(event_id)
            row=db.execute('SELECT * FROM event_visibility WHERE event_id=?',(event_id,)).fetchone()
            before=dict(row) if row else self.empty_visibility()
            before['hidden']=bool(before['hidden'])
            if type(payload.get('version')) is not int or payload['version']!=before['version']:
                raise Conflict('表示の指定が別端末で更新されています。再読込みしてください')
            if payload['hidden']==before['hidden']:return before
            after=dict(hidden=payload['hidden'],version=before['version']+1,updated_at=now())
            db.execute('INSERT INTO event_visibility VALUES(?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET '
                       'hidden=excluded.hidden,version=excluded.version,updated_at=excluded.updated_at',
                       (event_id,int(after['hidden']),after['version'],after['updated_at']))
            self.history(db,event_id,'owner','興味なしの表示設定',before,after)
            return after

    @staticmethod
    def organizer_identity(facts):
        return dumps([facts.get('organizer_name',''),facts.get('organizer_url','')])

    @staticmethod
    def empty_organizer_review():
        return {'decision':'pending','name':'','url':'','evidence':'','identity':'','actor':'','version':0,'updated_at':None}

    def review_organizer(self,event_id,payload,actor='owner'):
        allowed = {'decision','name','url','evidence','version'}
        if set(payload)-allowed:raise ValueError('主催者確認の項目を確認してください')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            event = db.execute('SELECT facts FROM events WHERE id=?',(event_id,)).fetchone()
            if not event:raise KeyError(event_id)
            row = db.execute('SELECT * FROM organizer_reviews WHERE event_id=?',(event_id,)).fetchone()
            before = dict(row) if row else self.empty_organizer_review()
            # わたしの確認作業も、本人の指定を上書きしない。
            if actor != 'owner' and before['actor']=='owner':return before
            if type(payload.get('version')) is not int or payload['version']!=before['version']:
                raise Conflict('主催者の確認が別端末で更新されています。再読込みしてください')
            if payload.get('decision') not in ORGANIZER_DECISIONS:raise ValueError('主催者の表示判断を確認してください')
            if any(not isinstance(payload.get(k,''),str) or len(payload.get(k,''))>n for k,n in [('name',300),('url',2048),('evidence',1000)]):
                raise ValueError('主催者名・確認先・根拠の入力を確認してください')
            name,evidence = plain(payload.get('name'),300),plain(payload.get('evidence'),1000)
            url = canonical_url(payload['url']) if payload.get('url') else ''
            if payload['decision']=='allowed' and not(name and url and evidence):
                raise ValueError('確認済みにするには主催者名・確認先URL・確認した根拠が必要です')
            after = dict(event_id=event_id,decision=payload['decision'],name=name,url=url,evidence=evidence,
                         identity=self.organizer_identity(json.loads(event['facts'])),actor=actor,version=before['version']+1,updated_at=now())
            if all(after[k]==before.get(k) for k in ('decision','name','url','evidence','identity','actor')):return before
            db.execute('INSERT INTO organizer_reviews VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET '
                       'decision=excluded.decision,name=excluded.name,url=excluded.url,evidence=excluded.evidence,identity=excluded.identity,'
                       'actor=excluded.actor,version=excluded.version,updated_at=excluded.updated_at',tuple(after[k] for k in ('event_id','decision','name','url','evidence','identity','actor','version','updated_at')))
            self.history(db,event_id,actor,'主催者の確認',before,after)
            return after

    def apply_organizer_evidence(self,entries):
        count = 0
        for entry in entries:
            url = canonical_url(entry['event_url'])
            with self.connect() as db:
                row = db.execute('SELECT id FROM events WHERE canonical_url=?',(url,)).fetchone()
                if not row or db.execute('SELECT 1 FROM organizer_reviews WHERE event_id=?',(row['id'],)).fetchone():continue
            try:
                self.review_organizer(row['id'],dict(decision='allowed',name=entry['name'],url=entry['url'],evidence=entry['evidence'],version=0),actor='reviewer')
                count += 1
            except Conflict:pass  # 先に本人が確認した場合は、その指定を維持する。
        return count

    def update_state(self, event_id, payload):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM events WHERE id=?', (event_id,)).fetchone():
                raise KeyError(event_id)
            row = db.execute('SELECT * FROM user_state WHERE event_id=?', (event_id,)).fetchone()
            before = dict(row) if row else self.empty_state(event_id)
            if type(payload.get('version')) is not int or payload['version'] != before['version']:
                raise Conflict('別端末で更新されています。再読込みして確認してください')
            after = dict(before)
            allowed = {'status', 'note', 'travel_minutes', 'language', 'language_evidence', 'version'}
            if set(payload) - allowed:
                raise ValueError('本人状態以外の項目は変更できません')
            for key in allowed - {'version'}:
                if key in payload:
                    after[key] = payload[key]
            if after['status'] is not None and after['status'] not in STATUSES:
                raise ValueError('参加状態を確認してください')
            if not isinstance(after['note'], str) or len(after['note']) > 10000:
                raise ValueError('メモは10000文字以内です')
            travel = after['travel_minutes']
            if travel is not None and (type(travel) is not int or not 0 <= travel <= 360):
                raise ValueError('移動時間は0〜360分です')
            if after['language'] not in LANGUAGES or not isinstance(after['language_evidence'], str):
                raise ValueError('言語の補足を確認してください')
            if len(after['language_evidence']) > 1000 or (after['language'] != '要確認' and not after['language_evidence'].strip()):
                raise ValueError('言語を確定する場合は確認した根拠を入力してください')
            if all(after[k] == before[k] for k in allowed - {'version'}):
                return before
            after['version'], after['updated_at'] = before['version'] + 1, now()
            columns = ('event_id', 'status', 'note', 'travel_minutes', 'language', 'language_evidence', 'version', 'updated_at')
            db.execute('INSERT INTO user_state VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET '
                       'status=excluded.status,note=excluded.note,travel_minutes=excluded.travel_minutes,'
                       'language=excluded.language,language_evidence=excluded.language_evidence,'
                       'version=excluded.version,updated_at=excluded.updated_at', tuple(after[c] for c in columns))
            self.history(db, event_id, 'owner', '本人の記録', before, after)
            return after

    @staticmethod
    def empty_state(event_id):
        return {'event_id': event_id, 'status': None, 'note': '', 'travel_minutes': None,
                'language': '要確認', 'language_evidence': '', 'version': 0, 'updated_at': None}

    def events(self):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM events ORDER BY updated_at DESC').fetchall()
            states = {r['event_id']: dict(r) for r in db.execute('SELECT * FROM user_state')}
            holidays = {r['date']: r['name'] for r in db.execute('SELECT * FROM holidays')}
            meta = {r['key']: r['value'] for r in db.execute('SELECT * FROM metadata')}
            reviews = {r['event_id']:dict(r) for r in db.execute('SELECT * FROM organizer_reviews')}
            visibility = {r['event_id']:dict(r,hidden=bool(r['hidden'])) for r in db.execute('SELECT * FROM event_visibility')}
            sources = [dict(r) for r in db.execute('SELECT source,external_id,event_id,last_seen FROM sources')]
        events = []
        for row in rows:
            facts = json.loads(row['facts'])
            state = states.get(row['id'], self.empty_state(row['id']))
            lang = state['language'] if state['language'] != '要確認' else facts['language']
            evidence = state['language_evidence'] if state['language'] != '要確認' else facts['language_evidence']
            item = dict(facts, id=row['id'], version=row['version'], first_seen=row['first_seen'],
                        updated_at=row['updated_at'], state=state, effective_language=lang,
                        effective_language_evidence=evidence, sources=[s for s in sources if s['event_id'] == row['id']])
            item['attendance'] = attendance(item, holidays, meta)
            item['day_off'],item['day_off_reason'] = day_off(item,holidays,meta)
            review = reviews.get(row['id'],self.empty_organizer_review()).copy()
            review['identity_changed'] = review['decision']=='allowed' and review['identity']!=self.organizer_identity(facts)
            review['effective_decision'] = 'pending' if review['identity_changed'] else review['decision']
            item['organizer_review'] = review
            item['visibility'] = visibility.get(row['id'],self.empty_visibility())
            item['duplicates'] = []
            events.append(item)
        groups = {}
        for event in events:
            key = (unicodedata.normalize('NFKC', event['title']).casefold().strip(), event['start_at'])
            if event['start_at']:
                groups.setdefault(key, []).append(event['id'])
        for event in events:
            key = (unicodedata.normalize('NFKC', event['title']).casefold().strip(), event['start_at'])
            event['duplicates'] = [x for x in groups.get(key, []) if x != event['id']]
        return sorted(events, key=lambda e: (e['start_at'] is None, e['start_at'] or '', e['title']))

    def get_history(self, event_id):
        with self.connect() as db:
            return [dict(r, before=json.loads(r['before_json']) if r['before_json'] else None,
                         after=json.loads(r['after_json'])) for r in db.execute(
                             'SELECT * FROM history WHERE event_id=? ORDER BY id DESC', (event_id,))]

    def tracked_ids(self, source):
        with self.connect() as db:
            return [r[0] for r in db.execute('SELECT external_id FROM sources WHERE source=?', (source,))]

    def record_run(self, source, started, status, count, detail):
        with self.connect() as db:
            db.execute('INSERT INTO collection_runs(source,started_at,finished_at,status,count,detail) VALUES(?,?,?,?,?,?)',
                       (source, started, now(), status, count, detail))

    def overview(self):
        with self.connect() as db:
            runs = [dict(r) for r in db.execute('SELECT * FROM collection_runs WHERE id IN '
                                              '(SELECT MAX(id) FROM collection_runs GROUP BY source) ORDER BY source')]
            metadata = {r['key']: r['value'] for r in db.execute('SELECT * FROM metadata')}
        return {'runs': runs, 'metadata': metadata}

    def backup(self, destination):
        dest = Path(destination)
        if dest.resolve() == self.path.resolve():
            raise ValueError('バックアップ先を元のDBとは別にしてください')
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle, name = tempfile.mkstemp(prefix='.backup-', suffix='.partial', dir=dest.parent)
        os.close(handle)
        stage = Path(name)
        try:
            with self.connect() as db, contextlib.closing(sqlite3.connect(stage)) as target:
                db.backup(target)
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('バックアップの整合性を確認できません')
            stage.chmod(0o600)
            os.replace(stage, dest)
        finally:
            stage.unlink(missing_ok=True)
        return dest


def day_off(event,holidays,meta):
    if not event.get('start_at'):return None,'開催日要確認'
    start=dt.datetime.fromisoformat(event['start_at']).astimezone(JST)
    end=dt.datetime.fromisoformat(event['end_at']).astimezone(JST) if event.get('end_at') else start
    # 日付をまたぐ展示も対象。終了が午前0時なら、その日を開催日に含めない。
    if end>start and end.time()==dt.time():end-=dt.timedelta(microseconds=1)
    first,last=start.date(),max(start.date(),end.date())
    until_weekend=0 if first.weekday()>=5 else 5-first.weekday()
    if (last-first).days>=until_weekend:return True,'開催日に土曜・日曜を含みます'
    holiday=next((name for day,name in holidays.items() if first.isoformat()<=day<=last.isoformat()),None)
    if holiday:return True,holiday
    years=json.loads(meta.get('holiday_years','[]'))
    if any(year not in years for year in range(first.year,last.year+1)):return None,'この年の祝日データは未確認です'
    return False,'平日開催'


def attendance(event, holidays, meta, current=None):
    current = current or dt.datetime.now(JST)
    if event['cancelled']:
        return {'code': 'cancelled', 'label': '開催中止', 'reason': event['cancel_evidence']}
    start = dt.datetime.fromisoformat(event['start_at']) if event['start_at'] else None
    end = dt.datetime.fromisoformat(event['end_at']) if event['end_at'] else None
    if (end or start) and (end or start) < current:
        return {'code': 'past', 'label': '終了・開始済み', 'reason': '本人の参加状態は自動変更しません'}
    if not start:
        return {'code': 'check', 'label': '日時要確認', 'reason': '公式の開始日時がありません'}
    if event.get('all_day'):
        return {'code': 'check', 'label': '開催時刻要確認', 'reason': 'カレンダー配信は終日扱いです。実際の開始・終了時刻を公式で確認してください'}
    if start < current and (not end or end.date() == start.date()):
        return {'code': 'check', 'label': '開催中・途中参加要確認', 'reason': '開始時刻を過ぎています。途中参加できるか公式で確認してください'}
    if end and end.date() != start.date():
        return {'code': 'check', 'label': '日程要確認', 'reason': '複数日イベント。各日の開場時間と途中参加を公式で確認してください'}
    day = start.date().isoformat()
    holiday = holidays.get(day)
    years = json.loads(meta.get('holiday_years', '[]'))
    free = start.weekday() >= 5 or bool(holiday)
    travel = event['state']['travel_minutes']
    estimated = travel is None and not event['online']
    travel = 0 if event['online'] else (45 if travel is None else travel)
    earliest = start.replace(hour=18, minute=0, second=0) + dt.timedelta(minutes=travel + 15)
    if free:
        code, label, reason = 'possible', '時間上は参加できそう', (holiday or '土日') + 'は休み'
    elif start.year not in years:
        code, label, reason = 'check', '祝日・時間要確認', 'この年の祝日データが未取得です'
    elif start < earliest:
        code, label = 'difficult', '勤務時間から参加は難しそう'
        reason = f'18時終業＋移動{travel}分＋余裕15分。到着目安{earliest:%H:%M}'
    else:
        code, label = 'possible', '時間上は参加できそう'
        reason = f'18時終業＋移動{travel}分＋余裕15分。到着目安{earliest:%H:%M}'
    work_conflict = code == 'difficult'
    if estimated:
        reason += '／移動45分は仮設定。経路は要確認'
    if not event['online'] and event['region'] not in REGIONS + [CITY_SCOPE]:
        code, label = 'check', '会場要確認'
        reason += '／都内・近郊か確認してください'
    language = event.get('effective_language', event['language'])
    if language == '日本語対応なし':
        code, label = 'difficult', '日本語対応なし'
        reason += '／公式に日本語対応がないと記載されています'
    if language in ('要確認', '日本語対応なし') and code == 'possible':
        code, label = 'check', '時間は合う・言語要確認'
        reason += '／日本語で参加できるか公式で確認してください'
    deadline = dt.datetime.fromisoformat(event['deadline_at']) if event['deadline_at'] else None
    if deadline and deadline < current:
        reason += '／記載の申込締切を過ぎています（申込済みかは本人の状態を参照）'
    return {'code': code, 'label': label, 'reason': reason, 'work_conflict': work_conflict}
