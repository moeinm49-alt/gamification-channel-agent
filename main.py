import argparse
import datetime as dt
import json
import pathlib
import sys
import traceback

from agent.config import ROOT, env, load_config
from agent.db import DB
from agent.llm import Gemini
from agent.pipeline import Deps, run
from agent.sources import Collector
from agent.telegram import Telegram


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task", nargs="?", default="run", choices=["run", "dry", "discover"])
    ap.add_argument("--force", action="store_true", help="حتی اگر امروز پست منتشر شده، دوباره اجرا کن")
    args = ap.parse_args()

    cfg = load_config()
    tg = Telegram(env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHANNEL_ID") or "@Gamification_club", env("ALERT_CHAT_ID"))

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
    db = DB(str(pathlib.Path(ROOT) / cfg["paths"]["db"]))
    try:
        d = Deps(cfg=cfg, db=db, collector=Collector(cfg, now()), llm=Gemini(env("GEMINI_API_KEY"), cfg["models"]),
                 tg=tg, now=now)
        report = run(d, mode="live" if args.task == "run" else "dry", force=args.force)
        return 1 if report["status"] == "publish_failed" else 0
    except Exception as e:
        msg = f"خطای اجرا: {type(e).__name__}: {e}"
        tg.alert(msg.replace(env("TELEGRAM_BOT_TOKEN") or "\0", "***")[:1500])
        traceback.print_exc()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
