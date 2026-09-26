from __future__ import annotations

from datetime import datetime, timedelta

from .models import Order, looks_like_company
from .pricing import pick_price, price_skip_reason

REMOTE_MODES = ("remote_only", "any", "offline_only")


def _norm(items) -> list[str]:
    return [s.strip().lower() for s in (items or []) if s and s.strip()]


def too_old(order: Order, cfg: dict, now: datetime | None = None) -> str:
    """Причина, если заказ обновлялся слишком давно (по времени из карточки ленты), иначе ''.
    Если время не распознано, заказ не отсекается — возраст проверится по дате создания."""
    minutes = float(cfg.get("max_updated_minutes") or 0)
    if minutes <= 0 or order.updated_at is None:
        return ""
    now = now or datetime.now()
    if now - order.updated_at > timedelta(minutes=minutes):
        when = order.updated_text or order.updated_at.strftime("%d.%m %H:%M")
        return f"обновлён давно ({when}), нужно не старше {minutes:g} мин"
    return ""


def created_too_old(created_at: datetime | None, cfg: dict, now: datetime | None = None) -> str:
    """Проверка даты создания со страницы заказа. Не удалось определить — заказ пропускается."""
    minutes = float(cfg.get("max_created_minutes") or 0)
    if minutes <= 0:
        return ""
    if created_at is None:
        return "не удалось определить дату создания заказа"
    now = now or datetime.now()
    if now - created_at > timedelta(minutes=minutes):
        return f"создан {created_at:%d.%m %H:%M}, это старше {minutes:g} мин"
    return ""


def match(order: Order, cfg: dict, now: datetime | None = None, pricing: dict | None = None) -> tuple[bool, str]:
    """Проверяет заказ по фильтрам (и по таблице цен, если передана). Возвращает (подходит, причина)."""
    mode = cfg.get("remote_mode", "remote_only")
    if mode == "remote_only" and not order.is_remote:
        return False, f"не дистанционный ({order.geo.splitlines()[0][:40] if order.geo else 'гео не указано'})"
    if mode == "offline_only" and order.is_remote:
        return False, "дистанционный заказ"

    age = too_old(order, cfg, now)
    if age:
        return False, age

    # Только частные лица: организации (школы, центры, ООО…) отсекаются по имени клиента.
    if looks_like_company(order.client_name):
        return False, f"клиент — организация («{order.client_name[:40]}»)"

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

    if pricing is not None and pick_price(order, pricing) is None:
        return False, price_skip_reason(order, pricing)

    return True, ""
