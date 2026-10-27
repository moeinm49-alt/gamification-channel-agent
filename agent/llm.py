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
                        r"image|tts|live|audio|embed|exp|vision|robotics|computer|thinking", n):
                    names.append(n)
            for n in sorted(names, reverse=True):
                if n not in self.models:
                    self.models.append(n)
            self.log(f"[مدل] مدل‌های یافت‌شده: {self.models}")
        except Exception as e:
            self.log(f"[مدل] کشف مدل ناموفق: {type(e).__name__}")

    def _call(self, model, prompt, system, temperature):
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature, "responseMimeType": "application/json",
                                 "maxOutputTokens": 8192},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        self.calls += 1
        try:
            r = requests.post(f"{API}/models/{model}:generateContent", headers=self._headers(),
                              json=payload, timeout=90)
            try:
                body = r.json()
            except ValueError:
                body = {"raw": r.text[:300]}
            return r.status_code, body
        except requests.RequestException as e:
            return 599, {"error": type(e).__name__}

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
                for attempt in range(3):
                    if time.time() - t0 > 240:
                        raise LLMError(f"سقف زمان فراخوانی مدل تمام شد ({last})")
                    t1 = time.time()
                    status, body = self._call(model, p, system, temperature)
                    self.log(f"[مدل] {model} → HTTP {status} ({time.time() - t1:.1f}s)")
                    if status == 200:
                        obj = self._parse(self._text(body))
                        if obj is not None:
                            return obj
                        last = "invalid json"
                        p = prompt + "\n\nReturn ONLY one valid JSON object, nothing else."
                        continue
                    msg = json.dumps(body, ensure_ascii=False)[:300]
                    if status == 429:
                        last = "rate limit"
                        self.sleep(min(20 * (attempt + 1), 60))
                        continue
                    if status >= 500:
                        last = f"HTTP {status}"
                        self.sleep(5 * (attempt + 1))
                        continue
                    if status in (400, 403) and "api key" in msg.lower():
                        raise LLMError("کلید Gemini نامعتبر است یا دسترسی ندارد")
                    last = f"HTTP {status} {model}"
                    self.log(f"[مدل] {model} کنار گذاشته شد: HTTP {status}")
                    self.dead.add(model)
                    break
            if not self.discovered:
                self._discover()
                continue
            break
        raise LLMError(f"همه‌ی مدل‌ها ناموفق بودند ({last})")
