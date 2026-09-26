from __future__ import annotations

import re
from datetime import datetime, timedelta
from functools import lru_cache

from .models import Order, looks_like_company
from .pricing import pick_price, price_skip_reason

REMOTE_MODES = ("remote_only", "any", "offline_only")

# Встроенный фильтр «набор репетиторов»: школы и посредники ищут преподавателей, а не учеников.
RECRUITMENT_PHRASES = (
    "в команду", "в нашу команду", "ищу преподавателя", "ищем преподавателя", "ищу репетитора",
    "ищем репетитора", "ищем педагога", "набираем преподавателей", "набор преподавателей",
    "набор репетиторов", "для репетиторов", "предложение для преподавателей", "вакансия",
    "ставка", "оплата за урок от", "поток учеников", "учеников предоставляем",
    "ученики предоставляются", "учеников и группы", "поиск учеников", "искать учеников",
    "выплаты каждую", "оформление по договору", "работа в нашей",
)

_ENDINGS = "аяыиеоуюйьё"


def _stem(word: str) -> str:
    """Грубое отсечение окончания: «математика» → «математик», «физике» → «физик»."""
    for _ in range(2):
        if len(word) > 4 and word[-1] in _ENDINGS:
            word = word[:-1]
    return word


@lru_cache(maxsize=512)
def _phrase_re(term: str):
    """«русский язык» ищет и «русскому языку», «математика» — и «по математике»."""
    words = re.findall(r"[\w-]+", term.lower().replace("ё", "е"))
    if not words:
        return None
    parts = [re.escape(_stem(w)) + r"[\w-]*" for w in words]
    return re.compile(r"(?<![\w-])" + r"[\s,.:;·\-]+".join(parts))


def _find(terms, text: str) -> str | None:
    """Первый из terms, найденный в text (с учётом окончаний), иначе None."""
    text = text.lower().replace("ё", "е")
    for term in terms or []:
        rx = _phrase_re(term.strip()) if term and term.strip() else None
        if rx and rx.search(text):
            return term.strip()
    return None


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

    text = f"{order.title}\n{order.description}"
    categories = [c for c in cfg.get("categories") or [] if c and c.strip()]
    # В ленте Profi.ru нет отдельной категории: предмет ищется в заголовке и описании
    # («Репетитор по подготовке к экзаменам · … Математика, Физика»).
    if categories and not _find(categories, f"{order.category}\n{text}"):
        return False, f"предмет не из списка («{order.title[:40]}»)"

    if cfg.get("skip_recruitment", True):
        phrase = _find(RECRUITMENT_PHRASES, text)
        if phrase:
            return False, f"набор репетиторов, а не ученик («{phrase}»)"

    include = [k for k in cfg.get("keywords_include") or [] if k and k.strip()]
    if include and not _find(include, text):
        return False, "нет ключевых слов"
    stop = _find(cfg.get("keywords_exclude"), text)
    if stop:
        return False, f"стоп-слово «{stop}»"

    if pricing is not None and pick_price(order, pricing) is None:
        return False, price_skip_reason(order, pricing)

    return True, ""
