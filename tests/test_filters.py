from datetime import datetime, timedelta

from profi_bot.config import DEFAULTS
from profi_bot.filters import created_too_old, match
from profi_bot.models import Order, order_from_raw, parse_budget, parse_client_type, parse_ru_time

NOW = datetime(2026, 9, 26, 20, 0)


def cfg(**kw):
    c = dict(DEFAULTS["filters"])
    c.update(kw)
    return c


def order(**kw):
    base = dict(id="1", url="u", title="Ремонт ванной", description="Нужно положить плитку",
                category="Ремонт и строительство", budget_min=20000, budget_max=30000, geo="Дистанционно · Москва",
                client_type="private")
    base.update(kw)
    return Order(**base)


def test_parse_budget():
    assert parse_budget("до 5 000 ₽") == (None, 5000)
    assert parse_budget("от 3 000 ₽") == (3000, None)
    assert parse_budget("3 000 – 5 000 ₽") == (3000, 5000)
    assert parse_budget("4 500 ₽") == (4500, 4500)
    assert parse_budget("") == (None, None)
    assert parse_budget("по договорённости") == (None, None)


def test_client_type_and_id():
    assert parse_client_type("ООО Ромашка") == "company"
    assert parse_client_type("Частное лицо") == "private"
    assert parse_client_type("") == "unknown"
    o = order_from_raw({"url": "https://profi.ru/backoffice/o.php?o=12345678", "budget": "до 1 000 ₽"})
    assert o.id == "12345678" and o.budget_max == 1000


def test_defaults_accept_everything():
    assert match(order(), cfg()) == (True, "")


def test_category_and_keywords():
    assert match(order(), cfg(categories=["ремонт"]))[0]
    assert not match(order(), cfg(categories=["репетитор"]))[0]
    assert match(order(), cfg(keywords_include=["плитк"]))[0]
    assert not match(order(), cfg(keywords_include=["обои"]))[0]
    ok, reason = match(order(), cfg(keywords_exclude=["ПЛИТКУ"]))
    assert not ok and "стоп-слово" in reason


def test_budget_overlap():
    assert match(order(), cfg(budget_min=25000))[0]
    assert not match(order(), cfg(budget_min=31000))[0]
    assert not match(order(), cfg(budget_max=10000))[0]
    assert match(order(budget_min=None, budget_max=None), cfg(budget_min=1000))[0]
    assert not match(order(budget_min=None, budget_max=None), cfg(allow_no_budget=False))[0]


def test_geo_and_remote():
    assert match(order(), cfg(geo=["москва"]))[0]
    assert not match(order(), cfg(geo=["казань"]))[0]
    offline = order(geo="\u00a0Садовая\u00a0200 м")
    ok, reason = match(offline, cfg())  # по умолчанию только дистанционные
    assert not ok and "не дистанционный" in reason
    assert match(offline, cfg(remote_mode="any"))[0]
    assert match(offline, cfg(remote_mode="offline_only"))[0]
    assert not match(order(), cfg(remote_mode="offline_only"))[0]


def test_parse_ru_time():
    assert parse_ru_time("Только что", NOW) == NOW
    assert parse_ru_time("2 минуты назад", NOW) == NOW - timedelta(minutes=2)
    assert parse_ru_time("1 час назад", NOW) == NOW - timedelta(hours=1)
    assert parse_ru_time("час назад", NOW) == NOW - timedelta(hours=1)
    assert parse_ru_time("11 часов назад", NOW) == NOW - timedelta(hours=11)
    assert parse_ru_time("Вчера в 14:14", NOW) == datetime(2026, 9, 25, 14, 14)
    assert parse_ru_time("Сегодня в 08:05", NOW) == datetime(2026, 9, 26, 8, 5)
    assert parse_ru_time("21 сентября", NOW) == datetime(2026, 9, 21)
    assert parse_ru_time("17 сентября в 18:15", NOW) == datetime(2026, 9, 17, 18, 15)
    assert parse_ru_time("28 декабря", datetime(2027, 1, 3)) == datetime(2026, 12, 28)
    assert parse_ru_time("17 сентября в 18:15    Обновлен только что", NOW) == datetime(2026, 9, 17, 18, 15)
    assert parse_ru_time("Дистанционно · Москва", NOW) is None
    assert parse_ru_time("Садовая\u00a0200 м", NOW) is None


def test_age_filters():
    fresh = order(updated_at=NOW - timedelta(minutes=30), updated_text="30 минут назад")
    stale = order(updated_at=NOW - timedelta(hours=5), updated_text="5 часов назад")
    assert match(fresh, cfg(max_updated_hours=2), NOW)[0]
    ok, reason = match(stale, cfg(max_updated_hours=2), NOW)
    assert not ok and "обновлён давно" in reason
    assert match(stale, cfg(max_updated_hours=0), NOW)[0]
    assert match(order(), cfg(max_updated_hours=2), NOW)[0]  # время неизвестно — не отсекаем в ленте
    c = cfg(max_created_hours=24)
    assert created_too_old(NOW - timedelta(hours=3), c, NOW) == ""
    assert "старше" in created_too_old(datetime(2026, 9, 17, 18, 15), c, NOW)
    assert "не удалось" in created_too_old(None, c, NOW)
    assert created_too_old(None, cfg(), NOW) == ""


def test_client_type():
    assert match(order(), cfg(client_types=["private"]))[0]
    assert not match(order(), cfg(client_types=["company"]))[0]
    assert match(order(client_type="unknown"), cfg(client_types=["company"]))[0]
