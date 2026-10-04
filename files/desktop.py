"""
桌面模式：啟動畫面伺服器、開視窗；視窗關掉（按 ✕）程式就整個結束。
放在 exe 旁邊，可以線上更新（launcher.py 在 exe 裡面，只負責呼叫這裡）。
"""
import ctypes
import ctypes.wintypes
import logging
import os
import socket
import subprocess
import sys
import time

WINDOW_TITLE = "健檢標案追蹤系統"     # 與 app.py 的 page_title 相同，用來找程式視窗
CLOSE_GRACE = 5                       # 視窗消失超過幾秒才算關閉（重新整理時標題會短暫變化）
OPEN_TIMEOUT = 90                     # 開視窗後幾秒內都沒出現，就再開一次
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
user32 = ctypes.windll.user32

log = logging.getLogger("launcher")


# ── 視窗 ────────────────────────────────────────────────

def find_window() -> int | None:
    """找標題含「健檢標案追蹤系統」的可見視窗（最小化也算）"""
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if WINDOW_TITLE in buf.value:
                    found.append(hwnd)
                    return False
        return True

    user32.EnumWindows(callback, 0)
    return found[0] if found else None


def focus_window() -> bool:
    hwnd = find_window()
    if hwnd:
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    return bool(hwnd)


def find_browser() -> str | None:
    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    return next((p for p in candidates if os.path.exists(p)), None)


def open_window(port: int) -> None:
    url = f"http://127.0.0.1:{port}"
    browser = find_browser()
    if browser:
        subprocess.Popen([browser, f"--app={url}", "--window-size=1280,860"])
    else:
        import webbrowser
        webbrowser.open(url)


def message_box(text: str) -> None:
    user32.MessageBoxW(0, text, WINDOW_TITLE, 0x10)


# ── 畫面伺服器 ──────────────────────────────────────────

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def stop_server(proc) -> None:
    if proc and proc.poll() is None:
        subprocess.call(["taskkill", "/F", "/T", "/PID", str(proc.pid)], creationflags=NO_WINDOW,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def backup_on_exit() -> None:
    try:
        import backup
        import db
        conn = db.open_db()
        try:
            r = backup.make_backup(conn, "關閉")
            log.info("關閉前備份：%s", "資料沒變，略過" if r.skipped else "、".join(r.created))
            for w in r.warnings:
                log.warning(w)
        finally:
            conn.close()
    except Exception:
        log.exception("關閉前備份失敗")


# ── 主流程 ──────────────────────────────────────────────

def run(app_dir: str, start_server, already_running: bool) -> None:
    """
    start_server(port) -> Popen：由 launcher 提供（打包版與開發版啟動方式不同）。
    already_running：已有一份程式在執行時為 True，只把那份的視窗叫出來。
    """
    port_file = os.path.join(app_dir, "data", ".port")
    restart_flag = os.path.join(app_dir, "data", ".restart")

    if already_running:
        log.info("已有一份在執行，改為開啟既有視窗")
        if not focus_window():
            try:
                open_window(int(open(port_file).read()))
            except (OSError, ValueError):
                message_box("程式已在執行中，請稍候視窗出現。")
        return

    os.makedirs(os.path.dirname(port_file), exist_ok=True)
    port = free_port()
    with open(port_file, "w") as f:
        f.write(str(port))
    if os.path.exists(restart_flag):
        os.remove(restart_flag)

    proc = start_server(port)
    if not wait_for_port(port, 60):
        log.error("畫面伺服器啟動失敗")
        stop_server(proc)
        message_box("無法啟動程式，請查看 logs 資料夾裡的記錄檔。")
        return
    open_window(port)
    opened_at, seen, missing_since, reopened, crashes = time.time(), False, None, False, 0

    while True:
        time.sleep(1)
        if os.path.exists(restart_flag):                  # 線上更新完成：重啟伺服器讓新程式生效
            os.remove(restart_flag)
            log.info("程式已更新，重新啟動畫面伺服器")
            stop_server(proc)
            proc = start_server(port)
            wait_for_port(port, 60)
            continue
        if proc.poll() is not None:                       # 伺服器意外結束：重啟一次
            crashes += 1
            log.error("畫面伺服器意外結束（第 %d 次）", crashes)
            if crashes > 1:
                message_box("程式發生錯誤已停止，請查看 logs 資料夾裡的記錄檔。")
                break
            proc = start_server(port)
            wait_for_port(port, 60)
            continue

        if find_window():
            seen, missing_since = True, None
        elif seen:
            missing_since = missing_since or time.time()
            if time.time() - missing_since >= CLOSE_GRACE:
                log.info("視窗已關閉")
                break
        elif time.time() - opened_at > OPEN_TIMEOUT:
            if reopened:
                log.error("視窗一直沒有出現，程式結束")
                message_box("無法開啟程式視窗，請查看 logs 資料夾裡的記錄檔。")
                break
            log.warning("視窗沒有出現，再開一次")
            open_window(port)
            opened_at, reopened = time.time(), True

    stop_server(proc)
    backup_on_exit()
    try:
        os.remove(port_file)
    except OSError:
        pass
    log.info("程式結束")
