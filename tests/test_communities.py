import asyncio
import csv
import json
import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from telethon import errors, types
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.contacts import SearchRequest

from app import Application
from test_search import channel
from tgsearch.backend import Backend
from tgsearch.communities import COMMUNITY_SEARCH_LABEL, Community, discovery_variants, export_communities_csv, export_communities_txt
from tgsearch.control import Cooldown, SearchPaused
from tgsearch.engine import Contact, SearchEngine, SearchOptions, recover_latest_report
from tgsearch.history import ContactHistory, timestamp


class CommunityClient:
    def __init__(self):
        self.chats = [channel(100, 'law_channel', 'Юристы Москвы', True),
                      channel(200, 'law_group', 'Форум юристов', False)]
        self.calls = []
        self.results = {}
        self.members = 5000

    async def __call__(self, request):
        self.calls.append(type(request).__name__)
        if isinstance(request, SearchRequest):
            return NS(chats=self.results.get(request.q, self.chats))
        if isinstance(request, GetFullChannelRequest):
            return NS(full_chat=NS(about='Юристы Москвы, реклама @UnwantedUser', participants_count=self.members, pinned_msg_id=99))
        raise AssertionError(type(request))

    async def get_entity(self, key):
        self.calls.append('get_entity')
        for chat in self.chats:
            if (isinstance(key, str) and chat.username == key) or (isinstance(key, types.PeerChannel) and chat.id == key.channel_id):
                return chat
        raise ValueError('unknown entity')

    async def get_messages(self, *args, **kwargs):
        raise AssertionError('Community mode must not read posts')

    async def get_participants(self, *args, **kwargs):
        raise AssertionError('Community mode must not collect administrators')


class CommunitySearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.private, self.results = self.root / '.local', self.root / 'results'
        self.private.mkdir(); self.results.mkdir()
        for item in (patch('tgsearch.engine.PRIVATE', self.private), patch('tgsearch.engine.RESULTS', self.results), patch('tgsearch.engine.prepare_storage')):
            item.start(); self.addCleanup(item.stop)
        self.client = CommunityClient()
        self.events = []

    def options(self, **kwargs):
        return SearchOptions('юрист', set(), communities_only=True, **kwargs)

    async def run_search(self, options=None, resume=None):
        return await SearchEngine(self.client, lambda *args: self.events.append(args), request_delay=0).run(options or self.options(), resume=resume)

    async def test_only_title_matches_qualify_without_reading_contacts_or_posts(self):
        self.client.chats += [channel(300, 'yurist_username', 'Ремонт квартир'),
                              channel(400, 'description_only', 'Разговоры обо всём'),
                              types.User(999, username='yurist_profile')]
        result = await self.run_search()
        self.assertEqual(set(result.communities), {100, 200})
        self.assertFalse(result.contacts)
        self.assertEqual(result.history.total(), 0)
        self.assertEqual(self.client.calls, ['SearchRequest'])
        self.assertTrue(result.complete)
        self.assertIn('Юристы Москвы', result.path.read_text(encoding='utf-8-sig'))
        self.assertIn('https://t.me/law_channel', result.path.read_text(encoding='utf-8-sig'))
        self.assertNotIn('UnwantedUser', result.path.read_text())

    def test_discovery_spellings_are_bounded_and_support_full_names(self):
        self.assertEqual(discovery_variants('юрист'), ['yurist', 'urist', 'jurist'])
        self.assertIn('forumyuristov', discovery_variants('ФОРУМ юристов'))
        self.assertLessEqual(len(discovery_variants('ФОРУМ юристов')), 6)
        self.assertEqual(discovery_variants('law'), [])

    async def test_people_only_discovery_tries_spellings_but_retains_original_title_filter(self):
        self.client.results = {'юрист': [], 'yurist': [self.client.chats[0],
                               channel(900, 'yurist_username', 'Ремонт квартир')]}
        result = await self.run_search()
        self.assertEqual(set(result.communities), {100, 200})
        self.assertEqual(result.discovery_queries, ['юрист', 'yurist', 'urist', 'jurist'])
        self.assertEqual(self.client.calls, ['SearchRequest'] * 4)
        self.assertEqual(result.history.total(), 0)
        self.assertTrue(result.complete)

    async def test_discovery_spellings_resume_after_budget_without_repeating_original(self):
        self.client.results = {'юрист': []}
        first = await self.run_search(self.options(max_operations=1))
        self.assertFalse(first.complete)
        snapshot = first.history.unfinished()
        self.assertEqual(snapshot['discovery_queries'], ['юрист', 'yurist', 'urist', 'jurist'])
        self.client.calls.clear()
        resumed = await self.run_search(self.options(max_operations=4), snapshot)
        self.assertEqual(resumed.limiter.total, 4)
        self.assertEqual(self.client.calls, ['SearchRequest'] * 3)
        self.assertEqual(set(resumed.communities), {100, 200})
        self.assertTrue(resumed.complete)

    async def test_group_and_channel_filters_remain_independent(self):
        for kind, expected in [('Каналы', {100}), ('Группы', {200})]:
            with self.subTest(kind=kind):
                result = await self.run_search(self.options(chat_type=kind))
                self.assertEqual(set(result.communities), expected)

    async def test_city_is_matched_only_in_title_in_this_mode(self):
        result = await self.run_search(self.options(city='Москва'))
        self.assertEqual(set(result.communities), {100})
        self.assertNotIn('GetFullChannelRequest', self.client.calls)

    async def test_audience_filter_uses_cache_without_scanning_publications(self):
        first = await self.run_search(self.options(min_members=1000))
        self.assertEqual(len(first.communities), 2)
        self.assertEqual(self.client.calls.count('GetFullChannelRequest'), 2)
        self.client.calls.clear()
        await self.run_search(self.options(min_members=1000))
        self.assertNotIn('GetFullChannelRequest', self.client.calls)
        self.client.calls.clear(); self.client.members = 50
        last = await self.run_search(self.options(min_members=1000, force_refresh=True))
        self.assertFalse(last.communities)
        self.assertEqual(last.skipped, 2)
        self.assertEqual(self.client.calls.count('GetFullChannelRequest'), 2)

    async def test_community_ids_never_enter_personal_history_even_when_ids_collide(self):
        history = ContactHistory(self.private / 'contacts.sqlite3')
        history.remember(Contact('existing_person', 100))
        original = history.get(100)
        self.client.chats += [self.client.chats[0]]
        first = await self.run_search()
        self.assertEqual(set(first.communities), {100, 200})
        second = await self.run_search()
        self.assertEqual(set(second.communities), {100, 200})
        reopened = ContactHistory(self.private / 'contacts.sqlite3', self.private / 'reports')
        self.assertEqual(reopened.total(), 1)
        self.assertEqual(reopened.get(100), original)
        report = json.loads((self.private / 'reports' / (second.path.stem + '.json')).read_text())
        self.assertEqual(report['contacts'], [])
        self.assertEqual(len(report['communities']), 2)

    async def test_stop_and_resume_preserve_found_communities(self):
        original = self.client.get_entity
        entered = asyncio.Event()
        async def pause(key):
            if key == 'law_group':
                entered.set(); await asyncio.Future()
            return await original(key)
        self.client.get_entity = pause
        options = self.options(sources='law_channel,law_group')
        options.keywords = ''
        engine = SearchEngine(self.client, lambda *args: None, request_delay=0)
        task = asyncio.create_task(engine.run(options))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        stopped = await task
        self.assertTrue(stopped.cancelled)
        self.assertEqual(set(stopped.communities), {100})
        self.client.get_entity = original
        resumed = await self.run_search(options, stopped.history.unfinished())
        self.assertEqual(set(resumed.communities), {100, 200})
        self.assertTrue(resumed.complete)
        self.assertEqual(stopped.path.resolve(), resumed.path.resolve())

    async def test_operation_budget_stops_discovery_and_retains_checkpoint(self):
        self.client.results = {'юрист': [self.client.chats[0]], 'адвокат': [channel(300, 'advokat_group', 'Адвокаты')]}
        options = self.options(max_operations=1); options.keywords = 'юрист, адвокат'
        result = await self.run_search(options)
        self.assertEqual(result.limiter.total, 1)
        self.assertEqual(set(result.communities), {100})
        self.assertFalse(result.complete)
        self.assertIsNotNone(result.history.unfinished())

    async def test_flood_stops_community_mode_and_persists_deadline(self):
        class FloodClient(CommunityClient):
            async def __call__(self, request):
                raise errors.FloodWaitError(None, capture=30)
        self.client = FloodClient()
        result = await self.run_search()
        self.assertFalse(result.complete)
        self.assertGreater(Cooldown(self.private).remaining(), 0)
        with self.assertRaises(SearchPaused):
            await self.run_search(resume=result.history.unfinished())

    async def test_nonmatching_usernames_do_not_consume_the_community_limit(self):
        self.client.chats.insert(0, channel(900, 'yurist_only_username', 'Общий чат'))
        result = await self.run_search(self.options(max_chats=1))
        self.assertEqual(set(result.communities), {100})
        self.assertEqual(len(result.seen), 1)

    async def test_completed_report_is_rebuilt_from_community_checkpoint(self):
        result = await self.run_search()
        original = result.path.read_bytes()
        result.path.unlink()
        (self.private / 'reports' / (result.path.stem + '.json')).unlink()
        recover_latest_report(result.history)
        self.assertEqual(result.path.read_bytes(), original)
        report = json.loads((self.private / 'reports' / (result.path.stem + '.json')).read_text())
        self.assertTrue(report['options']['communities_only'])
        self.assertEqual(report['stats']['communities'], 2)
        self.assertFalse(report['contacts'])

    def test_community_exports_keep_titles_links_types_and_safe_csv_fields(self):
        row = Community(1, 'law_channel', '=Юристы', 'Канал', 'https://t.me/law_channel', timestamp(), 1000)
        txt, csv_path = self.results / 'out.txt', self.results / 'out.csv'
        export_communities_txt(txt, [row, row])
        export_communities_csv(csv_path, [row, row])
        self.assertEqual(txt.read_text(encoding='utf-8-sig').count('https://t.me/law_channel'), 1)
        with csv_path.open(encoding='utf-8-sig') as source:
            rows = list(csv.reader(source, delimiter=';'))
        self.assertEqual(rows[1][0], "'=Юристы")
        self.assertEqual(rows[1][4], '1')
        self.assertEqual(len(rows), 2)
        self.assertEqual(txt.stat().st_mode & 0o777, 0o600)


class CommunityExportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.private = Path(self.tmp.name)
        self.client = NS(get_entity=AsyncMock(return_value=channel(100, 'renamed_law', 'Юристы: новое имя', True)))
        self.backend = Backend.__new__(Backend)
        self.backend.client = self.client
        self.backend.events = queue.Queue()
        self.backend.connect = AsyncMock(return_value=self.client)
        self.row = Community(100, 'old_law', 'Юристы', 'Канал', 'https://t.me/old_law', '')
        item = patch('tgsearch.backend.PRIVATE', self.private); item.start(); self.addCleanup(item.stop)

    async def test_stale_community_is_revalidated_by_channel_id(self):
        result = await self.backend.verify_communities([self.row])
        key = self.client.get_entity.call_args.args[0]
        self.assertIsInstance(key, types.PeerChannel)
        self.assertEqual(key.channel_id, 100)
        self.assertEqual(result[0].username, 'renamed_law')
        self.assertEqual(result[0].title, 'Юристы: новое имя')
        self.assertEqual(result[0].link, 'https://t.me/renamed_law')
        self.assertFalse((self.private / 'contacts.sqlite3').exists())

    async def test_fresh_community_needs_no_extra_rpc(self):
        self.row.checked_at = timestamp()
        result = await self.backend.verify_communities([self.row])
        self.assertEqual(result, [self.row])
        self.client.get_entity.assert_not_awaited()

    async def test_person_or_wrong_channel_id_is_never_exported_as_a_community(self):
        for entity in (types.User(100, username='person'), channel(200, 'different', 'Юристы')):
            self.client.get_entity.return_value = entity
            self.assertFalse(await self.backend.verify_communities([self.row]))
            self.assertEqual(self.row.verification, 'incomplete')

    async def test_flood_during_repeat_export_preserves_cooldown(self):
        self.client.get_entity.side_effect = errors.FloodWaitError(None, capture=30)
        with self.assertRaises(SearchPaused):
            await self.backend.verify_communities([self.row])
        self.assertEqual(self.row.verification, 'incomplete')
        self.assertGreater(Cooldown(self.private).remaining(), 0)


class CommunityUiTests(unittest.TestCase):
    def test_community_mode_disables_contact_only_controls(self):
        widgets = [Mock(), Mock()]
        view = NS(roles=NS(get=lambda: COMMUNITY_SEARCH_LABEL), mode_hint=Mock(), running=False, contact_only_widgets=widgets, chat_limit_input=Mock())
        Application.update_search_mode(view)
        for widget in widgets:
            widget.configure.assert_called_once_with(state='disabled')
        self.assertIn('только в названии', view.mode_hint.set.call_args.args[0])

    def test_thousand_community_limit_is_accepted_without_raising_api_budget(self):
        options = SearchOptions('юрист', set(), communities_only=True, max_chats=1000, max_operations=300)
        options.validate()
        self.assertEqual(options.max_operations, 300)
        with self.assertRaisesRegex(ValueError, '10000'):
            SearchOptions('юрист', set(), communities_only=True, max_chats=10001).validate()

    def test_search_settings_survive_closing_before_start(self):
        settings = {'topic': 'Юристы', 'city': '', 'roles': COMMUNITY_SEARCH_LABEL, 'chat_type': 'Все',
                    'max_chats': '1000', 'posts': '15', 'min_members': '1000', 'max_new': '100',
                    'max_operations': '300', 'cache_hours': '24', 'retention_days': '30',
                    'limit_mode': 'Остановить и сохранить'}
        view = NS(**{key: NS(get=lambda value=value: value) for key, value in settings.items()},
                  keywords=NS(get=lambda *args: 'юрист'), sources=NS(get=lambda *args: ''),
                  admins=NS(get=lambda: True), bots=NS(get=lambda: False))
        with tempfile.TemporaryDirectory() as folder, patch('app.PRIVATE', Path(folder)):
            Application.persist_search_settings(view)
            saved = json.loads((Path(folder) / 'search_settings.json').read_text())
            self.assertEqual(saved['roles'], COMMUNITY_SEARCH_LABEL)
            self.assertEqual(saved['max_chats'], '1000')
            self.assertEqual(saved['keywords'], 'юрист')
