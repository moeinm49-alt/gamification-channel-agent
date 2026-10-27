import json
import re
import time

import requests

API = "https://generativelanguage.googleapis.com/v1beta"


class LLMError(Exception):
    pass


class Gemini:
    """کلاینت ساده‌ی Gemini با جایگزینی خودکار مدل و مدیریت سقف مصرف رایگان."""

    def __init__(self, api_key, models, log=print, sleep=time.sleep):
        self.key = api_key
        self.models = list(models)
        self.dead = set()
        self.no_think = set()
        self.log = log
        self.sleep = sleep
        self.discovered = False
        self.calls = 0

    def _headers(self):
        return {"x-goog-api-key": self.key, "Content-Type": "application/json"}

    def _discover(self):
        self.discovered = True
        try:
            r = requests.get(f"{API}/models", headers=self._headers(), params={"pageSize": 200}, timeout=20)
            names = []
            for m in r.json().get("models", []):
                n = m["name"].split("/")[-1]
                ok = "generateContent" in m.get("supportedGenerationMethods", [])
                if ok and "flash" in n and not re.search(
                        r"image|tts|live|audio|embed|exp|vision|robotics|computer|thinking|omni", n):
                    names.append(n)
            for n in sorted(names, reverse=True):
                if n not in self.models:
                    self.models.append(n)
            self.log(f"[مدل] مدل‌های یافت‌شده: {self.models}")
        except Exception as e:
            self.log(f"[مدل] کشف مدل ناموفق: {type(e).__name__}")

    def _call(self, model, prompt, system, temperature):
        gen = {"temperature": temperature, "responseMimeType": "application/json", "maxOutputTokens": 8192}
        if model not in self.no_think:
            gen["thinkingConfig"] = {"thinkingBudget": 0}  # بدون «فکر کردن» طولانی؛ سریع‌تر و کم‌مصرف‌تر
        payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gen}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        self.calls += 1
        try:
            r = requests.post(f"{API}/models/{model}:generateContent", headers=self._headers(),
                              json=payload, timeout=120)
            try:
                body = r.json()
            except ValueError:
                body = {"raw": r.text[:300]}
            return r.status_code, body
        except requests.RequestException as e:
            return 599, {"error": {"message": type(e).__name__}}

    @staticmethod
    def _errmsg(body):
        try:
            return str(body["error"]["message"])[:220]
        except (KeyError, TypeError):
            return json.dumps(body, ensure_ascii=False)[:220]

    @staticmethod
    def _text(body):
        try:
            parts = body["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts if not p.get("thought"))
        except (KeyError, IndexError, TypeError):
            return ""

    @staticmethod
    def _parse(text):
        t = (text or "").strip()
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
        try:
            return json.loads(t)
        except ValueError:
            a, b = t.find("{"), t.rfind("}")
            if a != -1 and b > a:
                try:
                    return json.loads(t[a:b + 1])
                except ValueError:
                    return None
        return None

    def generate_json(self, prompt, system="", temperature=0.3):
        last = ""
        t0 = time.time()
        for _round in range(2):
            for model in [m for m in self.models if m not in self.dead]:
                p = prompt
                attempt = 0
                while attempt < 3:
                    if time.time() - t0 > 300:
                        raise LLMError(f"سقف زمان فراخوانی مدل تمام شد ({last})")
                    t1 = time.time()
                    status, body = self._call(model, p, system, temperature)
                    msg = self._errmsg(body) if status != 200 else ""
                    self.log(f"[مدل] {model} → HTTP {status} ({time.time() - t1:.1f}s) {msg}")
                    attempt += 1
                    if status == 200:
                        obj = self._parse(self._text(body))
                        if obj is not None:
                            return obj
                        last = "invalid json"
                        p = prompt + "\n\nReturn ONLY one valid JSON object, nothing else."
                        continue
                    low = msg.lower()
                    if status == 400 and "think" in low and model not in self.no_think:
                        self.no_think.add(model)  # این مدل thinkingBudget را نمی‌پذیرد؛ بدون آن تلاش مجدد
                        attempt -= 1
                        continue
                    if status in (400, 403) and "api key" in low:
                        raise LLMError("کلید Gemini نامعتبر است یا دسترسی ندارد")
                    if status == 429:
                        last = "rate limit"
                        if "perday" in low.replace(" ", "").replace("_", "") or "daily" in low:
                            self.log(f"[مدل] سهمیه‌ی روزانه‌ی {model} تمام شده؛ مدل بعدی")
                            self.dead.add(model)
                            break
                        m = re.search(r"retry in ([\d.]+)s", low)
                        wait = min(float(m.group(1)) + 2, 60) if m else min(20 * attempt, 60)
                        self.sleep(wait)
                        continue
                    if status >= 500 or status == 599:
                        last = f"HTTP {status}"
                        self.sleep(5 * attempt)
                        continue
                    last = f"HTTP {status} {model}"
                    self.dead.add(model)
                    break
            if not self.discovered:
                self._discover()
                continue
            break
        raise LLMError(f"همه‌ی مدل‌ها ناموفق بودند ({last})")
