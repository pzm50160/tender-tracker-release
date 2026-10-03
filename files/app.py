"""
健檢標案追蹤系統 — Streamlit 畫面
執行：streamlit run app.py
"""
import html
import os
from contextlib import closing
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

import backup
import db
import migrate
import schedule
import scraper
import update

st.set_page_config(page_title="健檢標案追蹤系統", page_icon="🏥", layout="wide",
                   initial_sidebar_state="expanded")

CARDS_PER_PAGE = 20


@st.cache_resource
def _init_db(path: str) -> bool:
    with closing(db.open_db(path)):
        pass
    return True


_init_db(db.db_path())
conn = db.connect()


# ── 共用動作（按鈕 callback 用自己的連線）────────────────────

def _toggle_read(tender_id, is_read):
    with closing(db.connect()) as c:
        db.set_read(c, tender_id, not is_read)


def _toggle_bid(tender_id, is_bid):
    with closing(db.connect()) as c:
        db.set_bid(c, tender_id, not is_bid)


def _mark_all_read():
    with closing(db.connect()) as c:
        db.mark_all_read(c)


@st.cache_data(ttl=60, show_spinner=False)
def _schedule_info():
    return schedule.query_task()


@st.cache_data(ttl=600, show_spinner=False)
def _old_schedule_time():
    return schedule.query_old_task()


@st.cache_data(ttl=3600, show_spinner=False)
def _remote_manifest():
    return update.fetch_manifest()


def esc(value, empty="—"):
    return html.escape(str(value)) if value not in (None, "") else empty


# ── 樣式 ─────────────────────────────────────────────────
st.markdown("""
<style>
.card { background:#f8f9fa; border-left:4px solid #2196F3; border-radius:6px; padding:12px 16px; margin-bottom:10px; }
.card.unread { border-left-color:#E53935; background:#fff5f5; }
.card.bid   { border-left-color:#7B1FA2; background:#f3e5f5; }
.card.bid.unread { border-left-color:#7B1FA2; background:#ede7f6; }
.card-title { font-size:1.05rem; font-weight:700; color:#1a1a2e; margin-bottom:6px; }
.card-meta  { font-size:0.85rem; color:#555; line-height:1.8; }
.badge { display:inline-block; padding:2px 9px; border-radius:12px; font-size:0.75rem; font-weight:600; margin-right:5px; }
.blue  { background:#e3f2fd; color:#1565C0; }
.green { background:#e8f5e9; color:#2E7D32; }
.red   { background:#ffebee; color:#B71C1C; }
.gray   { background:#f5f5f5; color:#424242; }
.purple { background:#ede7f6; color:#4527A0; }
</style>
""", unsafe_allow_html=True)


# ── 第一次使用：從舊版匯入 ────────────────────────────────

def migration_panel():
    """新資料庫還沒有資料、也還沒決定過要不要匯入時才顯示"""
    if db.get_setting(conn, "migrated_from") or db.get_setting(conn, "migration_skipped"):
        return
    if conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]:
        return
    st.info("第一次使用新版：可以把舊版的標案、已讀、已投標、關鍵字和搜尋記錄搬過來（舊資料不會被修改）。")
    found = migrate.find_old_dbs(os.path.dirname(db.APP_DIR))
    options = found + ["手動輸入路徑…"]
    choice = st.radio("舊版資料庫", options, index=0)
    path = choice
    if choice == options[-1]:
        path = st.text_input("舊版資料庫路徑（舊程式資料夾裡的 data\\tenders.db）")
    c1, c2, _ = st.columns([1, 1, 4])
    if c1.button("匯入舊資料", type="primary", disabled=not path):
        try:
            report = migrate.migrate(conn, path.strip().strip('"'))
        except (migrate.MigrationError, OSError) as e:
            st.error(str(e))
        else:
            source = report.pop("keywords_source")
            report.pop("逐筆內容差異")
            st.success("匯入完成，逐筆核對無誤：" + "、".join(f"{k} {v}" for k, v in report.items())
                       + f"（關鍵字來源：{source}）")
            st.button("開始使用")
        st.stop()
    if c2.button("不匯入，從空白開始"):
        db.set_setting(conn, "migration_skipped", True)
        st.rerun()
    st.stop()


migration_panel()


# ── 側邊欄 ───────────────────────────────────────────────
keywords = db.get_keywords(conn)
proc_setting = db.get_setting(conn, "procurement_types", [])

with st.sidebar:
    st.title("🏥 健檢標案追蹤")
    st.divider()

    st.subheader("篩選")
    col_d1, col_d2 = st.columns(2)
    date_from = col_d1.date_input("從", value=date.today() - timedelta(days=30), format="YYYY/MM/DD")
    date_to = col_d2.date_input("至", value=date.today(), min_value=date_from, format="YYYY/MM/DD")
    kw_filter = st.text_input("關鍵字", placeholder="機關名稱 / 標案名稱")
    unread_only = st.toggle("只看未讀", value=False)
    bid_only = st.toggle("顯示所有已投標", value=False)

    st.divider()
    st.subheader("立即搜尋 PCC")
    col_s1, col_s2 = st.columns(2)
    search_from = col_s1.date_input("起", value=date.today() - timedelta(days=7), format="YYYY/MM/DD")
    search_to = col_s2.date_input("迄", value=date.today(), min_value=search_from, format="YYYY/MM/DD")
    search_types = st.multiselect("採購性質", options=scraper.ALL_PROC_TYPES,
                                  default=proc_setting or scraper.ALL_PROC_TYPES,
                                  help="不選等同全部")
    do_search = st.button("開始搜尋", type="primary", width="stretch",
                          disabled=not keywords, help=None if keywords else "請先新增監控關鍵字")

    st.divider()
    st.subheader("監控關鍵字")
    for kw in keywords:
        col_kw, col_del = st.columns([4, 1])
        col_kw.caption(f"• {kw}")
        if col_del.button("✕", key=f"del_{kw}"):
            db.set_keywords(conn, [k for k in keywords if k != kw])
            st.rerun()
    with st.form("add_kw", clear_on_submit=True, border=False):
        new_kw = st.text_input("新增關鍵字", placeholder="輸入後按 Enter", label_visibility="collapsed")
        if st.form_submit_button("新增", width="stretch"):
            new_kw = new_kw.strip()
            if new_kw and new_kw not in keywords:
                db.set_keywords(conn, keywords + [new_kw])
                st.rerun()

    st.divider()
    st.subheader("自動排程")
    task = _schedule_info()
    old_time = _old_schedule_time() if not task else None
    if old_time:
        st.warning(f"偵測到舊版程式的每日排程（每天 {old_time}）")
        if st.button("改用新版排程（同一時間），並停用舊版排程", width="stretch"):
            ok, msg = schedule.replace_old_task()
            _schedule_info.clear(); _old_schedule_time.clear()
            if ok:
                st.session_state["flash"] = f"已改用新版排程，每天 {msg} 自動搜尋"
                st.rerun()
            st.error(msg)
    if task:
        st.success(f"排程已啟用：每天 {task['time']}" if task["time"] else "排程已啟用")
        if task["next_run"]:
            st.caption(f"下次執行：{task['next_run']}")
        if st.button("立即試跑一次", width="stretch",
                     help="馬上用排程的方式執行一次，約 1～2 分鐘後到「搜尋記錄」查看結果"):
            ok, msg = schedule.run_now()
            if ok:
                st.session_state["flash"] = "已開始在背景試跑，約 1～2 分鐘後到「搜尋記錄」查看結果"
                st.rerun()
            st.error(f"試跑失敗：{msg}")
        if st.button("停用排程", width="stretch"):
            ok, msg = schedule.delete_task()
            _schedule_info.clear()
            if not ok:
                st.error(f"停用失敗：{msg}")
            else:
                st.rerun()
    else:
        st.info("排程未啟用")
    default_time = (task or {}).get("time") or old_time or "08:00"
    sched_time = st.time_input("執行時間", value=datetime.strptime(default_time, "%H:%M").time())
    if st.button("更新排程時間" if task else "啟用每日排程", type="primary", width="stretch"):
        ok, msg = schedule.create_task(sched_time.strftime("%H:%M"))
        _schedule_info.clear()
        if ok:
            st.rerun()
        st.error(f"設定失敗：{msg}")
    new_setting = st.multiselect("排程搜尋的採購性質", options=scraper.ALL_PROC_TYPES,
                                 default=proc_setting, help="不選等同全部；也是「立即搜尋」的預設值")
    if new_setting != proc_setting:
        db.set_setting(conn, "procurement_types", new_setting)
        st.rerun()

    st.divider()
    st.subheader("資料備份")
    st.caption("每日排程搜尋完、以及從系統匣關閉程式時自動備份；資料沒變就不重複備份。")
    backups = backup.list_backups(backup.primary_dir())
    if backups:
        st.caption(f"最近備份：{backups[0].time:%Y/%m/%d %H:%M}（{backups[0].reason}），共 {len(backups)} 份")
    else:
        st.caption("尚無備份")
    if st.button("立即備份", width="stretch"):
        try:
            r = backup.make_backup(conn, "手動")
        except backup.BackupError as e:
            st.error(str(e))
        else:
            st.session_state["flash"] = ("資料沒有變動，不需要再備份" if r.skipped
                                         else f"已備份 {len(r.created)} 份")
            for w in r.warnings:
                st.warning(w)
            if not r.warnings:
                st.rerun()
    dir2 = db.get_setting(conn, "backup_dir2") or ""
    new_dir2 = st.text_input("第二備份位置（選填）", value=dir2,
                             placeholder=r"例如 E:\標案備份 或 OneDrive 資料夾",
                             help=f"主要備份位置：{backup.primary_dir()}")
    if new_dir2.strip() != dir2:
        db.set_setting(conn, "backup_dir2", new_dir2.strip())
        st.rerun()
    with st.expander("從備份還原"):
        if not backups:
            st.caption("尚無備份")
        else:
            by_path = {b.path: b for b in backups}
            pick = by_path[st.selectbox("選擇備份", list(by_path), format_func=lambda p: (
                f"{by_path[p].time:%Y/%m/%d %H:%M}　{by_path[p].reason}　{by_path[p].size // 1024} KB"))]
            sure = st.checkbox("我確定要用這份備份覆蓋目前的資料")
            if st.button("還原", type="primary", disabled=not sure, width="stretch"):
                try:
                    before = backup.restore(conn, pick.path)
                except backup.BackupError as e:
                    st.error(str(e))
                else:
                    st.session_state["flash"] = (
                        f"已還原到 {pick.time:%Y/%m/%d %H:%M} 的備份；還原前的資料已另存為備份，可以再還原回來")
                    st.rerun()


    st.divider()
    st.subheader("程式更新")
    local = update.local_manifest()
    if update.is_dev() or not local:
        st.caption("開發版，不自動更新")
    else:
        st.caption(f"目前版本：{local['version']}")
        try:
            remote = _remote_manifest()
        except update.UpdateError:
            st.caption("無法檢查更新（請確認網路）")
            remote = None
        if remote and update.has_update(remote, local):
            st.warning(f"有新版本 {remote['version']}" + (f"：{remote['notes']}" if remote.get("notes") else ""))
            if update.needs_full_package(remote):
                st.caption("這次更新需要下載完整版，請聯絡管理者")
            elif st.button("立即更新", type="primary", width="stretch"):
                with st.spinner("下載更新中…"):
                    try:
                        update.apply_update(remote)
                    except update.UpdateError as e:
                        st.error(str(e))
                    else:
                        _remote_manifest.clear()
                        st.success("更新完成，畫面幾秒後會自動重新載入")
        elif remote:
            st.caption("已是最新版本")
        if st.button("檢查更新", width="stretch"):
            _remote_manifest.clear()
            st.rerun()
        if update.can_rollback() and st.button("退回上一版", width="stretch"):
            try:
                update.rollback()
            except update.UpdateError as e:
                st.error(str(e))
            else:
                st.success("已退回上一版，畫面幾秒後會自動重新載入")

# ── 執行搜尋 ─────────────────────────────────────────────
if do_search:
    with st.status("正在搜尋政府電子採購網…", expanded=True) as status:
        bar = st.progress(0.0)

        def _progress(i, n, kw):
            bar.progress(i / n, text=f"（{i + 1}/{n}）{kw}")

        summary = scraper.run_scraper(
            conn, search_from.strftime("%Y/%m/%d"), search_to.strftime("%Y/%m/%d"),
            proc_types=search_types, progress=_progress)
        bar.progress(1.0, text="完成")
        if summary.errors:
            status.update(label=f"搜尋完成，但有 {len(summary.errors)} 個關鍵字失敗", state="error")
            for kw, err in summary.errors.items():
                st.write(f"❌ **{kw}**：{err}")
        else:
            status.update(label=f"搜尋完成：共 {summary.total} 筆，新增 {summary.new} 筆", state="complete")
            if summary.total == 0:
                st.write("沒有符合條件的標案（可嘗試擴大日期範圍或新增關鍵字）")


# ── 主畫面 ───────────────────────────────────────────────
st.title("政府採購｜健檢標案追蹤系統")
if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))

# 篩選條件變動時回到第一頁
_filter_key = (date_from, date_to, kw_filter, unread_only, bid_only, tuple(keywords))
if st.session_state.get("last_filter_key") != _filter_key:
    st.session_state["last_filter_key"] = _filter_key
    st.session_state["page"] = 0

tenders = db.get_tenders(
    conn,
    date_from=date_from.strftime("%Y/%m/%d"),
    date_to=date_to.strftime("%Y/%m/%d"),
    text=kw_filter.strip() or None,
    unread_only=unread_only,
    active_keywords=keywords,
    bid_only=bid_only,
)

m1, m2, m3, m4 = st.columns(4)
m1.metric("標案筆數", len(tenders))
m2.metric("未讀", sum(1 for t in tenders if not t["is_read"]))
m3.metric("關鍵字數", len(keywords))
last = db.get_fetch_logs(conn, 1)
m4.metric("最新更新", last[0]["fetched_at"][:10] if last else "尚無記錄")
st.divider()

tab_list, tab_table, tab_stats, tab_log = st.tabs(["卡片檢視", "表格 / 匯出", "統計", "搜尋記錄"])

# ── 卡片檢視 ─────────────────────────────────────────────
with tab_list:
    st.button("全部已讀", on_click=_mark_all_read)
    if not tenders:
        st.info("目前沒有資料。請點左側「開始搜尋」。")
    else:
        total_pages = max(1, -(-len(tenders) // CARDS_PER_PAGE))
        page = min(st.session_state.get("page", 0), total_pages - 1)
        st.session_state["page"] = page

        for t in tenders[page * CARDS_PER_PAGE:(page + 1) * CARDS_PER_PAGE]:
            unread, is_bid = not t["is_read"], bool(t["is_bid"])
            card_cls = "card" + (" bid" if is_bid else "") + (" unread" if unread else "")
            badges = (
                ('<span class="badge red">未讀</span>' if unread else "")
                + ('<span class="badge purple">已投標</span>' if is_bid else "")
                + f'<span class="badge blue">{esc(t["procurement_type"])}</span>'
                + "".join(f'<span class="badge green">{esc(k)}</span>' for k in t["keywords"])
            )
            budget = f"{t['budget']:,.0f} 元" if t["budget"] else "未揭露"
            link = (f' | <a href="{esc(t["detail_url"])}" target="_blank">查看詳情 ↗</a>'
                    if t["detail_url"] else "")
            name = esc(t["tender_name"] or t["tender_case_no"], "（名稱未載入）")
            st.markdown(f"""
<div class="{card_cls}">
  <div class="card-title">{badges} {name}</div>
  <div class="card-meta">
    🏢 {esc(t["agency"])} &nbsp;&nbsp;
    📋 {esc(t["tender_case_no"])} &nbsp;&nbsp;
    💰 {budget} &nbsp;&nbsp;
    📅 公告：{esc(t["publish_date"])} &nbsp;&nbsp;
    ⏰ 截止：{esc(t["deadline"])}{link}
  </div>
</div>""", unsafe_allow_html=True)
            b1, b2, _ = st.columns([1, 1, 6])
            b1.button("✓ 已讀" if not unread else "標為已讀", key=f"r_{t['id']}",
                      on_click=_toggle_read, args=(t["tender_id"], not unread))
            b2.button("✓ 已投標" if is_bid else "標為已投標", key=f"bid_{t['id']}",
                      type="primary" if is_bid else "secondary",
                      on_click=_toggle_bid, args=(t["tender_id"], is_bid))

        st.divider()
        pg = st.columns([1, 1, 3, 1, 1])
        if pg[0].button("⏮", disabled=page == 0, key="pg_first"):
            st.session_state["page"] = 0; st.rerun()
        if pg[1].button("◀", disabled=page == 0, key="pg_prev"):
            st.session_state["page"] = page - 1; st.rerun()
        pg[2].markdown(f"<div style='text-align:center;padding-top:6px'>第 {page + 1} / {total_pages} 頁"
                       f"　共 {len(tenders)} 筆</div>", unsafe_allow_html=True)
        if pg[3].button("▶", disabled=page >= total_pages - 1, key="pg_next"):
            st.session_state["page"] = page + 1; st.rerun()
        if pg[4].button("⏭", disabled=page >= total_pages - 1, key="pg_last"):
            st.session_state["page"] = total_pages - 1; st.rerun()

# ── 表格 / 匯出 ──────────────────────────────────────────
with tab_table:
    if not tenders:
        st.info("無資料")
    else:
        df = pd.DataFrame(tenders)
        df["keywords"] = df["keywords"].map("、".join)
        df_show = df[["tender_name", "agency", "tender_case_no", "procurement_type", "budget",
                      "publish_date", "deadline", "keywords", "is_read"]].rename(columns={
            "tender_name": "標案名稱", "agency": "機關", "tender_case_no": "案號",
            "procurement_type": "採購性質", "budget": "預算(元)", "publish_date": "公告日",
            "deadline": "截止投標", "keywords": "關鍵字", "is_read": "已讀",
        })
        st.dataframe(df_show, width="stretch", height=500, hide_index=True)
        st.download_button("下載 CSV", df_show.to_csv(index=False, encoding="utf-8-sig"),
                           "健檢標案.csv", "text/csv")

# ── 統計 ─────────────────────────────────────────────────
with tab_stats:
    if not tenders:
        st.info("無資料")
    else:
        df = pd.DataFrame(tenders)
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("依關鍵字")
            st.bar_chart(df.explode("keywords")["keywords"].value_counts().rename("筆數"))
        with c2:
            st.subheader("依採購性質")
            st.bar_chart(df["procurement_type"].value_counts().rename("筆數"))
        top = df[df["budget"].notna()].nlargest(15, "budget")
        if not top.empty:
            st.subheader("前 15 高預算標案")
            st.bar_chart(top.set_index("tender_name")["budget"])

# ── 搜尋記錄 ─────────────────────────────────────────────
with tab_log:
    logs = db.get_fetch_logs(conn, 100)
    if logs:
        df = pd.DataFrame(logs)[["fetched_at", "keyword", "count", "status", "message"]]
        df["status"] = df["status"].map({"success": "成功", "error": "失敗"}).fillna(df["status"])
        st.dataframe(df.rename(columns={"fetched_at": "時間", "keyword": "關鍵字", "count": "筆數",
                                        "status": "狀態", "message": "說明"}),
                     width="stretch", hide_index=True)
    else:
        st.info("尚無記錄")
