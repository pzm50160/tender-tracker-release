"""
資料庫：連線、建表、資料存取。
資料庫位置預設為程式旁的 data/tenders.db，可用環境變數 TENDER_DB_PATH 覆寫（測試用）。
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import quote

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_VERSION = 2

# 第一次建立資料庫時寫入的設定初值（沿用舊版 config.py 的行為）
DEFAULT_SETTINGS = {
    "keywords": [],
    "procurement_types": ["勞務類", "財物類"],
}


def db_path() -> str:
    return os.environ.get("TENDER_DB_PATH") or os.path.join(APP_DIR, "data", "tenders.db")


def sqlite_uri(path: str, readonly: bool = False) -> str:
    """路徑轉成 SQLite URI（中文、空白、#、? 都要編碼）"""
    uri = "file:" + quote(os.path.abspath(path).replace("\\", "/"), safe="/:")
    return uri + ("?mode=ro" if readonly else "")


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # autocommit，交易用 transaction()；uri=True 才能用唯讀方式 ATTACH 舊資料庫
    conn = sqlite3.connect(sqlite_uri(path), uri=True, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL              -- JSON
);
CREATE TABLE IF NOT EXISTS tenders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    tender_id        TEXT NOT NULL UNIQUE,   -- 機關_案號_招標次數
    tender_name      TEXT,
    agency           TEXT,
    tender_case_no   TEXT,
    procurement_type TEXT,
    tender_way       TEXT,
    budget           REAL,
    publish_date     TEXT,                   -- YYYY/MM/DD
    deadline         TEXT,
    opening_date     TEXT,
    detail_url       TEXT,
    fetched_at       TEXT,
    is_read          INTEGER NOT NULL DEFAULT 0,
    is_bid           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tenders_publish ON tenders (publish_date);
CREATE TABLE IF NOT EXISTS tender_keywords (
    tender_id TEXT NOT NULL REFERENCES tenders (tender_id) ON DELETE CASCADE,
    keyword   TEXT NOT NULL,
    PRIMARY KEY (tender_id, keyword)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_tender_keywords_kw ON tender_keywords (keyword);
CREATE TABLE IF NOT EXISTS fetch_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT,
    keyword    TEXT,
    count      INTEGER,
    status     TEXT,                     -- success | error
    message    TEXT
);
"""


def init_db(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"資料庫版本 {version} 比程式新（{SCHEMA_VERSION}），請更新程式")
    with transaction(conn):
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                conn.execute(stmt)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def open_db(path: str | None = None) -> sqlite3.Connection:
    conn = connect(path)
    init_db(conn)
    return conn


# ── 設定 ─────────────────────────────────────────────────

def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(conn, key, value) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def get_keywords(conn) -> list[str]:
    return get_setting(conn, "keywords", [])


def set_keywords(conn, keywords: list[str]) -> None:
    set_setting(conn, "keywords", keywords)


# ── 標案 ─────────────────────────────────────────────────

TENDER_FIELDS = (
    "tender_id", "tender_name", "agency", "tender_case_no", "procurement_type",
    "tender_way", "budget", "publish_date", "deadline", "opening_date",
    "detail_url", "fetched_at",
)


def upsert_tenders(conn, tenders: list[dict], keyword: str) -> None:
    """一個關鍵字的搜尋結果整批寫入；已讀／已投標不會被覆蓋，關鍵字是累加的"""
    if not tenders:
        return
    cols = ", ".join(TENDER_FIELDS)
    marks = ", ".join("?" * len(TENDER_FIELDS))
    updates = ", ".join(f"{f} = excluded.{f}" for f in TENDER_FIELDS[1:])
    with transaction(conn):
        conn.executemany(
            f"INSERT INTO tenders ({cols}) VALUES ({marks}) "
            f"ON CONFLICT (tender_id) DO UPDATE SET {updates}",
            [tuple(t.get(f) for f in TENDER_FIELDS) for t in tenders],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO tender_keywords (tender_id, keyword) VALUES (?, ?)",
            [(t["tender_id"], keyword) for t in tenders],
        )


def get_tenders(conn, date_from=None, date_to=None, text=None, unread_only=False,
                active_keywords=None, bid_only=False) -> list[dict]:
    """
    篩選規則沿用舊版：
    - bid_only 時忽略日期、未讀、關鍵字清單，只看已投標（文字搜尋仍有效）
    - active_keywords：標案的任一關鍵字在清單中就列出
    """
    where, params = [], []
    if bid_only:
        where.append("t.is_bid = 1")
    else:
        if date_from:
            where.append("t.publish_date >= ?"); params.append(date_from)
        if date_to:
            where.append("t.publish_date <= ?"); params.append(date_to)
        if unread_only:
            where.append("t.is_read = 0")
        if active_keywords:
            marks = ",".join("?" * len(active_keywords))
            where.append(f"EXISTS (SELECT 1 FROM tender_keywords k WHERE k.tender_id = t.tender_id "
                         f"AND k.keyword IN ({marks}))")
            params += list(active_keywords)
    if text:
        like = f"%{text}%"
        where.append("(t.tender_name LIKE ? OR t.agency LIKE ? OR EXISTS ("
                     "SELECT 1 FROM tender_keywords k WHERE k.tender_id = t.tender_id AND k.keyword LIKE ?))")
        params += [like, like, like]
    sql = (
        "SELECT t.*, (SELECT json_group_array(keyword) FROM ("
        "  SELECT keyword FROM tender_keywords k WHERE k.tender_id = t.tender_id ORDER BY keyword)"
        ") AS keywords FROM tenders t"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t.publish_date DESC, t.fetched_at DESC, t.id DESC"
    rows = []
    for r in conn.execute(sql, params):
        d = dict(r)
        d["keywords"] = json.loads(d["keywords"])
        rows.append(d)
    return rows


def set_read(conn, tender_id: str, read: bool) -> None:
    conn.execute("UPDATE tenders SET is_read = ? WHERE tender_id = ?", (int(read), tender_id))


def set_bid(conn, tender_id: str, bid: bool) -> None:
    conn.execute("UPDATE tenders SET is_bid = ? WHERE tender_id = ?", (int(bid), tender_id))


def mark_all_read(conn) -> None:
    conn.execute("UPDATE tenders SET is_read = 1 WHERE is_read = 0")


# ── 搜尋記錄 ─────────────────────────────────────────────

def log_fetch(conn, keyword: str, count: int, status: str, message: str = "") -> None:
    conn.execute(
        "INSERT INTO fetch_log (fetched_at, keyword, count, status, message) VALUES (?,?,?,?,?)",
        (datetime.now().isoformat(), keyword, count, status, message),
    )


def get_fetch_logs(conn, limit: int = 100) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM fetch_log ORDER BY id DESC LIMIT ?", (limit,))]
