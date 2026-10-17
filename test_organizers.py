"""主催者の素性が不明な候補を通常一覧と分け、本人判断を保護する。外部通信なし。"""
import json
import tempfile
import unittest
from pathlib import Path
from core import Store,Conflict,plain
from tests import sample
from collect import parse_techplay,parse_luma


class OrganizerTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'db.sqlite3';self.store=Store(self.path)
        self.id=self.store.upsert('test','1',sample(organizer_name='同名でも実績は未確認'))

    def tearDown(self):self.tmp.cleanup()

    def review(self,**kw):
        return dict(decision='allowed',name='確認した主催者',url='https://example.com/about',evidence='公式の活動と開催履歴を確認',version=0,**kw)

    def test_published_name_is_not_approval_and_no_name_inheritance(self):
        self.assertEqual(self.store.events()[0]['organizer_review']['effective_decision'],'pending')
        self.store.review_organizer(self.id,self.review())
        other=self.store.upsert('test','2',sample(url='https://example.com/other',organizer_name='同名でも実績は未確認'))
        event=next(e for e in self.store.events() if e['id']==other)
        self.assertEqual(event['organizer_review']['effective_decision'],'pending')

    def test_approval_requires_real_link_name_and_evidence(self):
        for change in [{'name':''},{'url':''},{'evidence':''},{'url':'javascript:alert(1)'}]:
            payload=self.review();payload.update(change)
            with self.assertRaises(ValueError):self.store.review_organizer(self.id,payload)
        self.assertEqual(self.store.events()[0]['organizer_review']['version'],0)

    def test_identity_change_requires_review_but_keeps_state_and_original_judgment(self):
        state=self.store.update_state(self.id,{'version':0,'status':'申込済み','note':'本人のメモ'})
        review=self.store.review_organizer(self.id,self.review())
        self.store.upsert('test','1',sample(organizer_name='別の掲載者'))
        event=self.store.events()[0]
        self.assertEqual(event['state'],state)
        self.assertTrue(event['organizer_review']['identity_changed'])
        self.assertEqual(event['organizer_review']['decision'],'allowed')
        self.assertEqual(event['organizer_review']['effective_decision'],'pending')
        self.assertEqual(event['organizer_review']['version'],review['version'])

    def test_old_device_and_reviewer_cannot_replace_owner_judgment(self):
        self.store.review_organizer(self.id,dict(decision='hidden',version=0))
        with self.assertRaises(Conflict):self.store.review_organizer(self.id,self.review())
        self.store.review_organizer(self.id,self.review(),actor='reviewer')
        self.assertEqual(self.store.events()[0]['organizer_review']['decision'],'hidden')

    def test_evidence_replay_restore_and_collector_preserve_personal_choice(self):
        entry=dict(event_url=sample()['url'],name='確認済み',url='https://example.com',evidence='公式活動実績')
        self.assertEqual(self.store.apply_organizer_evidence([entry]),1)
        self.store.review_organizer(self.id,dict(decision='hidden',version=1,evidence='本人は表示不要'))
        self.assertEqual(self.store.apply_organizer_evidence([entry]),0)
        self.store.upsert('test','1',sample())
        self.assertEqual(self.store.events()[0]['organizer_review']['effective_decision'],'hidden')
        backup=self.store.backup(Path(self.tmp.name)/'backup.sqlite3')
        self.assertEqual(Store(backup).export(),self.store.export())
        self.assertEqual(Store(self.path).events(),self.store.events())
        self.assertTrue(any(h['kind']=='主催者の確認' for h in self.store.get_history(self.id)))

    def test_official_feed_names_are_preserved_without_approval(self):
        rss=b'''<rss xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><item><title>AI maker</title><link>https://techplay.jp/event/123</link><dc:creator>Example community</dc:creator></item></channel></rss>'''
        _,facts=parse_techplay(rss)[0]
        event_id=self.store.upsert('TECH PLAY','123',facts)
        event=next(e for e in self.store.events() if e['id']==event_id)
        self.assertEqual(event['organizer_name'],'Example community')
        self.assertEqual(event['organizer_review']['effective_decision'],'pending')
        ics=b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:test-organizer\r\nSUMMARY:AI maker\r\nURL:https://luma.com/fixture\r\nDTSTART:20300101T100000Z\r\nORGANIZER;CN=Calendar host:MAILTO:calendar-invite@lu.ma\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'
        parsed,_=parse_luma(ics)
        self.assertEqual(parsed[0][1]['organizer_name'],'Calendar host')
        self.assertNotIn('organizer_url',parsed[0][1])

    def test_empty_calendar_title_does_not_drop_other_events(self):
        ics=b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:empty\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:valid\r\nSUMMARY:AI maker\r\nURL:https://luma.com/valid\r\nDTSTART:20300101T100000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'
        parsed,skipped=parse_luma(ics)
        self.assertEqual(skipped,1);self.assertEqual([e[0] for e in parsed],['valid'])

    def test_plain_title_ending_in_unescaped_ampersand_is_not_lost(self):
        self.assertEqual(plain('TREX PURPOSE RUN with DIM&DRIP'),'TREX PURPOSE RUN with DIM&DRIP')


if __name__=='__main__':unittest.main()
