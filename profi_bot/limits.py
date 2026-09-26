"""Лимиты откликов: в день, в час, платных в день, бюджет на платные в день."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .storage import Storage


@dataclass
class LimitStatus:
    blocked: bool
    reason: str = ""
    resume_at: datetime | None = None


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _positive(value) -> float | None:
    """0 или пусто — лимит выключен."""
    if value in (None, ""):
        return None
    value = float(value)
    return value if value > 0 else None


class Limits:
    def __init__(self, storage: Storage):
        self.storage = storage

    def check_global(self, cfg: dict, now: datetime | None = None, commission_allowed: bool = True) -> LimitStatus:
        """Общие лимиты (день/час). Если заблокировано — когда можно продолжить.
        commission_allowed=False: отклики за комиссию выключены, значит исчерпанные
        платные лимиты блокируют работу полностью."""
        now = now or datetime.now()
        day_start = _day_start(now)
        per_day = _positive(cfg.get("per_day"))
        if per_day is not None and self.storage.count_sent(day_start) >= per_day:
            return LimitStatus(True, f"дневной лимит {int(per_day)} откликов", day_start + timedelta(days=1))

        per_hour = _positive(cfg.get("per_hour"))
        if per_hour is not None:
            stamps = self.storage.sent_timestamps(now - timedelta(hours=1))
            if len(stamps) >= per_hour:
                # Окно освободится, когда самый старый из последних per_hour откликов выйдет за час.
                oldest = stamps[len(stamps) - int(per_hour)]
                return LimitStatus(True, f"часовой лимит {int(per_hour)} откликов", oldest + timedelta(hours=1))

        if not commission_allowed and not self.paid_allowed(cfg, 0, now):
            return LimitStatus(True, "исчерпаны лимиты платных откликов", day_start + timedelta(days=1))
        return LimitStatus(False)

    def paid_allowed(self, cfg: dict, cost: float, now: datetime | None = None) -> bool:
        """Можно ли сейчас отправить платный отклик стоимостью cost."""
        now = now or datetime.now()
        day_start = _day_start(now)
        paid_per_day = _positive(cfg.get("paid_per_day"))
        if paid_per_day is not None and self.storage.count_sent(day_start, "paid") >= paid_per_day:
            return False
        budget = _positive(cfg.get("budget_per_day"))
        if budget is not None:
            spent = self.storage.sum_cost(day_start)
            if spent >= budget or spent + float(cost or 0) > budget:
                return False
        return True
