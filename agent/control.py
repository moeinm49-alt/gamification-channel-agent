"""کنترل با دستورهای ربات تلگرام و تنظیمات ذخیره‌شده."""
import json
import pathlib
import re

from .textutil import to_ascii_digits

COMMANDS = [("status", "وضعیت سیستم و ساعت‌های انتشار"), ("time", "تغییر ساعت؛ مثال: /time 12:00 20:30"),
            ("now", "آماده‌سازی و انتشار همین الان"), ("pause", "توقف انتشار خودکار"),
            ("resume", "ادامه‌ی انتشار خودکار"), ("help", "راهنما")]

HELP = ("دستورها:\n/status — وضعیت\n"
        "/time 12:00 20:30 — ساعت پست اول (پژوهشی) و دوم (خبری)\n"
        "/time 12:30 — فقط ساعت پست اول\n/time news 21:00 — فقط ساعت پست خبری\n"
        "(ساعت‌ها فقط روی :۰۰ یا :۳۰ و بین ۰۷:۰۰ تا ۲۳:۰۰)\n"
        "/now — همین الان پست بعدیِ امروز را آماده و منتشر کن (/now news برای پست خبری)\n"
        "/pause — توقف، /resume — ادامه\n"
        "توجه: دستورها در اجرای بعدی سیستم (حداکثر ۳۰ دقیقه بعد) اعمال می‌شوند.")


def enabled_slots(cfg):
    return {k: v for k, v in cfg["schedule"]["slots"].items() if v.get("enabled", True)}


def slot_ids(cfg):
    return list(enabled_slots(cfg).keys())


def path(cfg):
    return pathlib.Path(cfg["_root"]) / cfg["paths"]["settings"]


def load(cfg):
    base = {"slots": {k: v["time"] for k, v in enabled_slots(cfg).items()}, "paused": False, "post_now": "",
            "update_offset": 0, "commands_registered": False, "prep": {"date": "", "n": {}}}
    p = path(cfg)
    if p.exists():
        try:
            saved = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            saved = {}
        if "post_time" in saved:  # سازگاری با نسخه‌ی قبلی (یک ساعت)
            saved.setdefault("slots", {})["insight"] = saved.pop("post_time")
        slots = dict(base["slots"])
        slots.update({k: v for k, v in (saved.get("slots") or {}).items() if k in slots})
        base.update({k: v for k, v in saved.items() if k != "slots"})
        base["slots"] = slots
    if not isinstance(base.get("post_now"), str):
        base["post_now"] = "any" if base.get("post_now") else ""
    prep = base.get("prep") or {}
    if not isinstance(prep.get("n"), dict):
        prep["n"] = {}
    base["prep"] = prep
    return base


def save(cfg, st):
    p = path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def parse_time(arg, cfg):
    """ساعت را به نزدیک‌ترین :۰۰ یا :۳۰ گرد می‌کند. خروجی: (HH:MM, توضیح) یا (None, خطا)."""
    m = re.match(r"^\s*(\d{1,2})[:٫.،](\d{2})\s*$", to_ascii_digits(arg or ""))
    if not m:
        return None, "قالب درست: /time 12:00 20:30"
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None, "ساعت نامعتبر است"
    total = int(round((h * 60 + mi) / 30.0)) * 30
    h, mi = divmod(total, 60)
    t = f"{h:02d}:{mi:02d}"
    if not (cfg["schedule"]["earliest"] <= t <= cfg["schedule"]["latest"]):
        return None, f"ساعت باید بین {cfg['schedule']['earliest']} و {cfg['schedule']['latest']} باشد"
    return t, ""


def apply_time_command(st, arg, cfg, cmd="/time"):
    """/time 12:00 20:30 | /time 12:30 | /time news 21:00 | /time2 20:30"""
    ids = slot_ids(cfg)
    toks = (arg or "").split()
    assign = []
    if cmd == "/time2" and toks:
        assign = [(ids[1], toks[0])] if len(ids) > 1 else []
    elif toks and toks[0].lower() in ids:
        assign = [(toks[0].lower(), toks[1])] if len(toks) > 1 else []
    else:
        assign = list(zip(ids, toks))
    if not assign:
        return "قالب درست: /time 12:00 20:30  یا  /time news 21:00"
    done = []
    for slot, raw in assign:
        t, err = parse_time(raw, cfg)
        if not t:
            return err
        st["slots"][slot] = t
        done.append(f"{cfg['schedule']['slots'][slot].get('label', slot)}: {t}")
    return "ساعت انتشار (به وقت تهران) تنظیم شد:\n" + "\n".join(done)


def process(d, st, status_text):
    """دستورهای جدید چت مجاز را اجرا می‌کند و تنظیمات را به‌روز می‌کند."""
    tg, cfg = d.tg, d.cfg
    if not tg.alert_chat:
        return st
    if not st.get("commands_registered") or st.get("commands_version") != 2:
        try:
            tg.set_commands(COMMANDS)
            st["commands_registered"] = True
            st["commands_version"] = 2
        except Exception as e:
            d.log(f"[کنترل] ثبت منوی دستورها ناموفق: {e}")
    try:
        updates = tg.get_updates(st.get("update_offset") or None)
    except Exception as e:
        d.log(f"[کنترل] دریافت دستورها ناموفق: {e}")
        return st
    ids = slot_ids(cfg)
    for u in updates:
        st["update_offset"] = max(st.get("update_offset", 0), u["update_id"] + 1)
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(tg.alert_chat):
            continue  # فقط از چت خود مدیر دستور می‌پذیریم
        text = (msg.get("text") or "").strip()
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd in ("/time", "/time2"):
            reply = apply_time_command(st, arg, cfg, cmd)
        elif cmd == "/pause":
            st["paused"] = True
            reply = "انتشار خودکار متوقف شد. برای ادامه /resume بزن."
        elif cmd == "/resume":
            st["paused"] = False
            reply = "انتشار خودکار از سر گرفته شد."
        elif cmd == "/now":
            target = arg.strip().lower()
            st["post_now"] = target if target in ids else "any"
            st["paused"] = False
            reply = "باشه؛ در همین اجرا پست آماده و منتشر می‌شود (اگر آن پست امروز هنوز منتشر نشده باشد)."
        elif cmd == "/status":
            reply = status_text(st)
        elif cmd in ("/help", "/start"):
            reply = HELP
        else:
            reply = "دستور نامشخص.\n" + HELP
        tg.alert(reply)
    return st
