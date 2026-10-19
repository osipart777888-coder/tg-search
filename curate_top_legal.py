"""Finish human-reviewed legal relevance and merge contacts for the final top 30."""
import asyncio
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from find_top_legal import emit, legal_relevant, ranked_rows, write_progress
from tgsearch.backend import Backend
from tgsearch.engine import Contact, Evidence, SearchEngine, SearchOptions
from tgsearch.extraction import ROLE_FILTERS
from tgsearch.history import ContactHistory
from tgsearch.storage import PRIVATE, RESULTS, atomic_text, export_csv, export_txt

# Public descriptions and posts show these are advertising/crypto arbitrage or a
# crafts/travel video blog. The word 'арбитраж' or a creator's name is not enough.
EXCLUDED = {"eldarmamaev", "biznes_leads", "arbitrazha", "thepartnerkin", "arbitrazh6", "advocategorov"}


async def main():
    pointer = json.loads((PRIVATE / "last_search.json").read_text())
    report_path = Path(pointer["report"])
    report = json.loads(report_path.read_text())
    rows = json.loads((PRIVATE / "legal_candidates.json").read_text())
    eligible = [row for row in rows if row["username"].lower() not in EXCLUDED and legal_relevant(row["title"], row["about"])]
    selected = ranked_rows(eligible)
    if len(selected) != 30:
        raise RuntimeError("Не удалось собрать 30 тематических сообществ.")
    previous = {row["username"].lower() for row in json.loads((PRIVATE / "legal_top30.json").read_text())}
    missing = [row for row in selected if row["username"].lower() not in previous]
    roots = [row["link"] for row in selected]
    merged = {}

    def add(contact):
        evidence = [item for item in contact.evidence if any(item.source == root or item.source.startswith(root + "/") for root in roots)]
        if not evidence:
            return
        if contact.user_id not in merged:
            merged[contact.user_id] = Contact(contact.username, contact.user_id, contact.bot, [], contact.is_new)
        merged[contact.user_id].is_new = merged[contact.user_id].is_new or contact.is_new
        for item in evidence:
            if item not in merged[contact.user_id].evidence:
                merged[contact.user_id].evidence.append(item)

    for row in report["contacts"]:
        add(Contact(row["username"], row["user_id"], row.get("bot", False), [Evidence(**e) for e in row["evidence"]], row.get("is_new", True)))
    backend = Backend.__new__(Backend)
    backend.client = None
    extra_report = None
    warnings = report["stats"].get("warnings", 0)
    try:
        if missing:
            client = await backend.connect()
            if not await client.is_user_authorized():
                raise RuntimeError("Сессия Telegram завершена.")
            engine = SearchEngine(client, emit)
            options = SearchOptions("", set(ROLE_FILTERS["Владельцы, администраторы и реклама"]),
                                    max_chats=len(missing), posts=15, min_members=1000,
                                    sources=", ".join(row["username"] for row in missing))
            await engine.run(options)
            if engine.reason or engine.cancelled:
                raise RuntimeError(engine.reason or "Проверка контактов остановлена.")
            for contact in engine.contacts.values():
                add(contact)
            warnings += engine.warnings
            extra_report = engine.path
    finally:
        await backend.disconnect()
    settings = json.loads((PRIVATE / "search_settings.json").read_text())
    settings["sources"] = ", ".join(row["username"] for row in selected)
    atomic_text(PRIVATE / "search_settings.json", json.dumps(settings, ensure_ascii=False, indent=2))
    existing = sorted(RESULTS.glob("Юристы_ТОП30_*.txt"))
    base = next(path for path in existing if not any(tag in path.stem for tag in ("_контакты", "_ники_сообществ")))
    prefix = base.stem
    contacts_path = RESULTS / (prefix + "_контакты.txt")
    names_path = RESULTS / (prefix + "_ники_сообществ.txt")
    csv_path = RESULTS / (prefix + "_источники.csv")
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    lines = ["ТОП-30 ЮРИДИЧЕСКИХ ГРУПП И КАНАЛОВ TELEGRAM", f"Проверено: {now:%d.%m.%Y %H:%M} МСК",
             "Порядок: по числу подписчиков / участников из Telegram API.",
             f"Рассмотрено 80 кандидатов; после фильтров подходят {len(eligible)}. Выбраны 30 крупнейших среди найденных.",
             "Размер аудитории не подтверждает качество или отсутствие накрутки.",
             "Русскоязычная юридическая тематика, без фильтра по городу; аудитория от 1000.",
             "Источники контактов: описание, закреплённый пост, последние 15 публикаций, доступный список администраторов.",
             "Роль из текста — подпись в источнике; скрытого владельца определить нельзя.",
             "Дополнительный источник кандидатов: https://livedune.com/ru/ratings/telegram/category/pravo/", ""]
    for rank, row in enumerate(selected, 1):
        count = f"{row['participants']:,}".replace(",", " ")
        lines.extend([f"{rank:02}. {row['title']}", f"    {row['type']} · {count} подписчиков / участников",
                      f"    @{row['username']} · {row['link']}"])
        matching = [(contact, e) for contact in merged.values() for e in contact.evidence if e.source == row["link"] or e.source.startswith(row["link"] + "/")]
        seen = set()
        for contact, evidence in matching:
            if (contact.username, evidence.role) in seen:
                continue
            seen.add((contact.username, evidence.role))
            lines.append(f"    {evidence.role_label}: @{contact.username} — {evidence.source}")
        if not matching:
            lines.append("    Доступные публичные контакты с подходящей ролью не найдены.")
        lines.append("")
    atomic_text(base, "\n".join(lines), "utf-8-sig")
    atomic_text(names_path, "\n".join('@' + row['username'] for row in selected) + "\n", "utf-8-sig")
    new_contacts = [c for c in merged.values() if c.is_new]
    export_txt(contacts_path, new_contacts)
    export_csv(csv_path, new_contacts)
    old_path = Path(report["file"])
    report["file"] = str(contacts_path)
    report["contacts"] = [dict(username=c.username, user_id=c.user_id, bot=c.bot, is_new=c.is_new,
                               evidence=[dict(role=e.role, title=e.title, source=e.source, excerpt=e.excerpt) for e in c.evidence]) for c in merged.values()]
    report["options"]["sources"] = settings["sources"]
    report["stats"].update(contacts=len(merged), new=len(new_contacts), known=len(merged) - len(new_contacts),
                           history_total=ContactHistory().total(), scanned=30, warnings=warnings)
    atomic_text(report_path, json.dumps(report, ensure_ascii=False, indent=2))
    for intermediate in (old_path, extra_report):
        if intermediate and intermediate.exists() and intermediate != contacts_path:
            intermediate.replace(PRIVATE / "reports" / intermediate.name)
    atomic_text(PRIVATE / "legal_top30.json", json.dumps(selected, ensure_ascii=False, indent=2))
    write_progress("done", selected=30, contacts=len(merged), report=str(base), contact_file=str(contacts_path), curated=True)
    print(json.dumps({"selected": 30, "contacts": len(merged), "report": str(base), "contact_file": str(contacts_path)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    import fcntl
    lock = (PRIVATE / "app.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    asyncio.run(main())
