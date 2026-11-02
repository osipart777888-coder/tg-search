"""Community results are separate from personal contact history."""
import csv
import io
import re
from dataclasses import dataclass

from .extraction import split_terms
from .storage import atomic_text, redact

COMMUNITY_SEARCH_LABEL = 'Только каналы и группы'


@dataclass
class Community:
    community_id: int
    username: str
    title: str
    kind: str
    link: str
    checked_at: str
    members: int | None = None
    verification: str = 'verified'
    russia_only: bool = False
    russia_basis: str = ''

    @property
    def status(self):
        return 'Найдено' if self.verification == 'verified' else 'Проверка не завершена'


def title_matches(keywords, title):
    def normalize(text):
        return re.sub(r'\s+', ' ', text.casefold()).strip()
    terms = split_terms(keywords)
    return not terms or any(normalize(term) in normalize(title) for term in terms)


def discovery_variants(query):
    """Bounded username spellings for discovery; never relax the title filter."""
    if not re.search('[а-яё]', query.casefold()):
        return []
    alphabet = dict(zip('абвгдеёзийклмнопрстуфыэьъ',
                        ['a', 'b', 'v', 'g', 'd', 'e', 'e', 'z', 'i', 'y', 'k', 'l',
                         'm', 'n', 'o', 'p', 'r', 's', 't', 'u', 'f', 'y', 'e', '', '']))
    alphabet.update({'ж': 'zh', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch', 'я': 'ya'})
    variants = []
    for yu in ('yu', 'u', 'ju'):
        mapping = {**alphabet, 'ю': yu}
        latin = ''.join(mapping.get(char, char) for char in query.casefold())
        for separator in ('', '_'):
            value = re.sub('[^a-z0-9]+', separator, latin).strip('_')
            if 3 <= len(value) <= 64 and value not in variants:
                variants.append(value)
    return variants[:6]


def export_communities_txt(path, communities):
    rows = {c.community_id: c for c in communities if c.verification == 'verified' and (not c.russia_only or c.russia_basis)}
    text = '\n\n'.join(f'{c.title}\n@{c.username} · {c.kind}\n{c.link}' + (f'\nПризнак РФ — {c.russia_basis}' if c.russia_only else '')
                         for c in sorted(rows.values(), key=lambda c: (c.title.casefold(), c.community_id)))
    atomic_text(path, text + ('\n' if text else ''), 'utf-8-sig')


def export_communities_csv(path, communities):
    def safe(value):
        text = redact(value) if value is not None else ''
        return "'" + text if text.startswith(('=', '+', '-', '@', '\t', '\r')) else text
    out = io.StringIO(newline='')
    writer = csv.writer(out, delimiter=';')
    writer.writerow(['Название', 'Тип', 'Ник сообщества', 'Ссылка', 'Telegram ID сообщества', 'Участников', 'Последняя проверка', 'Признак РФ'])
    rows = {c.community_id: c for c in communities if c.verification == 'verified' and (not c.russia_only or c.russia_basis)}
    for c in sorted(rows.values(), key=lambda c: (c.title.casefold(), c.community_id)):
        writer.writerow([safe(c.title), safe(c.kind), safe('@' + c.username), safe(c.link), c.community_id, c.members, c.checked_at, safe(c.russia_basis)])
    atomic_text(path, out.getvalue(), 'utf-8-sig')
