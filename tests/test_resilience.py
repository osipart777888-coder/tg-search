import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telethon import TelegramClient, errors, types
from telethon.sessions import MemorySession
from telethon.tl.functions.contacts import SearchRequest

import test_search as fixtures
from tgsearch.control import Cooldown, LimitReached, OperationLimiter, SearchPaused, session_lock
from tgsearch.engine import Contact, Evidence, SearchEngine, filtered_contacts
from tgsearch.history import ContactHistory


class ResilienceTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.EngineTests.setUp
    options = fixtures.EngineTests.options
    async def test_operation_budget_counts_each_category_and_stops(self):
        result = await self.engine.run(self.options(max_operations=3))
        self.assertEqual(result.stats()['operations'], 3)
        self.assertEqual(result.stats()['categories'], {'discovery': 1, 'descriptions': 1, 'publications': 0, 'users': 1, 'admins': 0})
        self.assertIn('лимит API', result.reason)
        self.assertEqual(set(result.contacts), {1})
        checkpoint = result.history.unfinished()
        self.assertEqual(checkpoint['counters']['counts']['users'], 1)
        self.assertFalse(result.complete)

    async def test_new_contact_budget_is_exact_and_resume_requires_larger_budget(self):
        first = await self.engine.run(self.options(max_new=1))
        self.assertEqual(set(first.new_contacts), {1})
        checkpoint = first.history.unfinished()
        same = await SearchEngine(self.client, lambda *a: None, request_delay=0).run(self.options(max_new=1), resume=checkpoint)
        self.assertEqual(set(same.new_contacts), {1})
        resumed = await SearchEngine(self.client, lambda *a: None, request_delay=0).run(self.options(max_new=3), resume=first.history.unfinished())
        self.assertEqual(set(resumed.new_contacts), {1, 2, 4})
        self.assertTrue(resumed.complete)
        self.assertEqual(resumed.path.resolve(), first.path.resolve())
        self.assertEqual(resumed.history.total(), 3)

    async def test_persistent_flood_prevents_new_engine_from_sending_requests(self):
        self.client.get_entity = AsyncMock(side_effect=errors.FloodWaitError(None, capture=90))
        first = await self.engine.run(self.options())
        before = list(self.client.calls)
        second = SearchEngine(self.client, lambda *a: None, request_delay=0)
        with self.assertRaises(SearchPaused):
            await second.run(self.options(), resume=first.history.unfinished())
        self.assertEqual(before, self.client.calls)
        with patch('tgsearch.control.time.time', return_value=time.time() + 100):
            self.assertEqual(Cooldown(first.limiter.cooldown.path.parent).remaining(), 0)

    async def test_cache_reuses_metadata_but_checks_new_publications_and_admins(self):
        await self.engine.run(self.options())
        self.client.calls.clear()
        self.client.users['newposter'] = types.User(777, username='NewPoster')
        self.client.messages[0].message = 'Владелец @NewPoster'
        result = await SearchEngine(self.client, lambda *a: None, request_delay=0).run(self.options())
        self.assertNotIn('GetFullChannelRequest', self.client.calls)
        self.assertIn('admins', self.client.calls)
        self.assertIn(777, result.new_contacts)

    async def test_expired_and_forced_cache_refresh(self):
        await self.engine.run(self.options())
        for options in (self.options(cache_hours=0), self.options(force_refresh=True)):
            self.client.calls.clear()
            await SearchEngine(self.client, lambda *a: None, request_delay=0).run(options)
            self.assertEqual(self.client.calls.count('GetFullChannelRequest'), 1)
        with self.engine.history.connection() as db:
            db.execute('UPDATE community_cache SET checked_at=0')
        self.client.calls.clear()
        await SearchEngine(self.client, lambda *a: None, request_delay=0).run(self.options())
        self.assertIn('GetFullChannelRequest', self.client.calls)

    async def test_failed_stage_and_pending_contacts_are_resumable(self):
        original = self.client.get_entity
        async def temporary(name):
            if name == 'adsuser':
                raise OSError('network')
            return await original(name)
        self.client.get_entity = temporary
        first = await self.engine.run(self.options())
        self.assertIn('adsuser', first.pending)
        self.assertNotIn(2, first.contacts)
        self.assertFalse(first.complete)
        self.client.get_entity = original
        self.client.calls.clear()
        resumed = await SearchEngine(self.client, lambda *a: None, request_delay=0).run(self.options(), resume=first.history.unfinished())
        self.assertTrue(resumed.complete)
        self.assertFalse(resumed.pending)
        self.assertNotIn('admins', self.client.calls)  # Completed stages are skipped.
        self.assertEqual(set(resumed.new_contacts), {1, 2, 4})

    async def test_exception_saves_checkpoint_and_found_contacts(self):
        original = self.client.get_entity
        async def crash(name):
            if name == 'adsuser':
                raise RuntimeError('fake-crash-secret')
            return await original(name)
        self.client.get_entity = crash
        with self.assertRaises(RuntimeError):
            await self.engine.run(self.options())
        checkpoint = self.engine.history.unfinished()
        self.assertEqual(checkpoint['contacts'][0]['user_id'], 1)
        self.assertNotIn('fake-crash-secret', json.dumps(checkpoint))
        self.client.get_entity = original
        result = await SearchEngine(self.client, lambda *a: None, request_delay=0).run(self.options(), resume=checkpoint)
        self.assertTrue(result.complete)
        self.assertEqual(set(result.new_contacts), {1, 2, 4})

    async def test_hard_process_exit_replays_uncommitted_stage_without_losing_contacts(self):
        private = self.engine.ensure_limiter().cooldown.path.parent
        results = private.parent / 'results'
        code = '''
import asyncio, os, sys
from pathlib import Path
from test_search import FakeClient
import tgsearch.engine as module
from tgsearch.engine import SearchEngine, SearchOptions
from tgsearch.extraction import ROLE_FILTERS
module.PRIVATE=Path(sys.argv[1]); module.RESULTS=Path(sys.argv[2]); module.prepare_storage=lambda: None
client=FakeClient(); original=client.get_entity
async def crash(name):
    if name=='adsuser': os._exit(23)
    return await original(name)
client.get_entity=crash
asyncio.run(SearchEngine(client, lambda *a: None, request_delay=0).run(SearchOptions('строители',set(ROLE_FILTERS['Владельцы, администраторы и реклама']))))
'''
        env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path.cwd()), str(Path.cwd() / 'tests'))))
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', code, str(private), str(results), env=env)
        self.assertEqual(await proc.wait(), 23)
        history = ContactHistory(private / 'contacts.sqlite3')
        checkpoint = history.unfinished()
        self.assertIsNotNone(checkpoint)
        self.assertGreater(sum(checkpoint['counters']['counts'].values()), 0)
        result = await SearchEngine(self.client, lambda *a: None, request_delay=0).run(self.options(), resume=checkpoint)
        self.assertEqual(set(result.new_contacts), {1, 2, 4})
        self.assertEqual(result.history.total(), 3)
        self.assertTrue(result.complete)

    async def test_completed_search_recovers_report_if_crash_occurs_after_sqlite_commit(self):
        from tgsearch.engine import recover_latest_report
        from tgsearch.storage import atomic_text
        def fail_last_report(path, text, *args):
            if str(path).endswith('.json') and json.loads(text).get('complete') is True:
                raise OSError('simulated disk interruption')
            return atomic_text(path, text, *args)
        with patch('tgsearch.engine.atomic_text', side_effect=fail_last_report):
            with self.assertRaises(OSError):
                await self.engine.run(self.options())
        self.assertIsNone(self.engine.history.unfinished())
        self.engine.path.unlink()
        recover_latest_report(self.engine.history)
        self.assertEqual(self.engine.path.read_text(encoding='utf-8-sig'), '@adsuser\n@bossuser\n@buildercontact\n')
        pointer = json.loads((self.engine.limiter.cooldown.path.parent / 'last_search.json').read_text())
        restored = json.loads(Path(pointer['report']).read_text())
        self.assertTrue(restored['complete'])
        self.assertEqual(restored['stats']['new'], 3)

    async def test_username_transfer_within_run_never_exports_old_id_under_new_owners_name(self):
        owner = types.User(999, username='BossUser')
        owner.participant = types.ChannelParticipantCreator(999, types.ChatAdminRights())
        self.client.admins = [owner]
        result = await self.engine.run(self.options())
        self.assertEqual(result.contacts[1].verification, 'incomplete')
        self.assertNotIn(1, result.new_contacts)
        self.assertIn(999, result.new_contacts)
        self.assertIsNone(result.history.get(1)['username'])
        self.assertEqual(result.history.get(999)['username'], 'bossuser')

    def test_owner_confidence_filters_never_turn_advertising_into_owner(self):
        evidence = [Evidence('owner', 'Title', 'link', 'API'), Evidence('owner_contact', 'Title', 'link', 'text'), Evidence('advertising', 'Title', 'link', 'реклама')]
        contact = Contact('testuser', 1, evidence=evidence)
        selected = list(filtered_contacts([contact], 'Подтверждённый создатель', 'Подтверждено API'))
        self.assertEqual([e.role for e in selected[0].evidence], ['owner'])
        self.assertFalse(list(filtered_contacts([Contact('adsuser', 2, evidence=evidence[2:])], 'Предполагаемый владелец')))
        self.assertEqual(evidence[2].category, 'Другой контакт')

    def test_search_lock_excludes_parallel_process(self):
        private = self.engine.ensure_limiter().cooldown.path.parent
        with session_lock(private):
            with self.assertRaises(ValueError):
                with session_lock(private):
                    self.fail('Second session lock acquired')


class RealRpcGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_counts_helper_rpcs_and_batched_rpcs_without_network(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            client = TelegramClient(MemorySession(), 123, 'a'*32, request_retries=0)
            original = AsyncMock(return_value='response')
            client._call = original
            limiter = OperationLimiter(folder, delay=0, maximum=2)
            with limiter.bind(client):
                async def helper():
                    await client(SearchRequest('abc', 1))
                    return await client(SearchRequest('def', 1))
                self.assertEqual(await limiter.call(helper, 'users'), 'response')
                with self.assertRaises(LimitReached):
                    await limiter.call(lambda: client(SearchRequest('ghi', 1)))
            self.assertEqual(original.await_count, 2)
            self.assertEqual(limiter.counts['users'], 2)
            self.assertIs(client._call, original)
            client.session.close()
