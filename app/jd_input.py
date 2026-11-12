"""Đọc JD từ text / link / PDF / ảnh. Làm hết phần có thể ở máy local trước khi gọi Claude."""
from __future__ import annotations

import io
import re

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

MAX_CHARS = 40_000  # ~10K token; JD thật hiếm khi dài hơn
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def clean_text(t: str) -> str:
    t = re.sub(r"[ \t ]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n\n", t)
    return t.strip()


def _limit(t: str) -> tuple[str, str | None]:
    if len(t) > MAX_CHARS:
        return t[:MAX_CHARS], f"JD dài {len(t):,} ký tự, chỉ dùng {MAX_CHARS:,} ký tự đầu."
    return t, None


def from_url(url: str) -> tuple[str, str | None]:
    if not re.match(r"^https?://", url):
        raise ValueError("Link phải bắt đầu bằng http:// hoặc https://")
    r = httpx.get(url, follow_redirects=True, timeout=25,
                  headers={"User-Agent": "Mozilla/5.0 (CV-Tailor local app)", "Accept-Language": "vi,en;q=0.8"})
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    if "pdf" in ctype:
        return from_pdf_text(r.content)
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = clean_text(main.get_text("\n"))
    if len(text) < 300:
        raise ValueError("Trang này gần như không có nội dung đọc được (có thể cần đăng nhập hoặc chạy JavaScript, "
                         "ví dụ LinkedIn). Hãy dán text JD hoặc chụp màn hình.")
    return _limit(text)


def from_pdf_text(data: bytes) -> tuple[str, str | None]:
    """Trích text từ PDF ở local. Trả về '' nếu là PDF scan (sẽ gửi PDF cho Claude đọc)."""
    reader = PdfReader(io.BytesIO(data))
    text = clean_text("\n".join((p.extract_text() or "") for p in reader.pages))
    if len(text) < 200:
        return "", None
    return _limit(text)
