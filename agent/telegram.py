import json
import time

import requests


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token, channel, alert_chat="", sleep=time.sleep):
        self.token = token
        self.channel = channel
        self.alert_chat = alert_chat
        self.sleep = sleep

    def _clean(self, s):
        return str(s).replace(self.token, "***") if self.token else str(s)

    def _call(self, method, data=None, files=None):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        last = ""
        for attempt in range(3):
            try:
                r = requests.post(url, data=data, files=files, timeout=60)
                body = r.json()
                if body.get("ok"):
                    return body["result"]
                last = f"{body.get('error_code')} {body.get('description')}"
                if body.get("error_code") == 429:
                    self.sleep(int(body.get("parameters", {}).get("retry_after", 5)) + 1)
                    continue
                if 400 <= int(body.get("error_code", 0)) < 500:
                    break  # خطای دائمی (قالب‌بندی، دسترسی)؛ تلاش مجدد بی‌فایده است
            except (requests.RequestException, ValueError) as e:
                last = self._clean(f"{type(e).__name__}: {e}")
            self.sleep(3 * (attempt + 1))
        raise TelegramError(self._clean(last))

    @staticmethod
    def _markup(buttons):
        """هر دکمه در یک ردیف جدا (دکمه‌ی تکی هم پذیرفته می‌شود)."""
        if not buttons:
            return None
        if isinstance(buttons, dict):
            buttons = [buttons]
        return json.dumps({"inline_keyboard": [[{"text": b["text"], "url": b["url"]}] for b in buttons]}, ensure_ascii=False)

    def send_message(self, chat, html_text, preview=False, button=None):
        data = {"chat_id": chat, "text": html_text, "parse_mode": "HTML",
                "disable_web_page_preview": "false" if preview else "true"}
        if button:
            data["reply_markup"] = self._markup(button)
        return self._call("sendMessage", data)["message_id"]

    def send_photo(self, chat, path, caption_html, button=None):
        data = {"chat_id": chat, "caption": caption_html, "parse_mode": "HTML"}
        if button:
            data["reply_markup"] = self._markup(button)
        with open(path, "rb") as f:
            res = self._call("sendPhoto", data, files={"photo": f})
        return res["message_id"]

    def alert(self, text):
        if not self.alert_chat:
            return None
        try:
            return self.send_message(self.alert_chat, text[:3900])
        except TelegramError:
            return None

    def get_updates(self, offset=None):
        data = {"limit": 100}
        if offset:
            data["offset"] = offset
        return self._call("getUpdates", data)

    def set_commands(self, commands):
        return self._call("setMyCommands", {"commands": json.dumps(
            [{"command": c, "description": d} for c, d in commands], ensure_ascii=False)})
