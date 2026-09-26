"""Шаблоны откликов с переменными и ротацией."""
from __future__ import annotations

import random
import string

from .models import Order
from .pricing import PriceQuote

VARIABLES = {
    "name": "имя клиента",
    "my_name": "ваше имя",
    "price": "ваша цена из таблицы цен (например «1 300»)",
    "deadline": "срок",
    "title": "название заказа",
    "category": "категория",
    "budget": "бюджет заказа",
}


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render(text: str, variables: dict) -> str:
    """Подставляет {переменные}; неизвестные остаются как есть, битые скобки не роняют рендер."""
    try:
        return string.Formatter().vformat(text, (), _SafeDict(variables))
    except (ValueError, IndexError):
        out = text
        for k, v in variables.items():
            out = out.replace("{" + k + "}", str(v))
        return out


def build_variables(order: Order | None, quote: PriceQuote, cfg: dict) -> dict:
    budget = order.budget if order else None
    return {
        "name": (order.client_name if order else "") or "",
        "my_name": cfg.get("my_name", ""),
        "price": quote.text,
        "deadline": cfg.get("deadline", ""),
        "title": order.title if order else "",
        "category": order.category if order else "",
        "budget": f"{int(budget):,}".replace(",", " ") if budget is not None else "",
    }


def active_templates(cfg: dict) -> list[dict]:
    return [t for t in cfg.get("items", []) if t.get("enabled", True) and t.get("text", "").strip()]


def pick_template(cfg: dict, counter: int, rng: random.Random | None = None) -> dict | None:
    """Выбирает шаблон: round_robin по счётчику или случайно."""
    items = active_templates(cfg)
    if not items:
        return None
    if cfg.get("rotation") == "random":
        return (rng or random).choice(items)
    return items[counter % len(items)]


def cleanup(text: str) -> str:
    """Убирает пустые хвосты вида «Здравствуйте, !» при пустом имени."""
    return text.replace(" , ", ", ").replace(", !", "!").replace(" !", "!").strip()
