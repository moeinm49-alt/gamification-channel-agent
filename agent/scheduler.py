"""اجرای زمان‌بندی‌شده: هر پست از قبل آماده می‌شود و سر ساعت تنظیم‌شده‌ی اسلات خودش منتشر می‌شود."""
import datetime as dt
from zoneinfo import ZoneInfo

from . import control
from .pipeline import apply_glossary, publish_ready, publish_reserve, reserve_ok, run


def status_text(d, st):
    db, cfg = d.db, d.cfg
    ready = {s: db.ready_post(s) for s in st["slots"]}
    lines = ["وضعیت: " + ("متوقف" if st["paused"] else "فعال")]
    for slot, t in sorted(st["slots"].items(), key=lambda kv: kv[1]):
        label = cfg["schedule"]["slots"][slot].get("label", slot)
        r = ready[slot]
        lines.append(f"• {label}: {t} (تهران)" + (f" — آماده: {r['headline']}" if r else ""))
    last = db.last_published()
    lines.append("آخرین پست: " + (f"{last['headline']} ({last['published_at'][:16]})" if last else "هنوز پستی نرفته"))
    return "\n".join(lines)


def _hm(s):
    h, m = s.split(":")
    return int(h), int(m)


def top_up_reserve(d, workdir=None):
    """بعد از انتشار موفق پست پژوهشی، اگر ذخیره‌ی پیش‌نویس‌های تأییدشده کم است، یک پیش‌نویس تازه می‌سازد."""
    cfg = d.cfg
    good = [r for r in d.db.reserve_posts() if reserve_ok(apply_glossary(r["text_html"], cfg), cfg)]
    target = int(cfg["schedule"].get("reserve_target", 3))
    if len(good) >= target:
        return
    d.log(f"[ذخیره] {len(good)} پیش‌نویس تأییدشده موجود است؛ ساخت یکی دیگر")
    try:
        run(d, "reserve", workdir=workdir, slot="insight")
    except Exception as e:  # ساخت ذخیره نباید انتشار موفق امروز را خراب کند
        d.log(f"[ذخیره] ساخت پیش‌نویس ناموفق: {type(e).__name__}: {e}")


def tick(d, workdir=None):
    cfg = d.cfg
    st = control.load(cfg)
    st = control.process(d, st, lambda s: status_text(d, s))
    try:
        return _tick(d, st, workdir)
    finally:
        st["post_now"] = ""
        control.save(cfg, st)


def _tick(d, st, workdir):
    cfg = d.cfg
    zone = ZoneInfo(cfg["timezone"])
    local = d.now().astimezone(zone)
    today = local.date().isoformat()
    post_now = st.get("post_now") or ""

    start = cfg["schedule"].get("start_date")
    if start and today < start and not post_now:
        d.log(f"[زمان‌بندی] شروع انتشار خودکار از {start}")
        return {"status": "not_started"}
    if st["paused"]:
        d.log("[زمان‌بندی] انتشار متوقف است (/resume)")
        return {"status": "paused"}

    ordered = sorted(st["slots"].items(), key=lambda kv: kv[1])
    first_open = next((sl for sl, _ in ordered if not d.db.published_today(cfg["timezone"], d.now(), sl)), None)
    results = []
    for slot, t in ordered:
        now_flag = post_now == slot or (post_now == "any" and slot == first_open)
        results.append(_tick_slot(d, st, slot, t, local, today, now_flag, workdir))
    pub = [r for r in results if r.get("status") == "published"]
    return pub[0] if pub else (results[-1] if results else {"status": "no_slots"})


def _tick_slot(d, st, slot, time_str, local, today, post_now, workdir):
    cfg, db = d.cfg, d.db
    zone = ZoneInfo(cfg["timezone"])
    label = cfg["schedule"]["slots"][slot].get("label", slot)
    if db.published_today(cfg["timezone"], d.now(), slot):
        d.log(f"[زمان‌بندی] {label}: امروز منتشر شده است")
        if post_now:
            d.tg.alert(f"{label} امروز قبلاً منتشر شده؛ /now فقط برای پستی است که هنوز نرفته.")
        return {"status": "already_posted", "slot": slot}

    h, m = _hm(time_str)
    post_dt = local.replace(hour=h, minute=m, second=0, microsecond=0)
    ch, cm = _hm(cfg["schedule"]["late_cutoff"])
    cutoff = local.replace(hour=ch, minute=cm, second=0, microsecond=0)
    lead = dt.timedelta(minutes=int(cfg["schedule"]["prepare_lead_min"]))

    row = db.ready_post(slot)
    if row and dt.datetime.fromisoformat(row["created_at"]).astimezone(zone).date().isoformat() != today:
        db.set_post(row["id"], status="expired")  # پست آماده‌ی روزهای قبل منقضی می‌شود
        db.set_article(row["article_id"], status="scored")
        row = None

    if local > cutoff and not post_now:
        d.log(f"[زمان‌بندی] {label}: از ساعت پایانی گذشته؛ امروز منتشر نمی‌شود")
        return {"status": "past_cutoff", "slot": slot}
    if local < post_dt - lead and not post_now:
        d.log(f"[زمان‌بندی] {label}: هنوز زود است (انتشار {time_str})")
        return {"status": "too_early", "slot": slot}

    if row is None:
        prep = st["prep"]
        if prep.get("date") != today:
            prep = st["prep"] = {"date": today, "n": {}}
        n = prep["n"].get(slot, 0)
        if n >= int(cfg["schedule"]["max_prepare_attempts"]):
            d.log(f"[زمان‌بندی] {label}: سقف تلاش برای آماده‌سازی امروز تمام شد")
            return {"status": "prepare_exhausted", "slot": slot}
        prep["n"][slot] = n + 1
        rep = run(d, "ready", workdir=workdir, slot=slot)
        if rep["status"] != "prepared":
            grace = dt.timedelta(minutes=int(cfg["schedule"].get("reserve_after_min", 90)))
            kind = cfg["schedule"]["slots"][slot]["kind"]
            if kind == "insight" and rep["status"] in ("llm_unavailable", "no_post") and local >= post_dt + grace:
                d.log("[زمان‌بندی] تولید پست ممکن نشد؛ استفاده از پست ذخیره")
                return publish_reserve(d)
            return dict(rep, slot=slot)  # علت و هشدار در run ثبت شده؛ اجرای بعدی دوباره تلاش می‌کند
        row = db.ready_post(slot)
        d.tg.alert(f"{label} امروز آماده شد و ساعت {time_str} منتشر می‌شود:\n{row['headline']}")

    if local >= post_dt or post_now:
        res = publish_ready(d, row)
        if res.get("status") == "published" and cfg["schedule"]["slots"][slot]["kind"] == "insight":
            top_up_reserve(d, workdir)
        return dict(res, slot=slot)
    return {"status": "prepared_waiting", "slot": slot}
