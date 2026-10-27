import datetime as dt
import html
import re
import time
from dataclasses import dataclass, field, asdict

import feedparser
import requests

from .textutil import strip_html, norm_title

UA = "GamificationClubAgent/1.0 (research digest bot)"


@dataclass
class Candidate:
    url: str
    title: str
    source_name: str
    source_type: str            # paper | blog
    abstract: str = ""
    published: str = ""
    doi: str = ""
    authors: str = ""
    venue: str = ""
    image_url: str = ""
    license: str = ""
    pmcid: str = ""
    is_oa: bool = False

    def to_dict(self):
        d = asdict(self)
        d["norm_title"] = norm_title(self.title)
        return d


def rebuild_abstract(inv):
    if not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


class Collector:
    def __init__(self, cfg, now, log=print, sleep=time.sleep):
        self.cfg = cfg
        self.now = now
        self.log = log
        self.sleep = sleep
        self.health = {}

    # ---------- شبکه
    def _get(self, url, params=None, as_json=True):
        last = ""
        for _ in range(2):
            try:
                r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=15)
                if r.status_code == 200:
                    return r.json() if as_json else r.content
                last = f"HTTP {r.status_code}"
            except (requests.RequestException, ValueError) as e:
                last = str(e)[:150]
            self.sleep(1.5)
        raise RuntimeError(last)

    def _since(self):
        d = self.now - dt.timedelta(days=int(self.cfg.get("lookback_days", 150)))
        return d.date().isoformat()

    def _record(self, name, items, err=None):
        h = self.health.setdefault(name, {"n": 0, "errors": []})
        h["n"] += len(items)
        if err:
            h["errors"].append(err)

    # ---------- OpenAlex
    def openalex(self, term):
        data = self._get("https://api.openalex.org/works", {
            "search": term,
            "filter": f"from_publication_date:{self._since()},has_abstract:true,type:article",
            "per-page": self.cfg.get("per_term_results", 8),
            "select": "id,doi,title,publication_date,abstract_inverted_index,primary_location,"
                      "authorships,open_access,best_oa_location"})
        out = []
        for w in data.get("results", []):
            title = w.get("title") or ""
            abstract = rebuild_abstract(w.get("abstract_inverted_index"))
            doi = (w.get("doi") or "").replace("https://doi.org/", "")
            if not title or len(abstract) < 200:
                continue
            loc = w.get("primary_location") or {}
            venue = ((loc.get("source") or {}).get("display_name")) or ""
            best = w.get("best_oa_location") or {}
            authors = ", ".join(a["author"]["display_name"] for a in (w.get("authorships") or [])[:4]
                                if a.get("author"))
            out.append(Candidate(
                url=f"https://doi.org/{doi}" if doi else w.get("id", ""), title=title,
                source_name="OpenAlex", source_type="paper", abstract=abstract,
                published=w.get("publication_date") or "", doi=doi, authors=authors, venue=venue,
                license=best.get("license") or "", is_oa=bool((w.get("open_access") or {}).get("is_oa"))))
        return out

    # ---------- Crossref
    def crossref(self, term):
        data = self._get("https://api.crossref.org/works", {
            "query.bibliographic": term,
            "filter": f"from-pub-date:{self._since()},type:journal-article,has-abstract:true",
            "rows": self.cfg.get("per_term_results", 8), "sort": "published", "order": "desc",
            "select": "DOI,title,abstract,author,container-title,published,URL,license"})
        out = []
        for w in data.get("message", {}).get("items", []):
            title = (w.get("title") or [""])[0]
            abstract = strip_html(w.get("abstract") or "")
            abstract = re.sub(r"^Abstract\s*", "", abstract)
            doi = w.get("DOI") or ""
            if not title or len(abstract) < 200 or not doi:
                continue
            parts = ((w.get("published") or {}).get("date-parts") or [[]])[0]
            pub = "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts))
            authors = ", ".join(f"{a.get('given', '')} {a.get('family', '')}".strip()
                                for a in (w.get("author") or [])[:4])
            out.append(Candidate(
                url=f"https://doi.org/{doi}", title=strip_html(title), source_name="Crossref",
                source_type="paper", abstract=abstract, published=pub, doi=doi, authors=authors,
                venue=(w.get("container-title") or [""])[0]))
        return out

    # ---------- Europe PMC
    def europepmc(self, term):
        today = self.now.date().isoformat()
        data = self._get("https://www.ebi.ac.uk/europepmc/webservices/rest/search", {
            "query": f'("{term}") AND (FIRST_PDATE:[{self._since()} TO {today}]) AND HAS_ABSTRACT:y',
            "format": "json", "resultType": "core", "pageSize": self.cfg.get("per_term_results", 8)})
        out = []
        for w in data.get("resultList", {}).get("result", []):
            title = strip_html(w.get("title") or "")
            abstract = strip_html(w.get("abstractText") or "")
            doi = w.get("doi") or ""
            if not title or len(abstract) < 200:
                continue
            url = f"https://doi.org/{doi}" if doi else f"https://europepmc.org/article/{w.get('source')}/{w.get('id')}"
            out.append(Candidate(
                url=url, title=title, source_name="Europe PMC", source_type="paper", abstract=abstract,
                published=w.get("firstPublicationDate") or "", doi=doi, authors=w.get("authorString") or "",
                venue=w.get("journalTitle") or "", license=w.get("license") or "",
                pmcid=w.get("pmcid") or "", is_oa=w.get("isOpenAccess") == "Y"))
        return out

    # ---------- RSS
    def rss(self, feed):
        raw = self._get(feed["url"], as_json=False)
        parsed = feedparser.parse(raw)
        cutoff = self.now - dt.timedelta(days=int(self.cfg.get("lookback_days", 150)))
        out = []
        for e in parsed.entries[:25]:
            link = e.get("link") or ""
            title = strip_html(e.get("title") or "")
            if not link or not title:
                continue
            pub = ""
            if e.get("published_parsed"):
                d = dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc)
                if d < cutoff:
                    continue
                pub = d.date().isoformat()
            img = ""
            for key in ("media_content", "media_thumbnail"):
                if e.get(key):
                    img = e[key][0].get("url", "")
                    break
            if not img:
                for enc in e.get("enclosures", []) or []:
                    if str(enc.get("type", "")).startswith("image"):
                        img = enc.get("href", "")
                        break
            summary = strip_html(e.get("summary") or "")
            out.append(Candidate(url=link, title=title, source_name=feed["name"], source_type="blog",
                                 abstract=summary, published=pub, authors=e.get("author", ""),
                                 venue=feed["name"], image_url=img))
        return out

    # ---------- اجرا
    def collect(self):
        terms = self.cfg.get("search_terms", [])
        jobs = []
        for t in terms:
            jobs.append(("OpenAlex", self.openalex, t))
            jobs.append(("Europe PMC", self.europepmc, t))
        for t in terms[: int(self.cfg.get("crossref_max_terms", 4))]:
            jobs.append(("Crossref", self.crossref, t))
        for f in self.cfg.get("rss_feeds", []):
            jobs.append((f["name"], self.rss, f))
        items = []
        t0 = time.time()
        deadline = float(self.cfg.get("collect_deadline_s", 170))
        for name, fn, arg in jobs:
            if time.time() - t0 > deadline:
                self._record(name, [], "زمان جمع‌آوری تمام شد")
                self.log(f"[منبع] {name}: رد شد (سقف زمان جمع‌آوری)")
                continue
            try:
                t1 = time.time()
                got = fn(arg)
                self.log(f"[منبع] {name}: {len(got)} مطلب ({time.time() - t1:.1f}s)")
                self._record(name, got)
                items += got
            except Exception as e:  # هر منبع مستقل است؛ خطای یکی بقیه را متوقف نمی‌کند
                self._record(name, [], f"{type(e).__name__}: {e}")
                self.log(f"[منبع] {name} خطا: {e}")
            self.sleep(0.3)
        seen, uniq = set(), []
        for c in items:
            key = (c.doi or c.url).lower()
            nt = norm_title(c.title)
            if key in seen or nt in seen:
                continue
            seen.add(key)
            seen.add(nt)
            uniq.append(c)
        return uniq

    # ---------- متن کامل
    def fetch_text(self, c, limit=14000):
        """برمی‌گرداند: (متن، آیا متن کامل داریم؟)"""
        text, full = c.abstract, False
        try:
            if c.source_type == "blog":
                import trafilatura
                raw = self._get(c.url, as_json=False)
                got = trafilatura.extract(raw, include_comments=False, include_tables=False) or ""
                if len(got) > 500:
                    text, full = got, True
            elif c.pmcid:
                raw = self._get(f"https://www.ebi.ac.uk/europepmc/webservices/rest/{c.pmcid}/fullTextXML",
                                as_json=False)
                got = strip_html(raw.decode("utf-8", "ignore"))
                if len(got) > 2000:
                    text, full = c.abstract + "\n\n" + got, True
        except Exception as e:
            self.log(f"[متن] دریافت متن کامل ناموفق: {e}")
        if len(text) > limit:
            text = text[:5000] + "\n[...]\n" + text[-(limit - 5000):]
        return text, full
