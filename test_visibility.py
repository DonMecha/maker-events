"""興味なしは本人の指定。取得事実・参加状態・主催者確認から独立させる。"""
import tempfile
import unittest
from pathlib import Path
from core import Store,Conflict
from tests import sample


class VisibilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'events.sqlite3';self.store=Store(self.path)
        self.id=self.store.upsert('test','1',sample())

    def tearDown(self):self.tmp.cleanup()

    def test_collection_restart_and_restore_preserve_hidden_without_changing_other_owner_fields(self):
        state=self.store.update_state(self.id,dict(version=0,status='参加確定',note='残すメモ'))
        review=self.store.review_organizer(self.id,dict(version=0,decision='allowed',name='主催者',url='https://example.com/about',evidence='公式実績を確認'))
        self.store.update_visibility(self.id,dict(hidden=True,version=0))
        self.store.upsert('test','1',sample(title='生成AI 変更後'))
        event=Store(self.path).events()[0]
        self.assertTrue(event['visibility']['hidden']);self.assertEqual(event['state'],state)
        self.assertEqual(event['organizer_review']['version'],review['version'])
        self.assertEqual(event['organizer_review']['decision'],'allowed')
        restored=Store(self.store.backup(Path(self.tmp.name)/'backup.sqlite3'))
        self.assertEqual(restored.export(),self.store.export())
        self.assertTrue(restored.events()[0]['visibility']['hidden'])
        self.assertTrue(any(h['kind']=='興味なしの表示設定' for h in restored.get_history(self.id)))

    def test_restore_does_not_approve_organizer_and_stale_device_cannot_undo_newer_choice(self):
        hidden=self.store.update_visibility(self.id,dict(hidden=True,version=0))
        with self.assertRaises(Conflict):self.store.update_visibility(self.id,dict(hidden=False,version=0))
        self.assertTrue(self.store.events()[0]['visibility']['hidden'])
        self.store.update_visibility(self.id,dict(hidden=False,version=hidden['version']))
        self.assertFalse(self.store.events()[0]['visibility']['hidden'])
        self.assertEqual(self.store.events()[0]['organizer_review']['effective_decision'],'pending')

    def test_declining_and_no_interest_are_independent_and_export_separate_histories(self):
        self.store.update_state(self.id,dict(version=0,status='見送り',note='日程が合わない'))
        event=self.store.events()[0]
        self.assertFalse(event['visibility']['hidden']);self.assertEqual(event['state']['status'],'見送り')
        self.store.update_visibility(self.id,dict(hidden=True,version=0))
        self.store.update_state(self.id,dict(version=1,status='気になる'))
        event=self.store.events()[0]
        self.assertTrue(event['visibility']['hidden']);self.assertEqual(event['state']['status'],'気になる')
        self.assertEqual(event['state']['note'],'日程が合わない')
        history=self.store.get_history(self.id)
        self.assertEqual(sum(h['kind']=='興味なしの表示設定' for h in history),1)
        self.assertEqual(sum(h['kind']=='本人の記録' for h in history),2)
        self.assertTrue(all(h['created_at'] for h in history))
        restored=Store(self.store.backup(Path(self.tmp.name)/'decisions.sqlite3'))
        self.assertEqual(restored.export(),self.store.export())

    def test_only_explicit_boolean_and_visibility_fields_are_accepted(self):
        for payload in [dict(hidden=1,version=0),dict(hidden='false',version=0),dict(hidden=True,version=False),dict(hidden=True,version=0,note='bad')]:
            with self.assertRaises((ValueError,Conflict)):self.store.update_visibility(self.id,payload)
        self.assertFalse(self.store.events()[0]['visibility']['hidden'])
        before=len(self.store.get_history(self.id))
        self.store.update_visibility(self.id,dict(hidden=False,version=0))
        self.assertEqual(len(self.store.get_history(self.id)),before)


if __name__=='__main__':unittest.main()
