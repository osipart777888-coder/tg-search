import asyncio
import json
import queue
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from telethon import errors, types
from telethon.tl.functions.auth import LogOutRequest

from app import Application
from tgsearch.backend import Backend
from tgsearch.control import Cooldown, SearchPaused
from tgsearch.security import EncryptedSession
from test_security import FakeKeychain


class AccountSwitchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.private = Path(self.tmp.name)
        self.session = EncryptedSession(self.private / 'telegram.session', FakeKeychain())
        self.addCleanup(self.session.close)
        self.client = AsyncMock(return_value=types.auth.LoggedOut())
        self.client.session = self.session
        self.client.is_user_authorized.return_value = True
        self.client.disconnect.side_effect = self.session.close
        self.client.get_me.return_value = NS(username='olduser', phone='79990000000')
        self.backend = Backend.__new__(Backend)
        self.backend.client = self.client
        self.backend.events = queue.Queue()
        self.backend.phone = '+79990000000'
        self.backend.active_phone = self.backend.phone
        self.backend.code_hash = 'old-login-hash'
        self.backend.code_is_text = True
        self.backend.resend_after = time.monotonic() + 60
        self.backend.persist_session = Mock()
        self.backend.search_task = self.backend.qr_task = None
        self.config = {'api_id': 123, 'api_hash': 'a' * 32, 'phone': self.backend.phone}
        self.saved = Mock()
        for item in (patch('tgsearch.backend.PRIVATE', self.private),
                     patch('tgsearch.backend.load_config', side_effect=lambda: dict(self.config)),
                     patch('tgsearch.backend.save_config', self.saved)):
            item.start(); self.addCleanup(item.stop)
        (self.private / 'contacts.sqlite3').write_bytes(b'history-must-survive')
        (self.private / 'reports').mkdir()
        (self.private / 'reports/previous.json').write_text('{"previous": true}')

    async def test_logout_revokes_one_session_and_resets_login_without_erasing_history(self):
        cooldown = self.private / 'cooldown.json'
        original = json.dumps({'until': time.time() - 10})
        cooldown.write_text(original)
        await self.backend.logout()
        request = self.client.call_args.args[0]
        self.assertIsInstance(request, LogOutRequest)
        self.client.assert_awaited_once()
        self.assertIsNone(self.backend.client)
        self.assertFalse((self.private / 'telegram.session').exists())
        self.assertEqual(self.backend.phone, '')
        self.assertEqual(self.backend.active_phone, '')
        self.assertIsNone(self.backend.code_hash)
        self.assertFalse(self.backend.code_is_text)
        self.assertEqual(self.backend.resend_after, 0)
        self.assertNotIn('phone', self.saved.call_args.args[0])
        self.assertEqual((self.private / 'contacts.sqlite3').read_bytes(), b'history-must-survive')
        self.assertTrue((self.private / 'reports/previous.json').exists())
        self.assertEqual(cooldown.read_text(), original)

    async def test_logout_error_keeps_session_and_does_not_report_success(self):
        self.client.side_effect = OSError('network problem')
        with self.assertRaises(OSError):
            await self.backend.logout()
        self.assertIs(self.backend.client, self.client)
        self.assertTrue((self.private / 'telegram.session').exists())
        self.assertEqual(self.backend.active_phone, '+79990000000')
        self.saved.assert_not_called()

    async def test_revoked_session_can_be_replaced_by_a_new_login(self):
        self.client.side_effect = errors.AuthKeyUnregisteredError(None)
        await self.backend.logout()
        self.assertIsNone(self.backend.client)
        self.assertFalse((self.private / 'telegram.session').exists())
        self.assertIsNone(self.backend.code_hash)

    async def test_flood_during_logout_is_saved_and_prevents_another_attempt(self):
        self.client.side_effect = errors.FloodWaitError(None, capture=30)
        with self.assertRaises(errors.FloodWaitError):
            await self.backend.logout()
        self.assertGreater(Cooldown(self.private).remaining(), 0)
        with self.assertRaises(SearchPaused):
            await self.backend.logout()
        self.client.assert_awaited_once()
        self.assertTrue((self.private / 'telegram.session').exists())
        self.saved.assert_not_called()

    async def test_flood_during_identity_check_does_not_erase_a_valid_session(self):
        self.client.get_me.side_effect = errors.FloodWaitError(None, capture=30)
        with self.assertRaises(errors.FloodWaitError):
            await self.backend.logout()
        self.client.assert_not_awaited()
        self.assertTrue((self.private / 'telegram.session').exists())
        self.assertGreater(Cooldown(self.private).remaining(), 0)

    async def test_changing_number_during_code_entry_clears_pending_login(self):
        self.client.is_user_authorized.return_value = False
        self.client.get_me.return_value = None
        await self.backend.logout()
        self.client.assert_not_awaited()
        self.assertIsNone(self.backend.code_hash)
        self.assertFalse((self.private / 'telegram.session').exists())

    async def test_number_change_cannot_silently_return_old_account(self):
        self.backend.configure_client = AsyncMock(return_value=self.client)
        self.client.send_code_request = AsyncMock()
        with self.assertRaisesRegex(ValueError, 'Сменить аккаунт'):
            await self.backend.request_code('+79991111111', '123', 'a' * 32)
        self.client.send_code_request.assert_not_awaited()
        self.saved.assert_not_called()

    async def test_new_number_gets_its_own_code_and_no_old_code_hash(self):
        await self.backend.logout()
        fresh = NS(is_user_authorized=AsyncMock(return_value=False),
                   send_code_request=AsyncMock(return_value=NS(phone_code_hash='new-hash', type=types.auth.SentCodeTypeApp(5), timeout=60)))
        self.backend.configure_client = AsyncMock(return_value=fresh)
        with patch('tgsearch.backend._write_diagnostic'):
            result = await self.backend.request_code('+79991111111', '123', 'a' * 32)
        fresh.send_code_request.assert_awaited_once_with('+79991111111')
        self.assertEqual(self.backend.phone, '+79991111111')
        self.assertEqual(self.backend.code_hash, 'new-hash')
        self.assertIsNone(result['identity'])

    async def test_switch_rejected_while_search_is_running(self):
        self.backend.search_task = asyncio.current_task()
        with self.assertRaises(ValueError):
            await self.backend.logout()
        self.client.assert_not_awaited()
        self.assertTrue((self.private / 'telegram.session').exists())

    async def test_successful_logout_remains_successful_if_phone_preferences_cannot_be_saved(self):
        self.saved.side_effect = OSError('disk error')
        with patch('tgsearch.backend.record_error'):
            await self.backend.logout()
        self.assertIsNone(self.backend.client)
        self.assertFalse((self.private / 'telegram.session').exists())


class AccountSwitchUiTests(unittest.TestCase):
    def view(self, authorized=True, stage='phone'):
        names = ('phone_entry', 'code_entry', 'password_entry', 'switch_button', 'request_button',
                 'qr_button', 'code_button', 'password_button', 'apply_api_button', 'logout_button')
        return NS(authorized=authorized, auth_stage=stage, running=False,
                  **{name: Mock() for name in names})

    def test_connected_number_readonly_with_visible_switch_action(self):
        view = self.view()
        Application.set_auth_busy(view, False)
        view.phone_entry.configure.assert_called_once_with(state='readonly')
        self.assertEqual(view.switch_button.configure.call_args.kwargs['state'], 'normal')
        view.request_button.configure.assert_called_once_with(state='disabled')

    def test_new_login_enables_phone_and_code_request(self):
        view = self.view(authorized=False)
        Application.set_auth_busy(view, False)
        view.phone_entry.configure.assert_called_once_with(state='normal')
        view.request_button.configure.assert_called_once_with(state='normal')
        self.assertEqual(view.switch_button.configure.call_args.kwargs['state'], 'disabled')

    def test_pending_code_has_an_explicit_other_number_action(self):
        view = self.view(authorized=False, stage='code')
        Application.set_auth_busy(view, False)
        view.phone_entry.configure.assert_called_once_with(state='readonly')
        view.code_entry.configure.assert_called_once_with(state='normal')
        view.switch_button.configure.assert_called_once_with(text='Другой номер', state='normal')

    def test_account_changed_event_clears_fields_and_retains_contact_data(self):
        view = self.view()
        view.phone, view.code, view.password = Mock(), Mock(), Mock()
        view.start_button, view.auth_state, view.status = Mock(), Mock(), Mock()
        view.tabs, view.login_canvas, view.login_tab = Mock(), Mock(), object()
        view.set_auth_busy, view.update_actions = Mock(), Mock()
        history, contacts = object(), {1: 'existing contact'}
        view.history, view.contacts = history, contacts
        Application.handle_event(view, 'account_changed', None)
        self.assertFalse(view.authorized)
        self.assertEqual(view.auth_stage, 'phone')
        for field in (view.phone, view.code, view.password):
            field.set.assert_called_once_with('')
        view.set_auth_busy.assert_called_once_with(False)
        view.phone_entry.focus_set.assert_called_once()
        self.assertIs(view.history, history)
        self.assertIs(view.contacts, contacts)
