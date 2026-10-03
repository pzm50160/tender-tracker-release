"""
每日排程執行的自動搜尋：搜最近 7 天 → 備份。
由 launcher.py --auto-search 呼叫；結果寫在 logs/auto_search.log。
"""
import logging

import backup
import db
import scraper

log = logging.getLogger("auto_search")


def main() -> int:
    """回傳 0＝全部成功，1＝有關鍵字失敗，2＝沒有執行"""
    conn = db.open_db()
    try:
        empty = not conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
        decided = db.get_setting(conn, "migrated_from") or db.get_setting(conn, "migration_skipped")
        if empty and not decided:
            # 還沒決定要不要匯入舊資料就先搜尋，之後就不能匯入了
            log.warning("尚未完成第一次設定（匯入舊資料），本次不搜尋")
            return 2
        if not db.get_keywords(conn):
            log.warning("沒有監控關鍵字，本次不搜尋")
            return 2

        summary = scraper.run_scraper(
            conn, progress=lambda i, n, kw: log.info("(%d/%d) %s", i + 1, n, kw))
        log.info("搜尋完成 %s ~ %s：共 %d 筆，新增 %d 筆", summary.start, summary.end,
                 summary.total, summary.new)
        for kw, err in summary.errors.items():
            log.error("失敗 %s：%s", kw, err)

        try:
            r = backup.make_backup(conn, "排程")
            log.info("備份：%s", "資料沒變，略過" if r.skipped else "、".join(r.created))
            for w in r.warnings:
                log.warning(w)
        except backup.BackupError as e:
            log.error("%s", e)
        return 1 if summary.errors else 0
    finally:
        conn.close()
