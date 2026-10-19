"""One dedicated asyncio loop; the Tk thread only receives queue events."""
import asyncio
import json
import queue
import re
import threading
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from telethon import TelegramClient, errors

from .storage import PRIVATE, load_config, save_config


def friendly_error(exc):
    messages = {
        errors.ApiIdInvalidError: "Telegram отклонил API ID / API Hash. Проверьте их в настройках.",
        errors.ApiIdPublishedFloodError: "Telegram ограничил использование этих общедоступных API-настроек. Нужны ваши собственные API ID и API Hash из my.telegram.org → API development tools. Введите их внизу вкладки входа, затем запросите код снова.",
        errors.AuthRestartError: "Telegram попросил повторить вход. Подождите немного и нажмите «Получить код» ещё раз.",
        errors.AuthKeyDuplicatedError: "Telegram отклонил эту сессию: она используется другим подключением. Закройте остальные копии программы и войдите снова.",
        errors.SendCodeUnavailableError: "Telegram сейчас не может отправить код этим способом. Подождите и повторите запрос кода.",
        errors.PhoneNumberFloodError: "Telegram временно ограничил отправку кодов на этот номер. Подождите перед следующим запросом.",
        errors.PhoneNumberAppSignupForbiddenError: "Для этого номера Telegram запрещает регистрацию через стороннее приложение. Сначала войдите или зарегистрируйтесь в официальном Telegram.",
        errors.PhoneNumberInvalidError: "Проверьте номер телефона: нужен международный формат, например +79991234567.",
        errors.PhoneNumberBannedError: "Telegram не разрешает вход для этого номера.",
        errors.PhoneCodeInvalidError: "Неверный код. Введите код из Telegram ещё раз.",
        errors.PhoneCodeEmptyError: "Введите код, который прислал Telegram.",
        errors.PhoneCodeExpiredError: "Срок кода истёк. Запросите новый код.",
        errors.PasswordHashInvalidError: "Неверный пароль двухэтапной проверки.",
        errors.AuthKeyUnregisteredError: "Сессия Telegram завершена. Войдите снова.",
        errors.SessionRevokedError: "Сессия отозвана. Перезапустите программу и войдите снова.",
    }
    for kind, message in messages.items():
        if isinstance(exc, kind):
            return message
    if isinstance(exc, errors.FloodWaitError):
        return f"Telegram временно ограничил вход. Попробуйте через {exc.seconds} сек."
    if isinstance(exc, (OSError, asyncio.TimeoutError)):
        return "Не удалось подключиться к Telegram. Проверьте интернет и доступ к Telegram."
    if isinstance(exc, ValueError):
        return str(exc)
    return f"Не удалось выполнить действие ({type(exc).__name__}). Название ошибки сохранено в локальном журнале. Можно повторить действие; если ошибка повторится, сообщите её название в скобках."


def _write_diagnostic(record):
    record = {"time": datetime.now(ZoneInfo("Europe/Moscow")).isoformat(), **record}
    try:
        PRIVATE.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = PRIVATE / "diagnostics.jsonl"
        with path.open("a", encoding="utf-8") as out:
            path.chmod(0o600)
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def record_error(action, exc):
    # Never record exception messages, arguments, local variables, credentials or codes.
    from pathlib import Path
    record = {"action": action, "error": type(exc).__name__,
              "frames": [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                         for frame in traceback.extract_tb(exc.__traceback__)]}
    _write_diagnostic(record)


def code_delivery_message(sent):
    kind = type(sent.type).__name__
    messages = {
        "SentCodeTypeApp": "Telegram сообщил, что отправил код в служебный чат «Telegram» на устройствах, где вы уже вошли. Проверьте этот чат на телефоне; SMS не ожидается.",
        "SentCodeTypeSms": "Telegram сообщил, что отправил код по SMS на указанный номер.",
        "SentCodeTypeSmsWord": "Telegram отправил SMS со словом-кодом. Введите слово целиком.",
        "SentCodeTypeSmsPhrase": "Telegram отправил SMS с фразой-кодом. Введите фразу целиком.",
        "SentCodeTypeCall": "Telegram сообщил, что позвонит на указанный номер. Прослушайте и введите код.",
        "SentCodeTypeMissedCall": "Ожидается короткий звонок. Код — последние цифры номера звонящего.",
        "SentCodeTypeEmailCode": "Telegram сообщил, что отправил код на вашу почту для входа. Проверьте входящие и спам.",
        "SentCodeTypeSetUpEmailRequired": "Telegram требует настроить почту для входа. Используйте вход по QR или настройте почту в официальном Telegram.",
        "SentCodeTypeFirebaseSms": "Telegram выбрал способ SMS для официального мобильного приложения. Используйте вход по QR.",
        "SentCodeTypeFragmentSms": "Код доступен для вашего номера Fragment. Можно вместо него использовать вход по QR.",
    }
    return messages.get(kind, "Telegram принял запрос кода, но выбрал неподдерживаемый способ доставки. Используйте вход по QR.")


class Backend:
    def __init__(self):
        self.events = queue.Queue()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.client = None
        self.phone = ""
        self.code_hash = None
        self.search_task = None
        self.engine = None
        self.qr_task = None
        self.resend_after = 0.0

    def emit(self, kind, data):
        self.events.put((kind, data))

    def submit(self, coro, action):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def done(result):
            try:
                self.emit(action, result.result())
            except Exception as exc:
                record_error(action, exc)
                self.emit("error", (action, friendly_error(exc)))

        future.add_done_callback(done)

    async def connect(self):
        if self.client is None:
            config = load_config()
            api_id = str(config.get("api_id", ""))
            api_hash = config.get("api_hash", "")
            if not api_id.isdigit() or int(api_id) < 1 or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash):
                raise ValueError("Укажите действительные API ID и API Hash на вкладке входа.")
            self.client = TelegramClient(str(PRIVATE / "telegram"), int(api_id), api_hash,
                                         flood_sleep_threshold=0, request_retries=1,
                                         connection_retries=2, retry_delay=1, timeout=15,
                                         device_model="Telegram Contacts Desktop")
        if not self.client.is_connected():
            await asyncio.wait_for(self.client.connect(), timeout=30)
        return self.client

    async def restore(self):
        client = await self.connect()
        return await self.identity() if await client.is_user_authorized() else None

    async def identity(self):
        me = await asyncio.wait_for(self.client.get_me(), timeout=25)
        if me is None:
            raise ValueError("Вход не завершён. Запросите код ещё раз.")
        return f"@{me.username}" if me.username else (me.first_name or "Аккаунт подключён")

    async def request_code(self, phone, api_id, api_hash):
        phone = re.sub(r"[\s()\-]", "", phone)
        if not re.fullmatch(r"\+\d{7,15}", phone):
            raise ValueError("Введите свой номер в международном формате: +79991234567.")
        if phone == self.phone and time.monotonic() < getattr(self, "resend_after", 0):
            remaining = int(self.resend_after - time.monotonic()) + 1
            raise ValueError(f"Подождите ещё {remaining} сек. перед повторным запросом. Если код не приходит, используйте вход по QR.")
        client = await self.configure_client(api_id, api_hash, phone)
        if await client.is_user_authorized():
            return {"identity": await self.identity()}
        sent = await asyncio.wait_for(client.send_code_request(phone), timeout=40)
        self.phone = phone
        self.code_hash = sent.phone_code_hash
        self.resend_after = time.monotonic() + max(30, getattr(sent, "timeout", None) or 60)
        self.code_is_text = type(sent.type).__name__ in {"SentCodeTypeSmsWord", "SentCodeTypeSmsPhrase"}
        _write_diagnostic({"action": "code_delivery", "code_type": type(sent.type).__name__,
                           "next_type": type(getattr(sent, "next_type", None)).__name__,
                           "timeout": getattr(sent, "timeout", None)})
        return {"identity": None, "delivery_message": code_delivery_message(sent)}

    async def configure_client(self, api_id, api_hash, phone=None):
        if not api_id.isdigit() or int(api_id) < 1 or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash):
            raise ValueError("Проверьте API ID и API Hash.")
        config = load_config()
        changed = config.get("api_id") != int(api_id) or config.get("api_hash") != api_hash
        if changed and self.client:
            await self.client.disconnect()
            self.client = None
        config.update(api_id=int(api_id), api_hash=api_hash)
        if phone is not None:
            config["phone"] = phone
        save_config(config)
        return await self.connect()

    async def qr_login(self, api_id, api_hash):
        self.qr_task = asyncio.current_task()
        try:
            client = await self.configure_client(api_id, api_hash)
            if await client.is_user_authorized():
                return {"identity": await self.identity()}
            qr = await asyncio.wait_for(client.qr_login(), timeout=30)
            for attempt in range(12):
                self.emit("qr_code", {"url": qr.url, "expires": qr.expires.timestamp()})
                try:
                    await asyncio.wait_for(qr.wait(), timeout=65)
                except asyncio.TimeoutError:
                    if attempt == 11:
                        break
                    await asyncio.wait_for(qr.recreate(), timeout=30)
                    continue
                except errors.SessionPasswordNeededError:
                    return {"password_required": True}
                self.code_hash = None
                return {"identity": await self.identity()}
            return {"expired": True}
        except asyncio.CancelledError:
            return {"cancelled": True}
        finally:
            self.qr_task = None

    def cancel_qr(self):
        def cancel():
            if self.qr_task and not self.qr_task.done():
                self.qr_task.cancel()
        self.loop.call_soon_threadsafe(cancel)

    async def sign_in(self, code):
        if not self.code_hash:
            raise ValueError("Сначала запросите код входа.")
        if not getattr(self, "code_is_text", False) and not re.fullmatch(r"\d{4,8}", code.replace(" ", "")):
            raise ValueError("Введите цифровой код входа из Telegram.")
        clean_code = code.strip() if getattr(self, "code_is_text", False) else code.replace(" ", "")
        if not clean_code or len(clean_code) > 256:
            raise ValueError("Введите код, который прислал Telegram.")
        try:
            await asyncio.wait_for(self.client.sign_in(self.phone, clean_code,
                                                       phone_code_hash=self.code_hash), timeout=40)
        except errors.SessionPasswordNeededError:
            return {"password_required": True}
        return {"identity": await self.identity()}

    async def password_sign_in(self, password):
        await asyncio.wait_for(self.client.sign_in(password=password), timeout=40)
        return {"identity": await self.identity()}

    async def search(self, options):
        from .engine import SearchEngine
        self.engine = None
        self.search_task = asyncio.current_task()
        try:
            client = await self.connect()
            if not await client.is_user_authorized():
                raise ValueError("Сначала войдите в Telegram на вкладке «Вход в Telegram».")
            self.engine = SearchEngine(client, self.emit)
            return await self.engine.run(options)
        except asyncio.CancelledError:
            return None
        finally:
            self.search_task = None

    def stop_search(self):
        def cancel():
            if self.search_task and not self.search_task.done():
                self.search_task.cancel()
        self.loop.call_soon_threadsafe(cancel)

    async def disconnect(self):
        if self.client:
            await self.client.disconnect()

    async def shutdown(self):
        current = asyncio.current_task()
        pending = [task for task in asyncio.all_tasks() if task is not current]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await self.disconnect()

    async def logout(self):
        if self.client:
            await self.client.log_out()
            self.client = None
        self.code_hash = None
        return None
