"""Расчёт цены отклика: fixed | from | range | formula."""
from __future__ import annotations

import ast
import math
import operator
from dataclasses import dataclass

from .models import Order


class FormulaError(ValueError):
    pass


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_UNARY_OPS = {ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {"min": min, "max": max, "round": round, "abs": abs}


def eval_formula(expr: str, variables: dict[str, float | None]) -> float:
    """Безопасно вычисляет арифметическую формулу, например «budget * 0.9 - 100»
    или «max(budget_min, 500)». Доступны переменные budget, budget_min, budget_max."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"синтаксическая ошибка: {exc.msg}") from None

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in variables:
                raise FormulaError(f"неизвестная переменная «{node.id}»")
            value = variables[node.id]
            if value is None:
                raise FormulaError(f"у заказа нет значения «{node.id}»")
            return value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            return _BIN_OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
            return _UNARY_OPS[type(node.op)](ev(node.operand))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _FUNCS
            and not node.keywords
        ):
            return _FUNCS[node.func.id](*[ev(a) for a in node.args])
        raise FormulaError(f"недопустимое выражение: {ast.dump(node)[:40]}")

    try:
        result = ev(tree)
    except ZeroDivisionError:
        raise FormulaError("деление на ноль") from None
    except TypeError as exc:
        raise FormulaError(str(exc)) from None
    if not isinstance(result, (int, float)) or math.isnan(result) or math.isinf(result):
        raise FormulaError("результат не число")
    return float(result)


@dataclass
class PriceQuote:
    mode: str
    value: float  # основная цена (для from/range — нижняя граница)
    value_max: float | None = None  # верхняя граница для range

    @property
    def text(self) -> str:
        v = _fmt(self.value)
        if self.mode == "from":
            return f"от {v}"
        if self.mode == "range" and self.value_max is not None:
            return f"{v}–{_fmt(self.value_max)}"
        return v


def _fmt(value: float) -> str:
    return f"{int(round(value)):,}".replace(",", " ")


def _round(value: float, step) -> float:
    step = float(step or 0)
    if step <= 0:
        return round(value)
    return round(value / step) * step


def _clamp(value: float, cfg: dict) -> float:
    if cfg.get("min_price") is not None:
        value = max(value, float(cfg["min_price"]))
    if cfg.get("max_price") is not None:
        value = min(value, float(cfg["max_price"]))
    return value


def calc_price(order: Order | None, cfg: dict) -> PriceQuote:
    mode = cfg.get("mode", "fixed")
    step = cfg.get("round_to")
    if mode == "range":
        low = _clamp(_round(float(cfg["range_min"]), step), cfg)
        high = _clamp(_round(float(cfg["range_max"]), step), cfg)
        return PriceQuote("range", low, max(low, high))
    if mode == "formula":
        variables = {
            "budget": order.budget if order else None,
            "budget_min": order.budget_min if order else None,
            "budget_max": order.budget_max if order else None,
        }
        try:
            value = eval_formula(cfg["formula"], variables)
        except FormulaError:
            value = float(cfg.get("fallback_price") or cfg.get("fixed") or 0)
        return PriceQuote("formula", _clamp(_round(value, step), cfg))
    value = _clamp(_round(float(cfg.get("fixed") or 0), step), cfg)
    return PriceQuote("from" if mode == "from" else "fixed", value)
