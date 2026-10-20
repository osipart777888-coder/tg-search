import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from telethon import errors, types
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.contacts import SearchRequest

from .control import LimitReached, OperationLimiter, SearchPaused, session_lock
from .extraction import ROLE_LABELS, city_matches, extract_candidates, make_queries, public_message_text
from .history import ContactHistory, timestamp
from .storage import PRIVATE, RESULTS, atomic_text, export_txt, prepare_storage, redact


@dataclass
class SearchOptions:
    keywords: str
    roles: set
    city: str = ""
    chat_type: str = "Все"
    max_chats: int = 30
    posts: int = 15
    min_members: int = 0
    check_admins: bool = True
    include_bots: bool = False
    sources: str = ""

    max_new: int = 100
    max_operations: int = 300
    cache_hours: float = 24
    force_refresh: bool = False
    limit_mode: str = "Остановить и сохранить"
    retention_days: int = 30

    def validate(self):
        if not self.roles or not self.roles <= ROLE_LABELS.keys():
            raise ValueError("Выберите, кого искать.")
        if self.chat_type not in {"Все", "Группы", "Каналы"}:
            raise ValueError("Неизвестный тип сообщества.")
        if not 1 <= self.max_chats <= 300 or not 0 <= self.posts <= 100 or self.min_members < 0:
            raise ValueError("Сообщества: 1–300; публикации: 0–100; размер аудитории: от 0.")
        if not 1 <= self.max_new <= 10000 or not 1 <= self.max_operations <= 10000 or not 0 <= self.cache_hours <= 8760:
            raise ValueError("Новые контакты и API-операции: 1–10000; кеш: 0–8760 часов.")
        if self.limit_mode not in {"Остановить и сохранить", "Остановить и показать уведомление"} or not 1 <= self.retention_days <= 3650:
            raise ValueError("Проверьте режим остановки и срок хранения: 1–3650 дней.")
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
    method: str = ""
    checked_at: str = ""
    community: str = ""
    community_id: int = 0

    @property
    def role_label(self):
        return ROLE_LABELS.get(self.role, self.role)

    @property
    def method_label(self):
        return "Telegram API" if self.role in {"owner", "admin"} else "Публичный текст"

    @property
    def confidence(self):
        return "api" if self.role in {"owner", "admin"} else "text"

    @property
    def confidence_label(self):
        return "Подтверждено API" if self.confidence == "api" else "Упоминание, требует оценки"

    @property
    def category(self):
        return {"owner": "Подтверждённый создатель", "admin": "Администратор", "owner_contact": "Предполагаемый владелец"}.get(self.role, "Другой контакт")


@dataclass
class Contact:
    username: str
    user_id: int
    bot: bool = False
    evidence: list = field(default_factory=list)
    is_new: bool = True
    checked_at: str = ""
    verification: str = "verified"

    @property
    def status(self):
        return "Проверка не завершена" if self.verification != "verified" else ("Новый" if self.is_new else "Ранее найден")


def contact_from_dict(row):
    return Contact(row['username'], row['user_id'], row.get('bot', False),
                   [Evidence(**item) for item in row.get('evidence', [])], row.get('is_new', True),
                   row.get('checked_at', ''), row.get('verification', 'verified'))


def filtered_contacts(contacts, role='Все типы', confidence='Любая достоверность', new_only=True, include_unverified=False):
    from dataclasses import replace
    for contact in contacts:
        if (new_only and not contact.is_new) or (not include_unverified and contact.verification != 'verified'):
            continue
        evidence = [e for e in contact.evidence if (role == 'Все типы' or e.category == role)
                    and (confidence == 'Любая достоверность' or
                         (confidence == 'Подтверждено API' and e.confidence == 'api') or
                         (confidence == 'Упоминания в тексте' and e.confidence == 'text'))]
        if evidence:
            yield replace(contact, evidence=evidence)


def recover_latest_report(history):
    """Rebuild interrupted TXT/JSON writes from the authoritative SQLite commit."""
    snapshot = history.latest_checkpoint()
    if not snapshot:
        return
    output = Path(snapshot['file']).resolve()
    if output.parent != RESULTS.resolve():
        raise ValueError('Путь контрольной точки вне папки результатов.')
    report = PRIVATE / 'reports' / (output.stem + '.json')
    if report.exists() and output.exists() and report.stat().st_mtime >= snapshot['updated_at']:
        return
    contacts = [contact_from_dict(row) for row in snapshot['contacts']]
    new = [c for c in contacts if c.is_new and c.verification == 'verified']
    export_txt(output, new)
    counters = snapshot.get('counters', {})
    counts = counters.get('counts', {})
    stats = {'contacts': len(contacts), 'new': len(new), 'known': len(contacts)-len(new),
             'scanned': len(snapshot.get('scanned_ids', [])), 'history_total': history.total(),
             'operations': sum(counts.values()), 'categories': counts, 'api_errors': counters.get('errors', 0)}
    data = {'file': str(output), 'options': snapshot['options'], 'contacts': snapshot['contacts'],
            'stats': stats, 'complete': snapshot['complete'], 'pending': list(snapshot.get('pending', {}).values()),
            'reason': 'Восстановлено из контрольной точки SQLite.'}
    atomic_text(report, json.dumps(data, ensure_ascii=False, indent=2))
    atomic_text(PRIVATE / 'last_search.json', json.dumps({'report': str(report)}, ensure_ascii=False))


class SearchEngine:
    def __init__(self, client, emit, request_delay=2.0):
        self.client, self.emit, self.request_delay = client, emit, request_delay
        self.contacts, self.resolved, self.full_cache, self.source_entities = {}, {}, {}, {}
        self.seen, self.processed, self.stages, self.queries_done, self.sources_done = set(), set(), set(), set(), set()
        self.queue, self.pending = {}, {}
        self.scanned = self.skipped = self.warnings = 0
        self.path, self.history, self.limiter = None, None, None
        self.reason, self.cancelled, self.complete = '', False, False
        self.dirty, self.last_save = set(), time.monotonic()
        self.alias_updates = []
        self.scanned_ids = set()
        self.attempted = set()

    @property
    def new_contacts(self):
        return {key: c for key, c in self.contacts.items() if c.is_new and c.verification == 'verified'}

    def log(self, message):
        self.emit('log', redact(message))

    def ensure_limiter(self):
        if self.limiter is None:
            PRIVATE.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.limiter = OperationLimiter(PRIVATE, self.request_delay, changed=self.operation_changed)
        return self.limiter

    def operation_changed(self):
        if self.history and self.path:
            self.history.reserve_operations(self.path.stem, {'counts': self.limiter.counts, 'errors': self.limiter.errors})
        self.emit('stats', self.stats())

    async def call(self, factory, category='discovery'):
        limiter = self.ensure_limiter()
        if limiter.wrapped:
            return await limiter.call(factory, category)
        with limiter.bind(self.client):
            return await limiter.call(factory, category)

    def warn(self, context, exc):
        self.warnings += 1
        detail = 'Telegram не открыл доступ' if isinstance(exc, (errors.ChatAdminRequiredError, errors.ChannelPrivateError)) else (
            'нет связи или истекло время ожидания' if isinstance(exc, (OSError, asyncio.TimeoutError)) else type(exc).__name__)
        self.log(f'{context}: {detail}.')

    def snapshot(self):
        return {'run_id': self.path.stem, 'file': str(self.path),
                'options': {**asdict(self.options), 'roles': sorted(self.options.roles)},
                'contacts': [asdict(c) for c in self.contacts.values()],
                'seen': sorted(self.seen), 'processed': sorted(self.processed), 'stages': sorted(self.stages),
                'queries_done': sorted(self.queries_done), 'sources_done': sorted(self.sources_done),
                'queue': list(self.queue.values()), 'pending': self.pending,
                'scanned_ids': sorted(self.scanned_ids), 'skipped': self.skipped, 'warnings': self.warnings,
                'complete': self.complete}

    def restore(self, data):
        path = Path(data['file']).resolve()
        if path.parent != RESULTS.resolve():
            raise ValueError('Путь незавершённого поиска вне папки результатов.')
        self.path = path
        self.contacts = {r['user_id']: contact_from_dict(r) for r in data['contacts']}
        for name in ('seen', 'processed', 'stages', 'queries_done', 'sources_done', 'scanned_ids'):
            setattr(self, name, set(data.get(name, [])))
        self.queue = {r['id']: r for r in data.get('queue', [])}
        self.pending = data.get('pending', {})
        self.skipped, self.warnings = data.get('skipped', 0), data.get('warnings', 0)
        self.scanned = len(self.scanned_ids)
        counters = data.get('counters', {})
        self.limiter.counts.update(counters.get('counts', {}))
        self.limiter.errors = counters.get('errors', 0)
        for contact in self.contacts.values():
            self.emit('contact', contact)
        for item in self.pending.values():
            self.emit('pending', item)

    async def run(self, options, resume=None):
        options.validate()
        prepare_storage()
        self.history = ContactHistory(PRIVATE / 'contacts.sqlite3', PRIVATE / 'reports')
        if resume:
            original = SearchOptions(**{**resume['options'], 'roles': set(resume['options']['roles'])})
            # Continued operations belong to the same run; only explicit budget
            # increases can extend an exhausted budget.
            original.max_operations = options.max_operations
            original.max_new = options.max_new
            original.max_chats = options.max_chats
            original.force_refresh = options.force_refresh
            options = original
        self.options = options
        self.ensure_limiter().maximum = options.max_operations
        self.limiter.cooldown.check()
        with session_lock(PRIVATE, 'search.lock'), self.limiter.bind(self.client):
            if resume:
                self.restore(resume)
            else:
                stamp = datetime.now(ZoneInfo('Europe/Moscow')).strftime('%Y-%m-%d_%H-%M-%S_%f')
                self.path = RESULTS / f'Новые_ники_{stamp}.txt'
            queries, sources = options.validate()
            self.log('Поиск публичных контактов. История общая для всех тем; TXT содержит новые аккаунты.')
            self.save()
            try:
                await self.process_queue()
                for name in sources:
                    if name.lower() in self.sources_done:
                        continue
                    if len(self.seen) >= options.max_chats:
                        break
                    try:
                        entity = self.source_entities.get(name.lower())
                        if entity is None:
                            entity = await self.call(lambda: self.client.get_entity(name), 'discovery')
                        self.enqueue(entity)
                        self.sources_done.add(name.lower())
                        self.save()
                        await self.process_queue()
                    except (errors.RPCError, ValueError, OSError, asyncio.TimeoutError) as exc:
                        self.warn(f'Сообщество @{name}', exc)
                        # Do not advance a failed source; it can be resumed.
                for keyword in queries:
                    if keyword in self.queries_done or len(self.seen) >= options.max_chats:
                        continue
                    self.log(f'Поиск сообществ: {keyword}')
                    try:
                        result = await self.call(lambda: self.client(SearchRequest(q=keyword, limit=100)), 'discovery')
                    except (errors.RPCError, OSError, asyncio.TimeoutError) as exc:
                        self.warn(f'Запрос «{keyword}»', exc)
                        continue
                    for chat in result.chats:
                        self.enqueue(chat)
                    self.queries_done.add(keyword)
                    self.save()  # Persist discovery results before processing any chat.
                    await self.process_queue()
                exhausted = len(self.seen) >= options.max_chats
                self.complete = (exhausted or (set(queries) <= self.queries_done and {s.lower() for s in sources} <= self.sources_done)) and not self.pending and self.seen <= self.processed
                if not self.complete:
                    self.reason = 'Проверка не завершена. Сохранена контрольная точка; можно продолжить поиск.'
            except asyncio.CancelledError:
                self.cancelled = True
                self.log('Поиск остановлен. Результаты и контрольная точка сохранены.')
            except SearchPaused as exc:
                self.reason = str(exc)
                self.log(self.reason)
            except Exception:
                self.reason = 'Поиск прерван из-за внутренней ошибки. Результаты и контрольная точка сохранены.'
                raise
            finally:
                self.save()
                self.emit('stats', self.stats())
        self.log(f'Сохранено: найдено {len(self.contacts)}, новых {len(self.new_contacts)}. {self.path.name}')
        return self

    def enqueue(self, chat):
        if isinstance(chat, types.Channel) and chat.username:
            self.queue[chat.id] = {'id': chat.id, 'username': chat.username, 'title': chat.title,
                                   'access_hash': getattr(chat, 'access_hash', 0) or 0,
                                   'broadcast': bool(chat.broadcast), 'megagroup': bool(chat.megagroup)}

    async def process_queue(self):
        for row in list(self.queue.values()):
            if row['id'] in self.processed or row['id'] in self.attempted:
                continue
            if row['id'] not in self.seen and len(self.seen) >= self.options.max_chats:
                break
            chat = types.Channel(id=row['id'], title=row['title'], username=row['username'],
                                 access_hash=row['access_hash'], broadcast=row['broadcast'],
                                 megagroup=row['megagroup'], photo=types.ChatPhotoEmpty(), date=None)
            self.attempted.add(chat.id)
            await self.visit(chat)

    def stats(self):
        limiter = self.limiter
        return {'contacts': len(self.contacts), 'new': len(self.new_contacts),
                'known': len(self.contacts) - len(self.new_contacts),
                'history_total': self.history.total() if self.history else 0,
                'scanned': self.scanned, 'seen': len(self.seen), 'skipped': self.skipped,
                'warnings': self.warnings, 'operations': limiter.total if limiter else 0,
                'categories': dict(limiter.counts) if limiter else {}, 'api_errors': limiter.errors if limiter else 0,
                'cooldown_until': limiter.cooldown.deadline() if limiter else 0, 'pending': len(self.pending)}

    def save(self):
        if self.path is None:
            return
        # One transaction commits both the batch and its checkpoint. A crash cannot
        # remember a contact without preserving its original per-run new status.
        changes = self.alias_updates + [self.contacts[key] for key in sorted(self.dirty, key=lambda k: self.contacts[k].checked_at)]
        self.history.remember_many(changes, run_id=self.path.stem, snapshot=self.snapshot())
        self.history.reserve_operations(self.path.stem, {'counts': self.limiter.counts, 'errors': self.limiter.errors})
        self.dirty.clear()
        self.alias_updates.clear()
        self.last_save = time.monotonic()
        export_txt(self.path, self.new_contacts.values())
        data = {'created_timezone': 'Europe/Moscow', 'file': str(self.path),
                'options': {**asdict(self.options), 'roles': sorted(self.options.roles)},
                'stats': self.stats(), 'cancelled': self.cancelled, 'reason': self.reason,
                'complete': self.complete, 'contacts': [asdict(c) for c in self.contacts.values()],
                'pending': list(self.pending.values())}
        reports = PRIVATE / 'reports'
        reports.mkdir(mode=0o700, exist_ok=True)
        report_path = reports / (self.path.stem + '.json')
        atomic_text(report_path, json.dumps(data, ensure_ascii=False, indent=2))
        atomic_text(PRIVATE / 'last_search.json', json.dumps({'report': str(report_path)}, ensure_ascii=False))

    async def stage(self, chat, name, action):
        key = f'{chat.id}:{name}'
        if key in self.stages:
            return True
        try:
            await action()
        except (errors.ChatAdminRequiredError, errors.ChannelPrivateError) as exc:
            self.warn(name, exc)
            # An explicit access denial is a completed attempt. A new search can
            # check access again; resuming must not loop on a closed admin list.
            self.stages.add(key)
            self.save()
            return True
        except (errors.RPCError, OSError, asyncio.TimeoutError) as exc:
            self.warn(name, exc)
            self.save()
            return False
        self.stages.add(key)
        self.save()
        return True

    async def visit(self, chat):
        if not isinstance(chat, types.Channel) or not chat.username or chat.id in self.processed:
            return
        if self.options.chat_type == 'Группы' and chat.broadcast or self.options.chat_type == 'Каналы' and not chat.broadcast:
            self.processed.add(chat.id)
            return
        self.seen.add(chat.id)
        self.log(f'Проверка: {chat.title} (@{chat.username})')
        self.save()
        metadata = None if self.options.force_refresh else self.history.cached_community(chat.id, self.options.cache_hours)
        if metadata is None:
            full = self.full_cache.get(chat.id)
            try:
                if full is None:
                    full = await self.call(lambda: self.client(GetFullChannelRequest(chat)), 'descriptions')
            except (errors.RPCError, OSError, asyncio.TimeoutError) as exc:
                self.warn('Описание', exc)
                return
            fc = full.full_chat
            metadata = {'about': getattr(fc, 'about', '') or '', 'count': getattr(fc, 'participants_count', None),
                        'pinned_id': getattr(fc, 'pinned_msg_id', None), 'title': chat.title, 'username': chat.username}
            self.history.cache_community(chat.id, chat.username, metadata)
        else:
            self.log('Описание из кеша; публикации и доступные администраторы проверяются заново.')
        about, count = metadata['about'], metadata['count']
        if not city_matches(self.options.city, chat.title + ' ' + about) or self.options.min_members and (count is None or count < self.options.min_members):
            self.skipped += 1
            self.processed.add(chat.id)
            self.save()
            return
        self.scanned_ids.add(chat.id)
        self.scanned = len(self.scanned_ids)
        link = f'https://t.me/{chat.username}'
        success = await self.stage(chat, 'description', lambda: self.collect_text(about, chat, link))
        pinned_id = metadata['pinned_id']
        if pinned_id:
            async def pinned():
                message = await self.call(lambda: self.client.get_messages(chat, ids=pinned_id), 'publications')
                if message:
                    await self.collect_text(public_message_text(message), chat, f'{link}/{message.id}', True)
            success = await self.stage(chat, 'pinned', pinned) and success
        if self.options.posts:
            async def posts():
                messages = await self.call(lambda: self.client.get_messages(chat, limit=self.options.posts), 'publications')
                for message in messages:
                    await self.collect_text(public_message_text(message), chat, f'{link}/{message.id}', True)
            success = await self.stage(chat, 'posts', posts) and success
        if self.options.check_admins and self.options.roles & {'owner', 'admin'}:
            async def admins():
                users = await self.call(lambda: self.client.get_participants(chat, filter=types.ChannelParticipantsAdmins(), limit=100), 'admins')
                for user in users:
                    role = 'owner' if isinstance(getattr(user, 'participant', None), types.ChannelParticipantCreator) else 'admin'
                    if role in self.options.roles:
                        self.add_user(user, Evidence(role, chat.title, link, 'Доступные сведения Telegram об администраторах',
                                                    'api', timestamp(), link, chat.id))
            success = await self.stage(chat, 'admins', admins) and success
        if success:
            self.processed.add(chat.id)
        self.save()
        self.emit('stats', self.stats())

    async def collect_text(self, text, chat, source, is_post=False):
        failed = False
        for candidate in extract_candidates(text, is_post):
            if candidate.role not in self.options.roles:
                continue
            name = candidate.username
            if name not in self.resolved:
                try:
                    self.resolved[name] = await self.call(lambda: self.client.get_entity(name), 'users')
                except (ValueError, errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
                    self.resolved[name] = None
                    self.log(f'@{name}: ник недоступен, пропущен.')
                except (errors.RPCError, OSError, asyncio.TimeoutError) as exc:
                    failed = True
                    self.pending[name] = {'username': name, 'source': source, 'title': chat.title, 'status': 'Проверка не завершена'}
                    self.emit('pending', self.pending[name])
                    self.warn(f'Проверка @{name}', exc)
                    continue
            self.pending.pop(name, None)
            user = self.resolved[name]
            self.add_user(user, Evidence(candidate.role, chat.title, source, redact(candidate.excerpt),
                                         'text', timestamp(), f'https://t.me/{chat.username}', chat.id))
        if failed:
            raise OSError('Some public contacts could not be verified')

    def add_user(self, user, evidence):
        if not isinstance(user, types.User) or user.deleted or not user.username or user.bot and not self.options.include_bots:
            return
        key = user.id
        if key not in self.contacts:
            is_new = self.history.classify(key, self.path.stem)
            if is_new and len(self.new_contacts) >= self.options.max_new:
                raise LimitReached('Достигнут лимит новых контактов. Для продолжения увеличьте лимит в настройках.')
            self.contacts[key] = Contact(user.username.lower(), key, bool(user.bot), is_new=is_new)
        contact = self.contacts[key]
        if contact.username != user.username.lower():
            self.alias_updates.append(asdict(contact))
        for previous in self.contacts.values():
            if previous.user_id != key and previous.username == user.username.lower():
                previous.verification = 'incomplete'
                self.dirty.add(previous.user_id)
                self.emit('contact', previous)
        contact.username, contact.checked_at, contact.verification = user.username.lower(), timestamp(), 'verified'
        contact.evidence = [e for e in contact.evidence if (e.role, e.source, e.excerpt) != (evidence.role, evidence.source, evidence.excerpt)]
        contact.evidence.append(evidence)
        self.dirty.add(key)
        self.emit('contact', contact)
        self.emit('stats', self.stats())
        if len(self.dirty) >= 10 or time.monotonic() - self.last_save >= 5:
            self.save()
