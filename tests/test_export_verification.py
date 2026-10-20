import queue
import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from telethon import types, errors
from tgsearch.backend import Backend
from tgsearch.engine import Contact, Evidence
from tgsearch.history import ContactHistory
from tgsearch.control import Cooldown, SearchPaused


class ExportVerificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.history = ContactHistory(self.root / 'contacts.sqlite3')
        self.user = Contact('olduser', 123, evidence=[Evidence('owner_contact', 'Title', 'https://t.me/test', 'Владелец')])
        self.history.remember(self.user)
        self.client = NS(get_entity=AsyncMock(return_value=types.User(123, username='NewUser')))
        self.backend = Backend.__new__(Backend)
        self.backend.client = self.client
        self.backend.events = queue.Queue()
        self.backend.connect = AsyncMock(return_value=self.client)
        for item in (patch('tgsearch.backend.PRIVATE', self.root), patch('tgsearch.history.ContactHistory', return_value=self.history),
                     patch('tgsearch.control.asyncio.sleep', new=AsyncMock())):
            item.start(); self.addCleanup(item.stop)

    async def test_export_can_be_cancelled_during_connection(self):
        entered = asyncio.Event()
        async def connect():
            entered.set()
            await asyncio.Future()
        self.backend.connect = connect
        task = asyncio.create_task(self.backend.verify_export([self.user]))
        await entered.wait()
        task.cancel()
        self.assertIsNone(await task)
        self.assertIsNone(self.backend.export_task)

    async def test_stale_handle_revalidated_by_id_and_rename_saved(self):
        result = await self.backend.verify_export([self.user])
        self.client.get_entity.assert_awaited_once_with(123)
        self.assertEqual(result[0].username, 'newuser')
        self.assertEqual(self.history.get(123)['username'], 'newuser')
        self.assertEqual(self.history.get(123)['aliases'], ['newuser', 'olduser'])

    async def test_fresh_verification_avoids_extra_request(self):
        self.user.checked_at = datetime.now(timezone.utc).isoformat()
        result = await self.backend.verify_export([self.user])
        self.assertEqual(result, [self.user])
        self.client.get_entity.assert_not_awaited()

    async def test_recent_incomplete_verification_is_rechecked(self):
        self.user.checked_at = datetime.now(timezone.utc).isoformat()
        self.user.verification = 'incomplete'
        result = await self.backend.verify_export([self.user])
        self.assertEqual(len(result), 1)
        self.client.get_entity.assert_awaited_once()

    async def test_reassigned_handle_or_failure_is_never_exported_as_old_id(self):
        for response in (types.User(999, username='OldUser'), types.User(123, deleted=True), OSError('secret-error')):
            self.client.get_entity.side_effect = response if isinstance(response, Exception) else None
            self.client.get_entity.return_value = response
            self.assertEqual(await self.backend.verify_export([self.user]), [])
            self.assertEqual(self.user.verification, 'incomplete')
            self.assertEqual(self.history.get(123)['verification'], 'incomplete')

    async def test_flood_during_export_persists_cooldown_and_stops(self):
        self.client.get_entity.side_effect = errors.FloodWaitError(None, capture=30)
        with self.assertRaises(SearchPaused):
            await self.backend.verify_export([self.user, Contact('nextuser', 456)])
        self.client.get_entity.assert_awaited_once()
        self.assertGreater(Cooldown(self.root).remaining(), 0)
