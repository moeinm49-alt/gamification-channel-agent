import datetime as dt
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

from agent.config import load_config
from agent.db import DB
from agent.pipeline import Deps, assemble, run, weighted_total
from agent.qc import deterministic_qc
from agent.sources import Candidate, rebuild_abstract
from agent.telegram import TelegramError
from agent.textutil import SourceIndex, numbers_in, plain_len

NOW = dt.datetime(2026, 10, 28, 16, 30, tzinfo=dt.timezone.utc)
SRC = ("In a field experiment with 1,200 employees, participants who received unexpected rewards "
       "showed a 12 percent increase in task persistence compared with the control group. "
       "However, the effect disappeared after rewards were removed, and the study was limited to one company.") * 3
CAND = Candidate(url="https://doi.org/10.1/x", title="Unexpected rewards and persistence", source_name="OpenAlex",
                 source_type="paper", abstract=SRC, published="2026-09-01", doi="10.1/x", venue="J Test")


def good_writer():
    return {"worth_posting": True, "core_finding": "unexpected rewards raised persistence", "evidence_type": "field experiment",
            "headline": "🎁 پاداش غیرمنتظره، پایداری بیشتر",
            "paragraphs": ["توی یک آزمایش میدانی با ۱۲۰۰ کارمند، پاداش‌های غیرمنتظره پایداری آدم‌ها توی کار رو **۱۲ درصد** بالا برد. اما وقتی پاداش‌ها حذف شد، اثرش هم از بین رفت و مطالعه فقط توی یک شرکت انجام شده بود.",
                           "یعنی سورپرایز کار می‌کنه، ولی موقتیه ✅ برای سازمان‌های دیگه باید خودتون امتحانش کنید."],
            "site_page_id": 1,
            "card_text": "پاداش غیرمنتظره پایداری را بالا برد، اما ماندگار نبود",
            "claims": [{"claim_fa": "۱۲ درصد", "evidence_quote_en": "showed a 12 percent increase in task persistence compared with the control group"},
                       {"claim_fa": "اثر از بین رفت", "evidence_quote_en": "the effect disappeared after rewards were removed"}]}


class FakeLLM:
    calls = 0

    def __init__(self, writer=None, verdict="pass", scores=None, polish=None):
        self.writer, self.verdict, self.scores, self.polish = writer or good_writer(), verdict, scores, polish
        self.seen = []

    def generate_json(self, prompt, system="", temperature=0.3, models=None):
        self.calls += 1
        self.seen.append(prompt)
        if "Score candidate" in prompt:
            return self.scores or {"items": [{"id": 0, "source_type": "primary_research", "mechanism": "reward timing", "relevance": 9, "credibility": 8,
                                              "depth_rarity": 8, "practical": 7, "shareability": 8,
                                              "overlap_with_previous": 1, "enough_text": True, "reason": "ok"}]}
        if "fact-checker" in prompt:
            return {"verdict": self.verdict, "issues": [] if self.verdict == "pass" else [{"type": "hype", "detail": "bad"}]}
        if "copy editor" in prompt:
            if self.polish is not None:
                return self.polish
            w = self.writer
            return {"headline": w.get("headline", ""), "paragraphs": w.get("paragraphs", []), "changes": []}
        return self.writer


class FakeCollector:
    def __init__(self, cands, news=None):
        self.cands, self.news, self.health = cands, news, {"OpenAlex": {"n": len(cands), "errors": []}}

    def collect(self, kind=None):
        return list(self.news if (kind == "news" and self.news is not None) else self.cands)

    def fetch_text(self, c, limit=14000):
        return c.abstract, False


class FakeTG:
    channel, alert_chat = "@chan", "1"

    def __init__(self, fail_photo=False):
        self.sent, self.fail_photo, self.alerts, self.updates = [], fail_photo, [], []

    def send_photo(self, chat, path, cap, button=None):
        if self.fail_photo:
            raise TelegramError("boom")
        self.sent.append(("photo", cap, button))
        return 11

    def send_message(self, chat, text, preview=False, button=None):
        self.sent.append(("text", text, button))
        return 12

    def alert(self, t):
        self.alerts.append(t)

    def get_updates(self, offset=None):
        ups, self.updates = self.updates, []
        return ups

    def set_commands(self, c):
        return True

    def say(self, text, uid=1, chat="1"):
        self.updates.append({"update_id": uid, "message": {"chat": {"id": chat}, "text": text}})


@pytest.fixture
def env(tmp_path):
    cfg = load_config()
    cfg["_root"] = str(pathlib.Path(__file__).resolve().parent.parent)
    cfg["schedule"] = dict(cfg["schedule"], start_date="", prepare_lead_min=90,
                           slots={"insight": {"kind": "insight", "time": "18:30", "label": "پژوهشی"}})
    cfg["paths"] = dict(cfg["paths"], reports=str(tmp_path / "rep"), settings=str(tmp_path / "settings.json"),
                        ready_dir=str(tmp_path / "ready"))
    db = DB(str(tmp_path / "t.db"))
    return cfg, db, tmp_path


CATALOG = [{"url": "https://moarek.ir/a/", "title": "A", "desc": "a", "texts": ["a1", "a2"]},
           {"url": "https://moarek.ir/b/", "title": "B", "desc": "b", "texts": ["b1", "b2"]}]


def deps(cfg, db, llm, tg, cands, now=NOW, news=None):
    return Deps(cfg=cfg, db=db, collector=FakeCollector(cands, news), llm=llm, tg=tg, now=lambda: now,
                log=lambda *_: None, catalog=CATALOG)


def test_abstract_and_numbers():
    assert rebuild_abstract({"a": [0], "b": [1]}) == "a b"
    assert numbers_in("۱۲ درصد و ۳٫۵") == {"12", "3.5"}


def test_weighted_total_range():
    it = dict(relevance=10, credibility=10, depth_rarity=10, practical=10, shareability=10, overlap_with_previous=0)
    assert weighted_total(it, load_config()["weights"], 10) == 100


def test_qc_catches_fake_number_and_quote(env):
    cfg, _, _ = env
    w = good_writer()
    html = assemble(w, CAND, cfg)
    assert deterministic_qc(html, w, CAND, SRC, cfg) == []
    w2 = good_writer()
    w2["paragraphs"] = [w["paragraphs"][0].replace("۱۲ درصد", "۴۷ درصد")]
    bad = deterministic_qc(assemble(w2, CAND, cfg), w2, CAND, SRC, cfg)
    assert any("47" in x for x in bad)
    w3 = good_writer()
    w3["claims"][0]["evidence_quote_en"] = "participants became dramatically more creative and happy at work overall"
    assert any("نقل" in x for x in deterministic_qc(html, w3, CAND, SRC, cfg))


def test_qc_style_rules(env):
    cfg, _, _ = env
    w = good_writer()
    w["headline"] = "پاداش بدون ایموجی"
    assert any("ایموجی" in x for x in deterministic_qc(assemble(w, CAND, cfg), w, CAND, SRC, cfg))
    w = good_writer()
    w["paragraphs"][0] += " نکته جالب اینجاست و پیشنهاد می‌شود #تست 🚀🚀🚀🚀"
    issues = deterministic_qc(assemble(w, CAND, cfg), w, CAND, SRC, cfg)
    assert len(issues) >= 4  # کلیشه، لحن رسمی، هشتگ، ایموجی زیاد
    w = good_writer()
    w["paragraphs"] = [x.replace("**", "") for x in w["paragraphs"]]
    assert any("بولد" in x for x in deterministic_qc(assemble(w, CAND, cfg), w, CAND, SRC, cfg))
    w = good_writer()
    w["paragraphs"] = ["الف " * 400]
    assert any("طول" in x for x in deterministic_qc(assemble(w, CAND, cfg), w, CAND, SRC, cfg))


def test_layout_links_and_button(env):
    cfg, _, _ = env
    post = assemble(good_writer(), CAND, cfg)
    assert post.startswith("<b>🎁")
    assert '<b>📣 <a href="https://t.me/Gamification_club">تحلیل‌های کاربردی گیمیفیکیشن و علوم رفتاری، هر روز در کانال Gamification Club</a></b>' in post
    assert "<b>۱۲ درصد</b>" in post and "**" not in post
    assert '<b>💼 من رو توی <a href="https://linkedin.com/in/moeinmoareknezhad">لینکدین</a> هم دنبال کن</b>' in post
    assert '🔗 منبع: <a href="https://doi.org/10.1/x">J Test</a>' in post
    from agent.pipeline import choose_button
    _, db, _ = env
    assert choose_button(good_writer(), CATALOG, db) == {"text": "b1", "url": "https://moarek.ir/b/"}
    bad = good_writer()
    bad["site_page_id"] = 99
    assert choose_button(bad, CATALOG, db)["url"] in ("https://moarek.ir/a/", "https://moarek.ir/b/")


def test_html_is_escaped(env):
    cfg, _, _ = env
    w = good_writer()
    w["headline"] = "a<b>&c"
    assert "<b>a&lt;b&gt;&amp;c</b>" in assemble(w, CAND, cfg)


def test_end_to_end_live_publishes_once(env):
    cfg, db, tmp = env
    tg = FakeTG()
    r = run(deps(cfg, db, FakeLLM(), tg, [CAND]), "live", workdir=str(tmp))
    assert r["status"] == "published" and len(tg.sent) == 1
    assert tg.sent[0][0] == "photo" and plain_len(tg.sent[0][1]) <= 1024
    assert [b["url"] for b in tg.sent[0][2]] == ["https://moarek.ir/b/"]  # فقط یک دکمه
    # اجرای دوباره در همان روز: انتشار تکراری نباید رخ دهد
    r2 = run(deps(cfg, db, FakeLLM(), tg, [CAND]), "live", workdir=str(tmp))
    assert r2["status"] == "skipped_already_posted" and len(tg.sent) == 1
    # روز بعد: همان مقاله دوباره منتشر نشود
    r3 = run(deps(cfg, db, FakeLLM(), tg, [CAND], NOW + dt.timedelta(days=1)), "live", workdir=str(tmp))
    assert r3["status"] == "no_post" and len(tg.sent) == 1


def test_no_candidate_above_threshold_means_no_post(env):
    cfg, db, tmp = env
    weak = {"items": [{"id": 0, "source_type": "news", "mechanism": "none", "relevance": 4, "credibility": 4, "depth_rarity": 3, "practical": 3,
                       "shareability": 3, "overlap_with_previous": 2, "enough_text": True, "reason": "weak"}]}
    tg = FakeTG()
    r = run(deps(cfg, db, FakeLLM(scores=weak), tg, [CAND]), "live", workdir=str(tmp))
    assert r["status"] == "no_post" and tg.sent == []


def test_failed_verifier_rejects_and_never_posts(env):
    cfg, db, tmp = env
    tg = FakeTG()
    llm = FakeLLM(verdict="fail")
    r = run(deps(cfg, db, llm, tg, [CAND]), "live", workdir=str(tmp))
    assert r["status"] == "no_post" and tg.sent == [] and r["rejected"]
    assert any("REJECTED by quality control" in p for p in llm.seen)  # بازخورد به نویسنده رسید


def test_photo_failure_falls_back_to_text(env):
    cfg, db, tmp = env
    tg = FakeTG(fail_photo=True)
    r = run(deps(cfg, db, FakeLLM(), tg, [CAND]), "live", workdir=str(tmp))
    assert r["status"] == "published" and tg.sent[0][0] == "text"


def test_dry_run_does_not_mark_published(env):
    cfg, db, tmp = env
    tg = FakeTG()
    r = run(deps(cfg, db, FakeLLM(), tg, [CAND]), "dry", workdir=str(tmp))
    assert r["status"] == "dry_run_ok" and len(tg.sent) == 2  # عنوان + پیش‌نمایش، فقط به چت خصوصی
    assert not db.published_today(cfg["timezone"], NOW)
    r2 = run(deps(cfg, db, FakeLLM(), tg, [CAND]), "live", workdir=str(tmp))
    assert r2["status"] == "published"


def test_writer_can_decline(env):
    cfg, db, tmp = env
    tg = FakeTG()
    r = run(deps(cfg, db, FakeLLM(writer={"worth_posting": False, "reject_reason": "thin"}), tg, [CAND]), "live", workdir=str(tmp))
    assert r["status"] == "no_post" and tg.sent == []


def test_card_renders_persian(tmp_path):
    from PIL import Image
    from agent.images import make_card
    out = tmp_path / "c.jpg"
    make_card("پاداش غیرمنتظره پایداری را بالا برد", "J Test", str(out), str(pathlib.Path(__file__).resolve().parent.parent / "assets/fonts/Vazirmatn-Bold.ttf"))
    assert Image.open(out).size == (1280, 720)


def test_no_mechanism_caps_relevance(env):
    cfg, db, tmp = env
    off = {"items": [{"id": 0, "source_type": "news", "mechanism": "none", "relevance": 9, "credibility": 9, "depth_rarity": 9,
                      "practical": 9, "shareability": 9, "overlap_with_previous": 0, "enough_text": True, "reason": "x"}]}
    tg = FakeTG()
    r = run(deps(cfg, db, FakeLLM(scores=off), tg, [CAND]), "live", workdir=str(tmp))
    assert r["status"] == "no_post" and tg.sent == []


# ---------------- زمان‌بندی و کنترل ----------------
from agent import control
from agent.scheduler import tick


def at(h, m):  # ساعت تهران ۲۸ اکتبر ۲۰۲۶ -> UTC
    return dt.datetime(2026, 10, 28, h, m, tzinfo=dt.timezone(dt.timedelta(hours=3, minutes=30))).astimezone(dt.timezone.utc)


def test_parse_time(env):
    cfg, _, _ = env
    assert control.parse_time("18:45", cfg)[0] == "19:00"
    assert control.parse_time("۱۸:۳۰", cfg)[0] == "18:30"
    assert control.parse_time("18:10", cfg)[0] == "18:00"
    assert control.parse_time("05:00", cfg)[0] is None
    assert control.parse_time("abc", cfg)[0] is None


def test_daily_timeline(env):
    cfg, db, tmp = env
    tg = FakeTG()
    llm = FakeLLM()

    def go(h, m):
        return tick(deps(cfg, db, llm, tg, [CAND], at(h, m)), workdir=str(tmp))

    assert go(15, 2)["status"] == "too_early"                      # خیلی زود
    r = go(17, 2)                                                   # ۹۰ دقیقه قبل: آماده‌سازی
    assert r["status"] == "prepared_waiting" and tg.sent == []
    calls = llm.calls
    assert go(17, 32)["status"] == "prepared_waiting" and llm.calls == calls  # دوباره تولید نمی‌شود
    r = go(18, 32)                                                  # سر ساعت: انتشار
    assert r["status"] == "published" and len(tg.sent) == 1
    assert len(tg.sent[0][2]) == 1
    assert go(19, 2)["status"] == "already_posted" and len(tg.sent) == 1


def test_late_start_prepares_and_publishes_same_tick(env):
    cfg, db, tmp = env
    tg = FakeTG()
    r = tick(deps(cfg, db, FakeLLM(), tg, [CAND], at(19, 2)), workdir=str(tmp))
    assert r["status"] == "published" and len(tg.sent) == 1


def test_past_cutoff_posts_nothing(env):
    cfg, db, tmp = env
    tg = FakeTG()
    r = tick(deps(cfg, db, FakeLLM(), tg, [CAND], at(23, 45)), workdir=str(tmp))
    assert r["status"] == "past_cutoff" and tg.sent == []


def test_commands_change_time_and_pause(env):
    cfg, db, tmp = env
    tg = FakeTG()
    tg.say("/time 19:00", 1)
    tg.say("/pause", 2)
    tg.say("/time 20:00", 3, chat="999")  # از چت غیرمجاز: نادیده گرفته می‌شود
    r = tick(deps(cfg, db, FakeLLM(), tg, [CAND], at(19, 2)), workdir=str(tmp))
    st = control.load(cfg)
    assert st["slots"]["insight"] == "19:00" and st["paused"] is True and r["status"] == "paused"
    assert any("19:00" in a for a in tg.alerts) and tg.sent == []
    tg.say("/resume", 4)
    r = tick(deps(cfg, db, FakeLLM(), tg, [CAND], at(19, 2)), workdir=str(tmp))
    assert r["status"] == "published"


def test_now_command_publishes_immediately(env):
    cfg, db, tmp = env
    tg = FakeTG()
    tg.say("/now", 1)
    r = tick(deps(cfg, db, FakeLLM(), tg, [CAND], at(9, 2)), workdir=str(tmp))  # صبح زود، ولی /now
    assert r["status"] == "published"


def test_stale_ready_post_expires_next_day(env):
    cfg, db, tmp = env
    tg = FakeTG(fail_photo=False)
    tick(deps(cfg, db, FakeLLM(), tg, [CAND], at(17, 2)), workdir=str(tmp))
    assert db.ready_post() is not None
    nxt = at(17, 2) + dt.timedelta(days=1)
    tick(deps(cfg, db, FakeLLM(), tg, [], nxt), workdir=str(tmp))
    assert db.ready_post() is None


def test_start_date_blocks_early_days(env):
    cfg, db, tmp = env
    cfg["schedule"]["start_date"] = "2026-10-29"
    tg = FakeTG()
    r = tick(deps(cfg, db, FakeLLM(), tg, [CAND], at(19, 2)), workdir=str(tmp))
    assert r["status"] == "not_started" and tg.sent == []


# ---------------- کیفیت زبان ----------------
from agent.textutil import fa_fix, repeated_words


def test_fa_fix_typography():
    out = fa_fix("چیز دیگه ای می کنه؛ کتاب ها رو بزرگ تر نمی خونه. ۰.۴۷۵ و كتاب")
    assert "دیگه\u200cای" in out and "می\u200cکنه" in out and "کتاب\u200cها" in out
    assert "بزرگ\u200cتر" in out and "نمی\u200cخونه" in out and "۰٫۴۷۵" in out and "ك" not in out
    assert fa_fix("، ای کاش") == "، ای کاش"  # «ای» ندا دست نمی‌خورد


def test_repetition_and_long_sentence_qc(env):
    cfg, _, _ = env
    w = good_writer()
    w["paragraphs"][0] += " نشون نشون نشون نشون"
    assert any("تکراری" in x for x in deterministic_qc(assemble(w, CAND, cfg), w, CAND, SRC, cfg))
    w = good_writer()
    w["paragraphs"] = [" ".join(["کلمه"] * 40) + "."]
    assert any("بلند" in x for x in deterministic_qc(assemble(w, CAND, cfg), w, CAND, SRC, cfg))


def test_polish_applied_when_safe_and_rejected_when_it_changes_numbers(env):
    cfg, db, tmp = env
    good = good_writer()
    better = {"headline": "🎁 پاداش غیرمنتظره چی کار می‌کنه؟", "paragraphs": [p.replace("توی", "تو") for p in good["paragraphs"]], "changes": ["x"]}
    tg = FakeTG()
    r = run(deps(cfg, db, FakeLLM(polish=better), tg, [CAND]), "live", workdir=str(tmp))
    assert r["status"] == "published" and "چی کار می‌کنه" in tg.sent[0][1]
    # ویراستاری که عدد را عوض کند نادیده گرفته می‌شود
    cfg2, db2, tmp2 = env[0], DB(str(tmp / "t2.db")), tmp
    bad = dict(better, paragraphs=[p.replace("۱۲", "۹۹") for p in good["paragraphs"]])
    tg2 = FakeTG()
    r2 = run(deps(cfg2, db2, FakeLLM(polish=bad), tg2, [CAND]), "live", workdir=str(tmp2))
    assert r2["status"] == "published" and "۹۹" not in tg2.sent[0][1] and "۱۲" in tg2.sent[0][1]


def test_button_texts_are_short_enough():
    cfg = load_config()
    limit = cfg["buttons"]["max_chars"]
    for page in cfg["buttons"]["pages"]:
        assert len(page["texts"]) >= 4
        for t in page["texts"]:
            assert len(t) <= limit, (t, len(t))


# ---------------- تنوع منبع و املای نام‌ها ----------------
from agent.pipeline import autofix, diversify


def test_glossary_fixes_name_spelling(env):
    cfg, _, _ = env
    w = autofix({"headline": "🎯 تحلیل یوکای چاو", "paragraphs": ["بر اساس نظر یوکای چاو و یو کای چو"]}, cfg)
    assert "یوکای چو" in w["headline"] and "چاو" not in w["headline"] + w["paragraphs"][0]


def test_diversify_balances_papers_and_blogs():
    mk = lambda i, t, n: Candidate(url=f"u{i}", title=f"t{i}", source_name=n, source_type=t)
    cands = [mk(i, "blog", "A") for i in range(10)] + [mk(100 + i, "paper", "OpenAlex") for i in range(10)]
    out = diversify(cands, 8)
    assert sum(c.source_type == "paper" for c in out) == 4


def test_recent_source_is_penalised_so_other_type_wins(env):
    cfg, db, tmp = env
    blog = Candidate(url="https://yk.example/post", title="Blog post about rewards", source_name="Yu-kai Chou Octalysis blog",
                     source_type="blog", abstract=SRC, published="2026-10-20", venue="Yu-kai Chou Octalysis")
    aid = db.upsert_article(blog.to_dict(), NOW)  # قبلاً از همین وبلاگ پست منتشر شده
    db.set_article(aid, status="published")
    db.add_post(aid, "h", "f", "<b>x</b>", {}, "published", NOW)
    other_blog = Candidate(url="https://yk.example/post2", title="Another post on loyalty", source_name="Yu-kai Chou Octalysis blog",
                           source_type="blog", abstract=SRC, published="2026-10-21", venue="Yu-kai Chou Octalysis")
    same = {"source_type": "x", "mechanism": "reward timing", "relevance": 9, "credibility": 9, "depth_rarity": 9,
            "practical": 9, "shareability": 9, "overlap_with_previous": 1, "enough_text": True, "reason": "ok"}
    scores = {"items": [dict(same, id=0), dict(same, id=1)]}
    tg = FakeTG()
    later = NOW + dt.timedelta(days=1)
    r = run(deps(cfg, db, FakeLLM(scores=scores), tg, [other_blog, CAND], later), "live", workdir=str(tmp))
    assert r["status"] == "published" and r["selected"]["url"] == CAND.url  # مقاله‌ی علمی، نه وبلاگ تکراری


def test_mixed_formal_register_is_flagged(env):
    cfg, _, _ = env
    w = good_writer()
    w["paragraphs"][0] += " نویسندگان معرفی کرده‌اند که این روش نیازمند بررسی است و تأثیر دارد و می‌شود دید."
    assert any("لحن" in x for x in deterministic_qc(assemble(w, CAND, cfg), w, CAND, SRC, cfg))


def test_absolute_words_are_flagged(env):
    cfg, _, _ = env
    w = good_writer()
    w["paragraphs"][0] += " این روش همیشه جواب میده."
    assert any("قطعی" in x for x in deterministic_qc(assemble(w, CAND, cfg), w, CAND, SRC, cfg))


def test_real_config_midday_tick_posts_even_if_github_is_late(env):
    """با ساعت واقعی تنظیمات (۱۲:۰۰): اجرای دیرهنگام گیت‌هاب ساعت ۱۴:۱۵ هم همان روز پست را منتشر می‌کند."""
    cfg, db, tmp = env
    cfg["schedule"] = dict(load_config()["schedule"], start_date="")
    assert cfg["schedule"]["slots"]["insight"]["time"] == "12:00" and cfg["schedule"]["slots"]["news"]["time"] == "20:30"
    tg = FakeTG()
    assert tick(deps(cfg, db, FakeLLM(), tg, [CAND], at(9, 17)), workdir=str(tmp))["status"] == "too_early"
    r = tick(deps(cfg, db, FakeLLM(), tg, [CAND], at(14, 15)), workdir=str(tmp))
    assert r["status"] == "published" and len(tg.sent) == 1


# ---------------- Gemini: رفتار در شلوغی و سهمیه ----------------
from agent.llm import Gemini, LLMError


class ScriptedGemini(Gemini):
    def __init__(self, script, models=("a", "b")):
        super().__init__("k", list(models), log=lambda *_: None, sleep=lambda s: self.slept.append(s))
        self.script, self.slept, self.seen = list(script), [], []
        self.discovered = True

    def _call(self, model, prompt, system, temperature):
        self.calls += 1
        self.seen.append(model)
        status, body = self.script.pop(0)
        return status, body


OK = (200, {"candidates": [{"content": {"parts": [{"text": '{"x": 1}'}]}}]})
BUSY = (503, {"error": {"message": "high demand"}})
DAILY = (429, {"error": {"message": "quota", "details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}})


def test_gemini_waits_and_retries_when_overloaded():
    g = ScriptedGemini([BUSY, BUSY, BUSY, BUSY, OK])
    assert g.generate_json("p") == {"x": 1}
    assert g.slept == [20, 20][:len(g.slept)] or g.slept[0] == 20  # بعد از دور اول صبر می‌کند
    assert g.calls == 5  # هر مدل در هر دور فقط یک بار صدا زده شد


def test_gemini_skips_model_with_daily_quota_exhausted():
    g = ScriptedGemini([DAILY, OK])
    assert g.generate_json("p") == {"x": 1}
    assert "a" in g.dead and g.seen == ["a", "b"]


def test_gemini_gives_up_when_budget_is_over():
    g = ScriptedGemini([BUSY] * 40)
    g.budget_s = 0.0
    try:
        g.generate_json("p")
        assert False
    except LLMError:
        pass
    assert g.calls == 0


# ---------------- پست ذخیره وقتی مدل زبانی خراب است ----------------
from agent.pipeline import publish_reserve


class DownLLM:
    calls = 0

    def generate_json(self, *a, **k):
        raise LLMError("همه‌ی مدل‌ها ناموفق بودند (HTTP 503)")


def test_publish_reserve_uses_a_stored_draft(env):
    cfg, db, tmp = env
    cfg["buttons"] = dict(cfg["buttons"], pages=CATALOG)
    run(deps(cfg, db, FakeLLM(), FakeTG(), [CAND]), "dry", workdir=str(tmp))  # یک پیش‌نویس تأییدشده می‌سازد
    tg = FakeTG()
    r = publish_reserve(deps(cfg, db, DownLLM(), tg, []))
    assert r["status"] == "published" and r.get("reserve") and len(tg.sent) == 1
    assert [b["url"] for b in tg.sent[0][2]] == ["https://moarek.ir/b/"]
    assert db.published_today(cfg["timezone"], NOW)
    assert publish_reserve(deps(cfg, db, DownLLM(), FakeTG(), []))["status"] == "no_reserve"  # دوباره منتشر نمی‌شود


def test_reserve_skips_drafts_that_fail_todays_rules(env):
    cfg, db, tmp = env
    w = good_writer()
    w["paragraphs"][0] += " این روش همیشه جواب میده."
    run(deps(cfg, db, FakeLLM(writer=w, verdict="pass"), FakeTG(), [CAND]), "dry", workdir=str(tmp))
    # QC ماشینی «همیشه» را قبل از ذخیره رد می‌کند؛ پس پیش‌نویسی در ذخیره نیست
    assert publish_reserve(deps(cfg, db, DownLLM(), FakeTG(), []))["status"] == "no_reserve"


def test_tick_falls_back_to_reserve_after_grace_when_llm_is_down(env):
    cfg, db, tmp = env
    run(deps(cfg, db, FakeLLM(), FakeTG(), [CAND]), "dry", workdir=str(tmp))
    tg = FakeTG()
    d = lambda h, m: deps(cfg, db, DownLLM(), tg, [CAND], at(h, m))
    assert tick(d(19, 2), workdir=str(tmp))["status"] == "llm_unavailable"      # هنوز زود برای ذخیره (۹۰ دقیقه بعد از ۱۸:۳۰)
    assert tg.sent == []
    r = tick(d(20, 5), workdir=str(tmp))
    assert r["status"] == "published" and r.get("reserve") and len(tg.sent) == 1


def test_reserve_is_topped_up_after_a_successful_post(env):
    cfg, db, tmp = env
    other = Candidate(url="https://doi.org/10.2/y", title="A second study on persistence", source_name="OpenAlex",
                      source_type="paper", abstract=SRC, published="2026-09-02", doi="10.2/y", venue="J Other")
    tg = FakeTG()
    scores = {"items": [{"id": i, "source_type": "primary_research", "mechanism": "reward timing", "relevance": 9, "credibility": 9,
                         "depth_rarity": 9, "practical": 9, "shareability": 9, "overlap_with_previous": 1, "enough_text": True,
                         "reason": "ok"} for i in (0, 1)]}
    r = tick(deps(cfg, db, FakeLLM(scores=scores), tg, [CAND, other], at(19, 2)), workdir=str(tmp))
    assert r["status"] == "published" and len(tg.sent) == 1          # فقط یک پست در کانال
    reserve = db.reserve_posts()
    assert len(reserve) == 1 and reserve[0]["status"] == "dry_run"   # و یک پیش‌نویس تازه در ذخیره


# ---------------- دو پست در روز: پژوهشی (۱۲:۰۰) و خبری (۲۰:۳۰) ----------------
NEWS = Candidate(url="https://www.marketingdive.com/news/loyalty-update/1/", title="A retailer rebuilt its loyalty streaks",
                 source_name="Marketing Dive", source_type="news", abstract=SRC, published="2026-11-20", venue="Marketing Dive")


def two_slot_cfg(env):
    cfg, db, tmp = env
    cfg["schedule"] = dict(load_config()["schedule"], start_date="")
    cfg["buttons"] = dict(cfg["buttons"], pages=CATALOG)
    return cfg, db, tmp


def test_two_slots_post_independently_the_same_day(env):
    cfg, db, tmp = two_slot_cfg(env)
    tg, llm = FakeTG(), FakeLLM()
    go = lambda h, m: tick(deps(cfg, db, llm, tg, [CAND], at(h, m), news=[NEWS]), workdir=str(tmp))
    r = go(12, 2)
    assert r["status"] == "published" and r["slot"] == "insight" and len(tg.sent) == 1
    assert db.published_today(cfg["timezone"], at(12, 2), "insight") and not db.published_today(cfg["timezone"], at(12, 2), "news")
    assert go(15, 0)["slot"] == "news" and len(tg.sent) == 1                        # خبر هنوز زود است
    assert [x for x in [go(18, 40)]][0]["status"] == "prepared_waiting"             # ۲ ساعت قبل: آماده می‌شود
    n_calls = llm.calls
    assert go(20, 5)["status"] == "prepared_waiting" and llm.calls == n_calls       # دوباره تولید نمی‌شود
    r = go(20, 32)
    assert r["status"] == "published" and r["slot"] == "news" and len(tg.sent) == 2
    assert go(21, 2)["status"] == "already_posted" and len(tg.sent) == 2


def test_news_slot_uses_news_prompts_and_news_pool(env):
    cfg, db, tmp = two_slot_cfg(env)
    tg, llm = FakeTG(), FakeLLM()
    r = run(deps(cfg, db, llm, tg, [CAND], at(20, 32), news=[NEWS]), "live", workdir=str(tmp), slot="news")
    assert r["status"] == "published" and r["selected"]["url"] == NEWS.url
    assert any("Score candidate NEWS items" in p for p in llm.seen)
    assert any("EVENING NEWS" in p for p in llm.seen)            # سبک نگارش خبری
    assert not any("Score candidate sources" in p for p in llm.seen)
    assert db.con.execute("SELECT slot FROM posts WHERE status='published'").fetchone()["slot"] == "news"


def test_time_commands_for_two_slots(env):
    cfg, db, tmp = two_slot_cfg(env)
    st = control.load(cfg)
    assert "تنظیم شد" in control.apply_time_command(st, "12:30 21:00", cfg)
    assert st["slots"] == {"insight": "12:30", "news": "21:00"}
    control.apply_time_command(st, "news 20:00", cfg)
    assert st["slots"]["news"] == "20:00" and st["slots"]["insight"] == "12:30"
    control.apply_time_command(st, "11:00", cfg)
    assert st["slots"]["insight"] == "11:00" and st["slots"]["news"] == "20:00"
    control.apply_time_command(st, "20:45", cfg, "/time2")
    assert st["slots"]["news"] == "21:00"
    assert control.apply_time_command(st, "news 03:00", cfg).startswith("ساعت باید")


def test_legacy_single_post_time_setting_is_migrated(env):
    cfg, _, tmp = two_slot_cfg(env)
    pathlib.Path(cfg["paths"]["settings"]).write_text(json.dumps({"post_time": "13:00", "paused": False, "post_now": False}), encoding="utf-8")
    st = control.load(cfg)
    assert st["slots"] == {"insight": "13:00", "news": "20:30"} and st["post_now"] == ""


def test_now_news_command_and_status_text(env):
    cfg, db, tmp = two_slot_cfg(env)
    tg = FakeTG()
    tg.say("/now news", 1)
    r = tick(deps(cfg, db, FakeLLM(), tg, [CAND], at(9, 2), news=[NEWS]), workdir=str(tmp))
    assert r["status"] == "published" and r["slot"] == "news"
    tg.say("/status", 2)
    tick(deps(cfg, db, FakeLLM(), tg, [CAND], at(9, 32), news=[NEWS]), workdir=str(tmp))
    assert any("12:00" in a and "20:30" in a for a in tg.alerts)
