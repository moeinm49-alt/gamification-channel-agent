import datetime as dt
import json
import pathlib
import sqlite3
from zoneinfo import ZoneInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources_health(
  name TEXT PRIMARY KEY, last_check TEXT, ok INTEGER, n_items INTEGER, last_error TEXT);
CREATE TABLE IF NOT EXISTS articles(
  id INTEGER PRIMARY KEY, url TEXT UNIQUE, doi TEXT, title TEXT, norm_title TEXT,
  authors TEXT, venue TEXT, source_name TEXT, source_type TEXT, published TEXT,
  collected_at TEXT, abstract TEXT, has_full_text INTEGER DEFAULT 0,
  score REAL, score_json TEXT, status TEXT DEFAULT 'new', reject_reason TEXT);
CREATE TABLE IF NOT EXISTS posts(
  id INTEGER PRIMARY KEY, article_id INTEGER, headline TEXT, core_finding TEXT,
  text_html TEXT, qc_json TEXT, status TEXT, tg_message_id INTEGER,
  created_at TEXT, published_at TEXT, image_json TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, mode TEXT, summary_json TEXT);
"""


def iso(t):
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path):
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)

    def close(self):
        self.con.commit()
        self.con.close()

    # ---- مقالات
    def is_seen(self, url, doi, norm_title):
        q = ("SELECT 1 FROM articles WHERE status IN ('published','rejected') AND "
             "(url=? OR (?!='' AND doi=?) OR (?!='' AND norm_title=?)) LIMIT 1")
        return self.con.execute(q, (url, doi or "", doi or "", norm_title or "", norm_title or "")).fetchone() is not None

    def upsert_article(self, c, now):
        row = self.con.execute("SELECT id FROM articles WHERE url=?", (c["url"],)).fetchone()
        if row:
            return row["id"]
        cur = self.con.execute(
            "INSERT INTO articles(url,doi,title,norm_title,authors,venue,source_name,source_type,"
            "published,collected_at,abstract) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (c["url"], c.get("doi", ""), c["title"], c["norm_title"], c.get("authors", ""),
             c.get("venue", ""), c.get("source_name", ""), c.get("source_type", ""),
             c.get("published", ""), iso(now), (c.get("abstract") or "")[:4000]))
        self.con.commit()
        return cur.lastrowid

    def set_article(self, aid, **fields):
        if not fields:
            return
        keys = ", ".join(f"{k}=?" for k in fields)
        self.con.execute(f"UPDATE articles SET {keys} WHERE id=?", (*fields.values(), aid))
        self.con.commit()

    # ---- پست‌ها
    def add_post(self, article_id, headline, core_finding, text_html, qc, status, now, image=None):
        cur = self.con.execute(
            "INSERT INTO posts(article_id,headline,core_finding,text_html,qc_json,status,created_at,image_json)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (article_id, headline, core_finding, text_html, json.dumps(qc, ensure_ascii=False),
             status, iso(now), json.dumps(image or {}, ensure_ascii=False)))
        self.con.commit()
        return cur.lastrowid

    def set_post(self, pid, **fields):
        keys = ", ".join(f"{k}=?" for k in fields)
        self.con.execute(f"UPDATE posts SET {keys} WHERE id=?", (*fields.values(), pid))
        self.con.commit()

    def recent_posts(self, n=40):
        rows = self.con.execute(
            "SELECT headline, core_finding FROM posts WHERE status='published' ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        return [dict(r) for r in rows]

    def published_today(self, tz, now):
        zone = ZoneInfo(tz)
        today = now.astimezone(zone).date()
        rows = self.con.execute("SELECT published_at FROM posts WHERE status='published' ORDER BY id DESC LIMIT 5").fetchall()
        for r in rows:
            if r["published_at"] and dt.datetime.fromisoformat(r["published_at"]).astimezone(zone).date() == today:
                return True
        return False

    # ---- سلامت منابع و اجراها
    def source_health(self, name, ok, n, err, now):
        self.con.execute(
            "INSERT INTO sources_health(name,last_check,ok,n_items,last_error) VALUES(?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET last_check=excluded.last_check, ok=excluded.ok, "
            "n_items=excluded.n_items, last_error=excluded.last_error",
            (name, iso(now), int(ok), n, err or ""))
        self.con.commit()

    def add_run(self, started, finished, mode, summary):
        self.con.execute("INSERT INTO runs(started_at,finished_at,mode,summary_json) VALUES(?,?,?,?)",
                         (iso(started), iso(finished), mode, json.dumps(summary, ensure_ascii=False)))
        self.con.commit()
