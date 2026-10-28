"""فهرست صفحات moarek.ir برای دکمه‌ی زیر هر پست؛ هفته‌ای یک بار از sitemap سایت ساخته می‌شود."""
import datetime as dt
import html
import json
import pathlib
import re
from concurrent.futures import ThreadPoolExecutor

import requests

from .sources import UA

SKIP_URL = re.compile(r"/(category|tag|author|cart|checkout|my-account|feed|wp-|product-tag|page/\d+)|[?#]|\.(jpg|jpeg|png|webp|pdf|xml)$", re.I)
SKIP_MAP = re.compile(r"category|tag|author|attachment|elementor|product_cat|product-cat|template|landing", re.I)


def _fetch(url, timeout=12):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        return r.text if r.status_code == 200 else None
    except requests.RequestException:
        return None


def _locs(xml):
    return re.findall(r"<loc>\s*(.*?)\s*</loc>", xml or "", flags=re.S)


def _clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _meta(page):
    t = re.search(r"<title[^>]*>(.*?)</title>", page, flags=re.S | re.I)
    d = (re.search(r'<meta[^>]+(?:name|property)=["\'](?:og:)?description["\'][^>]*content=["\'](.*?)["\']', page, flags=re.S | re.I)
         or re.search(r'<meta[^>]+content=["\'](.*?)["\'][^>]*(?:name|property)=["\'](?:og:)?description["\']', page, flags=re.S | re.I))
    title = re.split(r"\s[-|–—]\s", _clean(t.group(1)))[0] if t else ""
    return title, _clean(d.group(1)) if d else ""


def refresh(cfg, log=print):
    site = cfg["site"]
    base = site["url"].rstrip("/")
    idx = _fetch(base + "/sitemap_index.xml") or _fetch(base + "/sitemap.xml")
    if not idx:
        raise RuntimeError("sitemap در دسترس نبود")
    urls = []
    for sm in _locs(idx):
        if sm.endswith(".xml") and not SKIP_MAP.search(sm):
            urls += [u for u in _locs(_fetch(sm)) if not SKIP_URL.search(u)]
        elif not sm.endswith(".xml") and not SKIP_URL.search(sm):
            urls.append(sm)
    urls = list(dict.fromkeys(urls))[: int(site.get("max_pages", 90))]

    def one(u):
        page = _fetch(u, 10)
        if not page:
            return None
        title, desc = _meta(page)
        if not title or "404" in title or "پیدا نشد" in title:
            return None
        return {"url": u, "title": title[:90], "desc": desc[:160]}

    with ThreadPoolExecutor(6) as ex:
        pages = [p for p in ex.map(one, urls) if p]
    log(f"[سایت] {len(pages)} صفحه از {len(urls)} آدرس sitemap خوانده شد")
    return pages


def load_catalog(cfg, root, now, log=print):
    path = pathlib.Path(root) / cfg["paths"]["site_pages"]
    cached = None
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            cached = None
    pages = list(cached["pages"]) if cached else []
    age_ok = False
    if cached:
        age = now - dt.datetime.fromisoformat(cached["updated"])
        age_ok = age.days < int(cfg["site"].get("refresh_days", 7))
    if not age_ok:
        try:
            got = refresh(cfg, log)
        except Exception as e:
            log(f"[سایت] به‌روزرسانی فهرست صفحات ناموفق: {e}")
            got = []
        if got:
            pages = got
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"updated": now.isoformat(timespec="seconds"), "pages": pages},
                                       ensure_ascii=False, indent=1), encoding="utf-8")
    have = {p["url"] for p in pages}
    for s in cfg["site"].get("seed_pages", []):
        if s["url"] not in have:
            pages.append(dict(s))
    return pages
