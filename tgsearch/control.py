"""One persistent cooldown and one RPC gate for every search using this session."""
import asyncio
import fcntl
import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from telethon import errors, TelegramClient

from .storage import atomic_text

CATEGORIES = ('discovery', 'descriptions', 'publications', 'users', 'admins')
category = ContextVar('search_operation_category', default='discovery')


class SearchPaused(Exception):
    pass


class LimitReached(SearchPaused):
    pass


class Cooldown:
    def __init__(self, directory):
        self.path = Path(directory) / 'cooldown.json'

    def deadline(self):
        try:
            return float(json.loads(self.path.read_text())['until'])
        except FileNotFoundError:
            return 0
        except (ValueError, KeyError, TypeError):
            raise ValueError('Не удалось прочитать срок ограничения Telegram. Сохраните cooldown.json и проверьте его.') from None

    def remaining(self):
        return max(0, int(self.deadline() - time.time() + 0.999))

    def check(self):
        remaining = self.remaining()
        if remaining:
            raise SearchPaused(f'Telegram ограничил запросы. Осталось {remaining} сек. Найденное сохранено.')

    def impose(self, seconds):
        until = max(self.deadline(), time.time() + max(1, seconds))
        atomic_text(self.path, json.dumps({'until': until}))
        return until


@contextmanager
def session_lock(directory, name='session.lock'):
    path = Path(directory) / name
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open('a+') as lock:
        path.chmod(0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Сессия уже используется другой копией программы. Закройте её и повторите.') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class OperationLimiter:
    def __init__(self, directory, delay=2, maximum=300, changed=lambda: None):
        self.cooldown = Cooldown(directory)
        self.delay, self.maximum, self.changed = delay, maximum, changed
        self.counts = dict.fromkeys(CATEGORIES, 0)
        self.errors = 0
        self.last_call = 0
        self.gate = asyncio.Lock()
        self.wrapped = False

    @property
    def total(self):
        return sum(self.counts.values())

    async def rpc(self, factory, kind):
        async with self.gate:
            self.cooldown.check()
            if self.total >= self.maximum:
                raise LimitReached('Достигнут лимит API-операций. Результаты и контрольная точка сохранены.')
            await asyncio.sleep(max(0, self.delay - (time.monotonic() - self.last_call)))
            self.cooldown.check()
            self.last_call = time.monotonic()
            self.counts[kind] += 1
            self.changed()  # Durable operation reservation before sending the request.
            try:
                return await asyncio.wait_for(factory(), timeout=40)
            except (errors.FloodWaitError, errors.FloodPremiumWaitError, errors.SlowModeWaitError) as exc:
                self.errors += 1
                self.cooldown.impose(exc.seconds)
                self.changed()
                raise SearchPaused(f'Telegram ограничил запросы на {exc.seconds} сек. Поиск остановлен; результаты сохранены.') from None
            except (errors.RPCError, OSError, asyncio.TimeoutError):
                self.errors += 1
                self.changed()
                raise

    @contextmanager
    def bind(self, client):
        # Count actual Telethon RPCs, including calls hidden inside helpers such as
        # get_entity/get_participants. Disabled outside the explicit search scope.
        if not isinstance(client, TelegramClient):
            yield
            return
        original = client._call
        async def counted(sender, request, ordered=False, flood_sleep_threshold=None):
            if isinstance(request, (list, tuple)):
                return [await counted(sender, item, ordered=ordered, flood_sleep_threshold=0) for item in request]
            # request.resolve() can itself call client; resolve BEFORE taking gate.
            from telethon import utils
            await request.resolve(client, utils)
            return await self.rpc(lambda: original(sender, request, ordered=ordered, flood_sleep_threshold=0), category.get())
        client._call = counted
        self.wrapped = True
        try:
            yield
        finally:
            client._call = original
            self.wrapped = False

    async def call(self, factory, kind='discovery'):
        token = category.set(kind)
        try:
            if self.wrapped:
                self.cooldown.check()
                return await asyncio.wait_for(factory(), timeout=40)
            return await self.rpc(factory, kind)
        finally:
            category.reset(token)
