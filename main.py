import argparse
import datetime as dt
import json
import pathlib
import sys
import time
import traceback

from agent.config import ROOT, env, load_config
from agent.db import DB
from agent.llm import Gemini
from agent.pipeline import Deps, run
from agent.scheduler import tick
from agent.sources import Collector
from agent.telegram import Telegram


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task", nargs="?", default="tick", choices=["tick", "run", "dry", "discover", "ping", "langtest"])
    ap.add_argument("--force", action="store_true", help="حتی اگر امروز پست منتشر شده، دوباره اجرا کن")
    args = ap.parse_args()

    cfg = load_config()
    tg = Telegram(env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHANNEL_ID") or "@Gamification_club", env("ALERT_CHAT_ID"))

    if args.task == "ping":  # آزمایش مسیر ارسال پیام به چت خصوصی
        print("alert_chat set:", bool(tg.alert_chat))
        mid = tg.send_message(tg.alert_chat, "تست اتصال: اگر این پیام را می‌بینی، پیش‌نمایش‌ها هم به همین چت می‌رسند.")
        print("sent message id:", mid)
        return 0

    if args.task == "langtest":  # سنجش ویراستاری روی دو نمونه‌ی ضعیف
        from agent import prompts
        samples = [
            ("📊 داشبورد سنجش هوش مصنوعی‌تون بهتون دروغ می‌گه",
             ["خیلی از سازمان‌ها فکر می‌کنن با بالا رفتن آمار استفاده از هوش مصنوعی، کارها داره عمیق‌تر پیش می‌ره؛ ولی داده‌ها چیز دیگه ای می‌گن. در یک بررسی روی کارمندان مشخص شد که هرچند بیشتر افراد ابزارها رو لمس می‌کنن، اما اکثریت اون‌ها کمتر از ۱ درصد از ساعات کاری‌شون رو عملاً داخل این ابزارها می‌گذرانن.",
              "ماجرا وقتی جالب‌تر می‌شه که بدونیم در یک آزمایش تصادفی، توسعه‌دهندگان با وجود استفاده از هوش مصنوعی ۱۹ درصد کندتر کار رو تموم کردن، در حالی که خودسون فکر می‌ردن ۲۰ درصد سریع‌تر شدن!",
              "به نظرم داشبوردها دارن نمایشی از کار رو ثبت می‌کنن، نه عمق واقعی رو. وقتی خروجی دیگه معیار درستی برای تلاش انسانی نیست، باید دید کاربر چقدر واقعاً درگیر مینی‌لوپ‌های کاری می‌شه."]),
            ("🛒 کدوم اصل رفتاری توی خریدهای آنلاین از همه قوی‌تر عمل می‌کنه؟",
             ["توی یک تحقیق جدید روی ۱۶۳ جوان شاغل، مشخص شد که اثبات اجتماعی یا همون social proof قوی‌ترین و تاثیرگذارترین اصل رفتاری روی تصمیم‌گیری خرید آدم‌هاست. بعد از اون هم گریز از ضرر و بار بیش از حد انتخاب قرار دارن.",
              "نکته مهم اینه که پژوهش نشسته نشون بده تحلیل‌ها نشون دادن مصرف‌کننده‌ها همزمان با راحتی خرید، آسیب‌پذیری بالایی نسبت به خرید تکانشی و از دست رفتن استقلال دارن. یعنی طراحی رفتاری پلتفرم‌ها هم مزیت داره و هم ریسک جدی."])]
        import json as _j
        llm = Gemini(env("GEMINI_API_KEY"), cfg["models"], log=print)
        res = []
        for head, paras in samples:
            for label, models in (("lite", cfg["models_fast"]), ("quality", cfg["models_quality"])):
                try:
                    out = llm.generate_json(prompts.POLISH.format(headline=head, paragraphs=_j.dumps(paras, ensure_ascii=False)),
                                            temperature=0.2, models=models)
                except Exception as e:
                    out = {"error": str(e)[:200]}
                res.append({"model_pref": label, "out": out})
        pathlib.Path(ROOT, cfg["paths"]["reports"], "lang_test.json").write_text(_j.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        return 0

    if args.task == "discover":  # پیدا کردن شناسه‌ی چت خصوصی برای دریافت هشدارها
        chats = {}
        for u in tg.get_updates():
            ch = (u.get("message") or {}).get("chat") or {}
            if ch.get("type") == "private":
                chats[ch["id"]] = ch.get("first_name", "")
        out = pathlib.Path(ROOT) / cfg["paths"]["reports"] / "chat_discovery.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(chats, ensure_ascii=False), encoding="utf-8")
        print(f"{len(chats)} چت خصوصی پیدا شد")
        return 0

    now = lambda: dt.datetime.now(dt.timezone.utc)
    logfile = pathlib.Path(ROOT) / cfg["paths"]["reports"] / "last_run.log"
    logfile.parent.mkdir(exist_ok=True)
    logfile.write_text("", encoding="utf-8")
    secrets = [x for x in (env("TELEGRAM_BOT_TOKEN"), env("GEMINI_API_KEY")) if x]

    def log(msg):
        msg = str(msg)
        for sec in secrets:
            msg = msg.replace(sec, "***")
        line = f"{now().strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        with open(logfile, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    db = DB(str(pathlib.Path(ROOT) / cfg["paths"]["db"]))
    try:
        llm = Gemini(env("GEMINI_API_KEY"), cfg["models"], log=log)
        llm.deadline = time.time() + 22 * 60  # کل اجرا حداکثر ۲۲ دقیقه؛ بعدش تمیز تمام می‌شود و اجرای بعدی تلاش می‌کند
        d = Deps(cfg=cfg, db=db, collector=Collector(cfg, now(), log=log), llm=llm, tg=tg, now=now, log=log)
        if args.task == "tick":
            report = tick(d)
        else:
            report = run(d, mode="live" if args.task == "run" else "dry", force=args.force)
        return 1 if report.get("status") == "publish_failed" else 0
    except Exception as e:
        msg = f"خطای اجرا: {type(e).__name__}: {e}"
        tg.alert(msg.replace(env("TELEGRAM_BOT_TOKEN") or "\0", "***")[:1500])
        log(msg)
        log(traceback.format_exc()[-1500:])
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
