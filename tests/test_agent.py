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
            "closing_line": "از این نکته‌ها بیشتر می‌خوای؟ [[بیا توی کانال]]",
            "site_page_id": 1, "button_text": "🎯 پاداش رو درست طراحی کن",
            "card_text": "پاداش غیرمنتظره پایداری را بالا برد، اما ماندگار نبود",
            "claims": [{"claim_fa": "۱۲ درصد", "evidence_quote_en": "showed a 12 percent increase in task persistence compared with the control group"},
                       {"claim_fa": "اثر از بین رفت", "evidence_quote_en": "the effect disappeared after rewards were removed"}]}


class FakeLLM:
    calls = 0

    def __init__(self, writer=None, verdict="pass", scores=None):
        self.writer, self.verdict, self.scores = writer or good_writer(), verdict, scores
        self.seen = []

    def generate_json(self, prompt, system="", temperature=0.3):
        self.calls += 1
        self.seen.append(prompt)
        if "Score candidate sources" in prompt:
            return self.scores or {"items": [{"id": 0, "source_type": "primary_research", "mechanism": "reward timing", "relevance": 9, "credibility": 8,
                                              "depth_rarity": 8, "practical": 7, "shareability": 8,
                                              "overlap_with_previous": 1, "enough_text": True, "reason": "ok"}]}
        if "fact-checker" in prompt:
            return {"verdict": self.verdict, "issues": [] if self.verdict == "pass" else [{"type": "hype", "detail": "bad"}]}
        return self.writer


class FakeCollector:
    def __init__(self, cands):
        self.cands, self.health = cands, {"OpenAlex": {"n": len(cands), "errors": []}}

    def collect(self):
        return list(self.cands)

    def fetch_text(self, c, limit=14000):
        return c.abstract, False


class FakeTG:
    channel, alert_chat = "@chan", ""

    def __init__(self, fail_photo=False):
        self.sent, self.fail_photo = [], fail_photo

    def send_photo(self, chat, path, cap, button=None):
        if self.fail_photo:
            raise TelegramError("boom")
        self.sent.append(("photo", cap, button))
        return 11

    def send_message(self, chat, text, preview=False, button=None):
        self.sent.append(("text", text, button))
        return 12

    def alert(self, t):
        return None


@pytest.fixture
def env(tmp_path):
    cfg = load_config()
    cfg["_root"] = str(pathlib.Path(__file__).resolve().parent.parent)
    cfg["paths"] = dict(cfg["paths"], reports=str(tmp_path / "rep"))
    db = DB(str(tmp_path / "t.db"))
    return cfg, db, tmp_path


CATALOG = [{"url": "https://moarek.ir/a/", "title": "A", "desc": "a"}, {"url": "https://moarek.ir/b/", "title": "B", "desc": "b"}]


def deps(cfg, db, llm, tg, cands, now=NOW):
    return Deps(cfg=cfg, db=db, collector=FakeCollector(cands), llm=llm, tg=tg, now=lambda: now,
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
    w["closing_line"] = "اگه از این جور نکته‌های کاربردی و تازه خوشت میاد و می‌خوای هر روز بخونی، [[بیا توی کانال]]"
    assert any("طولانی" in x for x in deterministic_qc(assemble(w, CAND, cfg), w, CAND, SRC, cfg))
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
    assert '<b>📣 از این نکته‌ها بیشتر می‌خوای؟ <a href="https://t.me/Gamification_club">بیا توی کانال</a></b>' in post
    assert "<b>۱۲ درصد</b>" in post and "**" not in post
    assert '<b>💼 من رو توی <a href="https://linkedin.com/in/moeinmoareknezhad">لینکدین</a> هم دنبال کن</b>' in post
    assert '🔗 منبع: <a href="https://doi.org/10.1/x">J Test</a>' in post
    from agent.pipeline import choose_button
    assert choose_button(good_writer(), CATALOG) == {"text": "🎯 پاداش رو درست طراحی کن", "url": "https://moarek.ir/b/"}
    bad = good_writer()
    bad["site_page_id"] = 99
    assert choose_button(bad, CATALOG)["url"] in ("https://moarek.ir/a/", "https://moarek.ir/b/")


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
    assert tg.sent[0][2]["url"] == "https://moarek.ir/b/"
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
    assert r["status"] == "dry_run_ok" and tg.sent == []
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
