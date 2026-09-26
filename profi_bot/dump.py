"""Снимок страниц Profi.ru из вашего Chrome для настройки селекторов.

    python -m profi_bot.dump

    python -m profi_bot.dump --current   # снять открытые вкладки как есть

Подключается к Chrome по CDP (как и бот), сохраняет HTML и скриншоты ленты заказов и
страницы заказа, проверяет селекторы из config/selectors.yaml и упаковывает всё в zip.
На страницах скрипт ничего не нажимает. Чтобы снять следующий шаг отклика, дойдите до
него вручную и запустите с --current.

Из HTML вырезаются скрипты, стили, скрытые поля, токены, телефоны и e-mail.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import config as config_mod
from .browser import _EXTRACT_JS, enrich_raw
from .models import order_from_raw
from .sanitize import sanitized_html

ROOT_OUT = config_mod.ROOT_DIR / "dump"




class Dumper:
    def __init__(self, page, out: Path, report: list[str]):
        self.page = page
        self.out = out
        self.report = report

    def say(self, text: str) -> None:
        print(text)
        self.report.append(text)

    def save(self, name: str) -> None:
        html = sanitized_html(self.page)
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


ORDER_KEYS = [
    "respond_button", "already_responded", "client_name", "type_paid", "type_commission",
    "paid_cost", "commission_cost", "continue_button", "form",
    "message_input", "price_input", "submit_button", "success",
]


def _connect(pw, cdp_url: str):
    try:
        return pw.chromium.connect_over_cdp(cdp_url)
    except Exception as exc:  # noqa: BLE001
        print(f"Не удалось подключиться к Chrome по {cdp_url}.\n"
              f"Запустите Chrome с --remote-debugging-port=9222 и проверьте {cdp_url}/json/version\n({exc})")
        sys.exit(1)


def _dump_current(context, selectors: dict, out: Path, report: list[str]) -> None:
    """Снимает открытые вкладки Profi.ru как есть: без переходов и без кликов."""
    pages = [p for p in context.pages if "profi" in p.url]
    if not pages:
        print("Нет открытых вкладок Profi.ru. Откройте нужную страницу в Chrome и запустите снова.")
        report.append("Нет открытых вкладок Profi.ru")
        return
    for i, page in enumerate(pages, 1):
        d = Dumper(page, out, report)
        d.say(f"\n[Вкладка {i}] {page.url}")
        d.save(f"current_{i}")
        d.check_selectors("элементы отклика", selectors.get("order_page", {}), ORDER_KEYS)
        try:
            fields = page.evaluate("""() => Array.from(document.querySelectorAll(
                'input, textarea, select, button, [role=button], [contenteditable=true]'))
                .filter(e => e.offsetParent !== null)
                .map(e => [e.tagName.toLowerCase(), e.getAttribute('type') || '', e.getAttribute('name') || '',
                           e.getAttribute('placeholder') || '', e.getAttribute('data-testid') || '',
                           (e.innerText || e.value || '').trim().slice(0, 60)].join(' | '))""")
            (out / f"current_{i}_controls.txt").write_text(
                "tag | type | name | placeholder | data-testid | text\n" + "\n".join(fields), encoding="utf-8")
            d.say(f"  видимых полей и кнопок: {len(fields)} (см. current_{i}_controls.txt)")
        except Exception:  # noqa: BLE001
            pass


def _dump_pages(context, selectors: dict, args, out: Path, report: list[str]) -> None:
    """Открывает в новой вкладке ленту и один заказ. Ничего на страницах не нажимает."""
    page = context.new_page()
    page.set_default_timeout(10000)
    page.set_default_navigation_timeout(45000)
    d = Dumper(page, out, report)
    tabs = [p.url for p in context.pages if "profi" in p.url and p is not page]
    if tabs:
        d.say("Открытые вкладки Profi.ru:")
        for u in tabs:
            d.say(f"  {u}")

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
        parsed = [order_from_raw(enrich_raw(r, lst)).to_row() for r in raws if r.get("url")]
        (out / f"feed_{i}_parsed.json").write_text(json.dumps(parsed, ensure_ascii=False, indent=2),
                                                   encoding="utf-8")
        d.say(f"  бот распознал заказов: {len(parsed)} (см. feed_{i}_parsed.json)")
        for o in parsed[:5]:
            budget = o["budget_max"] or o["budget_min"]
            d.say(f"    · {o['title'][:40]} | бюджет {budget if budget is not None else '—'} | "
                  f"{o['geo'][:30]} | клиент {o['client_name'] or '—'}")
        if not order_url:
            if parsed:
                order_url = parsed[0]["url"]
            else:
                links = page.locator(_ORDER_LINK_FALLBACK)
                if links.count():
                    order_url = links.first.evaluate("a => a.href")
        try:
            hrefs = page.evaluate("() => Array.from(new Set(Array.from(document.links).map(a => a.href)))")
            (out / f"feed_{i}_links.txt").write_text("\n".join(hrefs), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    if not order_url:
        d.say("\n! Не нашёл ссылку на заказ. Откройте любой заказ в Chrome и запустите снова с "
              "--order <адрес заказа>")
    else:
        d.say(f"\n[Заказ] {order_url}")
        page.goto(order_url, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        d.save("order")
        d.check_selectors("страница заказа", selectors.get("order_page", {}), ORDER_KEYS)
    page.close()


def run(args) -> Path:
    from playwright.sync_api import sync_playwright

    cfg = config_mod.load_settings(args.config)
    selectors = config_mod.load_selectors(args.selectors)
    cdp_url = args.cdp or cfg["browser"]["cdp_url"]
    out = Path(args.out) if args.out else ROOT_OUT / datetime.now().strftime("%Y%m%d_%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    report: list[str] = [f"Снимок от {datetime.now():%Y-%m-%d %H:%M:%S}", f"CDP: {cdp_url}",
                         "Режим: " + ("открытые вкладки как есть (--current)" if args.current else "лента + заказ")]

    pw = sync_playwright().start()
    try:
        browser = _connect(pw, cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        if args.current:
            _dump_current(context, selectors, out, report)
        else:
            _dump_pages(context, selectors, args, out, report)
    finally:
        pw.stop()  # только отключение: ваш Chrome остаётся открытым

    (out / "report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    archive = shutil.make_archive(str(out), "zip", root_dir=out)
    print(f"\nГотово. Папка: {out}\nАрхив для отправки: {archive}\n"
          "Перед отправкой можно открыть скриншоты (*.png) и убедиться, что на них нет лишнего.")
    return Path(archive)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="profi_bot.dump", description="Снимок страниц Profi.ru для настройки")
    parser.add_argument("--current", action="store_true",
                        help="снять открытые вкладки Profi.ru как есть (без переходов и кликов)")
    parser.add_argument("--cdp", help="адрес Chrome DevTools (по умолчанию из settings.yaml)")
    parser.add_argument("--feed", help="адрес ленты заказов (по умолчанию из selectors.yaml)")
    parser.add_argument("--order", help="адрес конкретного заказа (по умолчанию — первый из ленты)")
    parser.add_argument("--out", help="папка для результата")
    parser.add_argument("--config", type=Path, default=config_mod.SETTINGS_PATH)
    parser.add_argument("--selectors", type=Path, default=config_mod.SELECTORS_PATH)
    run(parser.parse_args(argv))


if __name__ == "__main__":
    main()
