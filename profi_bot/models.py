from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

_NUM_RE = re.compile(r"\d[\d\s  ]*")
_ID_RE = re.compile(r"(?:[?&](?:o|id|order_?id)=|/)(\d{4,})")

REMOTE_MARKERS = ("дистанц", "онлайн", "удален", "удалён", "remote")
COMPANY_MARKERS = ("компан", "организац", "юр", "ооо", "ип ")


@dataclass
class Order:
    id: str
    url: str
    title: str = ""
    description: str = ""
    category: str = ""
    budget_min: float | None = None
    budget_max: float | None = None
    geo: str = ""
    client_name: str = ""
    client_type: str = "unknown"  # private / company / unknown
    updated_text: str = ""  # время из карточки ленты («2 минуты назад») — последнее обновление заказа
    updated_at: datetime | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def budget(self) -> float | None:
        return self.budget_max if self.budget_max is not None else self.budget_min

    @property
    def is_remote(self) -> bool:
        geo = self.geo.lower()
        return any(m in geo for m in REMOTE_MARKERS)

    def to_row(self) -> dict:
        d = asdict(self)
        d.pop("raw")
        d["updated_at"] = self.updated_at.isoformat(timespec="minutes") if self.updated_at else None
        return d


def parse_budget(text: str | None) -> tuple[float | None, float | None]:
    """«до 5 000 ₽» → (None, 5000); «от 3 000» → (3000, None);
    «3 000 – 5 000 ₽» → (3000, 5000); «4500 ₽» → (4500, 4500)."""
    if not text:
        return None, None
    nums = [float(re.sub(r"\D", "", n)) for n in _NUM_RE.findall(text) if re.sub(r"\D", "", n)]
    if not nums:
        return None, None
    low = text.lower()
    if len(nums) >= 2:
        return min(nums[0], nums[1]), max(nums[0], nums[1])
    value = nums[0]
    if "до" in low.split() or low.strip().startswith("до"):
        return None, value
    if low.strip().startswith("от"):
        return value, None
    return value, value


def parse_client_type(text: str | None) -> str:
    if not text or not text.strip():
        return "unknown"
    low = text.lower()
    return "company" if any(m in low for m in COMPANY_MARKERS) else "private"


def order_id_from_url(url: str) -> str:
    m = _ID_RE.search(url or "")
    if m:
        return m.group(1)
    return hashlib.sha1((url or "").encode()).hexdigest()[:16]


_MONTHS = {m: i for i, m in enumerate(
    ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
     "сентября", "октября", "ноября", "декабря"), 1)}
_AGO_RE = re.compile(r"(?:(\d+)\s+)?(секунд\w*|сек|минут\w*|мин|час\w*|дн\w*|день)\s+назад")
_DAY_WORD_RE = re.compile(r"(сегодня|вчера|позавчера)(?:\s*,?\s*в?\s*(\d{1,2}):(\d{2}))?")
_DATE_RE = re.compile(r"(\d{1,2})\s+(" + "|".join(_MONTHS) + r")(?:\s+(\d{4}))?(?:\s*,?\s*в?\s*(\d{1,2}):(\d{2}))?")
_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


_NOW_RE = re.compile(r"только что|^сейчас$")


def parse_ru_time(text: str | None, now: datetime | None = None) -> datetime | None:
    """«Только что», «2 минуты назад», «час назад», «Вчера в 14:14», «21 сентября»,
    «17 сентября в 18:15» → datetime. Берётся первое встретившееся в тексте время
    (важно для «17 сентября в 18:15 Обновлен только что»). Не распознано → None."""
    if not text:
        return None
    now = now or datetime.now()
    t = text.lower().replace("\u00a0", " ").replace("\u202f", " ").strip()
    candidates = [(m.start(), kind, m) for kind, rx in
                  (("now", _NOW_RE), ("ago", _AGO_RE), ("day", _DAY_WORD_RE), ("date", _DATE_RE), ("clock", _CLOCK_RE))
                  for m in [rx.search(t)] if m]
    if not candidates:
        return None
    _, kind, m = min(candidates, key=lambda c: c[0])
    if kind == "now":
        return now
    if kind == "ago":
        n = int(m.group(1) or 1)
        unit = m.group(2)
        if unit.startswith("сек"):
            delta = timedelta(seconds=n)
        elif unit.startswith("мин"):
            delta = timedelta(minutes=n)
        elif unit.startswith("час"):
            delta = timedelta(hours=n)
        else:
            delta = timedelta(days=n)
        return now - delta
    if kind == "day":
        days = {"сегодня": 0, "вчера": 1, "позавчера": 2}[m.group(1)]
        day = (now - timedelta(days=days)).date()
        hh, mm = (int(m.group(2)), int(m.group(3))) if m.group(2) else (0, 0)
        return datetime.combine(day, datetime.min.time()).replace(hour=hh, minute=mm)
    if kind == "date":
        day, month = int(m.group(1)), _MONTHS[m.group(2)]
        year = int(m.group(3)) if m.group(3) else now.year
        hh, mm = (int(m.group(4)), int(m.group(5))) if m.group(4) else (0, 0)
        try:
            result = datetime(year, month, day, hh, mm)
        except ValueError:
            return None
        if not m.group(3) and result > now + timedelta(days=1):
            result = result.replace(year=year - 1)  # «28 декабря» в январе — прошлый год
        return result
    result = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    return result if result <= now else result - timedelta(days=1)


def order_from_raw(raw: dict, now: datetime | None = None) -> Order:
    """Строит Order из словаря, извлечённого со страницы (см. browser.py)."""
    url = raw.get("url") or ""
    bmin, bmax = parse_budget(raw.get("budget"))
    return Order(
        id=str(raw.get("id") or order_id_from_url(url)),
        url=url,
        title=(raw.get("title") or "").strip(),
        description=(raw.get("description") or "").strip(),
        category=(raw.get("category") or "").strip(),
        budget_min=bmin,
        budget_max=bmax,
        geo=(raw.get("geo") or "").strip(),
        client_name=(raw.get("client_name") or "").strip(),
        client_type=parse_client_type(raw.get("client_type")),
        updated_text=(raw.get("updated") or "").strip(),
        updated_at=parse_ru_time(raw.get("updated"), now),
        raw=raw,
    )
