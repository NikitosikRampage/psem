import pytest

from profi_bot.config import DEFAULTS
from profi_bot.models import Order
from profi_bot.pricing import FormulaError, calc_price, eval_formula


def cfg(**kw):
    c = dict(DEFAULTS["pricing"])
    c.update(kw)
    return c


ORDER = Order(id="1", url="", budget_min=4000, budget_max=5000)


def test_modes():
    assert calc_price(ORDER, cfg(mode="fixed", fixed=1234, round_to=100)).text == "1 200"
    q = calc_price(ORDER, cfg(mode="from", fixed=1500))
    assert q.text == "от 1 500" and q.value == 1500
    q = calc_price(ORDER, cfg(mode="range", range_min=1000, range_max=2000))
    assert (q.value, q.value_max, q.text) == (1000, 2000, "1 000–2 000")
    assert calc_price(ORDER, cfg(mode="formula", formula="budget * 0.9")).value == 4500


def test_formula_fallback_and_clamp():
    no_budget = Order(id="2", url="")
    assert calc_price(no_budget, cfg(mode="formula", fallback_price=700, round_to=0)).value == 700
    assert calc_price(ORDER, cfg(mode="formula", formula="budget - 100", max_price=3000)).value == 3000
    assert calc_price(ORDER, cfg(mode="fixed", fixed=10, min_price=500)).value == 500


def test_eval_formula_safe():
    v = {"budget": 1000, "budget_min": 800, "budget_max": 1000}
    assert eval_formula("max(budget_min, 900) - 10", v) == 890
    assert eval_formula("round(budget / 3)", v) == 333
    for bad in ("__import__('os')", "budget.real", "open('x')", "budget +", "x * 2", "1/0", "[1]"):
        with pytest.raises(FormulaError):
            eval_formula(bad, v)
