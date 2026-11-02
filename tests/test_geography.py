import json
import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from telethon.tl.functions.channels import GetFullChannelRequest

import test_communities as communities_tests
from test_search import FakeClient, channel
from tgsearch.backend import Backend
from tgsearch.communities import Community, export_communities_txt
from tgsearch.engine import SearchEngine, SearchOptions
from tgsearch.geography import russia_basis


class RussiaTextTests(unittest.TestCase):
    def test_explicit_country_city_region_and_legal_jurisdiction(self):
        for title, about in [('Юристы России', ''), ('Юристы', 'ГК РФ'),
                             ('Юристы Москвы', ''), ('Адвокаты', 'Работаем в Казани'),
                             ('Юристы', 'Санкт-Петербург'), ('Юристы', 'Татарстан'),
                             ('Lawyers', 'Russia'), ('Юристы 🇷🇺', '')]:
            with self.subTest(title=title, about=about):
                self.assertTrue(russia_basis(title, about))

    def test_unknown_language_usernames_mixed_or_foreign_scope_stay_out(self):
        for title, about in [('Юристы', 'Русскоязычное сообщество'),
                             ('Юрист', 'Для россиян в Германии'), ('Юристы России', 'Казахстан'),
                             ('Юристы Москвы', 'Киев'), ('Юристы', 'Вне России'),
                             ('Юристы', 'Не работаем в РФ'), ('Юристы', '@moscow_law https://russia.example'),
                             ('Юристы', 'Про Сибирского кота')]:
            with self.subTest(title=title, about=about):
                self.assertEqual(russia_basis(title, about), '')

    def test_evidence_identifies_which_public_field_matched(self):
        self.assertEqual(russia_basis('Юристы', 'Работаем по РФ'), 'Описание: «РФ»')
        self.assertEqual(russia_basis('Юристы Москвы'), 'Название: «Москвы»')


class RussiaSearchTests(unittest.IsolatedAsyncioTestCase):
    setUp = communities_tests.CommunitySearchTests.setUp
    options = communities_tests.CommunitySearchTests.options
    run_search = communities_tests.CommunitySearchTests.run_search

    def prepare_chats(self):
        self.client.chats = [channel(100, 'law_rf', 'Юристы России'),
                             channel(200, 'law_kz', 'Юристы Казахстана'),
                             channel(300, 'law_unknown', 'Юристы: общий чат')]
        original = self.client.__class__
        class ScopedClient(original):
            async def __call__(self, request):
                result = await super().__call__(request)
                if isinstance(request, GetFullChannelRequest):
                    result.full_chat.about = 'Открытое сообщество. Русский язык.'
                return result
        self.client.__class__ = ScopedClient

    async def test_rf_mode_filters_unknown_and_foreign_without_reading_people(self):
        self.prepare_chats()
        result = await self.run_search(self.options(russia_only=True))
        self.assertEqual(set(result.communities), {100})
        row = result.communities[100]
        self.assertTrue(row.russia_only)
        self.assertIn('России', row.russia_basis)
        self.assertEqual(result.history.total(), 0)
        self.assertIn('Признак РФ', result.path.read_text(encoding='utf-8-sig'))
        self.assertEqual(result.limiter.counts['descriptions'], 3)
        report = json.loads((self.private / 'reports' / (result.path.stem + '.json')).read_text())
        self.assertTrue(report['options']['russia_only'])
        self.assertTrue(report['communities'][0]['russia_basis'])

    async def test_filter_off_preserves_original_search_and_avoids_description_calls(self):
        self.prepare_chats()
        result = await self.run_search()
        self.assertEqual(set(result.communities), {100, 200, 300})
        self.assertEqual(self.client.calls, ['SearchRequest'])
        self.assertFalse(result.communities[100].russia_only)

    async def test_region_in_description_does_not_relax_title_topic_match(self):
        self.client.chats = [channel(100, 'law_group', 'Юристы'), channel(200, 'other_group', 'Разговоры')]
        result = await self.run_search(self.options(russia_only=True))
        self.assertEqual(set(result.communities), {100})
        self.assertIn('Описание', result.communities[100].russia_basis)

    async def test_cached_country_metadata_and_forced_refresh(self):
        self.prepare_chats()
        await self.run_search(self.options(russia_only=True))
        self.client.calls.clear()
        await self.run_search(self.options(russia_only=True))
        self.assertNotIn('GetFullChannelRequest', self.client.calls)
        await self.run_search(self.options(russia_only=True, force_refresh=True))
        self.assertEqual(self.client.calls.count('GetFullChannelRequest'), 3)

    async def test_rf_setting_survives_budget_stop_and_resume(self):
        self.prepare_chats()
        first = await self.run_search(self.options(russia_only=True, max_operations=1))
        self.assertFalse(first.complete)
        checkpoint = first.history.unfinished()
        self.assertTrue(checkpoint['options']['russia_only'])
        # Resume keeps the original scope even if current UI options differ.
        resumed = await self.run_search(self.options(russia_only=False, max_operations=5), checkpoint)
        self.assertTrue(resumed.options.russia_only)
        self.assertTrue(resumed.complete)
        self.assertEqual(set(resumed.communities), {100})

    async def test_rf_filter_also_gates_contact_collection(self):
        client = FakeClient()
        client.chat.title = 'Строители Казахстана'
        options = SearchOptions('строители', {'owner', 'admin'}, russia_only=True)
        result = await SearchEngine(client, lambda *args: None, request_delay=0).run(options)
        self.assertFalse(result.contacts)
        self.assertNotIn('messages', client.calls)
        self.assertEqual(result.history.total(), 0)


class RussiaExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_rf_export_rechecks_description_and_excludes_changed_scope(self):
        row = Community(100, 'law_group', 'Юристы', 'Группа', 'https://t.me/law_group', '',
                        russia_only=True, russia_basis='Описание: «РФ»')
        async def full(request):
            return NS(full_chat=NS(about='Юристы Казахстана'))
        backend = Backend.__new__(Backend)
        backend.events = queue.Queue()
        class ExportClient:
            get_entity = AsyncMock(return_value=channel(100, 'law_group', 'Юристы'))
            __call__ = staticmethod(full)
        backend.client = ExportClient()
        backend.connect = AsyncMock(return_value=backend.client)
        with tempfile.TemporaryDirectory() as folder, patch('tgsearch.backend.PRIVATE', Path(folder)):
            rows = await backend.verify_communities([row], cache_hours=0)
            self.assertEqual(rows, [])
            self.assertEqual(row.verification, 'incomplete')
            self.assertEqual(row.russia_basis, '')
            path = Path(folder) / 'out.txt'
            export_communities_txt(path, [row])
            self.assertEqual(path.read_text(encoding='utf-8-sig'), '')
