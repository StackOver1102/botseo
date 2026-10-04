"""Kiểm tra URL nguồn còn sống và chứa câu trích (§5.8).

Chỉ fetch URL do P10 trả về hoặc người nhập; tôn trọng robots.txt; theo tối đa 5 redirect.
Trích text HTML bằng cách bỏ thẻ (đủ cho so khớp quote); trafilatura/pypdf là nâng cấp sau [CXM].
"""
from __future__ import annotations

import difflib
import html
import ipaddress
import os
import re
import socket
import unicodedata
from dataclasses import dataclass
from typing import Callable
from urllib import robotparser
from urllib.parse import urlsplit

import httpx

ROBOTS_AGENT = "seo-pipeline-factcheck"  # tên dùng khi đọc robots.txt
# Nhiều site Việt Nam chặn UA lạ; dùng UA kiểu trình duyệt nhưng vẫn ghi rõ tên công cụ
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36 "
              f"{ROBOTS_AGENT}/0.2")
# Thử thách cookie bằng JS (vd. laodong.vn): trang chỉ đặt cookie rồi reload
_COOKIE_CHALLENGE = re.compile(r'document\.cookie\s*=\s*"([^"=;]+)=([^";]+)"')
_SCRIPT = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class UrlCheck:
    url: str
    ok: bool  # 200 và chứa quote (substring hoặc fuzzy ≥ ngưỡng)
    status: int | None
    final_url: str | None
    quote_found: bool
    fuzzy_score: float
    note: str = ""


def _clean(s: str) -> str:
    return _WS.sub(" ", unicodedata.normalize("NFC", s)).strip()


def _norm(s: str) -> str:
    return _clean(s).lower()


def html_to_text(raw: str) -> str:
    """Text hiển thị của trang (giữ hoa/thường để đưa cho model đọc)."""
    return _clean(html.unescape(_TAG.sub(" ", _SCRIPT.sub(" ", raw))))


def quote_score(quote: str, text: str) -> float:
    """Tỷ lệ độ dài đoạn khớp liên tục dài nhất trên độ dài quote (không phân biệt hoa/thường)."""
    q, t = _norm(quote), _norm(text)
    if not q:
        return 0.0
    if q in t:
        return 1.0
    m = difflib.SequenceMatcher(None, q, t, autojunk=False).find_longest_match(0, len(q), 0, len(t))
    return m.size / len(q)


def relevant_excerpt(text: str, claim: str, width: int = 3000, windows: int = 2) -> str:
    """Các đoạn (mỗi đoạn 2×width ký tự) khớp claim nhiều nhất, không chồng nhau, theo thứ tự trong trang.
    Claim nhiều ý có thể nằm ở các chỗ khác nhau trong trang nên lấy hơn một đoạn."""
    size = 2 * width
    if len(text) <= size * windows:
        return text
    words = {w for w in re.findall(r"\w{3,}", _norm(claim))}
    low = _norm(text)
    step = width // 2
    scored = sorted(((sum(1 for w in words if w in low[p:p + size]), p) for p in range(0, max(1, len(low) - width), step)),
                    reverse=True)
    picked: list[int] = []
    for _score, pos in scored:
        if all(abs(pos - q) >= size for q in picked):
            picked.append(pos)
        if len(picked) == windows:
            break
    return "\n…\n".join(text[p:p + size] for p in sorted(picked))


def is_public_url(url: str) -> bool:
    """False nếu host là địa chỉ nội bộ (loopback, mạng riêng, link-local/metadata cloud, reserved) hoặc không phân
    giải được. URL đến từ output của model và từ form UI nên không được phép chạm vào dịch vụ nội bộ (SSRF)."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified):
            return False
    return True


def _block_private(request: httpx.Request) -> None:
    """Event hook của httpx: chạy cho mọi request, kể cả từng bước redirect."""
    if os.environ.get("SEO_ALLOW_PRIVATE_URLS") == "1":  # chỉ để thử với server nội bộ
        return
    if not is_public_url(str(request.url)):
        raise httpx.RequestError(f"chặn URL nội bộ/không phân giải được: {request.url.host}", request=request)


class UrlVerifier:
    def __init__(self, http: httpx.Client | None = None, fuzzy_min: float = 0.9):
        self.http = http or httpx.Client(timeout=20, follow_redirects=True, max_redirects=5,
                                         headers={"User-Agent": USER_AGENT, "Accept-Language": "vi,en;q=0.8"},
                                         event_hooks={"request": [_block_private]})
        self.fuzzy_min = fuzzy_min
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._pages: dict[str, tuple[int | None, str | None, str]] = {}
        self._raw: dict[str, str] = {}  # HTML gốc, để trích nội dung chính (main_text)

    def _allowed(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
        except ValueError:  # URL sai định dạng do model trả (vd. "http://[::1"): coi như không tải được
            return False
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                r = self.http.get(f"{base}/robots.txt")
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except (httpx.HTTPError, httpx.InvalidURL):
                rp.parse([])
            self._robots[base] = rp
        return self._robots[base].can_fetch(ROBOTS_AGENT, url)

    def _fetch(self, url: str) -> tuple[int | None, str | None, str]:
        if url not in self._pages:
            try:
                r = self.http.get(url)
                if len(r.content) < 2000 and (m := _COOKIE_CHALLENGE.search(r.text)):
                    # Trang chỉ đặt cookie bằng JS rồi reload: đặt cookie đó và tải lại một lần
                    r = self.http.get(url, cookies={m.group(1): m.group(2)})
                ctype = r.headers.get("content-type", "")
                is_html = "html" in ctype or not ctype
                text = html_to_text(r.text) if is_html else _clean(r.text)
                self._pages[url] = (r.status_code, str(r.url), text)
                if is_html:
                    self._raw[url] = r.text
            except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:
                # Không cache lỗi: timeout thoáng qua mà nhớ mãi thì nguồn bị coi là chết tới khi restart worker,
                # và claim đúng bị xoá khỏi bài. Lần gọi sau tải lại.
                return None, None, f"error: {e}"
        return self._pages[url]

    def verify(self, url: str, quote: str) -> UrlCheck:
        if not url.startswith(("http://", "https://")):
            return UrlCheck(url, False, None, None, False, 0.0, "url không hợp lệ")
        if not self._allowed(url):
            return UrlCheck(url, False, None, None, False, 0.0, "robots.txt chặn")
        status, final, text = self._fetch(url)
        if status != 200:
            return UrlCheck(url, False, status, final, False, 0.0, text if status is None else f"HTTP {status}")
        score = quote_score(quote, text)
        found = score >= self.fuzzy_min
        return UrlCheck(url, found, status, final, found, score, "" if score == 1.0 else "fuzzy")

    def fetch_text(self, url: str) -> tuple[int | None, str]:
        """(HTTP status, text hiển thị) — None khi URL sai, bị robots.txt chặn hoặc lỗi mạng."""
        if not url.startswith(("http://", "https://")) or not self._allowed(url):
            return None, ""
        status, _final, text = self._fetch(url)
        return status, text if status == 200 else ""


    def main_text(self, url: str) -> tuple[int | None, str]:
        """Như fetch_text nhưng chỉ lấy nội dung chính của bài (bỏ menu, sidebar, footer) — dùng làm tư liệu nhánh B."""
        status, text = self.fetch_text(url)
        raw = self._raw.get(url)
        if status != 200 or not raw:
            return status, text
        try:
            import trafilatura
            main = trafilatura.extract(raw, url=url, include_comments=False, include_tables=True, favor_precision=True)
        except Exception:  # noqa: BLE001 — trích lỗi thì dùng text cả trang
            main = None
        if not main:  # trang danh mục/trang chủ: không có bài chính, text cả trang chỉ là menu
            return status, ""
        return status, "\n".join(_clean(line) for line in main.splitlines() if line.strip())  # giữ xuống dòng giữa đoạn


    def article_markdown(self, url: str) -> tuple[int | None, str]:
        """Bài chính của trang dạng Markdown (giữ heading, danh sách, bảng) — dùng cho nhánh A dán link bài cũ.
        Không kiểm tra robots.txt: đây là bài của chính site do người dùng đưa vào."""
        if not url.startswith(("http://", "https://")):
            return None, ""
        status, _final, _text = self._fetch(url)
        raw = self._raw.get(url)
        if status != 200 or not raw:
            return status, ""
        try:
            import trafilatura
            md = trafilatura.extract(raw, url=url, output_format="markdown", include_tables=True, include_comments=False,
                                     include_formatting=True, favor_precision=True)
            md = (md or "").strip()
            if md and not md.startswith("# "):  # tiêu đề thường nằm ngoài khối bài: lấy từ metadata (og:title/<title>)
                meta = trafilatura.extract_metadata(raw)
                if meta and meta.title:
                    md = f"# {meta.title.strip()}\n\n{md}"
        except Exception:  # noqa: BLE001
            md = None
        return status, (md or "").strip()


Verifier = Callable[[str, str], UrlCheck]
Fetcher = Callable[[str], tuple[int | None, str]]
