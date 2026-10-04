"""
每日排程執行的自動搜尋：搜最近 7 天 → 備份 → 跳出 Windows 通知。
由 launcher.py --auto-search 呼叫；結果寫在 logs/auto_search.log，
執行狀態寫在資料庫設定 auto_search_status（畫面用來顯示進度與結果）。
"""
import base64
import logging
import subprocess
from datetime import datetime, timedelta
from xml.sax.saxutils import escape

import backup
import db
import scraper

log = logging.getLogger("auto_search")
STATUS_KEY = "auto_search_status"
STALE_AFTER = timedelta(hours=1)        # 排程最多跑 1 小時，超過還是「執行中」就是中斷了


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def set_status(conn, **status) -> None:
    db.set_setting(conn, STATUS_KEY, status)


def get_status(conn) -> dict | None:
    return db.get_setting(conn, STATUS_KEY)


def describe(status: dict | None, now: datetime | None = None) -> tuple[str, str]:
    """把執行狀態轉成畫面文字，回傳 (等級, 文字)；等級：none / running / success / warning / error"""
    if not status:
        return "none", "尚未執行過排程搜尋"
    now = now or datetime.now()
    started = datetime.fromisoformat(status["started"])
    when = f"{started:%m/%d %H:%M}"
    state = status["state"]
    if state == "running":
        if now - started > STALE_AFTER:
            return "error", f"{when} 開始的排程搜尋沒有正常結束，請按「立即試跑一次」再試"
        return "running", f"排程搜尋中（{when} 開始）{status.get('progress', '')}"
    if state == "skipped":
        return "warning", f"{when} 排程沒有搜尋：{status.get('reason', '')}"
    if state == "error":
        return "error", f"{when} 排程搜尋發生錯誤：{status.get('reason', '')}"
    failed = status.get("failed", [])
    text = f"{when} 排程搜尋完成：新增 {status.get('new', 0)} 筆（共 {status.get('total', 0)} 筆）"
    if failed:
        return "warning", text + f"，{len(failed)} 個關鍵字失敗（{'、'.join(failed)}），詳見「搜尋記錄」"
    return "success", text + "，全部成功"


def notify(title: str, body: str) -> None:
    """跳出 Windows 通知（右下角）。失敗不影響搜尋結果。"""
    toast = (f'<toast><visual><binding template="ToastGeneric"><text>{escape(title)}</text>'
             f'<text>{escape(body)}</text></binding></visual></toast>')
    script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('{toast.replace("'", "''")}')
$app = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""
    # 用 -EncodedCommand（UTF-16）傳中文，不受命令列編碼影響；$app 是 Windows 內建 PowerShell 的通知身分
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                           capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("通知顯示失敗：%s", e)
        return
    if r.returncode != 0:
        log.warning("通知顯示失敗：%s", r.stderr.decode("cp950", errors="replace").strip()[:300])


def main() -> int:
    """回傳 0＝全部成功，1＝有關鍵字失敗，2＝沒有執行，3＝發生錯誤"""
    conn = db.open_db()
    started = _now()
    try:
        empty = not conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
        decided = db.get_setting(conn, "migrated_from") or db.get_setting(conn, "migration_skipped")
        reason = ""
        if empty and not decided:
            # 還沒決定要不要匯入舊資料就先搜尋，之後就不能匯入了
            reason = "尚未完成第一次設定（匯入舊資料）"
        elif not db.get_keywords(conn):
            reason = "沒有監控關鍵字"
        if reason:
            log.warning("%s，本次不搜尋", reason)
            set_status(conn, state="skipped", started=started, finished=_now(), reason=reason)
            notify("健檢標案追蹤：排程沒有搜尋", reason)
            return 2

        set_status(conn, state="running", started=started, progress="")

        def progress(i, n, kw):
            log.info("(%d/%d) %s", i + 1, n, kw)
            set_status(conn, state="running", started=started, progress=f"（{i + 1}/{n}）{kw}")

        summary = scraper.run_scraper(conn, progress=progress)
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

        status = dict(state="done", started=started, finished=_now(), total=summary.total,
                      new=summary.new, failed=list(summary.errors))
        set_status(conn, **status)
        body = f"新增 {summary.new} 筆標案（共 {summary.total} 筆）"
        if summary.errors:
            notify("健檢標案追蹤：搜尋完成，但有失敗",
                   body + f"，{len(summary.errors)} 個關鍵字失敗，請打開程式查看「搜尋記錄」")
        else:
            notify("健檢標案追蹤：搜尋完成", body + "，全部成功")
        return 1 if summary.errors else 0
    except Exception as e:
        log.exception("自動搜尋發生錯誤")
        set_status(conn, state="error", started=started, finished=_now(), reason=str(e)[:200])
        notify("健檢標案追蹤：搜尋發生錯誤", "請打開程式查看，或聯絡管理者")
        return 3
    finally:
        conn.close()
