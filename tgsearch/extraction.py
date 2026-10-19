"""Contextual extraction; role labels describe evidence, not verified identity."""
import re
from dataclasses import dataclass

PRESETS = {
    "Строители": "строители, строительство, ремонт квартир, стройка, отделка",
    "Юристы": "юристы, юрист, юридическая помощь, адвокат, право",
    "Недвижимость": "недвижимость, риэлтор, аренда квартир, новостройки",
    "Красота": "салон красоты, косметолог, маникюр, парикмахер",
    "Медицина": "врачи, клиника, стоматология, медицина",
    "Авто": "автосервис, автомобили, автозапчасти, детейлинг",
    "Образование": "репетитор, обучение, курсы, образование",
    "Бизнес": "предприниматели, бизнес, маркетинг, продажи",
    "IT": "программисты, разработка, python, IT вакансии",
    "Своя тема": "",
}
ROLE_LABELS = {
    "owner": "Владелец (открытый список)",
    "owner_contact": "Указан как владелец",
    "admin": "Администратор (открытый список)",
    "admin_contact": "Контакт администратора",
    "advertising": "Контакт для покупки рекламы",
    "advertiser": "Указан как рекламодатель",
    "ad_contact": "Контакт из рекламного объявления",
    "contact": "Другой публичный контакт",
}
ROLE_FILTERS = {
    "Владельцы, администраторы и реклама": set(ROLE_LABELS) - {"contact"},
    "Только владельцы": {"owner", "owner_contact"},
    "Владельцы и администраторы": {"owner", "owner_contact", "admin", "admin_contact"},
    "Контакты для покупки рекламы": {"advertising"},
    "Рекламодатели / контакты объявлений": {"advertiser", "ad_contact"},
    "Все опубликованные контакты": set(ROLE_LABELS),
}

USERNAME = r"[A-Za-z][A-Za-z0-9_]{3,31}"
MENTION = re.compile(rf"(?<![\w@])@({USERNAME})(?!\w)")
LINK = re.compile(rf"(?i)(?<![\w.])(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/({USERNAME})(?!\w)")
RESERVED = {"joinchat", "addstickers", "addemoji", "share", "proxy", "socks", "login", "confirmphone", "boost", "invoice", "contact", "giftcode", "setlanguage", "addlist", "m", "iv"}
CUES = [
    ("advertiser", re.compile(r"рекламодатель\w*|спонсор\w*|advertiser\w*|sponsor\w*", re.I)),
    ("advertising", re.compile(r"(?:по\s+)?реклам[аеуы]\b|размещени[ея]\s+реклам\w*|сотрудничеств\w*|рекламн\w*\s+(?:менеджер|отдел)|advertising|\bads\b|promotion", re.I)),
    ("owner_contact", re.compile(r"владел[еь]ц\w*|основател\w*|создател\w*|\bowner\b|\bfounder\b", re.I)),
    ("admin_contact", re.compile(r"админ\w*|модератор\w*|управляющ\w*|\badmin\w*", re.I)),
]
AD_MARKER = re.compile(r"#реклама\b|\berid\s*[:=]|на\s+правах\s+рекламы|рекламодатель|спонсор|#ad\b|#sponsored\b", re.I)


@dataclass(frozen=True)
class Candidate:
    username: str
    role: str
    excerpt: str


def split_terms(text):
    values = re.split(r"[,;\n]+", text)
    return list(dict.fromkeys(v.strip().lower() for v in values if v.strip()))


def make_queries(keywords, city=""):
    terms = split_terms(keywords)
    if not terms or len(terms) > 20 or any(len(t) < 3 for t in terms):
        raise ValueError("Укажите от 1 до 20 запросов, минимум по 3 символа; разделяйте их запятыми.")
    values = [f"{term} {city.strip()}" for term in terms] + terms if city.strip() else terms
    return list(dict.fromkeys(values))


def city_matches(city, text):
    if not city.strip():
        return True
    aliases = {"москва": ["моск", "moscow", "msk"], "спб": ["петербург", "спб", "spb"],
               "санкт-петербург": ["петербург", "спб", "spb"]}
    return any(term in text.lower() for term in aliases.get(city.lower().strip(), [city.lower().strip()]))


def _line_role(text, start, end):
    line_start = max(text.rfind(sep, 0, start) for sep in ("\n", ";", "|")) + 1
    ends = [text.find(sep, end) for sep in ("\n", ";", "|")]
    line_end = min([pos for pos in ends if pos >= 0], default=len(text))
    line = text[line_start:line_end]
    relative = start - line_start
    cues = [(m.start(), m.end(), role) for role, pattern in CUES for m in pattern.finditer(line)]
    before = [cue for cue in cues if cue[1] <= relative]
    if before:
        return max(before, key=lambda cue: cue[1])[2]
    after = [cue for cue in cues if cue[0] >= end - line_start]
    remaining_contacts = MENTION.search(text[end:line_end]) or LINK.search(text[end:line_end])
    if after and not remaining_contacts:
        return min(after, key=lambda cue: cue[0])[2]
    if not line[:relative].strip(" \t•—-:"):
        prev = text[:max(0, line_start - 1)].split("\n")[-1]
        if not MENTION.search(prev) and not LINK.search(prev):
            for role, pattern in CUES:
                if pattern.search(prev):
                    return role
    return "contact"


def extract_candidates(text, is_post=False):
    found = {}
    for pattern in (MENTION, LINK):
        for match in pattern.finditer(text or ""):
            username = match.group(1).lower()
            if username in RESERVED:
                continue
            role = _line_role(text, match.start(), match.end())
            if role == "advertising" and is_post and AD_MARKER.search(text):
                line = text[text.rfind("\n", 0, match.start()) + 1:match.end()]
                if not re.search(r"по\s+рекламе|размещени|сотрудничеств|менеджер", line, re.I):
                    role = "ad_contact"
            if role == "contact" and is_post and AD_MARKER.search(text):
                role = "ad_contact"
            excerpt = text[max(0, match.start() - 90):match.end() + 130].strip()
            found[(username, role)] = Candidate(username, role, excerpt)
    return list(found.values())


def public_message_text(message):
    text = getattr(message, "message", "") or ""
    replacements = []
    for entity in getattr(message, "entities", None) or []:
        url = getattr(entity, "url", None)
        if url and LINK.search(url):
            raw = text.encode("utf-16-le")
            index = len(raw[:(entity.offset + entity.length) * 2].decode("utf-16-le"))
            replacements.append((index, f" ({url})"))
    for index, suffix in sorted(replacements, reverse=True):
        text = text[:index] + suffix + text[index:]
    return text
