"""Extension gửi cookie Gemini; server chỉ ghi vào .env của proxy và khởi động lại khi proxy báo hết hạn."""
import pytest
from fastapi.testclient import TestClient

from pipeline import gemini_cookies as gc

GOOD = "SID=abc; __Secure-1PSID=psid-moi; __Secure-1PSIDTS=ts-moi; NID=x#y $HOME"


@pytest.fixture
def proxy(tmp_path, config):
    (tmp_path / ".env").write_text("# ghi chú\nGEMINI_COOKIES=cu\nGEMINI_AUTH_USER=2\nGEMINI_TEMPORARY=true\n", encoding="utf-8")
    config["providers"]["gemini"]["cookie_sync"] = {"enabled": True, "proxy_dir": str(tmp_path), "min_interval_seconds": 300}
    gc._last.update(hash="", at=0.0)
    return tmp_path


def run(config, header, auth_user, states):
    """states: kết quả lần lượt của các lần kiểm tra proxy (lần cuối lặp lại)."""
    calls = {"restart": 0, "health": 0}

    def healthy(url):
        calls["health"] += 1
        return states[min(calls["health"], len(states)) - 1]

    def restart(command, cwd):
        calls["restart"] += 1
        calls["cmd"], calls["cwd"] = command, cwd

    return gc.sync(config, header, auth_user, healthy=healthy, restart=restart, wait_seconds=0), calls


def test_healthy_proxy_is_left_alone(config, proxy):
    res, calls = run(config, GOOD, "1", [True])
    assert res == {"updated": False, "healthy": True, "reason": "proxy đang hoạt động, giữ cookie hiện tại"}
    assert calls["restart"] == 0 and "GEMINI_COOKIES=cu" in (proxy / ".env").read_text(encoding="utf-8")


def test_expired_cookie_is_replaced_and_proxy_restarted(config, proxy):
    res, calls = run(config, GOOD, "1", [False, True])
    assert res["updated"] and res["healthy"] and calls["restart"] == 1 and calls["cwd"] == proxy
    assert calls["cmd"] == ["docker", "compose", "up", "-d", "--force-recreate"]
    env = (proxy / ".env").read_text(encoding="utf-8").splitlines()
    # nháy đơn: docker compose không nội suy $HOME, không cắt ở "#"; các dòng khác giữ nguyên
    assert env == ["# ghi chú", f"GEMINI_COOKIES='{GOOD}'", "GEMINI_AUTH_USER=1", "GEMINI_TEMPORARY=true"]
    assert GOOD not in str(res) and "psid-moi" not in str(res)  # kết quả không lộ cookie

    # Cùng cookie gửi lại ngay mà proxy vẫn lỗi: không khởi động lại liên tục
    res2, calls2 = run(config, GOOD, "1", [False])
    assert res2["updated"] is False and res2["healthy"] is False and calls2["restart"] == 0
    # Cookie khác (đã xoay vòng) thì áp tiếp
    res3, calls3 = run(config, GOOD.replace("ts-moi", "ts-moi-hon"), None, [False, False])
    assert res3["updated"] and res3["healthy"] is False and calls3["restart"] == 1
    env = (proxy / ".env").read_text(encoding="utf-8")
    assert "ts-moi-hon" in env and "GEMINI_AUTH_USER=1" in env  # auth_user None → giữ số cũ


def test_rejects_incomplete_cookie_and_respects_config(config, proxy):
    res, calls = run(config, "SID=abc; __Secure-1PSID=x", None, [False])
    assert res["updated"] is False and "__Secure-1PSIDTS" in res["reason"] and calls["restart"] == 0 and calls["health"] == 0
    with pytest.raises(gc.CookieSyncError):
        gc.write_env(proxy / ".env", "a=b\nGEMINI_AUTH_USER=9", None)  # không cho chèn dòng vào .env
    config["providers"]["gemini"]["cookie_sync"]["enabled"] = False
    assert run(config, GOOD, None, [False])[0]["updated"] is False
    config["providers"]["gemini"]["cookie_sync"]["enabled"] = True
    config["providers"]["gemini"]["mode"] = "external"
    assert run(config, GOOD, None, [False])[0]["updated"] is False


def test_endpoint_requires_extension_token(conn, db_url, monkeypatch):
    monkeypatch.setenv("SEO_DATABASE_URL", db_url)
    monkeypatch.setenv("SEO_EXTENSION_TOKEN", "bi-mat")
    from app import main
    seen = {}
    monkeypatch.setattr("pipeline.gemini_cookies.sync", lambda cfg, header, user: seen.update(h=header, u=user) or {"updated": False, "reason": "ok"})
    c = TestClient(main.app)
    body = {"cookie_header": GOOD, "auth_user": "2"}
    assert c.post("/external/gemini-cookies", json=body, headers={"X-Extension-Token": "sai", "X-Client-Id": "c"}).status_code == 401
    assert c.post("/external/gemini-cookies", json={**body, "auth_user": "abc"},
                  headers={"X-Extension-Token": "bi-mat", "X-Client-Id": "c"}).status_code == 422
    r = c.post("/external/gemini-cookies", json=body, headers={"X-Extension-Token": "bi-mat", "X-Client-Id": "c"})
    assert r.status_code == 200 and r.json() == {"updated": False, "reason": "ok"} and seen == {"h": GOOD, "u": "2"}
    # Extension không đọc được cookie: báo lý do, không gọi sync
    seen.clear()
    r = c.post("/external/gemini-cookies", json={"cookie_header": "", "auth_user": None, "error": "thiếu quyền"},
               headers={"X-Extension-Token": "bi-mat", "X-Client-Id": "c"})
    assert r.status_code == 200 and r.json()["updated"] is False and "thiếu quyền" in r.json()["reason"] and seen == {}
