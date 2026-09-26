import random

from profi_bot.models import Order
from profi_bot.pricing import PriceQuote
from profi_bot.response_type import choose_type
from profi_bot.templates import build_variables, cleanup, pick_template, render


def test_render_variables():
    order = Order(id="1", url="", title="Сайт", client_name="Иван", budget_max=15000)
    v = build_variables(order, PriceQuote("from", 1000), {"my_name": "Никита", "deadline": "2 дня"})
    text = render("Привет, {name}! {title}: {price} ₽ за {deadline}. {budget} {unknown} {my_name}", v)
    assert text == "Привет, Иван! Сайт: от 1 000 ₽ за 2 дня. 15 000 {unknown} Никита"


def test_render_broken_braces_and_empty_name():
    assert render("Цена {price", {"price": "1"}) == "Цена {price"
    assert cleanup(render("Здравствуйте, {name}!", {"name": ""})) == "Здравствуйте!"


def test_rotation():
    cfg = {"rotation": "round_robin", "items": [
        {"name": "a", "text": "A"}, {"name": "b", "text": "B", "enabled": False}, {"name": "c", "text": "C"}]}
    assert [pick_template(cfg, i)["name"] for i in range(4)] == ["a", "c", "a", "c"]
    cfg["rotation"] = "random"
    names = {pick_template(cfg, 0, random.Random(i))["name"] for i in range(30)}
    assert names == {"a", "c"}
    assert pick_template({"items": []}, 0) is None


def test_choose_type():
    cfg = {"priority": ["commission", "paid"], "allow_paid": True, "allow_commission": True}
    assert choose_type({"paid", "commission"}, cfg) == "commission"
    assert choose_type({"paid"}, cfg) == "paid"
    assert choose_type({"paid"}, cfg, paid_allowed_by_limits=False) is None
    cfg["priority"] = ["paid", "commission"]
    assert choose_type({"paid", "commission"}, cfg) == "paid"
    assert choose_type({"paid", "commission"}, cfg, paid_allowed_by_limits=False) == "commission"
    cfg["allow_commission"] = False
    assert choose_type({"commission"}, cfg) is None


def test_choose_type_max_commission():
    cfg = {"priority": ["commission", "paid"], "allow_paid": True, "allow_commission": True, "max_commission": 1500}
    assert choose_type({"paid", "commission"}, cfg, commission_cost=1000) == "commission"
    assert choose_type({"paid", "commission"}, cfg, commission_cost=2081) == "paid"
    assert choose_type({"commission"}, cfg, commission_cost=2081) is None
    assert choose_type({"commission"}, {**cfg, "max_commission": 0}, commission_cost=99999) == "commission"


def test_choose_type_paid_cost_range():
    cfg = {"priority": ["paid", "commission"], "allow_paid": True, "allow_commission": True,
           "paid_cost_min": 50, "paid_cost_max": 120}
    assert choose_type({"paid", "commission"}, cfg, paid_cost=90) == "paid"
    assert choose_type({"paid", "commission"}, cfg, paid_cost=140) == "commission"
    assert choose_type({"paid"}, cfg, paid_cost=140) is None
    assert choose_type({"paid"}, cfg, paid_cost=30) is None
    assert choose_type({"paid"}, cfg, paid_cost=None) == "paid"  # не прочиталась — не отсекаем
    assert choose_type({"paid"}, {**cfg, "paid_cost_min": 0, "paid_cost_max": 0}, paid_cost=999) == "paid"
