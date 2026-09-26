from profi_bot.config import DEFAULTS
from profi_bot.filters import match
from profi_bot.models import Order, order_from_raw, parse_budget, parse_client_type


def cfg(**kw):
    c = dict(DEFAULTS["filters"])
    c.update(kw)
    return c


def order(**kw):
    base = dict(id="1", url="u", title="Ремонт ванной", description="Нужно положить плитку",
                category="Ремонт и строительство", budget_min=20000, budget_max=30000, geo="Москва, м. Сокол",
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
    remote = order(geo="Дистанционно")
    assert match(remote, cfg(geo=["казань"], remote_ok=True))[0]
    assert not match(remote, cfg(geo=["казань"], remote_ok=False))[0]
    assert not match(remote, cfg(remote_ok=False))[0]


def test_client_type():
    assert match(order(), cfg(client_types=["private"]))[0]
    assert not match(order(), cfg(client_types=["company"]))[0]
    assert match(order(client_type="unknown"), cfg(client_types=["company"]))[0]
