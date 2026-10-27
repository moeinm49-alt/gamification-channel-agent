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
            "headline": "پاداش غیرمنتظره، پایداری بیشتر",
            "paragraphs": ["در یک آزمایش میدانی با ۱۲۰۰ کارمند، پاداش‌های غیرمنتظره پایداری در انجام کار را ۱۲ درصد بیشتر کرد؛ اما اثر بعد از حذف پاداش از بین رفت و مطالعه فقط در یک شرکت انجام شده بود. "
                           "این یافته نشان می‌دهد تأثیر پاداش‌های سورپرایزی موقتی است و برای نتیجه‌گیری درباره سازمان‌های دیگر شواهد بیشتری لازم است."],
            "closing_line": "پاداشی که ناگهان می‌آید، لزوماً عادتی نمی‌سازد.",
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
            return self.scores or {"items": [{"id": 0, "source_type": "primary_research", "relevance": 9, "credibility": 8,
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

    def send_photo(self, chat, path, cap):
        if self.fail_photo:
            raise TelegramError("boom")
        self.sent.append(("photo", cap))
        return 11

    def send_message(self, chat, text, preview=False):
        self.sent.append(("text", text))
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


def deps(cfg, db, llm, tg, cands, now=NOW):
    return Deps(cfg=cfg, db=db, collector=FakeCollector(cands), llm=llm, tg=tg, now=lambda: now, log=lambda *_: None)


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


def test_qc_blocks_cliche_emoji_and_length(env):
    cfg, _, _ = env
    w = good_writer()
    w["paragraphs"][0] += " نکته جالب اینجاست 🚀 #تست"
    issues = deterministic_qc(assemble(w, CAND, cfg), w, CAND, SRC, cfg)
    assert len(issues) >= 3
    w["paragraphs"] = ["الف " * 400]
    assert any("طول" in x for x in deterministic_qc(assemble(w, CAND, cfg), w, CAND, SRC, cfg))


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
    # اجرای دوباره در همان روز: انتشار تکراری نباید رخ دهد
    r2 = run(deps(cfg, db, FakeLLM(), tg, [CAND]), "live", workdir=str(tmp))
    assert r2["status"] == "skipped_already_posted" and len(tg.sent) == 1
    # روز بعد: همان مقاله دوباره منتشر نشود
    r3 = run(deps(cfg, db, FakeLLM(), tg, [CAND], NOW + dt.timedelta(days=1)), "live", workdir=str(tmp))
    assert r3["status"] == "no_post" and len(tg.sent) == 1


def test_no_candidate_above_threshold_means_no_post(env):
    cfg, db, tmp = env
    weak = {"items": [{"id": 0, "source_type": "news", "relevance": 4, "credibility": 4, "depth_rarity": 3, "practical": 3,
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
