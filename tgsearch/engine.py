import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from telethon import errors, types
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.contacts import SearchRequest

from .extraction import ROLE_LABELS, city_matches, extract_candidates, make_queries, public_message_text
from .history import ContactHistory
from .storage import PRIVATE, RESULTS, atomic_text, export_txt, prepare_storage


@dataclass
class SearchOptions:
    keywords: str
    roles: set
    city: str = ""
    chat_type: str = "Все"
    max_chats: int = 50
    posts: int = 30
    min_members: int = 0
    check_admins: bool = True
    include_bots: bool = False
    sources: str = ""

    def validate(self):
        if not self.roles or not self.roles <= ROLE_LABELS.keys():
            raise ValueError("Выберите, кого искать.")
        if self.chat_type not in {"Все", "Группы", "Каналы"}:
            raise ValueError("Неизвестный тип сообщества.")
        if not 1 <= self.max_chats <= 300 or not 0 <= self.posts <= 100 or self.min_members < 0:
            raise ValueError("Сообщества: 1–300; публикации: 0–100; размер аудитории: от 0.")
        queries = make_queries(self.keywords, self.city) if self.keywords.strip() else []
        sources = []
        for term in self.sources.replace("\n", ",").replace(";", ",").split(","):
            term = term.strip().removeprefix("https://").removeprefix("http://")
            term = term.removeprefix("t.me/").removeprefix("@").strip("/")
            if term:
                import re
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", term):
                    raise ValueError("В списке сообществ укажите публичные @ники или ссылки t.me/ник, без ссылок на посты.")
                sources.append(term)
        if len(sources) > 100:
            raise ValueError("Можно указать до 100 публичных сообществ за один поиск.")
        if not queries and not sources:
            raise ValueError("Укажите поисковые слова или публичные сообщества.")
        return queries, list(dict.fromkeys(sources))


@dataclass(frozen=True)
class Evidence:
    role: str
    title: str
    source: str
    excerpt: str

    @property
    def role_label(self):
        return ROLE_LABELS[self.role]


@dataclass
class Contact:
    username: str
    user_id: int
    bot: bool = False
    evidence: list = field(default_factory=list)
    is_new: bool = True


class SearchPaused(Exception):
    pass


class SearchEngine:
    def __init__(self, client, emit, request_delay=2.0):
        self.client = client
        self.emit = emit
        self.request_delay = request_delay
        self.last_call = 0.0
        self.contacts = {}
        self.resolved = {}
        self.seen = set()
        self.scanned = 0
        self.skipped = 0
        self.warnings = 0
        self.path = None
        self.reason = ""
        self.cancelled = False
        self.full_cache = {}
        self.source_entities = {}
        self.history = None

    @property
    def new_contacts(self):
        return {key: contact for key, contact in self.contacts.items() if contact.is_new}

    def log(self, message):
        self.emit("log", message)

    async def call(self, factory):
        for attempt in range(3):
            await asyncio.sleep(max(0, self.request_delay - (time.monotonic() - self.last_call)))
            self.last_call = time.monotonic()
            try:
                return await asyncio.wait_for(factory(), timeout=40)
            except errors.FloodWaitError as exc:
                if exc.seconds > 60 or attempt == 2:
                    raise SearchPaused(f"Telegram ограничил запросы. Повторите поиск через {exc.seconds} сек. Найденное сохранено.") from None
                self.log(f"Лимит Telegram: пауза {exc.seconds + 1} сек. Можно нажать «Стоп».")
                await asyncio.sleep(exc.seconds + 1)

    def warn(self, context, exc):
        self.warnings += 1
        if isinstance(exc, (errors.ChatAdminRequiredError, errors.ChannelPrivateError)):
            detail = "Telegram не открыл доступ"
        elif isinstance(exc, (OSError, asyncio.TimeoutError)):
            detail = "нет связи или истекло время ожидания"
        else:
            detail = type(exc).__name__
        self.log(f"{context}: {detail}.")

    async def run(self, options):
        queries, sources = options.validate()
        prepare_storage()
        self.options = options
        self.history = ContactHistory(PRIVATE / "contacts.sqlite3", PRIVATE / "reports")
        stamp = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d_%H-%M-%S_%f")
        self.path = RESULTS / f"Новые_ники_{stamp}.txt"
        self.log("Поиск начат. Ники проверяются в Telegram; каналы исключаются из списка контактов.")
        self.log(f"В общей истории: {self.history.total()} аккаунтов. В TXT попадут только новые.")
        self.save()
        try:
            for name in sources:
                try:
                    entity = self.source_entities.get(name.lower())
                    if entity is None:
                        entity = await self.call(lambda: self.client.get_entity(name))
                    await self.visit(entity)
                except (errors.RPCError, ValueError, OSError, asyncio.TimeoutError) as exc:
                    self.warn(f"Сообщество @{name}", exc)
                if len(self.seen) >= options.max_chats:
                    break
            for keyword in queries:
                if len(self.seen) >= options.max_chats:
                    self.log("Достигнут выбранный лимит сообществ.")
                    break
                self.log(f"Поиск сообществ: {keyword}")
                try:
                    result = await self.call(lambda: self.client(SearchRequest(q=keyword, limit=100)))
                except (errors.RPCError, OSError, asyncio.TimeoutError) as exc:
                    self.warn(f"Запрос «{keyword}»", exc)
                    continue
                for chat in result.chats:
                    if len(self.seen) >= options.max_chats:
                        break
                    await self.visit(chat)
        except asyncio.CancelledError:
            self.cancelled = True
            self.log("Поиск остановлен. Уже найденные ники сохранены.")
        except SearchPaused as exc:
            self.reason = str(exc)
            self.log(self.reason)
        except Exception:
            self.reason = "Поиск прерван из-за внутренней ошибки; уже найденные ники сохранены. Перезапустите программу."
            raise
        finally:
            self.save()
            self.emit("stats", self.stats())
        if not self.contacts:
            self.log("Подходящих открытых контактов не найдено. Попробуйте другие слова, больше сообществ или фильтр «Все опубликованные контакты».")
        elif not self.new_contacts:
            self.log("Все найденные аккаунты уже есть в истории. Новых контактов нет; TXT пуст.")
        self.log(f"Готово: найдено {len(self.contacts)}, новых {len(self.new_contacts)}. Файл: {self.path.name}")
        return self

    def stats(self):
        return {"contacts": len(self.contacts), "new": len(self.new_contacts),
                "known": len(self.contacts) - len(self.new_contacts),
                "history_total": self.history.total() if self.history is not None else 0,
                "scanned": self.scanned,
                "seen": len(self.seen), "skipped": self.skipped, "warnings": self.warnings}

    def save(self):
        if self.path is None:
            return
        export_txt(self.path, self.new_contacts.values())
        data = {"created_timezone": "Europe/Moscow", "file": str(self.path),
                "options": {**asdict(self.options), "roles": sorted(self.options.roles)},
                "stats": self.stats(), "cancelled": self.cancelled, "reason": self.reason,
                "contacts": [asdict(c) for c in self.contacts.values()]}
        reports = PRIVATE / "reports"
        reports.mkdir(exist_ok=True)
        report_path = reports / (self.path.stem + ".json")
        atomic_text(report_path, json.dumps(data, ensure_ascii=False, indent=2))
        atomic_text(PRIVATE / "last_search.json", json.dumps({"report": str(report_path)}, ensure_ascii=False))

    async def visit(self, chat):
        if not isinstance(chat, types.Channel) or not getattr(chat, "username", None):
            return
        is_channel = bool(chat.broadcast)
        if self.options.chat_type == "Группы" and is_channel:
            return
        if self.options.chat_type == "Каналы" and not is_channel:
            return
        if chat.id in self.seen:
            return
        self.seen.add(chat.id)
        self.log(f"Проверка: {chat.title} (@{chat.username})")
        full = self.full_cache.get(chat.id)
        try:
            if full is None:
                full = await self.call(lambda: self.client(GetFullChannelRequest(chat)))
        except (errors.RPCError, OSError, asyncio.TimeoutError) as exc:
            self.warn("Описание", exc)
        about = getattr(getattr(full, "full_chat", None), "about", "") or ""
        count = getattr(getattr(full, "full_chat", None), "participants_count", None)
        if not city_matches(self.options.city, chat.title + " " + about):
            self.skipped += 1
            self.log("Пропуск: город не указан в названии или описании.")
            self.emit("stats", self.stats())
            return
        if self.options.min_members and (count is None or count < self.options.min_members):
            self.skipped += 1
            self.log("Пропуск: аудитория меньше фильтра или её размер скрыт.")
            self.emit("stats", self.stats())
            return
        self.scanned += 1
        link = f"https://t.me/{chat.username}"
        await self.collect_text(about, chat, link)
        pinned_id = getattr(getattr(full, "full_chat", None), "pinned_msg_id", None)
        message_ids = set()
        if pinned_id:
            try:
                message = await self.call(lambda: self.client.get_messages(chat, ids=pinned_id))
                if message:
                    message_ids.add(message.id)
                    await self.collect_text(public_message_text(message), chat, f"{link}/{message.id}", True)
            except (errors.RPCError, OSError, asyncio.TimeoutError) as exc:
                self.warn("Закреплённая публикация", exc)
        if self.options.posts:
            try:
                messages = await self.call(lambda: self.client.get_messages(chat, limit=self.options.posts))
                for message in messages:
                    if message.id not in message_ids:
                        await self.collect_text(public_message_text(message), chat, f"{link}/{message.id}", True)
            except (errors.RPCError, OSError, asyncio.TimeoutError) as exc:
                self.warn("Публикации", exc)
        if self.options.check_admins and self.options.roles & {"owner", "admin"}:
            try:
                admins = await self.call(lambda: self.client.get_participants(
                    chat, filter=types.ChannelParticipantsAdmins(), limit=100))
                for user in admins:
                    role = "owner" if isinstance(getattr(user, "participant", None), types.ChannelParticipantCreator) else "admin"
                    if role in self.options.roles:
                        self.add_user(user, Evidence(role, chat.title, link, "Публично доступный список администраторов Telegram"))
            except (errors.RPCError, OSError, asyncio.TimeoutError) as exc:
                self.warn("Список администраторов", exc)
        self.save()
        self.emit("stats", self.stats())

    async def collect_text(self, text, chat, source, is_post=False):
        for candidate in extract_candidates(text, is_post):
            if candidate.role not in self.options.roles:
                continue
            if candidate.username not in self.resolved:
                try:
                    self.resolved[candidate.username] = await self.call(lambda: self.client.get_entity(candidate.username))
                except (ValueError, errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
                    self.resolved[candidate.username] = None
                    self.log(f"@{candidate.username}: ник недоступен, пропущен.")
                except (errors.RPCError, OSError, asyncio.TimeoutError) as exc:
                    self.warn(f"Проверка @{candidate.username}", exc)
                    continue
            user = self.resolved[candidate.username]
            self.add_user(user, Evidence(candidate.role, chat.title, source, candidate.excerpt))

    def add_user(self, user, evidence):
        if not isinstance(user, types.User) or user.deleted or not user.username:
            return
        if user.bot and not self.options.include_bots:
            return
        key = user.id
        if key not in self.contacts:
            self.contacts[key] = Contact(user.username.lower(), user.id, bool(user.bot))
        contact = self.contacts[key]
        contact.username = user.username.lower()
        if evidence not in contact.evidence:
            contact.evidence.append(evidence)
        contact.is_new = self.history.remember(contact, run_id=self.path.stem)
        self.emit("contact", contact)
        self.emit("stats", self.stats())
        # Commit history and persist the current new-only export after every contact.
        self.save()
