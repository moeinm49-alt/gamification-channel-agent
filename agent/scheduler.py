"""منطق اجرای زمان‌بندی‌شده: پست از قبل آماده می‌شود و سر ساعت تنظیم‌شده منتشر می‌شود."""
import datetime as dt
from zoneinfo import ZoneInfo

from . import control
from .pipeline import publish_ready, publish_reserve, run


def status_text(d, st):
    last, ready = d.db.last_published(), d.db.ready_post()
    return "\n".join([
        f"ساعت انتشار: {st['post_time']} (به وقت تهران)",
        "وضعیت: " + ("متوقف" if st["paused"] else "فعال"),
        "آخرین پست: " + (f"{last['headline']} ({last['published_at'][:16]})" if last else "هنوز پستی نرفته"),
        "پست آماده‌ی انتشار: " + (ready["headline"] if ready else "ندارد"),
    ])


def _hm(s):
    h, m = s.split(":")
    return int(h), int(m)


def tick(d, workdir=None):
    cfg = d.cfg
    st = control.load(cfg)
    st = control.process(d, st, lambda s: status_text(d, s))
    try:
        return _tick(d, st, workdir)
    finally:
        st["post_now"] = False
        control.save(cfg, st)


def _tick(d, st, workdir):
    cfg, db = d.cfg, d.db
    zone = ZoneInfo(cfg["timezone"])
    now = d.now()
    local = now.astimezone(zone)
    today = local.date().isoformat()
    post_now = bool(st.get("post_now"))

    start = cfg["schedule"].get("start_date")
    if start and today < start and not post_now:
        d.log(f"[زمان‌بندی] شروع انتشار خودکار از {start}")
        return {"status": "not_started"}
    if st["paused"]:
        d.log("[زمان‌بندی] انتشار متوقف است (/resume)")
        return {"status": "paused"}
    if db.published_today(cfg["timezone"], now):
        d.log("[زمان‌بندی] امروز پست منتشر شده است")
        if post_now:
            d.tg.alert("امروز قبلاً پست منتشر شده؛ /now فقط برای روزی است که هنوز پستی نرفته.")
        return {"status": "already_posted"}

    h, m = _hm(st["post_time"])
    post_dt = local.replace(hour=h, minute=m, second=0, microsecond=0)
    ch, cm = _hm(cfg["schedule"]["late_cutoff"])
    cutoff = local.replace(hour=ch, minute=cm, second=0, microsecond=0)
    lead = dt.timedelta(minutes=int(cfg["schedule"]["prepare_lead_min"]))

    row = db.ready_post()
    if row and dt.datetime.fromisoformat(row["created_at"]).astimezone(zone).date().isoformat() != today:
        db.set_post(row["id"], status="expired")  # پست آماده‌ی روزهای قبل منقضی می‌شود
        db.set_article(row["article_id"], status="scored")
        row = None

    if local > cutoff and not post_now:
        d.log("[زمان‌بندی] از ساعت پایانی گذشته؛ امروز پستی منتشر نمی‌شود")
        return {"status": "past_cutoff"}
    if local < post_dt - lead and not post_now:
        d.log(f"[زمان‌بندی] هنوز زود است (انتشار {st['post_time']})")
        return {"status": "too_early"}

    if row is None:
        prep = st.get("prep") or {}
        if prep.get("date") != today:
            prep = st["prep"] = {"date": today, "n": 0}
        if prep["n"] >= int(cfg["schedule"]["max_prepare_attempts"]):
            d.log("[زمان‌بندی] سقف تلاش برای آماده‌سازی پست امروز تمام شد")
            return {"status": "prepare_exhausted"}
        prep["n"] += 1
        rep = run(d, "ready", workdir=workdir)
        if rep["status"] != "prepared":
            grace = dt.timedelta(minutes=int(cfg["schedule"].get("reserve_after_min", 90)))
            if rep["status"] in ("llm_unavailable", "no_post") and local >= post_dt + grace:
                d.log("[زمان‌بندی] تولید پست ممکن نشد؛ استفاده از پست ذخیره")
                return publish_reserve(d)
            return rep  # علت و هشدار در run ثبت شده؛ اجرای بعدی دوباره تلاش می‌کند
        row = db.ready_post()
        d.tg.alert(f"پست امروز آماده شد و ساعت {st['post_time']} منتشر می‌شود:\n{row['headline']}")

    if local >= post_dt or post_now:
        return publish_ready(d, row)
    return {"status": "prepared_waiting"}
