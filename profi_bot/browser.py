"""Работа с уже запущенным Chrome через Playwright (connect_over_cdp).

Браузер пользователя не закрывается: бот открывает свою вкладку в существующем
профиле (с куками Profi.ru), а при остановке закрывает только её.
Все объекты Playwright должны использоваться из одного потока — того, где вызван connect().
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Order, order_from_raw, parse_budget
from .pricing import PriceQuote

_BUDGET_IN_TEXT = re.compile(
    r"((?:от|до)\s*)?\d[\d\s  ]*(?:\s*[–—-]\s*\d[\d\s  ]*)?\s*(?:₽|руб)", re.I
)

_EXTRACT_JS = """
(sel) => {
  const q = (root, s) => { if (!s) return null; try { return root.querySelector(s); } catch (e) { return null; } };
  const txt = (root, s) => { const el = q(root, s); return el ? el.innerText.trim() : ""; };
  let cards = [];
  try { cards = Array.from(document.querySelectorAll(sel.card)); } catch (e) { return []; }
  // Если селектор совпал и с карточкой, и с элементом внутри неё — берём только внешний.
  cards = cards.filter(c => !cards.some(o => o !== c && o.contains(c)));
  return cards.map(card => {
    let link = q(card, sel.link);
    if (!link && card.tagName === "A") link = card;
    if (!link) link = card.closest("a");
    return {
      id: sel.id_attr ? (card.getAttribute(sel.id_attr) || "") : "",
      url: link ? link.href : "",
      title: txt(card, sel.title) || (link ? link.innerText.trim().split("\\n")[0] : ""),
      description: txt(card, sel.description),
      budget: txt(card, sel.budget),
      geo: txt(card, sel.geo),
      category: txt(card, sel.category),
      client_name: txt(card, sel.client_name),
      client_type: txt(card, sel.client_type),
      full_text: card.innerText || "",
    };
  });
}
"""


def enrich_raw(raw: dict, lst: dict) -> dict:
    """Дополняет данные карточки тем, для чего нет отдельного селектора."""
    lines = [ln.strip() for ln in (raw.get("full_text") or "").splitlines() if ln.strip()]
    # Бюджет ищем в тексте, только если селектор бюджета не задан: в описаниях
    # часто встречаются суммы («ставка 700 ₽/час»), которые бюджетом не являются.
    if not raw.get("budget") and not lst.get("budget"):
        m = _BUDGET_IN_TEXT.search(raw.get("full_text", ""))
        raw["budget"] = m.group(0) if m else ""
    line_idx = lst.get("client_name_line")
    if not raw.get("client_name") and line_idx not in (None, "") and lines:
        try:
            candidate = lines[int(line_idx)]
        except (IndexError, ValueError):
            candidate = ""
        known = {raw.get("title", ""), raw.get("description", "")} | set((raw.get("geo") or "").splitlines())
        if candidate and len(candidate) <= 60 and candidate not in known and "₽" not in candidate:
            raw["client_name"] = candidate
    return raw


class AlreadyResponded(Exception):
    pass


class FormNotFound(Exception):
    pass


@dataclass
class ResponseForm:
    available_types: set[str] = field(default_factory=set)
    paid_cost: float | None = None
    commission_cost: float | None = None
    client_name: str = ""


class BrowserClient:
    def __init__(self, cdp_url: str, selectors: dict, log=print):
        self.cdp_url = cdp_url
        self.sel = selectors
        self.log = log
        self._pw = None
        self.browser = None
        self.page = None

    # --- подключение ---
    def connect(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        try:
            self.browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
        except Exception:
            self._pw.stop()
            self._pw = None
            raise
        context = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
        self.page = context.new_page()
        behavior = self.sel.get("behavior", {})
        self.page.set_default_navigation_timeout(behavior.get("navigation_timeout_ms", 30000))
        self.page.set_default_timeout(behavior.get("element_timeout_ms", 8000))

    def close(self) -> None:
        try:
            if self.page and not self.page.is_closed():
                self.page.close()
        except Exception:
            pass
        # playwright.stop() только отключается от CDP, сам Chrome пользователя остаётся открытым.
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self.browser = self.page = None

    # --- лента заказов ---
    def orders_urls(self) -> list[str]:
        urls = self.sel.get("orders_urls") or self.sel.get("orders_url") or []
        return [urls] if isinstance(urls, str) else list(urls)

    def fetch_orders(self) -> list[Order]:
        orders: dict[str, Order] = {}
        for url in self.orders_urls():
            for order in self._fetch_feed(url):
                orders.setdefault(order.id, order)
        return list(orders.values())

    def _fetch_feed(self, url: str) -> list[Order]:
        lst = self.sel.get("list", {})
        self.page.goto(url, wait_until="domcontentloaded")
        try:
            self.page.wait_for_selector(lst.get("wait_for") or lst["card"], state="attached")
        except Exception:
            self.log("warning", f"Лента {url}: карточки заказов не найдены (проверьте selectors.yaml)")
            return []
        for _ in range(int(lst.get("scroll_times", 0) or 0)):
            self.page.mouse.wheel(0, 4000)
            self.page.wait_for_timeout(700)
        raws = self.page.evaluate(_EXTRACT_JS, lst)
        return [order_from_raw(enrich_raw(raw, lst)) for raw in raws if raw.get("url")]

    # --- страница заказа ---
    def _find(self, selector: str | None):
        """Первый видимый элемент по селектору или None (без долгого ожидания)."""
        if not selector:
            return None
        loc = self.page.locator(selector)
        try:
            count = loc.count()
        except Exception:
            return None
        for i in range(count):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    return el
            except Exception:
                continue
        return None

    def open_order(self, order: Order) -> ResponseForm:
        op = self.sel.get("order_page", {})
        self.page.goto(order.url, wait_until="domcontentloaded")
        self.page.wait_for_timeout(800)
        if self._find(op.get("already_responded")):
            raise AlreadyResponded()

        button = self._find(op.get("respond_button"))
        if button:
            button.click()
            self.page.wait_for_timeout(600)
        if op.get("form"):
            try:
                self.page.wait_for_selector(op["form"], state="visible")
            except Exception:
                raise FormNotFound("форма отклика не появилась") from None

        form = ResponseForm()
        if self._find(op.get("type_paid")):
            form.available_types.add("paid")
        if self._find(op.get("type_commission")):
            form.available_types.add("commission")
        if not form.available_types:
            if not op.get("default_type"):
                # На Profi.ru блока тарифов нет, если отклик уже отправлен или заказ закрыт.
                raise AlreadyResponded("нет блока выбора тарифа — вероятно, отклик уже есть или заказ закрыт")
            form.available_types.add(op["default_type"])

        form.paid_cost = self._read_amount(op.get("paid_cost"))
        form.commission_cost = self._read_amount(op.get("commission_cost"))
        name_el = self._find(op.get("client_name"))
        if name_el:
            form.client_name = name_el.inner_text().strip()
        return form

    def _read_amount(self, selector: str | None) -> float | None:
        el = self._find(selector)
        if not el:
            return None
        low, high = parse_budget(el.inner_text())
        return high if high is not None else low

    def _type_text(self, el, text: str) -> None:
        delay = int(self.sel.get("behavior", {}).get("typing_delay_ms", 0) or 0)
        el.click()
        el.fill("")
        if delay > 0:
            el.press_sequentially(text, delay=delay)
        else:
            el.fill(text)

    def submit_response(
        self,
        response_type: str,
        message: str,
        quote: PriceQuote,
        dry_run: bool,
    ) -> bool:
        """Выбирает тариф, заполняет форму и (если не dry_run) отправляет.
        Возвращает True, если отклик отправлен."""
        op = self.sel.get("order_page", {})
        type_el = self._find(op.get(f"type_{response_type}"))
        if type_el:
            type_el.click()
            self.page.wait_for_timeout(500)

        if op.get("continue_button"):
            # Для комиссии «Продолжить» только открывает панель с текстом (проверено по снимку).
            # Что происходит после «Продолжить» у платного тарифа, пока не проверено, поэтому
            # в тестовом режиме бот останавливается на выборе тарифа.
            if dry_run and response_type not in (self.sel.get("behavior", {}).get("dry_run_continue_types")
                                                 or ["commission"]):
                return False
            cont = self._find(op["continue_button"])
            if not cont:
                raise FormNotFound("не найдена кнопка «Продолжить»")
            cont.click()
            if op.get("message_input"):
                try:
                    self.page.wait_for_selector(op["message_input"], state="visible")
                except Exception:
                    raise FormNotFound("после «Продолжить» не появилось поле текста отклика") from None

        price_el = self._find(op.get("price_input"))
        if price_el:
            price_el.fill(str(int(round(quote.value))))
        if quote.mode == "range" and quote.value_max is not None:
            price_max = self._find(op.get("price_max_input"))
            if price_max:
                price_max.fill(str(int(round(quote.value_max))))
        if quote.mode == "from":
            from_box = self._find(op.get("price_from_checkbox"))
            if from_box:
                from_box.check() if from_box.evaluate("e => e.type === 'checkbox'") else from_box.click()

        msg_el = self._find(op.get("message_input"))
        if not msg_el:
            raise FormNotFound("не найдено поле текста отклика")
        self._type_text(msg_el, message)

        if dry_run:
            return False
        submit = self._find(op.get("submit_button"))
        if not submit:
            raise FormNotFound("не найдена кнопка отправки")
        submit.click()
        try:
            if op.get("success"):
                self.page.wait_for_selector(op["success"], state="visible")
            else:
                # Отдельного сообщения об успехе нет — ждём, пока панель с кнопкой закроется.
                self.page.wait_for_selector(op["submit_button"], state="hidden")
        except Exception:
            raise FormNotFound("не дождались подтверждения отправки — проверьте заказ вручную") from None
        return True
