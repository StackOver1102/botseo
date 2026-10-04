"""Tự cập nhật cookie cho proxy Gemini cục bộ (providers.gemini.cookie_sync).

Extension Chrome (đang đăng nhập Gemini) gửi header Cookie của gemini.google.com về /external/gemini-cookies theo chu kỳ.
Ở đây: proxy còn hoạt động thì bỏ qua; proxy báo cookie hết hạn thì ghi cookie mới vào .env của proxy và tạo lại
container. Cookie chỉ đi từ Chrome → localhost → file .env trên máy này; không ghi vào log, DB hay artifact.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

import httpx

from .config import ROOT

log = logging.getLogger(__name__)
REQUIRED = ("__Secure-1PSID", "__Secure-1PSIDTS")
_lock = threading.Lock()
_last: dict[str, float | str] = {"hash": "", "at": 0.0}


class CookieSyncError(Exception):
    pass


def _settings(config: dict) -> dict | None:
    gem = config.get("providers", {}).get("gemini", {})
    sync = gem.get("cookie_sync") or {}
    if gem.get("mode") != "proxy" or not sync.get("enabled"):
        return None
    base = gem.get("proxy_base", "http://127.0.0.1:4981/gemini/v1beta")
    root = base.split("/gemini/")[0]
    proxy_dir = Path(sync.get("proxy_dir", "../gemini-web-to-api"))
    return {"models_url": f"{root}/openai/v1/models", "min_interval": sync.get("min_interval_seconds", 300),
            "proxy_dir": proxy_dir if proxy_dir.is_absolute() else (ROOT / proxy_dir).resolve(),
            "restart": sync.get("restart_command", ["docker", "compose", "up", "-d", "--force-recreate"])}


def proxy_healthy(models_url: str) -> bool:
    """Proxy đăng nhập được Gemini thì /openai/v1/models liệt kê model; cookie hết hạn thì data = null."""
    try:
        r = httpx.get(models_url, timeout=5)
        return r.status_code == 200 and bool(r.json().get("data"))
    except (httpx.HTTPError, ValueError):
        return False


def cookie_names(header: str) -> set[str]:
    return {pair.split("=", 1)[0].strip() for pair in header.split(";") if "=" in pair}


def write_env(env_path: Path, cookie_header: str, auth_user: str | None) -> None:
    """Thay dòng GEMINI_COOKIES (và GEMINI_AUTH_USER nếu biết) trong .env của proxy, giữ nguyên các dòng khác.
    Giá trị bọc nháy đơn: docker compose coi là chuỗi nguyên văn (không nội suy $, không cắt ở dấu #)."""
    if any(ch in cookie_header for ch in "\r\n"):
        raise CookieSyncError("cookie chứa ký tự xuống dòng")
    value = f"'{cookie_header}'" if "'" not in cookie_header else cookie_header
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    out, seen_cookie, seen_user = [], False, False
    for line in lines:
        if re.match(r"^\s*GEMINI_COOKIES\s*=", line):
            out.append(f"GEMINI_COOKIES={value}")
            seen_cookie = True
        elif re.match(r"^\s*GEMINI_AUTH_USER\s*=", line) and auth_user is not None:
            out.append(f"GEMINI_AUTH_USER={auth_user}")
            seen_user = True
        else:
            out.append(line)
    if not seen_cookie:
        out.append(f"GEMINI_COOKIES={value}")
    if auth_user is not None and not seen_user:
        out.append(f"GEMINI_AUTH_USER={auth_user}")
    tmp = env_path.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, env_path)


def _restart(command: list[str], cwd: Path) -> None:
    try:
        r = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CookieSyncError(f"không chạy được lệnh khởi động lại proxy ({command[0]}): {e}") from e
    if r.returncode != 0:
        raise CookieSyncError(f"khởi động lại proxy lỗi: {(r.stderr or r.stdout)[-300:]}")


def sync(config: dict, cookie_header: str, auth_user: str | None, *,
         healthy: Callable[[str], bool] = proxy_healthy, restart: Callable[[list[str], Path], None] = _restart,
         wait_seconds: float = 30) -> dict:
    """Trả {updated, healthy, reason}. Không bao giờ đưa giá trị cookie vào kết quả hay log."""
    s = _settings(config)
    if s is None:
        return {"updated": False, "healthy": None, "reason": "cookie_sync tắt hoặc gemini không ở mode proxy"}
    missing = [n for n in REQUIRED if n not in cookie_names(cookie_header)]
    if missing:
        return {"updated": False, "healthy": None, "reason": f"cookie thiếu {missing}: Chrome chưa đăng nhập Google"}
    with _lock:
        if healthy(s["models_url"]):
            return {"updated": False, "healthy": True, "reason": "proxy đang hoạt động, giữ cookie hiện tại"}
        digest = hashlib.sha256(cookie_header.encode()).hexdigest()
        if digest == _last["hash"] and time.monotonic() - float(_last["at"]) < s["min_interval"]:
            # Cùng cookie vừa áp mà proxy vẫn lỗi: khởi động lại nữa cũng vậy (sai tài khoản / Google chặn / proxy hỏng)
            return {"updated": False, "healthy": False, "reason": "cookie này vừa được áp nhưng proxy vẫn chưa đăng nhập được"}
        env_path = s["proxy_dir"] / ".env"
        if not s["proxy_dir"].is_dir():
            raise CookieSyncError(f"không thấy thư mục proxy: {s['proxy_dir']}")
        write_env(env_path, cookie_header, auth_user)
        _last.update(hash=digest, at=time.monotonic())
        restart(s["restart"], s["proxy_dir"])
        deadline = time.monotonic() + wait_seconds
        ok = healthy(s["models_url"])
        while not ok and time.monotonic() < deadline:
            time.sleep(2)
            ok = healthy(s["models_url"])
        log.info("gemini cookie sync: đã ghi cookie mới (%d cookie) và khởi động lại proxy; đăng nhập %s",
                 len(cookie_names(cookie_header)), "được" if ok else "CHƯA được")
        return {"updated": True, "healthy": ok,
                "reason": "đã cập nhật cookie và khởi động lại proxy" + ("" if ok else " nhưng proxy chưa đăng nhập được")}
