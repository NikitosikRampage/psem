import random
from datetime import datetime

from profi_bot.config import DEFAULTS, validate
from profi_bot.scheduler import in_work_hours, next_work_start, random_delay

WH = {"enabled": True, "start": "09:00", "end": "21:00", "days": [1, 2, 3, 4, 5]}
SAT = datetime(2026, 9, 26, 12, 0)  # суббота
MON = datetime(2026, 9, 28, 8, 30)


def test_work_hours():
    assert not in_work_hours(SAT, WH)
    assert not in_work_hours(MON, WH)
    assert in_work_hours(MON.replace(hour=9), WH)
    assert not in_work_hours(MON.replace(hour=21), WH)
    assert in_work_hours(SAT, {**WH, "enabled": False})
    assert next_work_start(SAT, WH) == datetime(2026, 9, 28, 9, 0)


def test_night_window():
    night = {"enabled": True, "start": "22:00", "end": "06:00", "days": [5]}  # пятница
    assert in_work_hours(datetime(2026, 9, 25, 23, 0), night)
    assert in_work_hours(datetime(2026, 9, 26, 5, 0), night)  # ночь с пятницы на субботу
    assert not in_work_hours(datetime(2026, 9, 26, 23, 0), night)


def test_random_delay():
    rng = random.Random(1)
    vals = [random_delay({"min": 1, "max": 2, "unit": "min"}, rng) for _ in range(100)]
    assert all(60 <= v <= 120 for v in vals) and len(set(vals)) > 50


def test_validate():
    assert validate(DEFAULTS) == []
    import copy

    bad = copy.deepcopy(DEFAULTS)
    bad["pricing"]["rules"] = [{"budget_min": 2000, "budget_max": 1000, "price": 1000}]
    bad["response"].update(allow_paid=False, allow_commission=False)
    bad["timing"]["work_hours"]["start"] = "9ч"
    assert len(validate(bad)) == 3
