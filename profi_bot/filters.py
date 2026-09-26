from __future__ import annotations

from datetime import datetime, timedelta

from .models import Order

REMOTE_MODES = ("remote_only", "any", "offline_only")


def _norm(items) -> list[str]:
    return [s.strip().lower() for s in (items or []) if s and s.strip()]


def too_old(order: Order, cfg: dict, now: datetime | None = None) -> str:
    """Причина, если заказ обновлялся слишком давно (по времени из карточки ленты), иначе ''.
    Если время не распознано, заказ не отсекается — возраст проверится по дате создания."""
    hours = float(cfg.get("max_updated_hours") or 0)
    if hours <= 0 or order.updated_at is None:
        return ""
    now = now or datetime.now()
    if now - order.updated_at > timedelta(hours=hours):
        when = order.updated_text or order.updated_at.strftime("%d.%m %H:%M")
        return f"обновлён давно ({when}), лимит {hours:g} ч"
    return ""


def created_too_old(created_at: datetime | None, cfg: dict, now: datetime | None = None) -> str:
    """Проверка даты создания со страницы заказа. Не удалось определить — заказ пропускается."""
    hours = float(cfg.get("max_created_hours") or 0)
    if hours <= 0:
        return ""
    if created_at is None:
        return "не удалось определить дату создания заказа"
    now = now or datetime.now()
    if now - created_at > timedelta(hours=hours):
        return f"создан {created_at:%d.%m %H:%M}, это старше {hours:g} ч"
    return ""


def match(order: Order, cfg: dict, now: datetime | None = None) -> tuple[bool, str]:
    """Проверяет заказ по фильтрам. Возвращает (подходит, причина отказа)."""
    mode = cfg.get("remote_mode", "remote_only")
    if mode == "remote_only" and not order.is_remote:
        return False, f"не дистанционный ({order.geo.splitlines()[0][:40] if order.geo else 'гео не указано'})"
    if mode == "offline_only" and order.is_remote:
        return False, "дистанционный заказ"

    age = too_old(order, cfg, now)
    if age:
        return False, age

    categories = _norm(cfg.get("categories"))
    if categories:
        # В ленте Profi.ru нет отдельной категории — роль предмета/услуги играет заголовок.
        cat = f"{order.category}\n{order.title}".lower()
        if not any(c in cat for c in categories):
            return False, f"категория «{order.category or order.title or '—'}» не в списке"

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
    if geo and not any(g in order.geo.lower() for g in geo):
        return False, f"гео «{order.geo.splitlines()[0] if order.geo else '—'}» не подходит"

    client_types = cfg.get("client_types") or []
    if client_types and order.client_type != "unknown" and order.client_type not in client_types:
        return False, f"тип клиента {order.client_type}"

    return True, ""
