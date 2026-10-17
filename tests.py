"""本人編集・同一性・失敗時の保存・勤務計算・接続保護の不変条件。外部通信しない。"""
import datetime as dt
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import collect
from core import Conflict, JST, Store, attendance, canonical_url, language_evidence, day_off
from server import Gate, node_user_matches
from wsgi import App
from waitress.server import create_server


def sample(**kw):
    return dict({'title':'生成AIアプリを作る会', 'url':'https://group.connpass.com/event/123/',
                 'start_at':'2026-10-16T19:30:00+09:00','end_at':'2026-10-16T21:00:00+09:00',
                 'description':'使用言語：日本語\n実装を持ち寄る会', 'venue':'秋葉原', 'address':'東京都千代田区'}, **kw)


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'events.sqlite3';self.store=Store(self.path)
        self.id=self.store.upsert('connpass','123',sample())

    def tearDown(self):
        self.tmp.cleanup()

    def test_collection_preserves_every_personal_field(self):
        state=self.store.update_state(self.id,{'version':0,'status':'申込済み','note':'持参する作品','travel_minutes':30,'language':'日本語対応あり','language_evidence':'公式が日本語字幕ありと明記'})
        self.store.upsert('connpass','123',sample(start_at='2026-10-16T20:00:00+09:00'))
        self.assertEqual(self.store.events()[0]['state'],state)
        self.assertEqual(len(self.store.get_history(self.id)),3)
        self.store.upsert('connpass','123',sample(start_at='2026-10-16T20:00:00+09:00'))
        self.assertEqual(len(self.store.get_history(self.id)),3)

    def test_stale_device_and_facts_reject_lost_update(self):
        self.store.update_state(self.id,{'version':0,'status':'気になる'})
        with self.assertRaises(Conflict):self.store.update_state(self.id,{'version':0,'note':'古い画面のメモ'})
        self.store.upsert('connpass','123',sample(title='生成AI 新タイトル'))
        with self.assertRaises(Conflict):self.store.edit_facts(self.id,{'version':1,'facts':{'title':'古い画面のタイトル'}})
        self.assertEqual(self.store.events()[0]['state']['note'],'')

    def test_url_and_source_identity_survive_schedule_change(self):
        same=self.store.upsert('manual','same',sample(url='https://connpass.com/event/123/?utm_source=test#top'))
        self.assertEqual(self.id,same)
        moved=self.store.upsert('connpass','123',sample(url='https://new.connpass.com/event/123/',start_at='2026-11-01T13:00:00+09:00',end_at='2026-11-01T16:00:00+09:00'))
        self.assertEqual(moved,self.id);self.assertEqual(len(self.store.events()),1)

    def test_ambiguous_cross_source_duplicates_keep_notes(self):
        other=self.store.upsert('doorkeeper','2',sample(url='https://example.doorkeeper.jp/events/2'))
        self.store.update_state(self.id,{'version':0,'note':'本人の記録'})
        events=self.store.events();self.assertEqual(len(events),2)
        event=next(e for e in events if e['id']==self.id)
        self.assertEqual(event['duplicates'],[other]);self.assertEqual(event['state']['note'],'本人の記録')

    def test_missing_cancel_field_does_not_uncancel(self):
        self.store.edit_facts(self.id,{'version':1,'facts':{'cancelled':True,'cancel_evidence':'公式に開催中止と記載','deadline_at':'2026-10-15T18:00:00+09:00'}})
        self.store.upsert('connpass','123',sample())
        event=self.store.events()[0];self.assertTrue(event['cancelled']);self.assertIsNotNone(event['deadline_at'])
        self.store.upsert('connpass','123',sample(cancelled=False))
        self.assertFalse(self.store.events()[0]['cancelled'])

    def test_backup_restores_state_and_complete_history(self):
        self.store.update_state(self.id,{'version':0,'note':'保存して復元するメモ','status':'参加確定'})
        backup=self.store.backup(Path(self.tmp.name)/'backup.sqlite3')
        restored=Store(backup)
        self.assertEqual(restored.events(),self.store.events());self.assertEqual(restored.export(),self.store.export())
        restarted=Store(self.path);self.assertEqual(restarted.export(),restored.export())

    def test_failed_backup_cannot_replace_previous_good_copy(self):
        dest=self.store.backup(Path(self.tmp.name)/'backup.sqlite3');before=dest.read_bytes()
        with patch('core.os.replace',side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):self.store.backup(dest)
        self.assertEqual(dest.read_bytes(),before)
        self.assertFalse(list(dest.parent.glob('.backup-*.partial')))
        with self.assertRaises(ValueError):self.store.backup(self.path)

    def test_html_is_text_and_bad_url_rejected(self):
        self.store.upsert('connpass','123',sample(description='<script>steal()</script><p>使用言語：日本語</p>'))
        self.assertNotIn('steal',self.store.events()[0]['description'])
        with self.assertRaises(ValueError):canonical_url('javascript:alert(1)')

    def test_explicit_language_only(self):
        self.assertEqual(language_evidence('日本語のイベント名、東京で開催')[0],'要確認')
        self.assertEqual(language_evidence('使用言語：日本語の場合は翻訳します')[0],'要確認')
        self.assertEqual(language_evidence('Language: English only\n')[0],'日本語対応なし')
        self.assertEqual(language_evidence('日本語字幕あり\n')[0],'日本語対応あり')

    def test_work_weekends_holidays_unknown_and_online(self):
        current=dt.datetime(2026,10,12,0,0,tzinfo=JST);meta={'holiday_years':'[2026]'}
        e=self.store.events()[0]
        self.assertEqual(attendance(e,{},meta,current)['code'],'possible')
        e['start_at']='2026-10-16T18:30:00+09:00'
        self.assertEqual(attendance(e,{},meta,current)['code'],'difficult')
        e['online']=True
        self.assertEqual(attendance(e,{},meta,current)['code'],'possible')
        e['online']=False;e['start_at']='2026-11-03T10:00:00+09:00';e['end_at']='2026-11-03T12:00:00+09:00'
        self.assertEqual(attendance(e,{'2026-11-03':'文化の日'},meta,current)['code'],'possible')
        self.assertEqual(attendance(e,{}, {},current)['code'],'check')
        e['start_at']='2026-10-17T13:00:00+09:00';e['end_at']='2026-10-17T17:00:00+09:00'
        self.assertEqual(attendance(e,{}, {},current)['code'],'possible')

    def test_work_conflict_is_separate_from_other_difficulty_and_recalculates(self):
        current=dt.datetime(2026,10,12,0,0,tzinfo=JST);meta={'holiday_years':'[2026]'}
        event=self.store.events()[0];event['start_at']='2026-10-16T18:30:00+09:00'
        event['effective_language']='日本語対応なし'
        result=attendance(event,{},meta,current)
        self.assertTrue(result['work_conflict']);self.assertEqual(result['label'],'日本語対応なし')
        event['start_at']='2026-10-16T20:00:00+09:00'
        result=attendance(event,{},meta,current)
        self.assertEqual(result['code'],'difficult');self.assertFalse(result['work_conflict'])
        event['start_at']='2026-10-16T18:30:00+09:00';event['state']['travel_minutes']=0
        self.assertFalse(attendance(event,{},meta,current)['work_conflict'])
        event['state']['travel_minutes']=None
        self.assertFalse(attendance(event,{'2026-10-16':'テスト祝日'},meta,current)['work_conflict'])
        self.assertFalse(attendance(event,{}, {},current)['work_conflict'])

    def test_day_off_uses_jst_holidays_and_multi_day_interval_without_guessing(self):
        meta={'holiday_years':'[2026]'}
        self.assertIs(day_off(sample(start_at='2026-10-17T13:00:00+09:00',end_at=None),{},meta)[0],True)
        self.assertIs(day_off(sample(start_at='2026-10-18T13:00:00+09:00',end_at=None),{},meta)[0],True)
        self.assertIs(day_off(sample(start_at='2026-11-03T13:00:00+09:00',end_at=None),{'2026-11-03':'文化の日'},meta)[0],True)
        self.assertIs(day_off(sample(start_at='2026-10-16T20:00:00+09:00',end_at=None),{},meta)[0],False)
        self.assertIs(day_off(sample(start_at='2026-10-16T18:00:00Z',end_at=None),{},meta)[0],True)
        self.assertIs(day_off(sample(start_at='2026-10-16T13:00:00+09:00',end_at='2026-10-18T17:00:00+09:00'),{},meta)[0],True)
        self.assertIs(day_off(sample(start_at='2026-10-16T13:00:00+09:00',end_at='2026-10-17T00:00:00+09:00'),{},meta)[0],False)
        self.assertIsNone(day_off(sample(start_at=None,end_at=None),{},meta)[0])
        self.assertIsNone(day_off(sample(start_at='2030-10-18T20:00:00+09:00',end_at=None),{},meta)[0])

    def test_failed_collection_keeps_events_and_reports(self):
        before=self.store.events()
        with patch.object(collect,'credential',return_value='TEST_ONLY'),patch.object(collect.Fetcher,'get',side_effect=collect.RequestFailure('通信失敗')):
            result=collect.daily(self.store)
        self.assertEqual(self.store.events(),before)
        self.assertTrue(all(r['status']=='error' for r in result['results']))
        self.assertTrue((self.path.parent/'backups'/result['backup']).is_file())

    def test_unconfigured_apis_never_make_request(self):
        with patch.object(collect,'credential',return_value=None),patch.object(collect,'sync_holidays',return_value=(100,'ok','テスト')),patch.object(collect,'sync_techplay',return_value=(1,'ok','テスト')),patch.object(collect,'sync_luma',return_value=(1,'ok','テスト')):
            result=collect.daily(self.store)
        self.assertEqual([r['status'] for r in result['results']],['ok','ok','ok','unconfigured','unconfigured'])

    def test_techplay_start_uses_event_date_not_publication_date(self):
        xml='''<rss xmlns:tp="https://rss.techplay.jp/" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><item><title>生成AI 開発会</title><link>https://techplay.jp/event/12345</link><pubDate>Mon, 12 Oct 2026 07:00:00 +0900</pubDate><description>日本語の説明だけ</description><dc:language>ja</dc:language><tp:eventStartTime>2026-11-01 13:00:00</tp:eventStartTime><tp:eventEndTime>2026-11-01 16:00:00</tp:eventEndTime><tp:eventPlace>オンライン</tp:eventPlace></item></channel></rss>'''
        parsed=collect.parse_techplay(xml.encode())[0][1]
        self.assertTrue(parsed['start_at'].startswith('2026-11-01T13:00'))
        self.assertEqual(parsed['language'],'要確認')
        self.assertTrue(parsed['online'])

    def test_luma_timezone_all_day_and_explicit_cancel(self):
        ics='''BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:stable-1\r\nSUMMARY:AI開発会\r\nDTSTART:20261101T040000Z\r\nDTEND:20261101T070000Z\r\nDESCRIPTION:Get up-to-date information at: https://luma.com/abcd1234\r\nLOCATION:Tokyo\r\nSTATUS:CANCELLED\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:stable-2\r\nSUMMARY:AI展示\r\nDTSTART;VALUE=DATE:20261103\r\nDTEND;VALUE=DATE:20261104\r\nDESCRIPTION:Official event https://luma.com/efgh5678\r\nLOCATION:Tokyo\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'''
        parsed,skipped=collect.parse_luma(ics.encode());self.assertEqual(skipped,0)
        self.assertTrue(parsed[0][1]['start_at'].startswith('2026-11-01T13:00'))
        self.assertTrue(parsed[0][1]['cancelled']);self.assertTrue(parsed[0][2])
        self.assertTrue(parsed[1][1]['all_day']);self.assertFalse(parsed[1][2])
        event_id=self.store.upsert('Luma',parsed[1][0],parsed[1][1])
        event=next(e for e in self.store.events() if e['id']==event_id)
        self.assertEqual(event['attendance']['label'],'開催時刻要確認')

    def test_feed_schedule_change_retains_owner_and_missing_is_not_cancel(self):
        raw=sample(url='https://luma.com/stableEvent',region='東京・近郊（公式都市配信）')
        event_id=self.store.upsert('Luma','stable-uid',raw)
        personal=self.store.update_state(event_id,{'version':0,'status':'参加確定','note':'持参作品'})
        changed=dict(raw,start_at='2026-10-16T20:00:00+09:00')
        with patch.object(collect,'parse_luma',return_value=([('stable-uid',changed,False)],0)):
            fetch=collect.Fetcher(interval=0)
            with patch.object(fetch,'get',return_value=b'fixture'):collect.sync_luma(self.store,fetch)
        event=next(e for e in self.store.events() if e['id']==event_id)
        self.assertEqual(event['state'],personal)
        with patch.object(collect,'parse_luma',return_value=([],0)):
            with patch.object(fetch,'get',return_value=b'fixture'):
                count,status,detail=collect.sync_luma(self.store,fetch)
        self.assertEqual(status,'partial');self.assertEqual(count,0)
        event=next(e for e in self.store.events() if e['id']==event_id)
        self.assertFalse(event['cancelled']);self.assertEqual(event['state'],personal)

    def test_connpass_v2_mapping_and_explicit_cancel(self):
        event=collect.connpass_event({'id':123,'title':'生成AI','url':sample()['url'],'open_status':'cancelled','started_at':sample()['start_at']})
        self.assertTrue(event['cancelled']);self.assertEqual(event['start_at'],sample()['start_at'])
        self.store.upsert('connpass','123',event)
        self.assertEqual(self.store.events()[0]['attendance']['code'],'cancelled')


class SecurityTest(unittest.TestCase):
    def test_tailscale_identity_fails_closed(self):
        valid={'Node':{'User':42,'MachineAuthorized':True},'UserProfile':{'ID':42}}
        self.assertTrue(node_user_matches(valid,42));self.assertFalse(node_user_matches(valid,99));self.assertFalse(node_user_matches({},42))
        valid['Node']['MachineAuthorized']=False;self.assertFalse(node_user_matches(valid,42))
        gate=Gate();gate.owner_id=42
        with patch('server.tailscale_json',side_effect=RuntimeError):
            self.assertFalse(gate.allowed('192.168.1.5'))
        with patch('server.tailscale_json',side_effect=OSError):self.assertFalse(gate.allowed('100.64.0.1'))
        self.assertFalse(gate.allowed('127.0.0.1',False));self.assertTrue(gate.allowed('127.0.0.1',True))

    def test_real_http_origin_host_and_unauthorized_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'db.sqlite3');gate=Gate()
            app=App(store,gate,1)
            server=create_server(app,host='127.0.0.1',port=0,threads=2,asyncore_loop_timeout=.05)
            port=server.effective_port;app.hosts={f'127.0.0.1:{port}'}
            thread=threading.Thread(target=server.run);thread.start()
            base='http://127.0.0.1:'+str(port)
            def send(path, payload=None, headers=None):
                return urllib.request.urlopen(urllib.request.Request(base+path,data=json.dumps(payload).encode() if payload is not None else None,headers=headers or {}),timeout=5)
            try:
                with send('/health') as response:self.assertEqual(response.status,200)
                for headers in [{'Content-Type':'application/json'},{'Content-Type':'application/json','Origin':'https://evil.test'}]:
                    with self.assertRaises(urllib.error.HTTPError) as error:send('/api/events',sample(),headers)
                    self.assertEqual(error.exception.code,403)
                with self.assertRaises(urllib.error.HTTPError) as error:send('/health',headers={'Host':'evil.test'})
                self.assertEqual(error.exception.code,403)
                with send('/api/events',sample(),{'Content-Type':'application/json','Origin':base}) as response:
                    event_id=json.load(response)['id']
                with send('/api/events/'+event_id+'/state',{'version':0,'note':'実HTTPで保存'},{'Content-Type':'application/json','Origin':base}) as response:self.assertEqual(response.status,200)
                with patch.object(gate,'allowed',return_value=False):
                    with self.assertRaises(urllib.error.HTTPError) as error:send('/api/events',headers={'X-Forwarded-For':'127.0.0.1'})
                    self.assertEqual(error.exception.code,403)
                self.assertEqual(store.events()[0]['state']['note'],'実HTTPで保存')
            finally:
                server.task_dispatcher.shutdown()
                for channel in list(server._map.values()):channel.close()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())


if __name__=='__main__':unittest.main(verbosity=2)
