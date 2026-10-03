"""
程式自動更新：從公開的發佈倉庫下載新版程式檔。
- 只更新程式旁的 .py 等檔案；exe 內的套件有變動時（min_exe_version 變高）需要下載完整版
- 全部下載並核對 SHA-256 之後才替換；替換前保留上一版在 _previous/，可一鍵退回
- 資料庫與備份不會被碰到
"""
import hashlib
import json
import os
import shutil
import ssl
from urllib.parse import quote

import httpx
import truststore

RELEASE_REPO = "pzm50160/tender-tracker-release"   # 發佈用的公開倉庫（只放程式碼）
RAW_BASE = f"https://raw.githubusercontent.com/{RELEASE_REPO}/main"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_MANIFEST = os.path.join(APP_DIR, "version.json")
STAGING = os.path.join(APP_DIR, "_update")
PREVIOUS = os.path.join(APP_DIR, "_previous")
RESTART_FLAG = os.path.join(APP_DIR, "data", ".restart")


class UpdateError(Exception):
    pass


def parse_version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def is_dev() -> bool:
    """開發中的資料夾（git）不自動更新"""
    return os.path.exists(os.path.join(APP_DIR, ".git"))


def local_manifest() -> dict | None:
    try:
        with open(LOCAL_MANIFEST, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def exe_version() -> str:
    """打包版的 launcher 會透過環境變數告訴畫面自己的版本"""
    return os.environ.get("TENDER_EXE_VERSION", "0.0.0")


def _client() -> httpx.Client:
    return httpx.Client(verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), timeout=20,
                        follow_redirects=True, headers={"Cache-Control": "no-cache"})


def fetch_manifest(client=None) -> dict:
    client = client or _client()
    try:
        r = client.get(f"{RAW_BASE}/manifest.json")
    except httpx.HTTPError as e:
        raise UpdateError("無法檢查更新（請確認網路）") from e
    if r.status_code == 404:
        raise UpdateError("目前沒有可下載的版本")
    if r.status_code != 200:
        raise UpdateError(f"無法檢查更新（伺服器回應 {r.status_code}）")
    try:
        return r.json()
    except ValueError as e:
        raise UpdateError("更新資訊格式錯誤") from e


def has_update(remote: dict, local: dict | None) -> bool:
    if not local:
        return False
    return parse_version(remote["version"]) > parse_version(local["version"])


def needs_full_package(remote: dict) -> bool:
    return parse_version(remote.get("min_exe_version", "0.0.0")) > parse_version(exe_version())


def _sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _target(name: str) -> str:
    path = os.path.normpath(os.path.join(APP_DIR, name))
    if not path.startswith(APP_DIR + os.sep):
        raise UpdateError(f"不合法的檔名：{name}")
    return path


def apply_update(remote: dict, client=None) -> None:
    if needs_full_package(remote):
        raise UpdateError("這次更新包含程式套件的變動，需要下載完整版，請聯絡管理者")
    client = client or _client()
    files: dict[str, str] = remote["files"]
    for name in files:                          # 下載前先擋掉會跳出程式資料夾的檔名
        _target(name)

    # 1. 全部下載到暫存區並核對
    shutil.rmtree(STAGING, ignore_errors=True)
    for name, digest in files.items():
        dest = os.path.join(STAGING, name)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            r = client.get(f"{RAW_BASE}/files/{quote(name)}")
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise UpdateError(f"下載 {name} 失敗：{e}") from e
        with open(dest, "wb") as f:
            f.write(r.content)
        if _sha256(dest) != digest:
            raise UpdateError(f"{name} 檔案內容不符（下載不完整或被竄改），已取消更新")

    # 2. 保留目前版本
    shutil.rmtree(PREVIOUS, ignore_errors=True)
    current = local_manifest() or {"files": {}}
    for name in set(current.get("files", {})) | set(files):
        src = _target(name)
        if os.path.exists(src):
            os.makedirs(os.path.dirname(os.path.join(PREVIOUS, name)), exist_ok=True)
            shutil.copy2(src, os.path.join(PREVIOUS, name))
    if os.path.exists(LOCAL_MANIFEST):
        shutil.copy2(LOCAL_MANIFEST, os.path.join(PREVIOUS, "version.json"))

    # 3. 替換；中途失敗就退回
    try:
        for name in files:
            os.makedirs(os.path.dirname(_target(name)), exist_ok=True)
            os.replace(os.path.join(STAGING, name), _target(name))
        with open(LOCAL_MANIFEST, "w", encoding="utf-8") as f:
            json.dump(remote, f, ensure_ascii=False, indent=2)
    except OSError as e:
        rollback()
        raise UpdateError(f"替換檔案失敗，已退回原版本：{e}") from e
    shutil.rmtree(STAGING, ignore_errors=True)
    request_restart()


def can_rollback() -> bool:
    return os.path.exists(os.path.join(PREVIOUS, "version.json"))


def rollback() -> None:
    if not os.path.isdir(PREVIOUS):
        raise UpdateError("沒有上一版可以退回")
    for root, _, names in os.walk(PREVIOUS):
        for n in names:
            src = os.path.join(root, n)
            rel = os.path.relpath(src, PREVIOUS)
            shutil.copy2(src, _target(rel))
    request_restart()


def request_restart() -> None:
    """通知 launcher 重新啟動畫面伺服器，讓新程式生效"""
    os.makedirs(os.path.dirname(RESTART_FLAG), exist_ok=True)
    open(RESTART_FLAG, "w").close()
