"""Работа с уже запущенным Chrome через Playwright (connect_over_cdp).

Браузер пользователя не закрывается: бот открывает свою вкладку в существующем
профиле (с куками Profi.ru), а при остановке закрывает только её.
Все объекты Playwright должны использоваться из одного потока — того, где вызван connect().
"""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .models import Order, order_from_raw, parse_budget, parse_ru_time
from .pricing import PriceQuote
from .sanitize import sanitized_html

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
    # Время обновления — строка карточки (обычно последняя), если она похожа на время.
    upd_idx = lst.get("updated_line")
    if not raw.get("updated") and upd_idx not in (None, "") and lines:
        try:
            candidate = lines[int(upd_idx)]
        except (IndexError, ValueError):
            candidate = ""
        if parse_ru_time(candidate) is not None:
            raw["updated"] = candidate
    line_idx = lst.get("client_name_line")
    if not raw.get("client_name") and line_idx not in (None, "") and lines:
        try:
            candidate = lines[int(line_idx)]
        except (IndexError, ValueError):
            candidate = ""
        known = {raw.get("title", ""), raw.get("description", "")} | set((raw.get("geo") or "").splitlines())
        if (candidate and len(candidate) <= 60 and candidate not in known and "₽" not in candidate
                and candidate != raw.get("updated")):
            raw["client_name"] = candidate
    return raw


class AlreadyResponded(Exception):
    pass


class OrderClosed(AlreadyResponded):
    """«Заказ скрыт — на него нельзя откликнуться», «Заказ закрыт» и т.п."""


class NoTariffs(AlreadyResponded):
    """Страница заказа загрузилась, но блока «Выберите тариф» нет."""


class FormNotFound(Exception):
    pass


class PageNotLoaded(FormNotFound):
    """Страница заказа не загрузилась — заказ стоит перепроверить позже."""


@dataclass
class ResponseForm:
    available_types: set[str] = field(default_factory=set)
    paid_cost: float | None = None
    commission_cost: float | None = None
    blocked_types: set[str] = field(default_factory=set)  # тарифы с замком («недоступен»)
    client_name: str = ""
    created_text: str = ""
    created_at: "datetime | None" = None


class BrowserClient:
    def __init__(self, cdp_url: str, selectors: dict, log=print, debug_dir: Path | str | None = None):
        self.cdp_url = cdp_url
        self.sel = selectors
        self.log = log
        self.debug_dir = Path(debug_dir) if debug_dir else None
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

    def back_to_feed(self) -> None:
        """Вернуться в ленту заказов (между проверками бот не стоит на странице заказа)."""
        urls = self.orders_urls()
        if not urls or not self.page or self.page.url.split("?")[0] == urls[0] and "o=" not in self.page.url:
            return
        try:
            self.page.goto(urls[0], wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001 — не критично, лента перезагрузится на следующем круге
            pass

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
        behavior = self.sel.get("behavior", {})
        self.page.goto(order.url, wait_until="domcontentloaded")
        # Кабинет Profi.ru дорисовывает блок тарифов скриптами — ждём его появления.
        if op.get("tariffs_block"):
            try:
                # Ждём либо блок тарифов, либо плашку «Заказ скрыт/закрыт» — что появится раньше.
                target = ", ".join(x for x in (op["tariffs_block"], op.get("closed_block")) if x)
                self.page.wait_for_selector(target, state="visible",
                                            timeout=int(behavior.get("order_load_timeout_ms", 15000)))
                closed = self._find(op.get("closed_block"))
                if closed:
                    raise OrderClosed(" ".join(closed.inner_text().split())[:120])
                types = ", ".join(op[k] for k in ("type_paid", "type_commission") if op.get(k))
                if types:
                    self.page.wait_for_selector(types, state="visible", timeout=3000)
            except OrderClosed:
                raise
            except Exception:  # noqa: BLE001 — не дождались: ниже разберёмся, почему
                pass
            self.page.wait_for_timeout(random.randint(300, 800))
        else:
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
        form.paid_cost = self._read_amount(op.get("paid_cost"))
        form.commission_cost = self._read_amount(op.get("commission_cost"))
        costs = {"paid": form.paid_cost, "commission": form.commission_cost}
        for kind in ("paid", "commission"):
            el = self._find(op.get(f"type_{kind}"))
            if not el:
                continue
            # Заблокированный тариф («Тариф недоступен…», замок, без цены) не считается доступным.
            text = el.inner_text().lower()
            blocked = op.get("unavailable_pattern") and re.search(op["unavailable_pattern"], text)
            if blocked or (op.get(f"{kind}_cost") and costs[kind] is None):
                form.blocked_types.add(kind)
            else:
                form.available_types.add(kind)
        if not form.available_types and not form.blocked_types:
            if not op.get("default_type"):
                pattern = op.get("order_loaded_pattern")
                try:
                    body = self.page.inner_text("body") if pattern else ""
                except Exception:  # noqa: BLE001
                    body = ""
                if pattern and not re.search(pattern, body):
                    path = self.save_debug(order, "not_loaded")
                    raise PageNotLoaded(f"страница заказа не загрузилась{_debug_note(path)}")
                # Карточка заказа есть, а тарифов нет: отклик уже отправлен или заказ закрыт.
                path = self.save_debug(order, "no_tariffs")
                raise NoTariffs("нет блока выбора тарифа — вероятно, отклик уже есть или заказ закрыт"
                                + _debug_note(path))
            form.available_types.add(op["default_type"])

        name_el = self._find(op.get("client_name"))
        if name_el:
            form.client_name = name_el.inner_text().strip()
        if op.get("created_pattern"):
            try:
                body = self.page.inner_text("body")
            except Exception:  # noqa: BLE001
                body = ""
            m = re.search(op["created_pattern"], body)
            if m:
                form.created_text = m.group(1).strip()
                form.created_at = parse_ru_time(form.created_text)
        return form

    def save_debug(self, order: Order, tag: str) -> Path | None:
        """Сохраняет очищенный HTML и скриншот страницы для разбора непонятных случаев."""
        if not self.debug_dir:
            return None
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            base = self.debug_dir / f"{order.id}_{tag}_{datetime.now():%Y%m%d_%H%M%S}"
            base.with_suffix(".html").write_text(sanitized_html(self.page), encoding="utf-8")
            self.page.screenshot(path=str(base.with_suffix(".png")), full_page=True, timeout=15000)
            return base.with_suffix(".png")
        except Exception:  # noqa: BLE001
            return None

    def _verify_tariff(self, response_type: str) -> None:
        """Страховка перед «Продолжить»: на кнопке написано, какой тариф выбран
        («Комиссия» или «817 ₽»). Не совпало с задуманным — ничего не нажимаем."""
        op = self.sel.get("order_page", {})
        if not op.get("continue_label"):
            return
        el = self._find(op["continue_label"])
        if not el:
            raise FormNotFound("не найдена кнопка «Продолжить» — остановлено, ничего не нажато")
        label = el.inner_text().lower()
        is_commission = "комисси" in label
        if (response_type == "commission") != is_commission:
            want = "«Комиссия»" if response_type == "commission" else "платный «Отклик»"
            shown = " ".join(label.split())
            raise FormNotFound(f"не удалось выбрать тариф {want} (на кнопке: «{shown}») — остановлено до «Продолжить»")

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
        send_after: float = 0,
        wait=None,
    ) -> bool:
        """Выбирает тариф, заполняет форму и (если не dry_run) отправляет.

        send_after — сколько секунд должно пройти от выбора тарифа до «Отправить»
        (ввод текста входит в это время, остаток добирается ожиданием);
        wait(seconds, text) — прерываемое ожидание движка (Пауза/Стоп).
        Возвращает True, если отклик отправлен."""
        op = self.sel.get("order_page", {})
        wait = wait or (lambda seconds, _text="": time.sleep(seconds))
        type_el = self._find(op.get(f"type_{response_type}"))
        if type_el:
            type_el.click()
        tariff_clicked = time.monotonic()
        wait(random.uniform(0.8, 2.5), "Выбран тариф")
        self._verify_tariff(response_type)

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
            self.page.wait_for_timeout(random.randint(600, 1500))
            if op.get("message_input"):
                try:
                    self.page.wait_for_selector(op["message_input"], state="visible")
                except Exception:
                    raise FormNotFound("после «Продолжить» не появилось поле текста отклика") from None

        price_el = self._find(op.get("price_input"))
        if op.get("price_input") and not price_el:
            raise FormNotFound("не найдено поле цены")
        if price_el:
            self._type_text(price_el, str(int(round(quote.value))))

        msg_el = self._find(op.get("message_input"))
        if not msg_el:
            raise FormNotFound("не найдено поле текста отклика")
        self._type_text(msg_el, message)

        left = send_after - (time.monotonic() - tariff_clicked)
        if left > 0:
            wait(left, "Пауза перед отправкой")
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


def _debug_note(path: Path | None) -> str:
    return f" (снимок страницы: {path})" if path else ""
