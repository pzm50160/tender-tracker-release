"""
從舊版（健檢標案追蹤系統 v1）的資料庫匯入資料。
舊資料庫只以唯讀方式開啟，不會被修改。匯入後逐筆核對，對不上就整批取消。
"""
import ast
import glob
import json
import os
import sqlite3

import db


class MigrationError(Exception):
    pass


def is_old_db(path: str) -> bool:
    """舊版資料庫的特徵：tenders 表有 matched_keyword 欄位"""
    try:
        conn = sqlite3.connect(db.sqlite_uri(path, readonly=True), uri=True)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(tenders)")}
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return "matched_keyword" in cols


def find_old_dbs(search_root: str) -> list[str]:
    """在 search_root 底下一層的資料夾找 data/tenders.db（例如與新程式並排的舊程式資料夾）"""
    found = []
    for p in sorted(glob.glob(os.path.join(search_root, "*", "data", "tenders.db"))):
        if is_old_db(p) and os.path.abspath(p) != os.path.abspath(db.db_path()):
            found.append(p)
    return found


def _old_config_keywords(old_db_path: str) -> list[str] | None:
    """
    舊版若從沒改過關鍵字，資料庫裡沒有設定，實際用的是舊程式 config.py 的 KEYWORDS。
    只讀取、不執行 config.py。
    """
    cfg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(old_db_path))), "config.py")
    if not os.path.exists(cfg):
        return None
    tree = ast.parse(open(cfg, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "KEYWORDS" for t in node.targets):
            return list(ast.literal_eval(node.value))
    return None


def _old_keywords(conn) -> list[str] | None:
    try:
        row = conn.execute("SELECT value FROM old.settings WHERE key = 'keywords'").fetchone()
    except sqlite3.OperationalError:
        return None
    return json.loads(row[0]) if row else None


TENDER_COLS = ("id",) + db.TENDER_FIELDS + ("is_read", "is_bid")


def migrate(new_conn: sqlite3.Connection, old_db_path: str) -> dict:
    """
    把舊資料庫匯入新資料庫（新資料庫必須還沒有任何標案）。
    回傳核對結果摘要；任何一項對不上就 rollback 並丟出 MigrationError。
    """
    if not is_old_db(old_db_path):
        raise MigrationError(f"不是舊版資料庫：{old_db_path}")
    if new_conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]:
        raise MigrationError("新資料庫已經有資料，不能再匯入")

    new_conn.execute("ATTACH DATABASE ? AS old", (db.sqlite_uri(old_db_path, readonly=True),))
    try:
        keywords = _old_keywords(new_conn)
        keywords_source = "舊資料庫設定"
        if keywords is None:
            keywords = _old_config_keywords(old_db_path)
            keywords_source = "舊程式 config.py"
        if keywords is None:
            keywords, keywords_source = [], "找不到（請手動設定）"

        cols = ", ".join(TENDER_COLS)
        old_cols = ", ".join(
            f"COALESCE({c}, 0)" if c in ("is_read", "is_bid") else c for c in TENDER_COLS)
        with db.transaction(new_conn):
            new_conn.execute(f"INSERT INTO tenders ({cols}) SELECT {old_cols} FROM old.tenders")
            new_conn.execute(
                "INSERT OR IGNORE INTO tender_keywords (tender_id, keyword) "
                "SELECT tender_id, matched_keyword FROM old.tenders "
                "WHERE matched_keyword IS NOT NULL AND matched_keyword != ''")
            new_conn.execute(
                "INSERT INTO fetch_log (id, fetched_at, keyword, count, status, message) "
                "SELECT id, fetched_at, keyword, count, "
                "  CASE WHEN status LIKE 'error%' THEN 'error' ELSE status END, "
                "  CASE WHEN status LIKE 'error: %' THEN substr(status, 8) ELSE '' END "
                "FROM old.fetch_log")
            db.set_keywords(new_conn, keywords)

            report = _verify(new_conn, keywords)
            db.set_setting(new_conn, "migrated_from", {
                "path": os.path.abspath(old_db_path),
                "keywords_source": keywords_source,
                **report,
            })
    finally:
        new_conn.execute("DETACH DATABASE old")
    report["keywords_source"] = keywords_source
    return report


def _verify(conn, keywords) -> dict:
    """在同一個交易裡核對；失敗會丟例外讓交易 rollback"""
    q = lambda sql: conn.execute(sql).fetchone()[0]
    checks = {
        "標案筆數": (q("SELECT COUNT(*) FROM old.tenders"), q("SELECT COUNT(*) FROM tenders")),
        "未讀": (q("SELECT COUNT(*) FROM old.tenders WHERE COALESCE(is_read,0)=0"),
                 q("SELECT COUNT(*) FROM tenders WHERE is_read=0")),
        "已投標": (q("SELECT COUNT(*) FROM old.tenders WHERE is_bid=1"),
                   q("SELECT COUNT(*) FROM tenders WHERE is_bid=1")),
        "搜尋記錄": (q("SELECT COUNT(*) FROM old.fetch_log"), q("SELECT COUNT(*) FROM fetch_log")),
        "標案關鍵字": (q("SELECT COUNT(*) FROM old.tenders WHERE COALESCE(matched_keyword,'')!=''"),
                     q("SELECT COUNT(*) FROM tender_keywords")),
    }
    # 每一筆每一欄都要相同（雙向比對）
    cols = ", ".join(TENDER_COLS)
    old_cols = ", ".join(f"COALESCE({c}, 0)" if c in ("is_read", "is_bid") else c for c in TENDER_COLS)
    diff = q(f"SELECT COUNT(*) FROM (SELECT {old_cols} FROM old.tenders EXCEPT SELECT {cols} FROM tenders)") \
         + q(f"SELECT COUNT(*) FROM (SELECT {cols} FROM tenders EXCEPT SELECT {old_cols} FROM old.tenders)")
    checks["逐筆內容差異"] = (0, diff)
    checks["關鍵字設定"] = (len(keywords), len(db.get_keywords(conn)))

    bad = {k: v for k, v in checks.items() if v[0] != v[1]}
    if bad:
        detail = "；".join(f"{k} 舊 {a} / 新 {b}" for k, (a, b) in bad.items())
        raise MigrationError(f"匯入核對不符，已取消匯入：{detail}")
    return {k: v[1] for k, v in checks.items()}
