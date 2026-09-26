from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field

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


def order_from_raw(raw: dict) -> Order:
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
        raw=raw,
    )
