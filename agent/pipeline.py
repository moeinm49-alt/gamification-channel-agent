import html
import json
import pathlib
import random
import re
import tempfile
from urllib.parse import urlparse
from dataclasses import dataclass
from typing import Any, Callable

from . import prompts
from .images import download_image, make_card, pick_image
from .llm import LLMError
from .textutil import EMOJI_BASE, fa_fix, numbers_in, plain_len, strip_html
from .qc import ABSOLUTES, FORMAL, WEAK_RE, deterministic_qc
from .sources import Candidate
from .telegram import TelegramError
from .textutil import norm_title


@dataclass
class Deps:
    cfg: dict
    db: Any
    collector: Any
    llm: Any
    tg: Any
    now: Callable
    log: Callable = print
    catalog: Any = None


def slot_kind(cfg, slot):
    return cfg["schedule"]["slots"][slot]["kind"]


def kind_cfg(cfg, kind):
    """وزن‌ها، شرط‌ها و حداقل امتیاز هر نوع پست (پژوهشی یا خبری)."""
    if kind == "news":
        n = cfg["news"]
        return {"weights": n["weights"], "gates": n["gates"], "min_score": n["min_score"]}
    return {"weights": cfg["weights"], "gates": cfg["gates"], "min_score": cfg["min_score"]}


def esc(s):
    return html.escape(str(s), quote=True)


def weighted_total(item, weights, image_score):
    s = {"relevance": item["relevance"], "credibility": item["credibility"],
         "depth_rarity": item["depth_rarity"], "practical": item["practical"],
         "shareability": item["shareability"], "difference": 10 - item["overlap_with_previous"],
         "image": image_score}
    return round(sum(w * max(0, min(10, float(s[k]))) / 10 for k, w in weights.items()), 1)


def src_key(source_type, source_name, venue):
    """کلید منبع برای تنوع: مقاله‌ها با نام مجله، وبلاگ‌ها با نام وب‌سایت."""
    return venue if source_type == "paper" and venue else source_name


def diversify(cands, cap):
    """انتخاب متنوع: نیمی مقاله‌ی علمی، نیمی وبلاگ/تحلیل، و در هر دسته به‌نوبت از همه‌ی منابع."""
    def round_robin(items):
        groups = {}
        for c in items:
            groups.setdefault(c.source_name, []).append(c)
        for g in groups.values():
            random.shuffle(g)
        out = []
        while any(groups.values()):
            for g in groups.values():
                if g:
                    out.append(g.pop())
        return out

    papers = round_robin([c for c in cands if c.source_type == "paper"])
    blogs = round_robin([c for c in cands if c.source_type != "paper"])
    out = []
    while len(out) < cap and (papers or blogs):
        for lst in (papers, blogs):
            if lst and len(out) < cap:
                out.append(lst.pop(0))
    return out


def score_candidates(d, cands, kind="insight"):
    recent = d.db.recent_posts(40)
    recent_txt = "\n".join(f"- {r['headline']} | {r['core_finding']}" for r in recent) or "(none yet)"
    need = ("relevance", "credibility", "depth_rarity", "practical", "shareability", "overlap_with_previous")
    size = int(d.cfg.get("score_batch", 20))
    scored = {}
    for off in range(0, len(cands), size):
        chunk = cands[off:off + size]
        items = [f"[{i}] ({c.source_type}) {c.title}\nVenue: {c.venue or c.source_name} | Date: {c.published}\n"
                 f"Text: {(c.abstract or '')[:450]}" for i, c in enumerate(chunk)]
        try:
            if kind == "news":
                prompt = prompts.SCORE_NEWS.format(today=d.now().date().isoformat(), recent=recent_txt, items="\n\n".join(items))
            else:
                prompt = prompts.SCORE.format(recent=recent_txt, items="\n\n".join(items))
            out = d.llm.generate_json(prompt, temperature=0.2, models=d.cfg.get("models_fast"))
        except LLMError:
            if scored:  # دسته‌ی اول موفق بوده؛ با همان ادامه می‌دهیم
                d.log("[امتیاز] دسته‌ی بعدی ناموفق بود؛ با نتایج موجود ادامه می‌دهیم")
                break
            raise
        if isinstance(out, list):  # بعضی مدل‌ها مستقیم فهرست برمی‌گردانند
            out = {"items": out}
        if not isinstance(out, dict):
            out = {}
        for it in out.get("items", []):
            try:
                i = int(it["id"])
                if 0 <= i < len(chunk) and all(k in it for k in need):
                    if str(it.get("mechanism", "none")).strip().lower() in ("", "none", "n/a", "null"):
                        it["relevance"] = min(float(it["relevance"]), 3)  # بدون سازوکار رفتاری مشخص، مرتبط نیست
                    scored[off + i] = it
            except (KeyError, ValueError, TypeError):
                continue
    return scored


ANCHOR = re.compile(r"\[\[(.{2,45}?)\]\]")


def anchor_html(text, url):
    return ANCHOR.sub(lambda m: f'<a href="{esc(url)}">{m.group(1)}</a>', esc(text), count=1)


def short_label(c):
    s = (c.venue or ("مقاله‌ی اصلی" if c.source_type == "paper" else c.source_name) or urlparse(c.url).netloc).strip()
    return s if len(s) <= 30 else "مقاله‌ی اصلی"


BOLD = re.compile(r"\*\*(.+?)\*\*")


def body_html(p):
    return BOLD.sub(lambda m: f"<b>{m.group(1)}</b>", esc(p)).replace("*", "")


def assemble(w, cand, cfg, cta_idx=0):
    parts = [f"<b>{esc(w['headline'])}</b>"] + [body_html(p) for p in w["paragraphs"]]
    parts.append(f'🔗 منبع: <a href="{esc(cand.url)}">{esc(short_label(cand))}</a>')
    variants = cfg["cta_variants"]
    cta = f'<a href="{esc(cfg["channel_link"])}">{esc(variants[cta_idx % len(variants)])}</a>'  # کل جمله لینک است
    li = anchor_html(cfg["linkedin"]["text"], cfg["linkedin"]["url"])
    parts.append(f'<b>{cfg["channel_emoji"]} {cta}</b>\n<b>{cfg["linkedin"]["emoji"]} {li}</b>')
    return "\n\n".join(parts)


def valid_writer_output(w):
    return bool(isinstance(w, dict) and isinstance(w.get("headline"), str) and w["headline"].strip()
                and isinstance(w.get("paragraphs"), list) and w["paragraphs"]
                and all(isinstance(p, str) for p in w["paragraphs"]))


def choose_button(w, avail, db):
    """صفحه‌ی مرتبط (انتخاب مدل) + متن دکمه‌ی چرخشی؛ اگر خروجی مدل نامعتبر بود، صفحه‌ای که اخیراً استفاده نشده."""
    try:
        page = avail[int(w.get("site_page_id"))]
    except (TypeError, ValueError, IndexError):
        page = random.choice(avail) if avail else None
    if not page:
        return None
    texts = page.get("texts") or [page.get("title", "بیشتر بخوانید")]
    return {"text": texts[db.count_button_uses(page["url"]) % len(texts)], "url": page["url"]}


def apply_glossary(text, cfg):
    for g in cfg.get("glossary", []):
        for bad in g.get("wrong", []):
            text = text.replace(bad, g["fa"])
    return text


def autofix(w, cfg):
    w["headline"] = apply_glossary(fa_fix(w["headline"]), cfg)
    w["paragraphs"] = [apply_glossary(fa_fix(p), cfg) for p in w["paragraphs"]]
    return w


def polish(d, w):
    """ویراستاری نهایی با مدل قوی؛ اگر عددها یا ساختار تغییر کند، نتیجه کنار گذاشته می‌شود."""
    try:
        out = d.llm.generate_json(prompts.POLISH.format(
            headline=w["headline"], paragraphs=json.dumps(w["paragraphs"], ensure_ascii=False)),
            temperature=0.2, models=d.cfg.get("models_quality"))
    except LLMError as e:
        d.log(f"[ویرایش] مدل در دسترس نبود؛ بدون ویراستاری ادامه می‌دهیم ({e})")
        return w
    if isinstance(out, list):
        out = next((x for x in out if isinstance(x, dict)), None)
    try:
        head, paras = str(out["headline"]).strip(), [str(p).strip() for p in out["paragraphs"]]
    except (KeyError, TypeError):
        return w
    old = " ".join(w["paragraphs"]) + " " + w["headline"]
    new = " ".join(paras) + " " + head
    if (not head or not paras or numbers_in(old) != numbers_in(new)
            or new.count("**") < 2 or not EMOJI_BASE.match(head)):
        d.log("[ویرایش] نتیجه‌ی ویراستاری ساختار/عددها را تغییر داد؛ نادیده گرفته شد")
        return w
    if out.get("changes"):
        d.log(f"[ویرایش] {len(out['changes'])} اصلاح: {'; '.join(map(str, out['changes']))[:300]}")
    w = dict(w, headline=head, paragraphs=paras)
    return autofix(w, d.cfg)


def write_and_check(d, cand, text, full, avail, kind="insight"):
    cfg = d.cfg
    pages = "\n".join(f"[{i}] {p['title']} — {p.get('desc', '')[:90]}" for i, p in enumerate(avail))
    coverage = "full text" if full else "abstract/summary only (do not claim anything beyond it)"
    feedback, last_issues = "", []
    for _ in range(int(cfg.get("writes_per_article", 2))):
        prompt = prompts.WRITE.format(
            style=(prompts.STYLE_NEWS if kind == "news" else prompts.STYLE).format(max_chars=cfg["post_max_chars"], glossary="\n".join(f"- {g['en']} → {g['fa']}" for g in cfg.get("glossary", []))), title=cand.title,
            venue=cand.venue or cand.source_name, published=cand.published, authors=cand.authors,
            coverage=coverage, text=text, feedback=feedback, samples=prompts.SAMPLES, pages=pages)
        w = d.llm.generate_json(prompt, temperature=0.4, models=cfg.get("models_quality"))
        if isinstance(w, list):
            w = next((x for x in w if isinstance(x, dict)), None)
        if isinstance(w, dict) and w.get("worth_posting") is False:
            return None, None, [f"نویسنده مطلب را رد کرد: {w.get('reject_reason', '')}"], None
        if not valid_writer_output(w):
            last_issues = ["خروجی نویسنده ساختار معتبر نداشت"]
            feedback = "\nYour previous output had an invalid structure. Follow the JSON schema exactly.\n"
            continue
        w = polish(d, autofix(w, cfg))
        post = assemble(w, cand, cfg, d.db.published_count())
        issues = deterministic_qc(post, w, cand, text, cfg)
        qc = {"deterministic": issues}
        if not issues:
            v = d.llm.generate_json(prompts.VERIFY.format(coverage=coverage, text=text, post=post), temperature=0.1,
                                    models=cfg.get("models_quality"))
            if isinstance(v, list):
                v = next((x for x in v if isinstance(x, dict)), {})
            if not isinstance(v, dict):
                v = {}
            vi = [f"{i.get('type')}: {i.get('detail')}" for i in (v.get("issues") or [])]
            qc["verifier"] = v.get("verdict")
            qc["verifier_issues"] = vi
            if v.get("verdict") == "pass":
                return w, post, qc, choose_button(w, avail, d.db)
            issues = vi or ["verifier failed the post"]
        last_issues = issues
        feedback = ("\nYour previous attempt was REJECTED by quality control for these reasons. "
                    "Fix them or set worth_posting=false:\n- " + "\n- ".join(issues) + "\n")
    return None, None, last_issues, None


def all_buttons(cfg, button):
    return [button] if button else []  # فقط یک دکمه زیر هر پست


def publish(d, post_html, image_path, buttons=None, chat=None):
    chat = chat or d.tg.channel
    if image_path:
        try:
            return d.tg.send_photo(chat, image_path, post_html, button=buttons), "photo"
        except TelegramError as e:
            d.log(f"[تلگرام] ارسال عکس ناموفق، ارسال متن بدون عکس: {e}")
    return d.tg.send_message(chat, post_html, button=buttons), "text"


def run(d, mode="live", force=False, workdir=None, slot="insight"):
    cfg, db = d.cfg, d.db
    kind = slot_kind(cfg, slot)
    kc = kind_cfg(cfg, kind)
    started = d.now()
    report = {"started": started.isoformat(timespec="seconds"), "mode": mode, "slot": slot, "sources": {}, "collected": 0,
              "new": 0, "scored": 0, "rejected": [], "selected": None, "score": None, "reason": "",
              "image": "none", "status": "", "errors": []}
    workdir = workdir or tempfile.mkdtemp()

    if mode in ("live", "ready") and not force and db.published_today(cfg["timezone"], started, slot):
        report["status"] = "skipped_already_posted"
        report["reason"] = "امروز قبلاً پست منتشر شده است"
        return finish(d, report, started)

    cands = d.collector.collect(kind)
    report["sources"] = d.collector.health
    for name, h in d.collector.health.items():
        db.source_health(name, not h["errors"] or h["n"] > 0, h["n"], "; ".join(h["errors"])[:300], started)
    report["collected"] = len(cands)
    fresh = [c for c in cands if not db.is_seen(c.url, c.doi, norm_title(c.title))]
    report["new"] = len(fresh)
    if not fresh:
        report["status"], report["reason"] = "no_post", "مطلب جدیدی پیدا نشد (منابع خالی یا خطا)"
        return finish(d, report, started)

    pool = diversify(fresh, int(cfg["max_candidates_to_score"]))
    try:
        scored = score_candidates(d, pool, kind)
    except LLMError as e:
        report["status"], report["reason"] = "llm_unavailable", f"مدل زبانی در دسترس نبود: {e}"
        return finish(d, report, started)
    report["scored"] = len(scored)
    g = kc["gates"]
    ranked = []
    recent_src = db.recent_sources(3)
    for i, it in scored.items():
        c = pool[i]
        raw = weighted_total(it, kc["weights"], 10 if c.image_url else 6)
        # تنوع منبع: تکرار منبع در ۳ پست آخر جریمه دارد و عوض شدن نوع منبع (مقاله ↔ وبلاگ) امتیاز می‌گیرد
        key = src_key(c.source_type, c.source_name, c.venue)
        recent_keys = [src_key(r["source_type"], r["source_name"], r["venue"]) for r in recent_src]
        total = raw - cfg.get("repeat_source_penalty", 20) * recent_keys.count(key)
        if recent_src and c.source_type != recent_src[0]["source_type"]:
            total += cfg.get("switch_type_bonus", 6)
        total = round(total, 1)
        gate_fail = (it["relevance"] < g["min_relevance"] or it["credibility"] < g["min_credibility"]
                     or it["overlap_with_previous"] > g["max_overlap_with_previous"] or not it.get("enough_text", True)
                     or it["depth_rarity"] < g.get("min_newsworthiness", 0))
        aid = db.upsert_article(c.to_dict(), started)
        it["raw_score"] = raw
        db.set_article(aid, score=total, score_json=json.dumps(it, ensure_ascii=False), status="scored")
        if total >= kc["min_score"] and not gate_fail:
            ranked.append((total, i, aid, it))
    ranked.sort(key=lambda x: -x[0])
    if not ranked:
        best = max((it.get("raw_score", 0) for it in scored.values()), default=0)
        report["status"] = "no_post"
        report["reason"] = f"هیچ مطلبی به حد نصاب {kc['min_score']} نرسید (بهترین امتیاز: {best})"
        return finish(d, report, started)

    catalog = d.catalog if d.catalog is not None else cfg["buttons"]["pages"]
    used = set(db.recent_button_urls(3))
    avail = [p for p in catalog if p["url"] not in used] or catalog

    for total, i, aid, it in ranked[: int(cfg["max_articles_to_write"])]:
        c = pool[i]
        text, full = d.collector.fetch_text(c, int(cfg.get("source_text_limit", 9000)))
        if len(text) < 400:
            db.set_article(aid, status="rejected", reject_reason="متن ناکافی")
            report["rejected"].append({"title": c.title, "reason": "متن ناکافی"})
            continue
        db.set_article(aid, has_full_text=int(full))
        try:
            w, post, qc, button = write_and_check(d, c, text, full, avail, kind)
        except LLMError as e:
            report["status"], report["reason"] = "llm_unavailable", f"مدل زبانی در دسترس نبود: {e}"
            return finish(d, report, started)
        if not post:
            reason = "; ".join(qc)[:400]
            db.set_article(aid, status="rejected", reject_reason=reason)
            report["rejected"].append({"title": c.title, "reason": reason})
            continue
        img_path, img_info = pick_image(c, w.get("card_text") or w["headline"], cfg, workdir, cfg["_root"], db.image_used)
        report["image"] = img_info.get("kind", "none")
        pid = db.add_post(aid, w["headline"], w.get("core_finding", ""), post, qc,
                          {"dry": "dry_run", "live": "pending", "ready": "ready", "reserve": "dry_run"}[mode], started, img_info,
                          (button or {}).get("url", ""), slot)
        report["selected"] = {"title": c.title, "url": c.url, "venue": c.venue, "headline": w["headline"],
                              "post_chars": plain_len(post), "evidence_type": w.get("evidence_type"),
                              "full_text": full}
        report["score"] = total
        report["reason"] = it.get("reason", "")
        if mode == "live":
            try:
                mid, how = publish(d, post, img_path, all_buttons(cfg, button))
                d.log(f"[تلگرام] پست در کانال منتشر شد (message_id={mid}, {how})")
                db.set_post(pid, status="published", tg_message_id=mid, published_at=d.now().isoformat(timespec="seconds"))
                db.set_article(aid, status="published")
                report["status"] = "published"
                report["image"] = img_info.get("kind") if how == "photo" else "none"
            except TelegramError as e:
                db.set_post(pid, status="failed", error=str(e)[:300])
                report["status"] = "publish_failed"
                report["errors"].append(f"ارسال به تلگرام ناموفق: {e}")
        elif mode == "ready":
            ready_dir = pathlib.Path(cfg["_root"]) / cfg["paths"]["ready_dir"]
            ready_dir.mkdir(parents=True, exist_ok=True)
            rel = ""
            if img_path:
                rel = f"{cfg['paths']['ready_dir']}/post_{pid}.jpg"
                (pathlib.Path(cfg["_root"]) / rel).write_bytes(pathlib.Path(img_path).read_bytes())
            db.set_post(pid, status="ready", button_json=json.dumps(button or {}, ensure_ascii=False), image_path=rel)
            report["status"] = "prepared"
            report["post_id"] = pid
        elif mode == "reserve":
            report["status"] = "reserve_ok"  # پیش‌نویس تأییدشده برای روزهای خرابی مدل؛ بدون پیش‌نمایش و هشدار
        else:
            report["status"] = "dry_run_ok"
            if d.tg.alert_chat:
                try:
                    d.tg.send_message(d.tg.alert_chat, "پیش‌نمایش آزمایشی (در کانال منتشر نشد):")
                    pm, how = publish(d, post, img_path, all_buttons(cfg, button), chat=d.tg.alert_chat)
                    d.log(f"[تلگرام] پیش‌نمایش ارسال شد (message_id={pm}, {how})")
                except TelegramError as e:
                    report["errors"].append(f"ارسال پیش‌نمایش ناموفق: {e}")
        report["button"] = button
        return finish(d, report, started)

    report["status"] = "no_post"
    report["reason"] = "مطالب برتر در کنترل کیفیت یا کفایت متن رد شدند"
    return finish(d, report, started)


def finish(d, report, started):
    finished = d.now()
    report["finished"] = finished.isoformat(timespec="seconds")
    report["seconds"] = int((finished - started).total_seconds())
    report["llm_calls"] = getattr(d.llm, "calls", 0)
    d.db.add_run(started, finished, report["mode"], {k: v for k, v in report.items() if k != "sources"})
    root = pathlib.Path(d.cfg["_root"]) / d.cfg["paths"]["reports"]
    root.mkdir(parents=True, exist_ok=True)
    day = started.astimezone().date().isoformat() if False else started.date().isoformat()
    suffix = {"dry": "-dry", "reserve": "-reserve"}.get(report["mode"], "") + ("" if report.get("slot", "insight") == "insight" else "-" + report["slot"])
    (root / f"{day}{suffix}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    summary = summarize(report)
    d.log(summary)
    if report["mode"] != "reserve" and report["status"] in ("published", "no_post", "publish_failed", "dry_run_ok", "llm_unavailable"):
        d.tg.alert(summary)
    return report


def summarize(r):
    lines = [f"گزارش اجرا ({r['mode']}): {r['status']}",
             f"منابع: {len(r['sources'])} | یافت‌شده: {r['collected']} | جدید: {r['new']} | امتیازدهی‌شده: {r['scored']} | ردشده: {len(r['rejected'])}"]
    if r.get("selected"):
        s = r["selected"]
        lines.append(f"منتخب: {s['headline']} (امتیاز {r['score']})\n{s['url']}\nتصویر: {r['image']} | متن کامل: {s['full_text']}")
    if r.get("reason"):
        lines.append(f"دلیل: {r['reason']}")
    for x in r["rejected"][:4]:
        lines.append(f"رد: {x['title'][:60]} ← {x['reason'][:140]}")
    dead = [n for n, h in r["sources"].items() if h["errors"] and not h["n"]]
    if dead:
        lines.append("منابع دارای خطا: " + ", ".join(dead))
    for e in r["errors"]:
        lines.append(f"خطا: {e}")
    return "\n".join(lines)


def publish_ready(d, row):
    """انتشار پستی که از قبل آماده و ذخیره شده است."""
    cfg, db = d.cfg, d.db
    button = json.loads(row["button_json"]) if row["button_json"] else None
    img = None
    if row["image_path"]:
        p = pathlib.Path(cfg["_root"]) / row["image_path"]
        img = str(p) if p.exists() else None
    try:
        mid, how = publish(d, row["text_html"], img, all_buttons(cfg, button))
    except TelegramError as e:
        d.log(f"[تلگرام] انتشار پست آماده ناموفق: {e}")
        d.tg.alert(f"انتشار پست آماده ناموفق بود (تلاش بعدی در اجرای بعدی): {e}")
        return {"status": "publish_failed", "error": str(e)}
    now = d.now().isoformat(timespec="seconds")
    db.set_post(row["id"], status="published", tg_message_id=mid, published_at=now)
    db.set_article(row["article_id"], status="published")
    if img:
        pathlib.Path(img).unlink(missing_ok=True)
    d.log(f"[تلگرام] پست در کانال منتشر شد (message_id={mid}, {how})")
    d.tg.alert(f"منتشر شد در کانال: {row['headline']}")
    root = pathlib.Path(cfg["_root"]) / cfg["paths"]["reports"]
    root.mkdir(parents=True, exist_ok=True)
    rep = {"status": "published", "headline": row["headline"], "message_id": mid, "published_at": now}
    (root / f"{d.now().date().isoformat()}-publish-{(row['slot'] if 'slot' in row.keys() and row['slot'] else 'insight')}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    return rep


def reserve_ok(html_text, cfg):
    """بررسی مجدد ماشینی پیش‌نویس ذخیره‌شده با قواعد امروز (کلمه‌ی قطعی، لحن رسمی، طول)."""
    body = strip_html(re.sub(r"<a [^>]*>.*?</a>", " ", html_text, flags=re.S))
    if any(a in body for a in ABSOLUTES) or any(f in body for f in FORMAL):
        return False
    if len(re.findall(WEAK_RE, body)) >= 3:
        return False
    return plain_len(html_text) <= cfg["post_max_chars"]


def publish_reserve(d):
    """وقتی مدل زبانی در دسترس نیست: یکی از پیش‌نویس‌های قبلاً تأییدشده‌ی کنترل کیفیت را منتشر می‌کند."""
    cfg, db = d.cfg, d.db
    for row in db.reserve_posts():
        text = apply_glossary(row["text_html"], cfg)
        if not reserve_ok(text, cfg):
            continue
        info = json.loads(row["image_json"] or "{}")
        out = pathlib.Path(tempfile.mkdtemp()) / "post_image.jpg"
        path = None
        if info.get("kind") == "source" and info.get("url") and download_image(info["url"], str(out), cfg.get("max_image_mb", 5)):
            path = str(out)
        if not path:
            try:
                headline = re.sub(r"^[^\w]+", "", strip_html(row["headline"])).strip()
                make_card(info.get("text") or headline, row["venue"] or "", str(out),
                          str(pathlib.Path(cfg["_root"]) / cfg["paths"]["font"]))
                path = str(out)
            except Exception as e:
                d.log(f"[ذخیره] ساخت کارت تصویری ناموفق؛ بدون عکس: {e}")
        button = None
        page = next((p for p in cfg["buttons"]["pages"] if p["url"] == row["button_url"]), None)
        if page:
            texts = page["texts"]
            button = {"text": texts[db.count_button_uses(page["url"]) % len(texts)], "url": page["url"]}
        try:
            mid, how = publish(d, text, path, all_buttons(cfg, button))
        except TelegramError as e:
            d.log(f"[ذخیره] انتشار پست ذخیره ناموفق: {e}")
            return {"status": "publish_failed", "error": str(e)}
        now = d.now().isoformat(timespec="seconds")
        db.set_post(row["id"], status="published", tg_message_id=mid, published_at=now, button_url=(button or {}).get("url", ""))
        db.set_article(row["article_id"], status="published")
        d.log(f"[تلگرام] پست ذخیره منتشر شد (post={row['id']}, message_id={mid}, {how})")
        d.tg.alert(f"چون مدل زبانی در دسترس نبود، یکی از پست‌های قبلاً تأییدشده منتشر شد:\n{row['headline']}")
        return {"status": "published", "reserve": True, "headline": row["headline"], "message_id": mid}
    return {"status": "no_reserve"}
