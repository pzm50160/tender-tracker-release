"""
Windows 工作排程：每天自動搜尋。
排程執行的是 launcher 的 --auto-search 模式（打包版是 exe 本身，開發版是 pythonw launcher.py）。
"""
import os
import re
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape

TASK_NAME = "標案追蹤_自動搜尋"     # 與舊版不同名，並行期間互不覆蓋
OLD_TASK_NAME = "健檢標案自動搜尋"  # 舊版程式建立的排程名稱
APP_DIR = os.path.dirname(os.path.abspath(__file__))
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def auto_search_command() -> tuple[str, str]:
    """回傳 (執行檔, 參數)"""
    if getattr(sys, "frozen", False):
        return sys.executable, "--auto-search"
    python = sys.executable
    pythonw = os.path.join(os.path.dirname(python), "pythonw.exe")   # 不跳黑色視窗
    if os.path.exists(pythonw):
        python = pythonw
    return python, f'"{os.path.join(APP_DIR, "launcher.py")}" --auto-search'


def _run(args: list[str]) -> subprocess.CompletedProcess:
    # schtasks 輸出是系統語系編碼（繁中 Windows 為 cp950）
    return subprocess.run(["schtasks", *args], capture_output=True, text=True,
                          encoding="cp950", errors="replace", creationflags=_NO_WINDOW)


def _query_xml(name: str) -> str | None:
    r = subprocess.run(["schtasks", "/query", "/tn", name, "/xml"], capture_output=True,
                       creationflags=_NO_WINDOW)
    if r.returncode != 0:
        return None
    raw = r.stdout
    # XML 宣告寫 UTF-16，但 schtasks 輸出到管線時其實是系統編碼；只有真的帶 BOM 才當 UTF-16
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp950", errors="replace")


def _daily_time(xml: str) -> str:
    m = re.search(r"<StartBoundary>[^<]*T(\d{2}:\d{2})", xml)
    return m.group(1) if m else ""


def query_task() -> dict | None:
    """回傳 {"time": "08:00", "next_run": str}；沒有排程時回傳 None"""
    xml = _query_xml(TASK_NAME)
    if xml is None:
        return None
    info = {"time": _daily_time(xml), "next_run": ""}
    r = _run(["/query", "/tn", TASK_NAME, "/fo", "LIST", "/v"])
    for line in r.stdout.splitlines():
        key, _, value = line.partition(":")
        if key.strip() in ("下次執行時間", "Next Run Time"):
            info["next_run"] = value.strip()
    return info


def query_old_task() -> str | None:
    """舊版排程存在時回傳每日執行時間（HH:MM），否則 None"""
    xml = _query_xml(OLD_TASK_NAME)
    return (_daily_time(xml) or "08:00") if xml is not None else None


def task_xml(hhmm: str) -> str:
    exe, args = auto_search_command()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>健檢標案追蹤：每日自動搜尋政府電子採購網</Description></RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T{hhmm}:00</StartBoundary>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType></Principal></Principals>
  <Settings>
    <StartWhenAvailable>true</StartWhenAvailable>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(exe)}</Command>
      <Arguments>{escape(args)}</Arguments>
      <WorkingDirectory>{escape(os.path.dirname(exe) if getattr(sys, "frozen", False) else APP_DIR)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def create_task(hhmm: str) -> tuple[bool, str]:
    """
    每天 hhmm 執行。錯過時間（關機、睡眠）開機後會補跑；用電池也會跑；最多跑 1 小時。
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "task.xml")
        with open(path, "w", encoding="utf-16") as f:
            f.write(task_xml(hhmm))
        r = _run(["/create", "/tn", TASK_NAME, "/xml", path, "/f"])
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def delete_task(name: str = TASK_NAME) -> tuple[bool, str]:
    r = _run(["/delete", "/tn", name, "/f"])
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def run_now() -> tuple[bool, str]:
    """立刻觸發一次排程（用來確認這台電腦的排程能正常執行）"""
    r = _run(["/run", "/tn", TASK_NAME])
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def replace_old_task() -> tuple[bool, str]:
    """用舊版排程的時間建立新版排程，成功後才停用舊版排程"""
    hhmm = query_old_task()
    if hhmm is None:
        return False, "找不到舊版排程"
    ok, msg = create_task(hhmm)
    if not ok:
        return False, f"建立新版排程失敗：{msg}"
    ok, msg = delete_task(OLD_TASK_NAME)
    if not ok:
        return False, f"新版排程已建立（每天 {hhmm}），但停用舊版排程失敗：{msg}"
    return True, hhmm
