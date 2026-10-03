"""
資料庫備份與還原。
- 備份檔是壓縮的 zip，裡面一個 tenders.db；zip 註解存內容指紋，資料沒變就不重複備份
- 預設存在程式旁的 backups/，可另設第二個位置（隨身碟、OneDrive…）
- 每個位置只保留最新 N 份（設定 backup_keep，預設 30）
"""
import hashlib
import os
import re
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime

import db

FILE_RE = re.compile(r"^tenders_(\d{8}_\d{6})_(.+)\.zip$")


class BackupError(Exception):
    pass


def primary_dir() -> str:
    """data/ 資料夾旁的 backups/"""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(db.db_path()))), "backups")


def backup_dirs(conn) -> list[str]:
    dirs = [primary_dir()]
    second = (db.get_setting(conn, "backup_dir2") or "").strip()
    if second and os.path.abspath(second) != os.path.abspath(dirs[0]):
        dirs.append(second)
    return dirs


def fingerprint(conn) -> str:
    """資料內容的指紋（與檔案在磁碟上的排列無關）"""
    h = hashlib.sha256()
    for line in conn.iterdump():
        h.update(line.encode("utf-8"))
    return h.hexdigest()


@dataclass
class BackupInfo:
    path: str
    time: datetime
    reason: str
    size: int


def list_backups(folder: str) -> list[BackupInfo]:
    """新的在前"""
    items = []
    if os.path.isdir(folder):
        for name in os.listdir(folder):
            m = FILE_RE.match(name)
            if m:
                p = os.path.join(folder, name)
                items.append(BackupInfo(p, datetime.strptime(m.group(1), "%Y%m%d_%H%M%S"),
                                        m.group(2), os.path.getsize(p)))
    return sorted(items, key=lambda b: (b.time, b.path), reverse=True)


def _last_fingerprint(folder: str) -> str | None:
    for b in list_backups(folder):
        try:
            with zipfile.ZipFile(b.path) as z:
                return z.comment.decode("ascii") or None
        except (zipfile.BadZipFile, OSError):
            continue
    return None


@dataclass
class BackupResult:
    created: list[str] = field(default_factory=list)   # 新建立的備份檔
    skipped: bool = False                              # 資料沒變，沒有備份
    warnings: list[str] = field(default_factory=list)  # 第二位置失敗等


def make_backup(conn, reason: str, force: bool = False, now: datetime | None = None) -> BackupResult:
    """
    reason：排程、關閉、還原前、手動…（會出現在檔名）。
    主要位置失敗會丟 BackupError；第二位置失敗只記在 warnings。
    """
    result = BackupResult()
    fp = fingerprint(conn)
    dirs = backup_dirs(conn)
    if not force and _last_fingerprint(dirs[0]) == fp:
        result.skipped = True
        return result

    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    name = f"tenders_{stamp}_{reason}.zip"
    keep = int(db.get_setting(conn, "backup_keep", 30))
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = os.path.join(tmp, "tenders.db")
        dest = sqlite3.connect(snapshot)
        try:
            conn.backup(dest)                      # SQLite 線上備份，使用中也能安全複製
            dest.execute("PRAGMA journal_mode = DELETE")   # 備份檔自成一檔，不帶 -wal
        finally:
            dest.close()
        packed = os.path.join(tmp, name)
        with zipfile.ZipFile(packed, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snapshot, "tenders.db")
            z.comment = fp.encode("ascii")

        for i, folder in enumerate(dirs):
            try:
                os.makedirs(folder, exist_ok=True)
                target = os.path.join(folder, name)
                partial = target + ".part"
                with open(packed, "rb") as src, open(partial, "wb") as out:
                    out.write(src.read())
                os.replace(partial, target)        # 寫完才改名，不會留下半個備份檔
                result.created.append(target)
                _prune(folder, keep)
            except OSError as e:
                if i == 0:
                    raise BackupError(f"備份失敗（{folder}）：{e}") from e
                result.warnings.append(f"第二備份位置失敗（{folder}）：{e}")
    return result


def _prune(folder: str, keep: int) -> None:
    for old in list_backups(folder)[keep:]:
        os.remove(old.path)


def restore(conn, zip_path: str) -> BackupResult:
    """
    用備份檔覆蓋目前的資料。先檢查備份檔完整，再把目前資料備份一份（原因「還原前」），最後才覆蓋。
    """
    with tempfile.TemporaryDirectory() as tmp:
        try:
            with zipfile.ZipFile(zip_path) as z:
                z.extract("tenders.db", tmp)
        except (zipfile.BadZipFile, KeyError, OSError) as e:
            raise BackupError(f"備份檔無法讀取：{e}") from e
        src = sqlite3.connect(os.path.join(tmp, "tenders.db"))
        try:
            try:
                ok = src.execute("PRAGMA integrity_check").fetchone()[0]
                version = src.execute("PRAGMA user_version").fetchone()[0]
            except sqlite3.DatabaseError as e:
                raise BackupError(f"備份檔已損壞：{e}") from e
            if ok != "ok":
                raise BackupError(f"備份檔已損壞：{ok}")
            if version != db.SCHEMA_VERSION:
                raise BackupError(f"備份檔版本 {version} 與程式（{db.SCHEMA_VERSION}）不同，不能還原")
            before = make_backup(conn, "還原前", force=True)
            src.backup(conn)
        finally:
            src.close()
    return before
