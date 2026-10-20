"""Explicit, confirmed cleanup. Login, settings and cooldown are never cleared."""
import json
import time
from pathlib import Path

from .security import atomic_bytes, encryption_key, seal


def obsolete_reports(private, days):
    private = Path(private)
    protected = set()
    try:
        protected.add(Path(json.loads((private / 'last_search.json').read_text())['report']).resolve())
    except (OSError, ValueError, KeyError, TypeError):
        pass
    result = []
    for path in (private / 'reports').glob('*.json'):
        if path.is_symlink() or path.resolve() in protected or time.time() - path.stat().st_mtime < days * 86400:
            continue
        try:
            data = json.loads(path.read_text())
            # Legacy reports and unfinished checkpoints are preserved by default.
            if data.get('complete') is True:
                result.append(path)
        except (OSError, ValueError, AttributeError):
            continue
    return result


def remove_reports(paths, private, confirmed=False):
    if not confirmed:
        raise ValueError('Удаление требует подтверждения пользователя.')
    root = (Path(private) / 'reports').resolve()
    for path in paths:
        path = Path(path)
        if path.is_symlink() or path.parent.resolve() != root or path.suffix != '.json':
            raise ValueError('Недопустимый путь отчёта.')
        path.unlink(missing_ok=True)


def clear_history(private, confirmed=False):
    if not confirmed:
        raise ValueError('Удаление требует подтверждения пользователя.')
    private = Path(private)
    reports = private / 'reports'
    if reports.is_symlink():
        raise ValueError('Папка отчётов не должна быть ссылкой.')
    for path in reports.iterdir() if reports.exists() else []:
        if path.is_symlink() or not path.is_file():
            raise ValueError('В папке отчётов обнаружен нестандартный файл. Очистка остановлена.')
    # Files are unlinked, not overwritten: secure erasure on SSD/APFS snapshots
    # cannot be promised. Other user exports/backups are outside this operation.
    for name in ('contacts.sqlite3', 'contacts.sqlite3-journal', 'contacts.sqlite3-wal',
                 'contacts.sqlite3-shm', 'last_search.json'):
        path = private / name
        if path.is_symlink():
            raise ValueError('Недопустимая ссылка вместо файла базы.')
    for path in reports.iterdir() if reports.exists() else []:
        path.unlink()
    for name in ('contacts.sqlite3', 'contacts.sqlite3-journal', 'contacts.sqlite3-wal',
                 'contacts.sqlite3-shm', 'last_search.json'):
        (private / name).unlink(missing_ok=True)


def encrypt_backup(source, destination):
    source, destination = Path(source), Path(destination)
    if source == destination or destination.exists():
        raise ValueError('Выберите новый файл .enc для защищённой копии.')
    if source.stat().st_size > 128 * 1024 * 1024:
        raise ValueError('Для этой команды размер копии должен быть не более 128 МБ.')
    atomic_bytes(destination, seal(source.read_bytes(), encryption_key()))


def restore_backup(source, destination):
    """Explicit restore into a new directory; never overwrite existing user data."""
    import io
    import tarfile
    from .security import unseal
    destination = Path(destination)
    if destination.exists():
        raise ValueError('Для восстановления выберите новую папку.')
    raw = unseal(Path(source).read_bytes(), encryption_key())
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        destination.mkdir(mode=0o700, parents=True)
        archive.extractall(destination, filter='data')
    for path in destination.rglob('*'):
        if not path.is_symlink():
            path.chmod(0o700 if path.is_dir() else 0o600)
