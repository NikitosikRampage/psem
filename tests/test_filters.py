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


def test_price_table_as_filter():
    pricing = {"rules": [{"budget_min": 0, "budget_max": 4000, "price": 1200}],
               "take_no_budget": False, "no_budget_price": 0}
    assert match(order(budget_min=None, budget_max=1400), cfg(), pricing=pricing)[0]
    ok, reason = match(order(), cfg(), pricing=pricing)  # бюджет 30 000 — вне таблицы
    assert not ok and "не попадает" in reason
    ok, reason = match(order(budget_min=None, budget_max=None), cfg(), pricing=pricing)
    assert not ok and "не указан" in reason


def test_only_private_clients():
    for name in ("Школа Формула Будущего", "ООО Ромашка", "Онлайн-школа Физтех", "ИП Иванов", "Центр развития"):
        ok, reason = match(order(client_name=name), cfg())
        assert not ok and "организация" in reason, name
    for name in ("Юрий", "Анна", "Ипполит", "Мария Курсова", ""):
        assert match(order(client_name=name), cfg())[0], name


def test_remote_mode():
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


def test_age_filters_minutes():
    fresh = order(updated_at=NOW - timedelta(minutes=15), updated_text="15 минут назад")
    stale = order(updated_at=NOW - timedelta(minutes=25), updated_text="25 минут назад")
    assert match(fresh, cfg(max_updated_minutes=20), NOW)[0]
    ok, reason = match(stale, cfg(max_updated_minutes=20), NOW)
    assert not ok and "обновлён давно" in reason and "20 мин" in reason
    assert match(stale, cfg(max_updated_minutes=0), NOW)[0]
    assert match(order(), cfg(max_updated_minutes=20), NOW)[0]  # время неизвестно — не отсекаем в ленте
    c = cfg(max_created_minutes=20)
    assert created_too_old(NOW - timedelta(minutes=19), c, NOW) == ""
    assert "старше 20 мин" in created_too_old(NOW - timedelta(minutes=25), c, NOW)
    assert "не удалось" in created_too_old(None, c, NOW)
    assert created_too_old(None, cfg(), NOW) == ""
