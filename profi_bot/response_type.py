from __future__ import annotations

LABELS = {"paid": "платный", "commission": "за комиссию"}


def choose_type(available: set[str], cfg: dict, paid_allowed_by_limits: bool = True) -> str | None:
    """Выбирает тип отклика по приоритету среди доступных на странице заказа.

    available — какие варианты реально есть в форме (paid/commission);
    cfg — секция response настроек; paid_allowed_by_limits — не исчерпаны ли лимиты платных.
    """
    allowed = set()
    if cfg.get("allow_paid", True) and paid_allowed_by_limits:
        allowed.add("paid")
    if cfg.get("allow_commission", True):
        allowed.add("commission")
    priority = list(cfg.get("priority") or ["commission", "paid"])
    for t in ("commission", "paid"):
        if t not in priority:
            priority.append(t)
    for t in priority:
        if t in allowed and t in available:
            return t
    return None
