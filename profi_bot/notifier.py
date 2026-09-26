from __future__ import annotations

import threading

import requests


class TelegramNotifier:
    """Отправка уведомлений через Bot API. Ошибки сети не прерывают работу бота."""

    def __init__(self, cfg_getter, log=None):
        self._cfg = cfg_getter  # функция, возвращающая актуальную секцию telegram
        self._log = log

    def send(self, text: str, background: bool = True) -> bool:
        cfg = self._cfg()
        if not cfg.get("enabled") or not cfg.get("token") or not cfg.get("chat_id"):
            return False
        if background:
            threading.Thread(target=self._post, args=(cfg, text), daemon=True).start()
            return True
        return self._post(cfg, text)

    def _post(self, cfg: dict, text: str) -> bool:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{cfg['token']}/sendMessage",
                json={"chat_id": cfg["chat_id"], "text": text[:4000], "disable_web_page_preview": True},
                timeout=10,
            )
            if resp.status_code != 200 and self._log:
                self._log("warning", f"Telegram: HTTP {resp.status_code} {resp.text[:200]}")
            return resp.status_code == 200
        except requests.RequestException as exc:
            if self._log:
                self._log("warning", f"Telegram недоступен: {exc}")
            return False
