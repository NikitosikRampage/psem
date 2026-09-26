"""Рабочие часы и рандомизированные задержки."""
from __future__ import annotations

import random
from datetime import datetime, time, timedelta


def parse_hhmm(value: str) -> time:
    try:
        h, m = str(value).strip().split(":")
        return time(int(h), int(m))
    except (ValueError, TypeError):
        raise ValueError(f"bad time {value!r}") from None


def in_work_hours(now: datetime, cfg: dict) -> bool:
    """cfg — timing.work_hours. days: 1=Пн … 7=Вс. Поддерживает ночные окна (22:00–06:00):
    для них день недели проверяется по дню начала окна."""
    if not cfg.get("enabled", True):
        return True
    start, end = parse_hhmm(cfg["start"]), parse_hhmm(cfg["end"])
    days = set(cfg.get("days") or range(1, 8))
    t = now.time()
    if start == end:
        return now.isoweekday() in days
    if start < end:
        return now.isoweekday() in days and start <= t < end
    if t >= start:
        return now.isoweekday() in days
    if t < end:
        return (now - timedelta(days=1)).isoweekday() in days
    return False


def next_work_start(now: datetime, cfg: dict) -> datetime:
    """Ближайший момент (с шагом в минуту), когда начнутся рабочие часы."""
    if in_work_hours(now, cfg):
        return now
    start = parse_hhmm(cfg["start"])
    for offset in range(0, 8):
        day = (now + timedelta(days=offset)).date()
        candidate = datetime.combine(day, start)
        if candidate > now and in_work_hours(candidate, cfg):
            return candidate
    return now + timedelta(hours=1)


def to_seconds(value: float, unit: str) -> float:
    return float(value) * (60 if unit == "min" else 1)


def random_delay(cfg: dict, rng: random.Random | None = None) -> float:
    """cfg — {min, max, unit}. Возвращает секунды."""
    low = to_seconds(cfg.get("min", 0), cfg.get("unit", "sec"))
    high = to_seconds(cfg.get("max", 0), cfg.get("unit", "sec"))
    if high < low:
        low, high = high, low
    return (rng or random).uniform(low, high)
