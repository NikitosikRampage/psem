from profi_bot.models import Order, order_from_raw
from profi_bot.pricing import pick_price, price_skip_reason

CFG = {
    "rules": [
        {"budget_min": 0, "budget_max": 1000, "price": 1000},
        {"budget_min": 1000, "budget_max": 1500, "price": 1300},
        {"budget_min": 1500, "budget_max": 4000, "price": 1500},
    ],
    "take_no_budget": True,
    "no_budget_price": 1200,
}


def price(budget_text, cfg=CFG):
    q = pick_price(order_from_raw({"url": "u", "budget": budget_text}), cfg)
    return q.value if q else None


def test_rules():
    assert price("800 ₽") == 1000
    assert price("1000 ₽") == 1000  # граница — первая подходящая строка
    assert price("1 200 ₽") == 1300
    assert price("до 1400 ₽") == 1300  # «до» — верхняя граница
    assert price("750–950 ₽") == 1000  # диапазон — верхняя граница 950
    assert price("4000 ₽") == 1500
    assert price("5000 ₽") is None  # вне таблицы — заказ не подходит


def test_no_budget_and_open_top():
    assert price("") == 1200
    assert price("", {**CFG, "take_no_budget": False}) is None
    open_top = {**CFG, "rules": [{"budget_min": 2000, "budget_max": None, "price": 1800}]}
    assert price("99 000 ₽", open_top) == 1800
    assert price("1 500 ₽", open_top) is None


def test_text_and_reasons():
    q = pick_price(Order(id="1", url="", budget_max=1400), CFG)
    assert q.text == "1 300" and "1000–1500" in q.reason
    assert "не попадает" in price_skip_reason(Order(id="1", url="", budget_max=9000), CFG)
    assert "не указан" in price_skip_reason(Order(id="1", url=""), CFG)
