"""Снимок страниц Profi.ru из вашего Chrome для настройки селекторов.

    python -m profi_bot.dump

Подключается к Chrome по CDP (как и бот), сохраняет HTML и скриншоты ленты заказов,
страницы заказа и формы отклика (кнопку «Отправить» НЕ нажимает), проверяет текущие
селекторы из config/selectors.yaml и упаковывает всё в zip, который можно прислать.

Из HTML вырезаются скрипты, стили, скрытые поля, токены, телефоны и e-mail.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import config as config_mod
from .browser import _EXTRACT_JS
from .models import order_from_raw

ROOT_OUT = config_mod.ROOT_DIR / "dump"

# Чистка DOM-клона внутри страницы: без скриптов, стилей, скрытых полей и токенов.
_SANITIZE_JS = r"""
() => {
  const doc = document.documentElement.cloneNode(true);
  doc.querySelectorAll('script, noscript, style, iframe, svg, link[rel="preload"], link[rel="prefetch"]')
     .forEach(e => e.remove());
  doc.querySelectorAll('meta').forEach(m => {
    const n = (m.getAttribute('name') || m.getAttribute('property') || '').toLowerCase();
    if (/csrf|token|verification|session/.test(n)) m.remove();
  });
  doc.querySelectorAll('input').forEach(i => {
    if ((i.getAttribute('type') || '').toLowerCase() === 'hidden' || /token|csrf/i.test(i.name || ''))
      i.setAttribute('value', '');
  });
  doc.querySelectorAll('*').forEach(e => {
    for (const a of Array.from(e.attributes)) {
      if (a.name === 'style' || a.name.startsWith('on')) e.removeAttribute(a.name);
      else if (a.name === 'src' && a.value.startsWith('data:')) e.setAttribute('src', 'data:');
      else if (a.name === 'srcset') e.removeAttribute(a.name);
    }
  });
  return '<!doctype html>\n' + doc.outerHTML;
}
"""

_PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Только «Откликнуться»: широкий «Отклик» совпал бы и с «Отправить отклик».
_RESPOND_FALLBACK = "button:has-text('Откликнуться'), a:has-text('Откликнуться')"
_ORDER_LINK_FALLBACK = "a[href*='o.php'], a[href*='/order'], a[href*='order_id'], a[href*='orderId']"


def _scrub(html: str) -> str:
    html = _PHONE_RE.sub("+7 000 000-00-00", html)
    return _EMAIL_RE.sub("user@example.com", html)


class Dumper:
    def __init__(self, page, out: Path, report: list[str]):
        self.page = page
        self.out = out
        self.report = report

    def say(self, text: str) -> None:
        print(text)
        self.report.append(text)

    def save(self, name: str) -> None:
        try:
            html = _scrub(self.page.evaluate(_SANITIZE_JS))
        except Exception as exc:  # noqa: BLE001
            html = f"<!-- не удалось очистить DOM: {exc} -->"
        (self.out / f"{name}.html").write_text(html, encoding="utf-8")
        try:
            self.page.screenshot(path=str(self.out / f"{name}.png"), full_page=True, timeout=20000)
        except Exception:  # noqa: BLE001
            try:
                self.page.screenshot(path=str(self.out / f"{name}.png"), timeout=20000)
            except Exception as exc:  # noqa: BLE001
                self.say(f"  ! скриншот {name} не снят: {exc}")
        self.say(f"  сохранено: {name}.html / {name}.png  ({self.page.url})")

    def check_selectors(self, title: str, selectors: dict, keys: list[str]) -> None:
        """Сколько элементов находит каждый селектор (всего / видимых)."""
        self.say(f"  Проверка селекторов: {title}")
        for key in keys:
            sel = selectors.get(key)
            if not sel:
                self.say(f"    {key:26} — (пусто)")
                continue
            try:
                loc = self.page.locator(sel)
                total = loc.count()
                visible = sum(1 for i in range(min(total, 50)) if loc.nth(i).is_visible())
                self.say(f"    {key:26} найдено {total:3}, видимых {visible:3}   [{sel}]")
            except Exception as exc:  # noqa: BLE001
                self.say(f"    {key:26} ОШИБКА селектора: {str(exc).splitlines()[0][:120]}   [{sel}]")


def _feed_urls(selectors: dict, override: str | None) -> list[str]:
    if override:
        return [override]
    urls = selectors.get("orders_urls") or selectors.get("orders_url") or []
    return [urls] if isinstance(urls, str) else list(urls)


def _ask(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{question} [y/N]: ").strip().lower() in ("y", "yes", "д", "да")
    except EOFError:
        return False


def run(args) -> Path:
    from playwright.sync_api import sync_playwright

    cfg = config_mod.load_settings(args.config)
    selectors = config_mod.load_selectors(args.selectors)
    cdp_url = args.cdp or cfg["browser"]["cdp_url"]
    out = Path(args.out) if args.out else ROOT_OUT / datetime.now().strftime("%Y%m%d_%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    report: list[str] = [f"Снимок от {datetime.now():%Y-%m-%d %H:%M:%S}", f"CDP: {cdp_url}"]

    pw = sync_playwright().start()
    try:
        try:
            browser = pw.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:  # noqa: BLE001
            print(f"Не удалось подключиться к Chrome по {cdp_url}.\n"
                  f"Запустите Chrome с --remote-debugging-port=9222 и проверьте {cdp_url}/json/version\n({exc})")
            sys.exit(1)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        page.set_default_timeout(10000)
        page.set_default_navigation_timeout(45000)
        d = Dumper(page, out, report)

        # 0. Какие вкладки Profi.ru уже открыты у пользователя — просто список адресов.
        tabs = [p.url for p in context.pages if "profi" in p.url and p is not page]
        if tabs:
            d.say("Открытые вкладки Profi.ru:")
            for u in tabs:
                d.say(f"  {u}")

        # 1. Лента заказов.
        order_url = args.order
        lst = selectors.get("list", {})
        for i, url in enumerate(_feed_urls(selectors, args.feed), 1):
            d.say(f"\n[Лента {i}] {url}")
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            if "login" in page.url or "auth" in page.url:
                d.say("  ! Похоже, открылась страница входа — войдите в Profi.ru в этом Chrome и повторите.")
            for _ in range(int(lst.get("scroll_times", 0) or 0)):
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(800)
            d.save(f"feed_{i}")
            d.check_selectors("карточки ленты", lst, ["wait_for", "card", "link"])
            try:
                raws = page.evaluate(_EXTRACT_JS, lst)
            except Exception:  # noqa: BLE001
                raws = []
            parsed = [order_from_raw(r).to_row() for r in raws if r.get("url")]
            (out / f"feed_{i}_parsed.json").write_text(json.dumps(parsed, ensure_ascii=False, indent=2),
                                                       encoding="utf-8")
            d.say(f"  бот распознал заказов: {len(parsed)} (см. feed_{i}_parsed.json)")
            if not order_url:
                if parsed:
                    order_url = parsed[0]["url"]
                else:
                    links = page.locator(_ORDER_LINK_FALLBACK)
                    if links.count():
                        order_url = links.first.evaluate("a => a.href")
        # Адреса всех ссылок на странице ленты — помогает понять формат ссылок на заказы.
        try:
            hrefs = page.evaluate("() => Array.from(new Set(Array.from(document.links).map(a => a.href)))")
            (out / "feed_links.txt").write_text("\n".join(hrefs), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

        # 2. Страница заказа и форма отклика.
        if not order_url:
            d.say("\n! Не нашёл ссылку на заказ. Откройте любой заказ в Chrome и запустите снова с "
                  "--order <адрес заказа>")
        else:
            op = selectors.get("order_page", {})
            d.say(f"\n[Заказ] {order_url}")
            page.goto(order_url, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            d.save("order")
            d.check_selectors("страница заказа", op, ["respond_button", "already_responded", "client_name"])

            respond = page.locator(op.get("respond_button") or _RESPOND_FALLBACK)
            visible = [respond.nth(i) for i in range(respond.count()) if respond.nth(i).is_visible()]
            if not visible and op.get("respond_button"):
                respond = page.locator(_RESPOND_FALLBACK)
                visible = [respond.nth(i) for i in range(respond.count()) if respond.nth(i).is_visible()]
            if args.no_click:
                d.say("  Форму не открываю (--no-click).")
            elif not visible:
                d.say("  ! Кнопка «Откликнуться» не найдена — форма не снята.")
            elif _ask("\nНажать «Откликнуться», чтобы снять форму? Отправлять отклик скрипт НЕ будет.", args.yes):
                visible[0].click()
                page.wait_for_timeout(3000)
                d.save("form")
                d.check_selectors("форма отклика", op, [
                    "form", "type_paid", "type_commission", "commission_percent_input", "paid_cost",
                    "message_input", "price_input", "price_max_input", "price_from_checkbox",
                    "submit_button", "success",
                ])
                # Больше ничего не нажимаем: переключатели типа могут оказаться кнопкой оплаты.
                d.say("  Отклик НЕ отправлен. Вкладку закрываю без сохранения формы.")
            else:
                d.say("  Форму пропускаю.")
        page.close()
    finally:
        pw.stop()  # только отключение: ваш Chrome остаётся открытым

    (out / "report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    archive = shutil.make_archive(str(out), "zip", root_dir=out)
    print(f"\nГотово. Папка: {out}\nАрхив для отправки: {archive}\n"
          "Перед отправкой можно открыть скриншоты (*.png) и убедиться, что на них нет лишнего.")
    return Path(archive)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="profi_bot.dump", description="Снимок страниц Profi.ru для настройки")
    parser.add_argument("--cdp", help="адрес Chrome DevTools (по умолчанию из settings.yaml)")
    parser.add_argument("--feed", help="адрес ленты заказов (по умолчанию из selectors.yaml)")
    parser.add_argument("--order", help="адрес конкретного заказа (по умолчанию — первый из ленты)")
    parser.add_argument("--no-click", action="store_true", help="не нажимать «Откликнуться»")
    parser.add_argument("--yes", "-y", action="store_true", help="не спрашивать подтверждение")
    parser.add_argument("--out", help="папка для результата")
    parser.add_argument("--config", type=Path, default=config_mod.SETTINGS_PATH)
    parser.add_argument("--selectors", type=Path, default=config_mod.SELECTORS_PATH)
    run(parser.parse_args(argv))


if __name__ == "__main__":
    main()
