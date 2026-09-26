"""Цена в отклике по таблице «бюджет клиента → моя цена».

На Profi.ru в отклике одно поле «Цена», туда пишется одно число. Пользователь задаёт строки:
    бюджет клиента от 0    до 1000 → 1000
                   от 1000 до 1500 → 1300
Берётся первая строка, в которую попал бюджет. Не попал ни в одну — заказ не подходит.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Order


@dataclass
class PriceQuote:
    value: float
    reason: str = ""  # какая строка таблицы сработала — для логов

    @property
    def text(self) -> str:
        return f"{int(round(self.value)):,}".replace(",", " ")


def _num(value) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def rule_matches(budget: float, rule: dict) -> bool:
    low = _num(rule.get("budget_min")) or 0
    high = _num(rule.get("budget_max"))
    return budget >= low and (high is None or high == 0 or budget <= high)


def pick_price(order: Order | None, cfg: dict) -> PriceQuote | None:
    """Цена для заказа или None, если заказ не подходит по бюджету.
    Бюджет заказа — верхняя граница: «до 1400» → 1400, «750–950» → 950."""
    budget = order.budget if order else None
    if budget is None:
        if cfg.get("take_no_budget", True) and _num(cfg.get("no_budget_price")):
            return PriceQuote(float(cfg["no_budget_price"]), "бюджет не указан")
        return None
    for rule in cfg.get("rules") or []:
        price = _num(rule.get("price"))
        if price and rule_matches(budget, rule):
            high = _num(rule.get("budget_max"))
            span = f"{_num(rule.get('budget_min')) or 0:g}–{high:g}" if high else f"от {_num(rule.get('budget_min')) or 0:g}"
            return PriceQuote(price, f"бюджет {budget:g} ₽ в строке {span}")
    return None


def price_skip_reason(order: Order, cfg: dict) -> str:
    """Почему для заказа нет цены (для логов)."""
    if order.budget is None:
        return "бюджет не указан, а такие заказы выключены"
    return f"бюджет {order.budget:g} ₽ не попадает ни в одну строку таблицы цен"
