from __future__ import annotations

from .models import Order


def _norm(items) -> list[str]:
    return [s.strip().lower() for s in (items or []) if s and s.strip()]


def match(order: Order, cfg: dict) -> tuple[bool, str]:
    """Проверяет заказ по фильтрам. Возвращает (подходит, причина отказа)."""
    categories = _norm(cfg.get("categories"))
    if categories:
        cat = order.category.lower()
        if not any(c in cat for c in categories):
            return False, f"категория «{order.category or '—'}» не в списке"

    text = f"{order.title}\n{order.description}".lower()
    include = _norm(cfg.get("keywords_include"))
    if include and not any(k in text for k in include):
        return False, "нет ключевых слов"
    for k in _norm(cfg.get("keywords_exclude")):
        if k in text:
            return False, f"стоп-слово «{k}»"

    fmin, fmax = cfg.get("budget_min"), cfg.get("budget_max")
    if order.budget_min is None and order.budget_max is None:
        if not cfg.get("allow_no_budget", True):
            return False, "бюджет не указан"
    elif fmin is not None or fmax is not None:
        # Пересечение диапазона бюджета заказа с диапазоном фильтра.
        o_low = order.budget_min if order.budget_min is not None else 0
        o_high = order.budget_max if order.budget_max is not None else float("inf")
        if fmin is not None and o_high < fmin:
            return False, f"бюджет {order.budget:g} меньше {fmin:g}"
        if fmax is not None and o_low > fmax:
            return False, f"бюджет {order.budget:g} больше {fmax:g}"

    geo = _norm(cfg.get("geo"))
    if geo:
        order_geo = order.geo.lower()
        remote_pass = cfg.get("remote_ok", True) and order.is_remote
        if not remote_pass and not any(g in order_geo for g in geo):
            return False, f"гео «{order.geo or '—'}» не подходит"
    elif not cfg.get("remote_ok", True) and order.is_remote:
        return False, "дистанционные заказы отключены"

    client_types = cfg.get("client_types") or []
    if client_types and order.client_type != "unknown" and order.client_type not in client_types:
        return False, f"тип клиента {order.client_type}"

    return True, ""
