"""Run a read-only audience-ranked legal community search with the local session."""
import asyncio
import json
import re
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

from telethon import errors, types
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.contacts import SearchRequest

from tgsearch.backend import Backend
from tgsearch.engine import SearchEngine, SearchOptions, SearchPaused
from tgsearch.extraction import ROLE_FILTERS
from tgsearch.storage import PRIVATE, RESULTS, atomic_text, export_csv, prepare_storage

QUERIES = ["юристы", "юрист", "юридическая", "адвокат", "адвокаты", "судебная практика", "юридическая помощь", "юридический", "банкротство", "арбитраж"]
SEEDS = ["sivakovairina", "LawAndFinance", "advokatzhorin", "oooavirta", "vs_court", "akaloy",
         "Gordonekaterina", "povorotnapravo", "sudpraktik", "sovetiadvokata", "consultant_plus",
         "zemelniyvopros", "bezdelkaBuh", "pravonadom1", "bchlaw", "voennik365", "PeopleIsLawyer",
         "zemelnoe_pravo", "zaconodatel", "doktorprava", "prv_adv", "furinlaw", "yardrey", "vsrf_ru", "pravo_ru"]
LEGAL = re.compile(r"юрист|юридич|адвокат|юриспруд|судебн|\bсуд\b|\bсуда\b|\bсудов\b|правов|\bправо\b|закон|банкрот|нотари|арбитражн(?:ый|ая|ое|ые|ого|ых|ому|ым|ыми)|правообладател", re.I)
FOREIGN = re.compile(r"[іїєґўқғңұүөҳҷӣӯ]", re.I)
# Reviewed on 19 October 2026: advertising/crypto arbitrage and a crafts/travel
# creator whose name contains 'Адвокат' do not fit a legal professional shortlist.
EXCLUDED_NAMES = {"eldarmamaev", "biznes_leads", "arbitrazha", "thepartnerkin", "arbitrazh6", "advocategorov"}
STAMP = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d_%H-%M")
PREFIX = f"Юристы_ТОП30_{STAMP}"


def write_progress(stage, **extra):
    atomic_text(PRIVATE / "legal_progress.json", json.dumps({"stage": stage, **extra}, ensure_ascii=False, indent=2))


def emit(kind, value):
    if kind == "log":
        print(value, flush=True)
    elif kind == "stats":
        write_progress("contacts", **value)


def legal_relevant(title, about):
    text = title + " " + about
    return bool(LEGAL.search(text)) and len(FOREIGN.findall(text)) <= 2


def ranked_rows(rows, limit=30):
    return sorted((r for r in rows if r.get("participants", 0) >= 1000 and r["username"].lower() not in EXCLUDED_NAMES),
                  key=lambda row: (-row["participants"], row["username"].lower()))[:limit]


async def main():
    prepare_storage()
    backend = Backend.__new__(Backend)
    backend.client = None
    try:
        client = await backend.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Сначала завершите вход в программе.")
        engine = SearchEngine(client, emit)
        candidates = {}
        origins = {}

        def add(chat, origin):
            if isinstance(chat, types.Channel) and chat.username and not chat.scam and not chat.fake:
                candidates[chat.id] = chat
                origins.setdefault(chat.id, set()).add(origin)

        for keyword in QUERIES:
            result = await engine.call(lambda: client(SearchRequest(q=keyword, limit=100)))
            for chat in result.chats:
                add(chat, keyword)
            print(f"Обнаружение: {keyword}: всего {len(candidates)}", flush=True)
            write_progress("discovery", keyword=keyword, candidates=len(candidates))
        # A public directory adds established channels missed by substring search.
        for seed in SEEDS:
            try:
                add(await engine.call(lambda: client.get_entity(seed)), "каталог / подборка")
            except (ValueError, errors.RPCError) as exc:
                engine.warn(f"Источник @{seed}", exc)
        rows = []
        chats = sorted(candidates.values(), key=lambda c: (
            "каталог / подборка" not in origins[c.id], -(getattr(c, "participants_count", None) or 0)))
        cache = {}
        for index, chat in enumerate(chats[:160], 1):
            try:
                full = await engine.call(lambda: client(GetFullChannelRequest(chat)), 'descriptions')
            except (errors.RPCError, OSError, asyncio.TimeoutError) as exc:
                engine.warn(f"Аудитория @{chat.username}", exc)
                continue
            about = getattr(full.full_chat, "about", "") or ""
            if not legal_relevant(chat.title, about):
                continue
            count = getattr(full.full_chat, "participants_count", None)
            if count is None or count < 1000:
                continue
            cache[chat.id] = full
            rows.append({"id": chat.id, "username": chat.username, "title": chat.title,
                         "type": "Канал" if chat.broadcast else "Группа", "participants": count,
                         "about": about, "link": f"https://t.me/{chat.username}",
                         "origins": sorted(origins[chat.id])})
            atomic_text(PRIVATE / "legal_candidates.json", json.dumps(rows, ensure_ascii=False, indent=2))
            write_progress("audience", checked=index, total=min(160, len(chats)), relevant=len(rows))
            print(f"Аудитория {index}/{min(160, len(chats))}: @{chat.username}: {count}", flush=True)
        selected = ranked_rows(rows)
        if len(selected) < 30:
            raise RuntimeError(f"Найдено только {len(selected)} подходящих сообществ; требуется 30.")
        engine.full_cache = cache
        engine.source_entities = {row["username"].lower(): candidates[row["id"]] for row in selected}
        role_label = "Владельцы, администраторы и реклама"
        options = SearchOptions("", set(ROLE_FILTERS[role_label]), max_chats=30, posts=15,
                                min_members=1000, check_admins=True, include_bots=False,
                                sources=", ".join(row["username"] for row in selected))
        settings = {"topic": "Юристы", "keywords": "", "city": "", "roles": role_label,
                    "chat_type": "Все", "max_chats": "30", "posts": "15", "min_members": "1000",
                    "check_admins": True, "include_bots": False, "sources": options.sources}
        atomic_text(PRIVATE / "search_settings.json", json.dumps(settings, ensure_ascii=False, indent=2))
        # Save the ranking before looking for contacts, so the main result survives limits.
        def save_ranking():
            lines = ["ТОП-30 ЮРИДИЧЕСКИХ ГРУПП И КАНАЛОВ TELEGRAM", f"Проверено: {datetime.now(ZoneInfo('Europe/Moscow')):%d.%m.%Y %H:%M} МСК",
                     "Порядок: по числу участников / подписчиков, полученному из Telegram API.",
                     f"Найдено кандидатов: {len(candidates)}; проверено до 160; подходят по теме и аудитории: {len(rows)}.",
                     "Это топ среди найденных сообществ; размер аудитории не подтверждает её качество.",
                     "Поиск русскоязычный, без привязки к городу. Минимум: 1000 участников.",
                     "Дополнительные кандидаты: https://livedune.com/ru/ratings/telegram/category/pravo/", ""]
            for rank, row in enumerate(selected, 1):
                lines.extend([f"{rank:02}. {row['title']}", f"    {row['type']} · {row['participants']:,} участников / подписчиков".replace(",", " "),
                              f"    @{row['username']} · {row['link']}"])
                hits = [(c, e) for c in engine.contacts.values() for e in c.evidence if e.source == row["link"] or e.source.startswith(row["link"] + "/")]
                if hits:
                    seen = set()
                    for contact, evidence in hits:
                        key = (contact.username, evidence.role)
                        if key in seen:
                            continue
                        seen.add(key)
                        lines.append(f"    {evidence.role_label}: @{contact.username} — {evidence.source}")
                else:
                    lines.append("    Открытый контакт владельца / рекламы не найден или ещё не проверен.")
                lines.append("")
            atomic_text(RESULTS / (PREFIX + ".txt"), "\n".join(lines), "utf-8-sig")
            atomic_text(RESULTS / (PREFIX + "_ники_сообществ.txt"), "\n".join('@' + row['username'] for row in selected) + "\n", "utf-8-sig")
            atomic_text(PRIVATE / "legal_top30.json", json.dumps(selected, ensure_ascii=False, indent=2))
        save_ranking()
        await engine.run(options)
        save_ranking()
        from tgsearch.storage import export_txt
        export_txt(RESULTS / (PREFIX + "_контакты.txt"), engine.new_contacts.values())
        export_csv(RESULTS / (PREFIX + "_источники.csv"), engine.new_contacts.values())
        report = PRIVATE / "reports" / (engine.path.stem + ".json")
        atomic_text(PRIVATE / "last_search.json", json.dumps({"report": str(report)}, ensure_ascii=False))
        write_progress("done", selected=len(selected), contacts=len(engine.contacts), new_contacts=len(engine.new_contacts), report=str(RESULTS / (PREFIX + ".txt")), contact_file=str(RESULTS / (PREFIX + "_контакты.txt")))
        print(json.dumps({"selected": len(selected), "contacts": len(engine.contacts), "new_contacts": len(engine.new_contacts), "file": str(RESULTS / (PREFIX + ".txt"))}, ensure_ascii=False), flush=True)
    finally:
        await backend.disconnect()


if __name__ == "__main__":
    prepare_storage()
    import fcntl
    lock = (PRIVATE / "app.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    asyncio.run(main())
