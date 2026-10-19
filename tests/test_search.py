import asyncio
import csv
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from telethon import errors, types
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.contacts import SearchRequest

from tgsearch.engine import Contact, Evidence, SearchEngine, SearchOptions
from tgsearch.extraction import ROLE_FILTERS, city_matches, extract_candidates, make_queries, public_message_text
from tgsearch.storage import export_csv, export_txt


def channel(chat_id=100, username="builders_chat", title="Строители Москвы", broadcast=False):
    return types.Channel(chat_id, title, types.ChatPhotoEmpty(), datetime.now(timezone.utc),
                         username=username, broadcast=broadcast, megagroup=not broadcast)


class ExtractionTests(unittest.TestCase):
    def roles(self, text, post=False):
        return {c.username: c.role for c in extract_candidates(text, post)}

    def test_separate_roles_and_multiple_contacts(self):
        self.assertEqual(self.roles("Владелец: @BossUser\nПо рекламе: t.me/AdsUser\nАдмин @ModUser"),
                         {"bossuser": "owner_contact", "adsuser": "advertising", "moduser": "admin_contact"})
        self.assertEqual(self.roles("Админ @BossUser, по рекламе @AdsUser"),
                         {"bossuser": "admin_contact", "adsuser": "advertising"})

    def test_labels_on_next_line_and_trailing(self):
        self.assertEqual(self.roles("Реклама:\n@AdsUser"), {"adsuser": "advertising"})
        self.assertEqual(self.roles("@BossUser — владелец"), {"bossuser": "owner_contact"})

    def test_no_email_invite_or_service_links(self):
        self.assertEqual(self.roles("mail@example.com https://t.me/+abcdef t.me/joinchat/test t.me/addstickers/Cats"), {})

    def test_deduplicate_case_and_link_formats(self):
        found = extract_candidates("По рекламе @AdsUser и https://t.me/ADSUSER и telegram.me/AdsUser")
        self.assertEqual(len(found), 1)

    def test_distinguish_ad_sales_from_advertiser(self):
        self.assertEqual(self.roles("Рекламодатель: @CompanyContact\n#реклама erid: abc", True), {"companycontact": "advertiser"})
        self.assertEqual(self.roles("Заказать ремонт: @BuilderContact\n#реклама erid: abc", True), {"buildercontact": "ad_contact"})
        self.assertEqual(self.roles("По рекламе @SalesContact\n#реклама", True), {"salescontact": "advertising"})

    def test_unlabelled_contact_stays_uncertain(self):
        self.assertEqual(self.roles("Связь @GeneralContact; реклама @AdsUser"),
                         {"generalcontact": "contact", "adsuser": "advertising"})
        self.assertEqual(self.roles("Связь @GeneralContact, по рекламе @AdsUser")["generalcontact"], "contact")

    def test_hidden_link_with_utf16_emoji_offset(self):
        message = NS(message="🔨 По рекламе: Менеджер", entities=[types.MessageEntityTextUrl(15, 8, "https://t.me/AdsUser")])
        text = public_message_text(message)
        self.assertIn("Менеджер (https://t.me/AdsUser)", text)
        self.assertEqual(self.roles(text), {"adsuser": "advertising"})

    def test_queries_and_city_filter(self):
        self.assertEqual(make_queries("Юрист, юрист; адвокат", "Москва"), ["юрист Москва", "адвокат Москва", "юрист", "адвокат"])
        self.assertTrue(city_matches("Москва", "Московские строители"))
        self.assertTrue(city_matches("СПб", "Стройка Санкт-Петербурга"))
        self.assertFalse(city_matches("Москва", "Строители Казани"))
        with self.assertRaises(ValueError):
            make_queries("a")

    def test_txt_one_handle_per_line_sorted_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.txt"
            export_txt(path, [Contact("ZuluUser", 1), Contact("alphauser", 2), Contact("ALPHAUSER", 2)])
            self.assertEqual(path.read_text(encoding="utf-8-sig"), "@alphauser\n@zuluuser\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_csv_sources_and_formula_escaping(self):
        contact = Contact("normaluser", 1, evidence=[Evidence("admin", "=bad()", "https://t.me/test", "+123")])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.csv"
            export_csv(path, [contact])
            with path.open(encoding="utf-8-sig") as source:
                rows = list(csv.reader(source, delimiter=";"))
            self.assertEqual(rows[1][2], "'=bad()")
            self.assertEqual(rows[1][4], "'+123")


class FakeClient:
    def __init__(self):
        self.chat = channel()
        self.about = "Владелец @BossUser\nПо рекламе @AdsUser\nПо рекламе @ContactBot\nАдмин @NotAUser\nАдмин @GoneUser"
        self.users = {"bossuser": types.User(1, username="BossUser"),
                      "adsuser": types.User(2, username="AdsUser"),
                      "contactbot": types.User(3, username="ContactBot", bot=True),
                      "notauser": channel(900, "NotAUser"),
                      "goneuser": types.User(8, username="GoneUser", deleted=True)}
        self.messages = [NS(id=12, message="Заказать ремонт @BuilderContact\n#реклама erid: xyz", entities=[])]
        self.users["buildercontact"] = types.User(4, username="BuilderContact")
        owner = self.users["bossuser"]
        owner.participant = types.ChannelParticipantCreator(owner.id, types.ChatAdminRights())
        self.admins = [owner]
        self.calls = []

    async def __call__(self, request):
        self.calls.append(type(request).__name__)
        if isinstance(request, SearchRequest):
            return NS(chats=[self.chat, self.chat])
        if isinstance(request, GetFullChannelRequest):
            return NS(full_chat=NS(about=self.about, participants_count=3000, pinned_msg_id=12))
        raise AssertionError(type(request))

    async def get_entity(self, name):
        self.calls.append("resolve:" + name)
        if name == "builders_chat":
            return self.chat
        return self.users[name]

    async def get_messages(self, chat, **kwargs):
        self.calls.append("messages")
        return self.messages[0] if "ids" in kwargs else self.messages

    async def get_participants(self, chat, **kwargs):
        self.calls.append("admins")
        return self.admins


class EngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "private").mkdir()
        (root / "results").mkdir()
        self.patches = [patch("tgsearch.engine.PRIVATE", root / "private"),
                        patch("tgsearch.engine.RESULTS", root / "results"),
                        patch("tgsearch.engine.prepare_storage")]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.client = FakeClient()
        self.events = []
        self.engine = SearchEngine(self.client, lambda *args: self.events.append(args), request_delay=0)

    def options(self, **kwargs):
        return SearchOptions("строители, строительство", set(ROLE_FILTERS["Владельцы, администраторы и реклама"]), **kwargs)

    async def test_end_to_end_search_filters_dedup_sources_and_auto_export(self):
        result = await self.engine.run(self.options())
        self.assertEqual(set(result.contacts), {1, 2, 4})
        self.assertEqual(result.scanned, 1)
        self.assertEqual(len(result.contacts[1].evidence), 2)
        self.assertIn("https://t.me/builders_chat/12", result.contacts[4].evidence[0].source)
        self.assertEqual(result.path.read_text(encoding="utf-8-sig"), "@adsuser\n@bossuser\n@buildercontact\n")
        self.assertEqual(self.client.calls.count("resolve:adsuser"), 1)

    async def test_owner_only_does_not_return_admin_or_ad_contacts(self):
        options = self.options()
        options.roles = {"owner", "owner_contact"}
        result = await self.engine.run(options)
        self.assertEqual(set(result.contacts), {1})

    async def test_optional_bots_and_explicit_source(self):
        options = self.options(include_bots=True, sources="https://t.me/builders_chat")
        options.keywords = ""
        result = await self.engine.run(options)
        self.assertIn(3, result.contacts)
        self.assertNotIn("SearchRequest", self.client.calls)

    async def test_city_and_audience_filters(self):
        result = await self.engine.run(self.options(city="Казань"))
        self.assertEqual(result.scanned, 0)
        self.assertFalse(result.contacts)
        self.assertNotIn("admins", self.client.calls)

    async def test_channel_type_filter(self):
        result = await self.engine.run(self.options(chat_type="Каналы"))
        self.assertEqual(result.scanned, 0)
        self.assertNotIn("GetFullChannelRequest", self.client.calls)

    async def test_hidden_admin_list_is_logged_and_public_contacts_survive(self):
        async def hidden(*args, **kwargs):
            raise errors.ChatAdminRequiredError(None)
        self.client.get_participants = hidden
        result = await self.engine.run(self.options())
        self.assertEqual(set(result.contacts), {1, 2, 4})
        self.assertEqual(result.warnings, 1)

    async def test_stop_keeps_contact_found_mid_channel(self):
        waiting = asyncio.Event()
        original = self.client.get_entity

        async def get_entity(name):
            if name == "adsuser":
                waiting.set()
                await asyncio.Future()
            return await original(name)
        self.client.get_entity = get_entity
        task = asyncio.create_task(self.engine.run(self.options()))
        await asyncio.wait_for(waiting.wait(), 3)
        task.cancel()
        result = await task
        self.assertTrue(result.cancelled)
        self.assertEqual(result.path.read_text(encoding="utf-8-sig"), "@bossuser\n")
        self.client.get_entity = original
        repeat = await SearchEngine(self.client, lambda *args: None, request_delay=0).run(self.options())
        self.assertNotIn("@bossuser", repeat.path.read_text(encoding="utf-8-sig"))
        self.assertEqual(set(repeat.new_contacts), {2, 4})

    async def test_repeat_search_exports_only_new_accounts_across_engines(self):
        first = await self.engine.run(self.options())
        first_file = first.path.read_bytes()
        self.client.users["freshcontact"] = types.User(500, username="FreshContact")
        self.client.about += "\nПо рекламе: @FreshContact"
        second = await SearchEngine(self.client, lambda *args: None, request_delay=0).run(self.options())
        self.assertEqual(second.path.read_text(encoding="utf-8-sig"), "@freshcontact\n")
        self.assertEqual(second.stats()["contacts"], 4)
        self.assertEqual(second.stats()["new"], 1)
        self.assertEqual(second.stats()["known"], 3)
        self.assertEqual(first.path.read_bytes(), first_file)
        third = await SearchEngine(self.client, lambda *args: None, request_delay=0).run(self.options())
        self.assertEqual(third.path.read_text(encoding="utf-8-sig"), "")
        self.assertEqual(third.stats()["new"], 0)
        self.assertEqual(third.history.total(), 4)

    async def test_renamed_user_is_not_new_in_next_search(self):
        await self.engine.run(self.options())
        owner = types.User(1, username="RenamedBoss")
        owner.participant = types.ChannelParticipantCreator(1, types.ChatAdminRights())
        self.client.users["renamedboss"] = owner
        self.client.admins = [owner]
        self.client.about = self.client.about.replace("@BossUser", "@RenamedBoss")
        repeat = await SearchEngine(self.client, lambda *args: None, request_delay=0).run(self.options())
        self.assertEqual(repeat.path.read_text(encoding="utf-8-sig"), "")
        self.assertEqual(repeat.history.get(1)["username"], "renamedboss")

    async def test_failed_resolution_is_not_marked_as_already_collected(self):
        original = self.client.get_entity
        async def transient(name):
            if name == "adsuser":
                raise ValueError("temporarily unavailable")
            return await original(name)
        self.client.get_entity = transient
        first = await self.engine.run(self.options())
        self.assertIsNone(first.history.get(2))
        self.client.get_entity = original
        second = await SearchEngine(self.client, lambda *args: None, request_delay=0).run(self.options())
        self.assertEqual(second.path.read_text(encoding="utf-8-sig"), "@adsuser\n")

    async def test_long_flood_wait_stops_without_bypassing_limit(self):
        async def flood(name):
            raise errors.FloodWaitError(None, capture=120)
        self.client.get_entity = flood
        result = await self.engine.run(self.options())
        self.assertIn("120", result.reason)
        self.assertTrue(result.path.exists())

    async def test_short_flood_wait_retries(self):
        attempts = 0
        async def operation():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise errors.FloodWaitError(None, capture=1)
            return "ok"
        result = await self.engine.call(operation)
        self.assertEqual(result, "ok")
        self.assertEqual(attempts, 2)

    def test_bad_options(self):
        for kwargs in ({"max_chats": 0}, {"posts": 101}, {"min_members": -1}, {"sources": "https://t.me/chan/123"}):
            with self.assertRaises(ValueError):
                self.options(**kwargs).validate()


if __name__ == "__main__":
    unittest.main()
