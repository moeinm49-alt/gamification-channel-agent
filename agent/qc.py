import re

from .textutil import EMOJI, SourceIndex, numbers_in, plain_len, strip_html

CLICHES = ["نکته جالب اینجاست", "در دنیای امروز", "با ما همراه باشید", "برای مطالب بیشتر", "عضو شوید",
           "کلیک کنید", "انقلابی", "باورنکردنی", "راز موفقیت", "شگفت‌انگیز", "شگفت انگیز", "امروزه"]


def deterministic_qc(post_html, writer, cand, source_text, cfg):
    """بررسی‌های ماشینی و بدون هوش مصنوعی. فهرست مشکلات را برمی‌گرداند (خالی = قبول)."""
    issues = []
    plain = strip_html(post_html)
    n = plain_len(post_html)
    if n > cfg["post_max_chars"]:
        issues.append(f"طول پست {n} کاراکتر است و از سقف {cfg['post_max_chars']} بیشتر است؛ کوتاه‌تر بنویس")
    if n < cfg["post_min_chars"]:
        issues.append("پست خیلی کوتاه است و محتوای کافی ندارد")
    if cand.url not in post_html:
        issues.append("لینک منبع در پست نیست")
    if cfg["channel_link"] not in post_html:
        issues.append("لینک کانال در پست نیست")
    if EMOJI.search(plain):
        issues.append("ایموجی نباید باشد")
    if "#" in plain:
        issues.append("هشتگ نباید باشد")
    for c in CLICHES:
        if c in plain:
            issues.append(f"عبارت کلیشه‌ای/اغراق‌آمیز «{c}» حذف شود")
    meta = f"{cand.title} {cand.venue} {cand.published} {cand.authors}"
    idx = SourceIndex(source_text, meta)
    body_plain = strip_html(re.sub(r"<a [^>]*>.*?</a>", " ", post_html, flags=re.S))
    bad = sorted(x for x in numbers_in(body_plain) if x not in idx.numbers)
    if bad:
        issues.append(f"این عددها در متن منبع وجود ندارند و باید حذف یا اصلاح شوند: {', '.join(bad)}")
    claims = writer.get("claims") or []
    if len(claims) < 2:
        issues.append("حداقل دو ادعا همراه با نقل‌قول دقیق از منبع لازم است")
    else:
        ok = [c for c in claims if idx.quote_supported(str(c.get("evidence_quote_en", "")))]
        if len(ok) < max(2, int(len(claims) * 0.8)):
            issues.append("نقل‌قول‌های پشتیبان ادعاها در متن منبع پیدا نشدند؛ فقط ادعاهایی بنویس که عبارت دقیقشان در منبع هست")
    return issues
