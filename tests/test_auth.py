import asyncio
import json
import queue
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from telethon import errors, types
from tgsearch.backend import Backend, code_delivery_message, friendly_error, record_error


class AuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.backend = Backend.__new__(Backend)
        self.backend.events = queue.Queue()
        self.backend.client = NS(sign_in=AsyncMock(), get_me=AsyncMock(return_value=NS(username="localuser")))
        self.backend.phone = "+79990000000"
        self.backend.code_hash = "temporary-code-hash"
        self.backend.search_task = None
        self.backend.engine = None
        self.backend.qr_task = None

    async def test_code_login(self):
        result = await self.backend.sign_in("12345")
        self.assertEqual(result, {"identity": "@localuser"})
        self.backend.client.sign_in.assert_awaited_once_with("+79990000000", "12345", phone_code_hash="temporary-code-hash")

    async def test_2fa_challenge_and_password(self):
        self.backend.client.sign_in.side_effect = errors.SessionPasswordNeededError(None)
        result = await self.backend.sign_in("12345")
        self.assertTrue(result["password_required"])
        self.backend.client.sign_in.side_effect = None
        result = await self.backend.password_sign_in("dummy-test-password")
        self.assertEqual(result["identity"], "@localuser")

    async def test_cannot_sign_in_without_code_request(self):
        self.backend.code_hash = None
        with self.assertRaises(ValueError):
            await self.backend.sign_in("12345")
        self.backend.client.sign_in.assert_not_awaited()

    async def test_invalid_phone_rejected_before_api(self):
        with patch("tgsearch.backend.save_config") as save:
            with self.assertRaises(ValueError):
                await self.backend.request_code("999", "123", "a" * 32)
            save.assert_not_called()

    async def test_cancel_during_initial_connection(self):
        pending = asyncio.Event()
        async def connect():
            pending.set()
            await asyncio.Future()
        self.backend.connect = connect
        task = asyncio.create_task(self.backend.search(None))
        await pending.wait()
        task.cancel()
        self.assertIsNone(await task)
        self.assertIsNone(self.backend.search_task)

    def test_error_journal_omits_sensitive_message(self):
        with tempfile.TemporaryDirectory() as folder, patch('tgsearch.backend.PRIVATE', Path(folder)):
            try:
                raise RuntimeError('sensitive-test-password-and-phone')
            except RuntimeError as exc:
                record_error('code_sent', exc)
            path = Path(folder) / 'diagnostics.jsonl'
            content = path.read_text(encoding='utf-8')
            self.assertNotIn('sensitive-test', content)
            self.assertEqual(json.loads(content)['error'], 'RuntimeError')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_published_api_error_explains_required_action(self):
        text = friendly_error(errors.ApiIdPublishedFloodError(None))
        self.assertIn('my.telegram.org', text)
        self.assertNotIn('Перезапустите', text)

    def configure_qr(self, wait=None):
        qr = NS(url='tg://login?token=dummy-test-token',
                expires=datetime.now(timezone.utc) + timedelta(seconds=30),
                wait=wait or AsyncMock(), recreate=AsyncMock())
        self.backend.client.qr_login = AsyncMock(return_value=qr)
        self.backend.client.is_user_authorized = AsyncMock(return_value=False)
        self.backend.client.send_code_request = AsyncMock()
        self.backend.configure_client = AsyncMock(return_value=self.backend.client)
        return qr

    async def test_qr_login_does_not_request_sms(self):
        self.configure_qr()
        result = await self.backend.qr_login('123', 'a' * 32)
        self.assertEqual(result, {'identity': '@localuser'})
        kind, data = self.backend.events.get_nowait()
        self.assertEqual(kind, 'qr_code')
        self.assertIn('url', data)
        self.backend.client.send_code_request.assert_not_awaited()
        self.assertIsNone(self.backend.qr_task)

    async def test_qr_refresh_after_expiry(self):
        qr = self.configure_qr(AsyncMock(side_effect=[asyncio.TimeoutError(), None]))
        result = await self.backend.qr_login('123', 'a' * 32)
        self.assertEqual(result['identity'], '@localuser')
        qr.recreate.assert_awaited_once()
        self.assertEqual(self.backend.events.qsize(), 2)

    async def test_qr_requires_2fa(self):
        self.configure_qr(AsyncMock(side_effect=errors.SessionPasswordNeededError(None)))
        self.assertEqual(await self.backend.qr_login('123', 'a' * 32), {'password_required': True})

    async def test_qr_cancel_removes_active_task(self):
        entered = asyncio.Event()
        async def wait():
            entered.set()
            await asyncio.Future()
        self.configure_qr(wait)
        task = asyncio.create_task(self.backend.qr_login('123', 'a' * 32))
        await entered.wait()
        task.cancel()
        self.assertEqual(await task, {'cancelled': True})
        self.assertIsNone(self.backend.qr_task)

    def test_delivery_message_follows_server_type(self):
        app = NS(type=types.auth.SentCodeTypeApp(5))
        sms = NS(type=types.auth.SentCodeTypeSms(5))
        self.assertIn('служебный чат', code_delivery_message(app))
        self.assertIn('по SMS', code_delivery_message(sms))


if __name__ == "__main__":
    unittest.main()
