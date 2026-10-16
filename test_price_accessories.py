import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from price_server.accessories import (ACCESSORIES_CATEGORIES, ACCESSORIES_KEY,
    ACCESSORIES_LEGACY_KEYS, ACCESSORIES_TITLE, unified_accessories_catalog)
from price_server.calendar_plan import CALENDAR_PLAN_ENTRIES
from price_server.post_formatting import format_price_sections, PRICE_NOTICE_HTML
from price_server.quick_links import QUICK_LINK_POST_SPECS
from price_server.repository import PriceRepository
from price_server.service import PricePublicationService
from price_server.telegram_api import TelegramContentTooLong, telegram_text_units, telegram_visible_text
from test_price_server import FakeTelegram, snapshot_with_sections


class AccessoriesTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.repo = PriceRepository(Path(self.folder.name) / 'price.db')
        self.now = datetime.now(timezone.utc)
        self.repo.ingest_snapshot(snapshot_with_sections(self.now, list(ACCESSORIES_LEGACY_KEYS)))
        self.fake = FakeTelegram()
        self.settings = SimpleNamespace(telegram_channel_id='-1001463992448',
            telegram_channel_username='testchannel')
        self.service = PricePublicationService(self.settings, self.repo, self.fake)

    def test_catalog_combines_all_categories_without_mutating_source_or_other_groups(self):
        source = [{'group_id':'phones','sections':[{'section_key':'phones-test'}]},
                  {'group_id':'accessories','title':'Old','sections':[
                      {'section_key':key,'position':i,'category_names':[f'category-{i}'],
                       'enabled':True,'title':key,'catalog_label':key}
                      for i,key in enumerate(ACCESSORIES_LEGACY_KEYS)]}]
        original = copy.deepcopy(source)
        combined = unified_accessories_catalog(source)
        self.assertEqual(source, original)
        self.assertEqual(combined[0], source[0])
        self.assertEqual(len(combined[1]['sections']), 1)
        item = combined[1]['sections'][0]
        self.assertEqual(item['section_key'], ACCESSORIES_KEY)
        self.assertEqual(item['title'], ACCESSORIES_TITLE)
        self.assertTrue(set(ACCESSORIES_CATEGORIES).issubset(item['category_names']))
        self.assertTrue({f'category-{i}' for i in range(4)}.issubset(item['category_names']))
        self.assertEqual(unified_accessories_catalog(combined), combined)

    def test_all_legacy_jobs_resolve_whole_accessory_set(self):
        for key in (*ACCESSORIES_LEGACY_KEYS,'accessories-combined'):
            with self.subTest(key=key):
                section = self.service._section_for_job({'section_key':key,'snapshot_policy':'latest'})
                self.assertEqual(section['section_key'], ACCESSORIES_KEY)
                chunks = self.service._section_blocks(section)
                self.assertEqual(len(chunks), 1)
                for original in ACCESSORIES_LEGACY_KEYS:
                    self.assertIn(original, chunks[0])
                self.assertEqual(chunks[0].count(PRICE_NOTICE_HTML), 1)

    def test_latest_and_pinned_jobs_never_mix_snapshot_versions(self):
        old = self.repo.get_current_snapshot(include_payload=False)['snapshot_id']
        newer = snapshot_with_sections(self.now+timedelta(minutes=1), [ACCESSORIES_KEY])
        newer['sections'][0]['title'] = ACCESSORIES_TITLE
        newer['sections'][0]['telegram_blocks'] = ['Fresh combined model: 123']
        from price_server.contracts import section_content_hash, canonical_content_hash
        newer['sections'][0]['content_hash'] = section_content_hash(newer['sections'][0])
        newer['content_sha256'] = canonical_content_hash(newer)
        self.repo.ingest_snapshot(newer)
        latest = self.service._section_for_job({'section_key':'accessories-pencil','snapshot_policy':'latest'})
        pinned = self.service._section_for_job({'section_key':'accessories-pencil','snapshot_policy':'pinned','snapshot_id':old})
        self.assertIn('Fresh combined model', ''.join(self.service._section_blocks(latest)))
        self.assertNotIn('Fresh combined model', ''.join(self.service._section_blocks(pinned)))
        self.assertEqual(latest['title'], ACCESSORIES_TITLE)

    def test_latest_accessory_reads_pin_all_parts_to_one_snapshot(self):
        snapshot_id = self.repo.get_current_snapshot(include_payload=False)['snapshot_id']
        self.repo.get_current_snapshot = Mock(wraps=self.repo.get_current_snapshot)
        self.repo.get_section = Mock(wraps=self.repo.get_section)
        self.service._section_for_job({'section_key':'accessories-mouse','snapshot_policy':'latest'})
        self.repo.get_current_snapshot.assert_called_once_with(include_payload=False)
        self.assertTrue(all(call.kwargs['snapshot_id'] == snapshot_id
                            for call in self.repo.get_section.call_args_list))

    def test_legacy_send_creates_one_current_publication(self):
        result = self.service.execute_job({'section_key':'accessories-mouse','action':'send'})
        self.assertEqual(len(result['message_ids']), 1)
        self.assertEqual(len(self.fake.sent), 1)
        posts = self.repo.list_telegram_posts(current_only=True)
        self.assertEqual([p['section_key'] for p in posts], [ACCESSORIES_KEY])
        self.assertEqual(posts[0]['section_name'], ACCESSORIES_TITLE)

    def test_edit_reuses_original_airtag_id_and_keeps_every_section(self):
        self.repo.upsert_telegram_post({'section_key':ACCESSORIES_KEY,'channel_id':self.settings.telegram_channel_id,
            'message_id':5251,'part_no':1,'is_current':True,'section_name':'AirTag'})
        self.service.execute_job({'section_key':'accessories-pencil','action':'edit_current'})
        self.assertEqual([m[1] for m in self.fake.edited], [5251])
        self.assertFalse(self.fake.sent)
        self.assertTrue(all(k in self.fake.edited[0][2] for k in ACCESSORIES_LEGACY_KEYS))

    def test_single_message_uses_full_telegram_capacity(self):
        blocks = ['Model: 100\n' + 'x'*3500, 'y'*250]
        chunks = format_price_sections([(ACCESSORIES_KEY,ACCESSORIES_TITLE,blocks)], single_message=True)
        self.assertEqual(len(chunks), 1)
        self.assertGreater(telegram_text_units(chunks[0]), 3600)
        self.assertLessEqual(telegram_text_units(chunks[0]), 4096)

    def test_oversized_accessories_fail_before_any_telegram_send_or_edit(self):
        section = {'section_key':ACCESSORIES_KEY,'title':ACCESSORIES_TITLE,
                   'telegram_blocks':['Model: 100\n'+'🙂'*2200]}
        self.service._section_for_job = Mock(return_value=section)
        with self.assertRaises(TelegramContentTooLong):
            self.service.execute_job({'section_key':ACCESSORIES_KEY,'action':'send'})
        self.assertFalse(self.fake.sent)
        self.assertFalse(self.fake.edited)

    def test_calendar_and_quick_link_have_one_accessory_identity(self):
        entries = [e for e in CALENDAR_PLAN_ENTRIES if e.day==30 and e.slot==3]
        self.assertEqual([e.section_key for e in entries], [ACCESSORIES_KEY])
        main = next(s for s in QUICK_LINK_POST_SPECS if s['quick_post_key']=='quick-index-catalog')
        target = next(t for t in main['targets'] if t['link_key']=='accessories-combined')
        self.assertEqual(target['section_keys'], [ACCESSORIES_KEY])
        self.assertEqual(target['fallback_url'], 'https://t.me/texnikach/5251')
        keys = self.service._bulk_section_keys({'payload':{'section_keys':list(ACCESSORIES_LEGACY_KEYS)}})
        self.assertEqual(keys, [ACCESSORIES_KEY])
