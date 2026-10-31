import html as _html
import re
import unicodedata

_DIG = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\U0001F000-\U0001F2FF\u2B00-\u2BFF\uFE0F\u200d]")
EMOJI_BASE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\U0001F000-\U0001F2FF\u2B00-\u2BFF]")
WORDNUM = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
           "seven": "7", "eight": "8", "nine": "9", "ten": "10", "twice": "2",
           "double": "2", "half": "0.5", "triple": "3"}


def to_ascii_digits(s):
    return (s or "").translate(_DIG)


def strip_html(s):
    s = _TAG.sub(" ", s or "")
    s = _html.unescape(s)
    return _WS.sub(" ", s).strip()


def plain_len(s):
    return len(_html.unescape(_TAG.sub("", s or "")))


def norm_title(t):
    return re.sub(r"[\W_]+", "", (t or "").lower())


def norm_text(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"[‘’“”\"']", "", s)
    s = re.sub(r"[‐‑–—]", "-", s)
    return _WS.sub(" ", s).strip()


def numbers_in(s):
    s = (s or "").translate(_DIG).replace("٫", ".").replace("٬", ",")
    s = re.sub(r"(?<=\d),(?=\d{3}\b)", "", s)
    return set(re.findall(r"\d+(?:\.\d+)?", s))


def tokens(s):
    return re.findall(r"\w+", s)


class SourceIndex:
    """نمایه‌ی متن منبع برای بررسی عددها و نقل‌قول‌ها."""

    def __init__(self, text, meta=""):
        self.norm = norm_text(text)
        toks = tokens(self.norm)
        self.grams = {tuple(toks[i:i + 4]) for i in range(max(0, len(toks) - 3))}
        nums = numbers_in(text) | numbers_in(meta)
        low = set(toks)
        for w, d in WORDNUM.items():
            if w in low:
                nums.add(d)
        self.numbers = nums

    def quote_supported(self, quote, min_ratio=0.85):
        q = norm_text(quote)
        toks = tokens(q)
        if len(toks) < 4:
            return False
        if q in self.norm:
            return True
        grams = [tuple(toks[i:i + 4]) for i in range(len(toks) - 3)]
        hit = sum(1 for g in grams if g in self.grams)
        return hit / len(grams) >= min_ratio


_FA = r"[ء-غف-يپچژکگی]"
_END = r"(?=[\s،.؟!؛:)»\"]|$)"


def fa_fix(s):
    """اصلاح‌های مطمئن نگارشی: ی/ک عربی، نیم‌فاصله‌ی «می‌»، «ها»، «ای»، «تر»، اعشار فارسی."""
    s = (s or "").replace("ي", "ی").replace("ك", "ک")
    s = re.sub(rf"(?<!{_FA})(ن?می) (?={_FA})", "\\1\u200c", s)
    s = re.sub(rf"(?<={_FA}) (ها|های|ای|تر|ترین|تری){_END}", "\u200c\\1", s)
    s = re.sub(r"(?<=[۰-۹])\.(?=[۰-۹])", "٫", s)
    return s


def repeated_words(s, min_len=3, limit=4):
    from collections import Counter
    words = re.findall(r"[ء-غف-يپچژکگی\u200c]{%d,}" % min_len, s or "")
    stop = {"این", "برای", "کرد", "رو", "که", "توی", "یعنی", "اما", "ولی", "هم", "باید", "خیلی", "یک"}
    return sorted(w for w, c in Counter(words).items() if c >= limit and w not in stop)
