"""
政府電子採購網 標案搜尋爬蟲
每個關鍵字分別搜「標案名稱」和「機關名稱」，合併去重後寫入資料庫。
"""
import re
import ssl
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import httpx
import truststore
from bs4 import BeautifulSoup

import db

# ── 採購網固定參數（對方網站規定的值，不是我們的設定）──────────────
BASE_URL = "https://web.pcc.gov.tw"
INDEX_URL = f"{BASE_URL}/prkms/tender/common/basic/indexTenderBasic"
SEARCH_URL = f"{BASE_URL}/prkms/tender/common/basic/readTenderBasic"
ALL_PROC_TYPES = ["工程類", "財物類", "勞務類"]
SEARCH_FIELDS = ("tenderName", "orgName")
PAGE_SIZE = 100
MAX_PAGES = 30          # 單一欄位最多翻 30 頁（3000 筆），防止無限翻頁
PAGE_DELAY = 1.0        # 翻頁間隔秒數，避免對採購網造成負擔
QUERY_DELAY = 0.5       # 欄位、關鍵字之間的間隔秒數
RETRIES = 3             # 連線失敗重試次數

UID_MAX_LEN = 120       # 唯一鍵長度上限，與舊版相同（舊資料才能合併）


class ScrapeError(Exception):
    pass


# ── 解析 ─────────────────────────────────────────────────

def roc_to_western(roc_date: str) -> str:
    """115/05/08 → 2026/05/08；格式不符就原樣回傳"""
    parts = (roc_date or "").split("/")
    if len(parts) == 3 and parts[0].isdigit():
        return f"{int(parts[0]) + 1911}/{parts[1]}/{parts[2]}"
    return roc_date or ""


def parse_budget(text: str) -> float | None:
    m = re.search(r"[\d.]+", (text or "").replace(",", ""))
    return float(m.group()) if m else None


_NAME_RE = re.compile(r'pageCode2Img\("((?:[^"\\]|\\.)*)"\)')


def parse_tender_name(cell_html: str) -> str:
    """標案名稱藏在 JavaScript pageCode2Img("名稱") 裡"""
    m = _NAME_RE.search(cell_html)
    return re.sub(r"\\(.)", r"\1", m.group(1)) if m else ""


@dataclass
class Page:
    tenders: list[dict]          # 已套用採購性質篩選
    raw_count: int               # 本頁篩選前的筆數
    total: int | None            # 頁面寫的「共有 N 筆」
    next_page: str | None        # 下一頁參數，例如 d-49738-p=2


def parse_page(html: str, proc_types: list[str]) -> Page:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="tb_01")
    if table is None:
        raise ScrapeError("搜尋結果頁找不到結果表格，採購網可能改版了")

    tenders, raw_count = [], 0
    for row in table.find_all("tr")[1:]:            # 第一列是表頭
        cells = row.find_all("td")
        if len(cells) < 8 or not cells[0].get_text(strip=True).isdigit():
            continue                                  # 「無符合條件資料」等非資料列
        raw_count += 1
        case_no = cells[2].get_text("\n", strip=True).split("\n")[0].strip()
        agency = cells[1].get_text(strip=True)
        bid_round = cells[3].get_text(strip=True)
        procurement_type = cells[5].get_text(strip=True)
        if proc_types and procurement_type and procurement_type not in proc_types:
            continue
        link = cells[2].find("a", href=True)
        opening = cells[9].get_text(strip=True) if len(cells) > 9 else ""
        tenders.append({
            "tender_id": f"{agency}_{case_no}_{bid_round}"[:UID_MAX_LEN],
            "tender_name": parse_tender_name(str(cells[2])),
            "agency": agency,
            "tender_case_no": case_no,
            "procurement_type": procurement_type,
            "tender_way": cells[4].get_text(strip=True),
            "budget": parse_budget(cells[8].get_text(strip=True) if len(cells) > 8 else ""),
            "publish_date": roc_to_western(cells[6].get_text(strip=True)),
            "deadline": roc_to_western(cells[7].get_text(strip=True)),
            # 開標欄目前是「檢視」按鈕，只有含日期時才取
            "opening_date": roc_to_western(opening) if "/" in opening else "",
            "detail_url": BASE_URL + link["href"] if link else "",
        })

    total = None
    banner = soup.find(id="pagebanner")
    if banner:
        m = re.search(r"共有\s*([\d,]+)\s*筆", banner.get_text())
        if m:
            total = int(m.group(1).replace(",", ""))

    next_page = None
    pager = soup.find(class_=lambda c: c and "page" in c.lower())
    if pager:
        link = pager.find("a", string=re.compile(r"下一頁|Next|›|»"))
        if link and link.get("href"):
            m = re.search(r"d-\d+-p=\d+", link["href"])
            next_page = m.group() if m else None
    return Page(tenders, raw_count, total, next_page)


# ── 抓取 ─────────────────────────────────────────────────

def make_client() -> httpx.Client:
    # 採購網憑證鏈用 Python 內建憑證庫驗不過，改用 Windows 系統憑證庫驗證（不關閉驗證）
    client = httpx.Client(
        verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        timeout=30,
        follow_redirects=True,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/140.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        },
    )
    _get(client, INDEX_URL)                 # 取得 Cookie
    client.headers["Referer"] = INDEX_URL
    return client


def _get(client: httpx.Client, url: str, params: dict | None = None,
         sleep=time.sleep) -> httpx.Response:
    """連線錯誤或伺服器 5xx 時重試（間隔 2、4 秒）；4xx 不重試"""
    for attempt in range(1, RETRIES + 1):
        try:
            resp = client.get(url, params=params)
            if resp.status_code < 500:
                resp.raise_for_status()
                return resp
            error = f"伺服器錯誤 {resp.status_code}"
        except httpx.HTTPStatusError as e:
            raise ScrapeError(f"採購網回應 {e.response.status_code}") from e
        except httpx.HTTPError as e:
            error = f"{type(e).__name__}: {e}"
        if attempt < RETRIES:
            sleep(2 ** attempt)
    raise ScrapeError(f"連線失敗（已重試 {RETRIES} 次）：{error}")


def build_params(keyword: str, field_name: str, start: str, end: str,
                 page: str | None = None) -> dict:
    params = {
        "pageSize": str(PAGE_SIZE),
        "firstSearch": "false" if page else "true",
        "searchType": "basic",
        "isBinding": "N",
        "isLogIn": "N",
        "level_1": "on",
        "orgName": keyword if field_name == "orgName" else "",
        "orgId": "",
        "tenderId": "",
        "tenderType": "TENDER_DECLARATION",
        "tenderWay": "TENDER_WAY_ALL_DECLARATION",
        "tenderName": keyword if field_name == "tenderName" else "",
        "dateType": "isDate",
        "tenderStartDate": start,
        "tenderEndDate": end,
        "radProctrgCate": "",
        "policyAdvocacy": "",
    }
    if page:
        key, value = page.split("=")
        params[key] = value
    return params


def search_field(client, keyword: str, field_name: str, start: str, end: str,
                 proc_types: list[str], sleep=time.sleep) -> list[dict]:
    """搜尋單一欄位的所有頁；中途失敗會丟 ScrapeError，並附上已抓到的部分"""
    results, raw_seen, page_param, total = [], 0, None, None
    for page_no in range(1, MAX_PAGES + 1):
        try:
            resp = _get(client, SEARCH_URL, build_params(keyword, field_name, start, end, page_param),
                        sleep=sleep)
            page = parse_page(resp.text, proc_types)
        except ScrapeError as e:
            e.partial = results
            e.args = (f"第 {page_no} 頁：{e.args[0]}",)
            raise
        results.extend(page.tenders)
        raw_seen += page.raw_count
        total = page.total if page.total is not None else total
        if not page.next_page or not page.raw_count:
            break
        page_param = page.next_page
        sleep(PAGE_DELAY)
    if total is not None and raw_seen < total:
        err = ScrapeError(f"只抓到 {raw_seen} / {total} 筆（超過 {MAX_PAGES} 頁上限或翻頁中斷）")
        err.partial = results
        raise err
    return results


@dataclass
class KeywordResult:
    keyword: str
    tenders: list[dict] = field(default_factory=list)
    error: str = ""


def search_keyword(client, keyword: str, start: str, end: str, proc_types: list[str],
                   sleep=time.sleep) -> KeywordResult:
    """標案名稱＋機關名稱各搜一次，合併去重；有錯誤時保留已抓到的部分"""
    found: dict[str, dict] = {}
    errors = []
    for i, field_name in enumerate(SEARCH_FIELDS):
        if i:
            sleep(QUERY_DELAY)
        try:
            items = search_field(client, keyword, field_name, start, end, proc_types, sleep)
        except ScrapeError as e:
            items = getattr(e, "partial", [])
            label = "標案名稱" if field_name == "tenderName" else "機關名稱"
            errors.append(f"{label}{e}")
        for t in items:
            found.setdefault(t["tender_id"], t)
    return KeywordResult(keyword, list(found.values()), "；".join(errors))


@dataclass
class RunSummary:
    start: str
    end: str
    total: int = 0          # 各關鍵字筆數加總（同一案可能被多個關鍵字算到）
    new: int = 0            # 資料庫原本沒有的標案數
    errors: dict[str, str] = field(default_factory=dict)


def run_scraper(conn, start_date: str | None = None, end_date: str | None = None,
                proc_types: list[str] | None = None, keywords: list[str] | None = None,
                progress=None, client=None, sleep=time.sleep) -> RunSummary:
    """
    start_date / end_date：YYYY/MM/DD，預設近 7 天。
    proc_types：None 時用資料庫設定；空清單＝全部採購性質。
    progress(i, n, keyword)：每個關鍵字開始前呼叫，給畫面顯示進度。
    """
    today = date.today()
    start_date = start_date or (today - timedelta(days=7)).strftime("%Y/%m/%d")
    end_date = end_date or today.strftime("%Y/%m/%d")
    if proc_types is None:
        proc_types = db.get_setting(conn, "procurement_types", [])
    if set(proc_types) >= set(ALL_PROC_TYPES):
        proc_types = []
    keywords = db.get_keywords(conn) if keywords is None else keywords

    summary = RunSummary(start_date, end_date)
    before = conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
    try:
        client = client or make_client()
    except ScrapeError as e:
        for kw in keywords:
            db.log_fetch(conn, kw, 0, "error", f"無法連上採購網：{e}")
            summary.errors[kw] = str(e)
        return summary

    for i, keyword in enumerate(keywords):
        if progress:
            progress(i, len(keywords), keyword)
        if i:
            sleep(QUERY_DELAY)
        fetched_at = datetime.now().isoformat()
        result = search_keyword(client, keyword, start_date, end_date, proc_types, sleep)
        for t in result.tenders:
            t["fetched_at"] = fetched_at
        db.upsert_tenders(conn, result.tenders, keyword)
        summary.total += len(result.tenders)
        if result.error:
            summary.errors[keyword] = result.error
            db.log_fetch(conn, keyword, len(result.tenders), "error", result.error)
        else:
            db.log_fetch(conn, keyword, len(result.tenders), "success")
    summary.new = conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0] - before
    return summary


if __name__ == "__main__":
    conn = db.open_db()
    s = run_scraper(conn, progress=lambda i, n, kw: print(f"[{i + 1}/{n}] {kw}"))
    print(f"完成：{s.start} ~ {s.end}，共 {s.total} 筆，新增 {s.new} 筆")
    for kw, err in s.errors.items():
        print(f"  失敗 {kw}：{err}")
