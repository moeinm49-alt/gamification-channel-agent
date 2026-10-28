import html
import json
import pathlib
import random
import tempfile
from dataclasses import dataclass
from typing import Any, Callable

from . import prompts
from .images import pick_image
from .llm import LLMError
from .qc import deterministic_qc
from .sources import Candidate
from .telegram import TelegramError
from .textutil import norm_title, plain_len


@dataclass
class Deps:
    cfg: dict
    db: Any
    collector: Any
    llm: Any
    tg: Any
    now: Callable
    log: Callable = print


def esc(s):
    return html.escape(str(s), quote=True)


def weighted_total(item, weights, image_score):
    s = {"relevance": item["relevance"], "credibility": item["credibility"],
         "depth_rarity": item["depth_rarity"], "practical": item["practical"],
         "shareability": item["shareability"], "difference": 10 - item["overlap_with_previous"],
         "image": image_score}
    return round(sum(w * max(0, min(10, float(s[k]))) / 10 for k, w in weights.items()), 1)


def diversify(cands, cap):
    """انتخاب متنوع: به‌نوبت از هر منبع، تا یک منبع همه‌ی سهمیه را نگیرد."""
    groups = {}
    for c in cands:
        groups.setdefault(c.source_name, []).append(c)
    for g in groups.values():
        random.shuffle(g)
    out = []
    while len(out) < cap and any(groups.values()):
        for g in groups.values():
            if g and len(out) < cap:
                out.append(g.pop())
    return out


def score_candidates(d, cands):
    recent = d.db.recent_posts(40)
    recent_txt = "\n".join(f"- {r['headline']} | {r['core_finding']}" for r in recent) or "(none yet)"
    items = []
    for i, c in enumerate(cands):
        items.append(f"[{i}] ({c.source_type}) {c.title}\nVenue: {c.venue or c.source_name} | Date: {c.published}\n"
                     f"Text: {(c.abstract or '')[:450]}")
    out = d.llm.generate_json(prompts.SCORE.format(recent=recent_txt, items="\n\n".join(items)), temperature=0.2)
    scored = {}
    need = ("relevance", "credibility", "depth_rarity", "practical", "shareability", "overlap_with_previous")
    for it in out.get("items", []):
        try:
            i = int(it["id"])
            if 0 <= i < len(cands) and all(k in it for k in need):
                scored[i] = it
        except (KeyError, ValueError, TypeError):
            continue
    return scored


def assemble(w, cand, cfg):
    parts = [f"<b>{esc(w['headline'])}</b>"] + [esc(p) for p in w["paragraphs"]]
    label = cand.title if len(cand.title) <= 80 else cand.title[:77] + "…"
    meta = ", ".join(x for x in (cand.venue, (cand.published or "")[:4]) if x)
    parts.append(f'منبع: <a href="{esc(cand.url)}">{esc(label)}</a>' + (f" ({esc(meta)})" if meta else ""))
    parts.append(f'{esc(w["closing_line"])} <a href="{esc(cfg["channel_link"])}">{esc(cfg["channel_handle"])}</a>')
    return "\n\n".join(parts)


def valid_writer_output(w):
    return (isinstance(w, dict) and isinstance(w.get("headline"), str) and w["headline"].strip()
            and isinstance(w.get("paragraphs"), list) and w["paragraphs"]
            and all(isinstance(p, str) for p in w["paragraphs"])
            and isinstance(w.get("closing_line"), str) and w["closing_line"].strip())


def write_and_check(d, cand, text, full):
    cfg = d.cfg
    coverage = "full text" if full else "abstract/summary only (do not claim anything beyond it)"
    feedback, last_issues = "", []
    for _ in range(int(cfg.get("writes_per_article", 2))):
        prompt = prompts.WRITE.format(
            style=prompts.STYLE.format(max_chars=cfg["post_max_chars"]), title=cand.title,
            venue=cand.venue or cand.source_name, published=cand.published, authors=cand.authors,
            coverage=coverage, text=text, feedback=feedback)
        w = d.llm.generate_json(prompt, temperature=0.4)
        if isinstance(w, dict) and w.get("worth_posting") is False:
            return None, None, [f"نویسنده مطلب را رد کرد: {w.get('reject_reason', '')}"]
        if not valid_writer_output(w):
            last_issues = ["خروجی نویسنده ساختار معتبر نداشت"]
            feedback = "\nYour previous output had an invalid structure. Follow the JSON schema exactly.\n"
            continue
        post = assemble(w, cand, cfg)
        issues = deterministic_qc(post, w, cand, text, cfg)
        qc = {"deterministic": issues}
        if not issues:
            v = d.llm.generate_json(prompts.VERIFY.format(coverage=coverage, text=text, post=post), temperature=0.1)
            vi = [f"{i.get('type')}: {i.get('detail')}" for i in (v.get("issues") or [])]
            qc["verifier"] = v.get("verdict")
            qc["verifier_issues"] = vi
            if v.get("verdict") == "pass":
                return w, post, qc
            issues = vi or ["verifier failed the post"]
        last_issues = issues
        feedback = ("\nYour previous attempt was REJECTED by quality control for these reasons. "
                    "Fix them or set worth_posting=false:\n- " + "\n- ".join(issues) + "\n")
    return None, None, last_issues


def publish(d, post_html, image_path):
    ch = d.tg.channel
    if image_path:
        try:
            return d.tg.send_photo(ch, image_path, post_html), "photo"
        except TelegramError as e:
            d.log(f"[تلگرام] ارسال عکس ناموفق، ارسال متن بدون عکس: {e}")
    return d.tg.send_message(ch, post_html), "text"


def run(d, mode="live", force=False, workdir=None):
    cfg, db = d.cfg, d.db
    started = d.now()
    report = {"started": started.isoformat(timespec="seconds"), "mode": mode, "sources": {}, "collected": 0,
              "new": 0, "scored": 0, "rejected": [], "selected": None, "score": None, "reason": "",
              "image": "none", "status": "", "errors": []}
    workdir = workdir or tempfile.mkdtemp()

    if mode == "live" and not force and db.published_today(cfg["timezone"], started):
        report["status"] = "skipped_already_posted"
        report["reason"] = "امروز قبلاً پست منتشر شده است"
        return finish(d, report, started)

    cands = d.collector.collect()
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
        scored = score_candidates(d, pool)
    except LLMError as e:
        report["status"], report["reason"] = "llm_unavailable", f"مدل زبانی در دسترس نبود: {e}"
        return finish(d, report, started)
    report["scored"] = len(scored)
    g = cfg["gates"]
    ranked = []
    for i, it in scored.items():
        c = pool[i]
        total = weighted_total(it, cfg["weights"], 10 if c.image_url else 6)
        gate_fail = (it["relevance"] < g["min_relevance"] or it["credibility"] < g["min_credibility"]
                     or it["overlap_with_previous"] > g["max_overlap_with_previous"] or not it.get("enough_text", True))
        aid = db.upsert_article(c.to_dict(), started)
        db.set_article(aid, score=total, score_json=json.dumps(it, ensure_ascii=False), status="scored")
        if total >= cfg["min_score"] and not gate_fail:
            ranked.append((total, i, aid, it))
    ranked.sort(key=lambda x: -x[0])
    if not ranked:
        best = max((weighted_total(it, cfg["weights"], 6) for it in scored.values()), default=0)
        report["status"] = "no_post"
        report["reason"] = f"هیچ مطلبی به حد نصاب {cfg['min_score']} نرسید (بهترین امتیاز: {best})"
        return finish(d, report, started)

    for total, i, aid, it in ranked[: int(cfg["max_articles_to_write"])]:
        c = pool[i]
        text, full = d.collector.fetch_text(c, int(cfg.get("source_text_limit", 9000)))
        if len(text) < 400:
            db.set_article(aid, status="rejected", reject_reason="متن ناکافی")
            report["rejected"].append({"title": c.title, "reason": "متن ناکافی"})
            continue
        db.set_article(aid, has_full_text=int(full))
        try:
            w, post, qc = write_and_check(d, c, text, full)
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
                          "dry_run" if mode != "live" else "ready", started, img_info)
        report["selected"] = {"title": c.title, "url": c.url, "venue": c.venue, "headline": w["headline"],
                              "post_chars": plain_len(post), "evidence_type": w.get("evidence_type"),
                              "full_text": full}
        report["score"] = total
        report["reason"] = it.get("reason", "")
        if mode == "live":
            try:
                mid, how = publish(d, post, img_path)
                db.set_post(pid, status="published", tg_message_id=mid, published_at=d.now().isoformat(timespec="seconds"))
                db.set_article(aid, status="published")
                report["status"] = "published"
                report["image"] = img_info.get("kind") if how == "photo" else "none"
            except TelegramError as e:
                db.set_post(pid, status="failed", error=str(e)[:300])
                report["status"] = "publish_failed"
                report["errors"].append(f"ارسال به تلگرام ناموفق: {e}")
        else:
            report["status"] = "dry_run_ok"
            preview = "پیش‌نمایش آزمایشی (در کانال منتشر نشد)\n\n" + post
            try:
                if img_path:
                    d.tg.send_photo(d.tg.alert_chat, img_path, preview[:1024]) if d.tg.alert_chat else None
                    if d.tg.alert_chat and len(preview) > 1024:
                        d.tg.send_message(d.tg.alert_chat, preview)
                elif d.tg.alert_chat:
                    d.tg.send_message(d.tg.alert_chat, preview)
            except TelegramError as e:
                report["errors"].append(f"ارسال پیش‌نمایش ناموفق: {e}")
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
    suffix = "" if report["mode"] == "live" else "-dry"
    (root / f"{day}{suffix}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    summary = summarize(report)
    d.log(summary)
    if report["status"] in ("published", "no_post", "publish_failed", "dry_run_ok", "llm_unavailable"):
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
