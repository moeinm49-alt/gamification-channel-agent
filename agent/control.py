"""کنترل با دستورهای ربات تلگرام و تنظیمات ذخیره‌شده."""
import json
import pathlib
import re

from .textutil import to_ascii_digits

COMMANDS = [("status", "وضعیت سیستم و ساعت انتشار"), ("time", "تغییر ساعت انتشار؛ مثال: /time 18:30"),
            ("now", "آماده‌سازی و انتشار همین الان"), ("pause", "توقف انتشار خودکار"),
            ("resume", "ادامه‌ی انتشار خودکار"), ("help", "راهنما")]

HELP = ("دستورها:\n/status — وضعیت\n/time 18:30 — ساعت انتشار (فقط :۰۰ یا :۳۰، بین ۰۷:۰۰ تا ۲۳:۰۰)\n"
        "/now — همین الان پست امروز را آماده و منتشر کن\n/pause — توقف\n/resume — ادامه\n"
        "توجه: دستورها در اجرای بعدی سیستم (حداکثر ۳۰ دقیقه بعد) اعمال می‌شوند.")


def path(cfg):
    return pathlib.Path(cfg["_root"]) / cfg["paths"]["settings"]


def load(cfg):
    base = {"post_time": cfg["schedule"]["post_time"], "paused": False, "post_now": False,
            "update_offset": 0, "commands_registered": False, "prep": {"date": "", "n": 0}}
    p = path(cfg)
    if p.exists():
        try:
            base.update(json.loads(p.read_text(encoding="utf-8")))
        except ValueError:
            pass
    return base


def save(cfg, st):
    p = path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def parse_time(arg, cfg):
    """ساعت را به نزدیک‌ترین :۰۰ یا :۳۰ گرد می‌کند. خروجی: (HH:MM, توضیح) یا (None, خطا)."""
    m = re.match(r"^\s*(\d{1,2})[:٫.،](\d{2})\s*$", to_ascii_digits(arg or ""))
    if not m:
        return None, "قالب درست: /time 18:30"
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None, "ساعت نامعتبر است"
    total = h * 60 + mi
    total = int(round(total / 30.0)) * 30
    h, mi = divmod(total, 60)
    t = f"{h:02d}:{mi:02d}"
    if not (cfg["schedule"]["earliest"] <= t <= cfg["schedule"]["latest"]):
        return None, f"ساعت باید بین {cfg['schedule']['earliest']} و {cfg['schedule']['latest']} باشد"
    return t, ""


def process(d, st, status_text):
    """دستورهای جدید چت مجاز را اجرا می‌کند و تنظیمات را به‌روز می‌کند."""
    tg, cfg = d.tg, d.cfg
    if not tg.alert_chat:
        return st
    if not st.get("commands_registered"):
        try:
            tg.set_commands(COMMANDS)
            st["commands_registered"] = True
        except Exception as e:
            d.log(f"[کنترل] ثبت منوی دستورها ناموفق: {e}")
    try:
        updates = tg.get_updates(st.get("update_offset") or None)
    except Exception as e:
        d.log(f"[کنترل] دریافت دستورها ناموفق: {e}")
        return st
    for u in updates:
        st["update_offset"] = max(st.get("update_offset", 0), u["update_id"] + 1)
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(tg.alert_chat):
            continue  # فقط از چت خود مدیر دستور می‌پذیریم
        text = (msg.get("text") or "").strip()
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd == "/time":
            t, err = parse_time(arg, cfg)
            if t:
                st["post_time"] = t
                reply = f"ساعت انتشار روی {t} (به وقت تهران) تنظیم شد."
            else:
                reply = err
        elif cmd == "/pause":
            st["paused"] = True
            reply = "انتشار خودکار متوقف شد. برای ادامه /resume بزن."
        elif cmd == "/resume":
            st["paused"] = False
            reply = "انتشار خودکار از سر گرفته شد."
        elif cmd == "/now":
            st["post_now"] = True
            st["paused"] = False
            reply = "باشه؛ در همین اجرا پست امروز آماده و منتشر می‌شود (اگر امروز پستی منتشر نشده باشد)."
        elif cmd == "/status":
            reply = status_text(st)
        elif cmd in ("/help", "/start"):
            reply = HELP
        else:
            reply = "دستور نامشخص.\n" + HELP
        tg.alert(reply)
    return st
