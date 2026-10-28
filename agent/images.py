import io
import pathlib
import re
from html.parser import HTMLParser

import requests
from PIL import Image, ImageDraw, ImageFont

from .sources import UA

BAD_URL = re.compile(r"logo|favicon|placeholder|default|avatar|sprite|pixel", re.I)


class _Meta(HTMLParser):
    def __init__(self):
        super().__init__()
        self.og = ""

    def handle_starttag(self, tag, attrs):
        if tag != "meta" or self.og:
            return
        a = dict(attrs)
        if (a.get("property") in ("og:image", "twitter:image") or a.get("name") in ("twitter:image", "og:image")) \
                and a.get("content"):
            self.og = a["content"]


def find_page_image(url):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        p = _Meta()
        p.feed(r.text[:200000])
        if p.og.startswith("//"):
            return "https:" + p.og
        return p.og
    except Exception:
        return ""


def download_image(url, out_path, max_mb=5):
    """دانلود، اعتبارسنجی و بازذخیره‌ی تصویر. در صورت هر مشکل None برمی‌گرداند."""
    if not url or BAD_URL.search(url):
        return None
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=25, stream=True)
        if r.status_code != 200 or not str(r.headers.get("content-type", "")).startswith("image/"):
            return None
        data = b""
        for chunk in r.iter_content(65536):
            data += chunk
            if len(data) > max_mb * 1024 * 1024:
                return None
        if len(data) < 15000:
            return None
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data)).convert("RGB")
        if img.width < 600 or img.height < 300:
            return None
        if img.width > 1280:
            img = img.resize((1280, int(img.height * 1280 / img.width)))
        img.save(out_path, "JPEG", quality=88)
        return {"kind": "source", "url": url, "size": [img.width, img.height]}
    except Exception:
        return None


def _shape(s):
    import arabic_reshaper
    from bidi.algorithm import get_display
    return get_display(arabic_reshaper.reshape(s))


def make_card(text, label, out_path, font_path):
    """کارت تصویری برند کانال با جمله‌ی کلیدی پست (رایگان و بدون سرویس خارجی)."""
    W, H = 1280, 720
    img = Image.new("RGB", (W, H), "#0f172a")
    d = ImageDraw.Draw(img)
    for y in range(H):  # گرادیان ملایم
        k = y / H
        d.line([(0, y), (W, y)], fill=(int(15 + 14 * k), int(23 + 22 * k), int(42 + 35 * k)))
    d.rectangle([0, 0, 14, H], fill="#2dd4bf")
    basic = ImageFont.Layout.BASIC  # شکل‌دهی را خودمان انجام می‌دهیم؛ موتور raqm دوباره وارونه می‌کرد
    big = ImageFont.truetype(font_path, 62, layout_engine=basic)
    small = ImageFont.truetype(font_path, 28, layout_engine=basic)
    max_w = W - 220
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if d.textlength(_shape(t), font=big) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    lines = lines[:5]
    lh = 100
    y = (H - lh * len(lines)) // 2 - 20
    for ln in lines:
        s = _shape(ln)
        d.text(((W - d.textlength(s, font=big)) / 2, y), s, font=big, fill="#f8fafc")
        y += lh
    d.text((70, H - 80), "GAMIFICATION CLUB", font=small, fill="#2dd4bf")
    if label:
        s = label[:48]
        d.text((W - 70 - d.textlength(s, font=small), H - 80), s, font=small, fill="#94a3b8")
    img.save(out_path, "JPEG", quality=90)
    return {"kind": "card", "text": text}


def pick_image(cand, card_text, cfg, workdir, root, is_used=None):
    out = pathlib.Path(workdir) / "post_image.jpg"
    if cfg.get("use_source_images", True) and cand.source_type == "blog":
        url = cand.image_url or find_page_image(cand.url)
        info = None
        if url and not (is_used and is_used(url)):  # عکس تکراری (مثلاً بنر عمومی سایت) استفاده نمی‌شود
            info = download_image(url, str(out), cfg.get("max_image_mb", 5))
        if info:
            return str(out), info
    font = str(pathlib.Path(root) / cfg["paths"]["font"])
    try:
        info = make_card(card_text, cand.venue or cand.source_name, str(out), font)
        return str(out), info
    except Exception:
        return None, {"kind": "none"}
