"""Сквозной тест: фейковый «Profi.ru» на localhost + настоящий Chrome, к которому
движок подключается по CDP (как к браузеру пользователя) с селекторами из config/selectors.yaml."""
from __future__ import annotations

import copy
import json
import os
import re
import queue
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from profi_bot import config as config_mod
from profi_bot.engine import STOPPED, Engine
from profi_bot.storage import Storage

ORDERS = {
    "10000001": dict(title="Ремонт ванной комнаты", desc="Положить плитку 6 м2", budget="до 30 000 ₽",
                     geo="Москва, м. Сокол", client="Анна", types=["paid", "commission"]),
    "10000002": dict(title="Сборка шкафа", desc="Нужна сборка мебели, тел. +7 916 123-45-67",
                     budget="3 000 ₽", geo="Москва", client="Олег", types=["paid"]),
    "10000003": dict(title="Покраска стен", desc="Срочно, дёшево, ставка 700 ₽/час", budget="",
                     geo="Москва", client="ООО Ромашка", types=["paid"]),
    "10000004": dict(title="Ремонт кухни", desc="Плитка на фартук", budget="до 15 000 ₽",
                     geo="Москва", client="Ирина", types=["paid"], already=True),
    "10000005": dict(title="Ремонт балкона", desc="Плитка на пол", budget="10 000 – 20 000 ₽",
                     geo="Дистанционно · Москва", client="Пётр", types=["commission"]),
    "10000006": dict(title="Ремонт под ключ", desc="Квартира 60 м2", budget="90 000 ₽",
                     geo="Москва", client="Глеб", types=["paid"]),
}


def _snippet(oid: str, o: dict) -> str:
    """Карточка в разметке, как в ленте Profi.ru (снимок 26.09.2026)."""
    price = (f'<span>{o["budget"]} false</span><span aria-hidden="true"><span>{o["budget"]}</span></span>'
             if o["budget"] else '<span>false</span><span aria-hidden="true"><span></span></span>')
    return f"""<a data-testid="{oid}_order-snippet" id="{oid}" href="/backoffice/n.php?o={oid}&analytics_data=x"
                 aria-label="{o['title']}">
        <div><button type="button" aria-label="Скрыть заказ" data-testid="{oid}_refuse"></button></div>
        <div><div><h3>{o['title']}</h3></div><div>{price}</div></div>
        <div><div><p>{o['desc']}</p>
          <ul role="list"><li aria-label="Дистанционно:" role="listitem"><span>{o['geo']}</span></li></ul></div></div>
        <div><div><div><span>{o['client']}</span></div></div><div><span>Только что</span></div></div>
      </a>"""


def _feed_html() -> str:
    cards = "".join(f"<div><div>{_snippet(oid, o)}</div></div>" for oid, o in ORDERS.items())
    return (f"<html><head><meta name='csrf-token' content='SECRET123'></head><body>"
            f"<div id='content-content'>{cards}</div>"
            "<script>window.__STATE__ = {token: 'SECRET123'}</script></body></html>")


def _tariff(kind: str, label: str, amount: str) -> str:
    return f"""<div bordercolor="x" class="opt" data-kind="{kind}" onclick="pick('{kind}')">
        <div><span>{label}</span><span> • </span><span color="x">{amount}</span></div>
        <p>Вы платите … Откликнуться можно бесплатно.</p></div>"""


def _order_html(oid: str) -> str:
    """Страница заказа как на Profi.ru: блок тарифов → «Продолжить» → панель отклика справа."""
    o = ORDERS[oid]
    cross_sell = _snippet("99999999", dict(title="Похожий заказ", desc="Не стесняйтесь откликнуться!",
                                            budget="500 ₽", geo="Москва", client="Кто-то"))
    if o.get("already"):
        # У заказа с отправленным откликом блока тарифов нет.
        return f"<html><body><h1>{o['title']}</h1><div>Перейти в чат</div>{cross_sell}</body></html>"
    tariffs = ""
    if "paid" in o["types"]:
        tariffs += _tariff("paid", "Отклик", "150 ₽")
    if "commission" in o["types"]:
        tariffs += _tariff("commission", "Комиссия", "2066 ₽")
    return f"""<html><body>
      <h1>{o['title']}</h1>
      <div data-testid="orderCard/tariffs"><p>Выберите тариф</p><a>Детали</a>
        <div>{tariffs}</div>
        <div onclick="cont()"><div>Продолжить</div><div id="chosen"></div></div>
      </div>
      <h3>Похожие заказы</h3>{cross_sell}
      <div class="order-card-bid-window-container"><div data-testid="bid_window_container" id="panel" style="display:none">
        <p>Предложение или вопрос клиенту</p>
        <textarea placeholder="Уточните детали задачи или предложите свои условия"></textarea>
        <p>Стоимость занятия</p>
        <div data-testid="bid_form_price"><label><span><input type="text" value=""><span>Цена</span></span></label></div>
        <div data-testid="bid_form_footer"><span color="#181818">2 066 ₽</span>
          <button data-testid="payment_methods_form_pay_button" onclick="send()">Отправить сообщение</button></div>
      </div></div>
      <textarea tabindex="-1" aria-hidden="true"></textarea>
      <script>
        let kind = '';
        function pick(k) {{ kind = k; document.getElementById('chosen').innerText = k; }}
        async function cont() {{
          await fetch('/continue?o={oid}&kind=' + kind, {{method: 'POST', body: '{{}}'}});
          document.getElementById('panel').style.display = 'block';
        }}
        async function send() {{
          const panel = document.getElementById('panel');
          const body = {{kind, message: panel.querySelector('textarea').value,
                         price: panel.querySelector('input').value}};
          await fetch('/submit?o={oid}', {{method: 'POST', body: JSON.stringify(body)}});
          panel.style.display = 'none';
        }}
      </script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    submissions: list = []
    continues: list = []
    opened: dict = {}  # id заказа → время открытия страницы

    def log_message(self, *args):
        pass

    def _send(self, body: str, code: int = 200):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/backoffice/n.php":
            query = parse_qs(url.query)
            if "o" in query:
                self.opened[query["o"][0]] = time.time()
                return self._send(_order_html(query["o"][0]))
            return self._send(_feed_html())
        self._send("not found", 404)

    def do_POST(self):
        url = urlparse(self.path)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        oid = parse_qs(url.query)["o"][0]
        if url.path == "/continue":
            self.continues.append((oid, parse_qs(url.query).get("kind", [""])[0]))
        else:
            self.submissions.append({"order": oid, "at": time.time(), **json.loads(body)})
        self._send("ok")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _chrome_path() -> str | None:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            path = p.chromium.executable_path
    except Exception:
        return None
    return path if path and os.path.exists(path) else None


@pytest.fixture(scope="module")
def site():
    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture(scope="module")
def chrome():
    path = _chrome_path()
    if not path:
        pytest.skip("Chromium для Playwright не найден")
    port = _free_port()
    profile = tempfile.mkdtemp(prefix="profi-test-chrome-")
    proc = subprocess.Popen(
        [path, "--headless=new", "--no-sandbox", f"--remote-debugging-port={port}",
         f"--user-data-dir={profile}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/json/version", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.skip("Chrome не поднял CDP")
    yield url
    proc.terminate()
    proc.wait(10)


def _cfg(cdp_url: str, **overrides) -> dict:
    cfg = copy.deepcopy(config_mod.DEFAULTS)
    cfg["browser"].update(cdp_url=cdp_url, poll_interval_sec=600, dry_run=False)
    cfg["timing"]["delay_before"] = {"min": 0, "max": 0, "unit": "sec"}
    cfg["timing"]["interval_between"] = {"min": 0, "max": 0, "unit": "sec"}
    cfg["timing"]["tariff_to_send"] = {"min": 0, "max": 0, "unit": "sec"}
    cfg["timing"]["work_hours"]["enabled"] = False
    cfg["filters"].update(categories=["ремонт", "сборка", "покраска"], keywords_exclude=["дёшево"],
                          remote_mode="any")
    cfg["templates"].update(my_name="Никита", deadline="3 дня", items=[
        {"name": "t1", "enabled": True, "text": "Здравствуйте, {name}! {title} за {price} ₽, {deadline}. {my_name}"},
        {"name": "t2", "enabled": True, "text": "Добрый день! Цена {price} ₽"},
    ])
    cfg["pricing"].update(rules=[
        {"budget_min": 0, "budget_max": 5000, "price": 2700},
        {"budget_min": 5000, "budget_max": 25000, "price": 18000},
        {"budget_min": 25000, "budget_max": 40000, "price": 27000},
    ], take_no_budget=True, no_budget_price=1500)
    cfg["response"].update(priority=["commission", "paid"])
    cfg["limits"].update(per_day=10, per_hour=10, paid_per_day=5, budget_per_day=1000)
    for key, value in overrides.items():
        cfg[key].update(value)
    return cfg


def _selectors(site: str) -> dict:
    """Селекторы берутся как есть из config/selectors.yaml — меняется только адрес ленты."""
    sel = config_mod.load_selectors()
    sel["orders_urls"] = [f"{site}/backoffice/n.php"]
    sel["behavior"]["typing_delay_ms"] = 0
    return sel


def _run_until(engine: Engine, predicate, timeout: float = 60) -> None:
    engine.start()
    end = time.time() + timeout
    while time.time() < end and not predicate():
        time.sleep(0.2)
    engine.stop()
    engine.join(20)
    assert engine.state == STOPPED


def test_engine_end_to_end(site, chrome, tmp_path):
    _Handler.submissions.clear()
    _Handler.continues.clear()
    storage = Storage(tmp_path / "db.sqlite")
    events: queue.Queue = queue.Queue()
    engine = Engine(_cfg(chrome), _selectors(site), storage, events)
    _run_until(engine, lambda: len(storage.recent_responses()) >= 3
               and storage.is_seen("10000004") and storage.is_seen("10000003") and storage.is_seen("10000006"))

    subs = {s["order"]: s for s in _Handler.submissions}
    assert set(subs) == {"10000001", "10000002", "10000005"}

    # Заказ 1: доступны оба типа, приоритет — комиссия; цена = 30 000 * 0.9.
    s1 = subs["10000001"]
    assert s1["kind"] == "commission" and s1["price"] == "27000"
    assert s1["message"] == "Здравствуйте, Анна! Ремонт ванной комнаты за 27 000 ₽, 3 дня. Никита"
    # Заказ 2: только платный; второй шаблон по ротации.
    s2 = subs["10000002"]
    assert s2["kind"] == "paid" and s2["price"] == "2700" and s2["message"] == "Добрый день! Цена 2 700 ₽"
    # Заказ 5: диапазон бюджета 10–20 тыс., гео «дистанционно».
    assert subs["10000005"]["kind"] == "commission" and subs["10000005"]["price"] == "18000"

    rows = {r["order_id"]: r for r in storage.recent_responses()}
    assert rows["10000002"]["cost"] == 150  # стоимость отклика прочитана со страницы
    assert rows["10000001"]["cost"] == 2066  # сумма комиссии — для информации
    assert all(r["status"] == "sent" for r in rows.values())
    stats = storage.stats()
    assert stats["sent_today"] == 3 and stats["paid_today"] == 1 and stats["spent_today"] == 150

    orders = {r["id"]: r for r in (dict(x) for x in storage._query("SELECT * FROM orders"))}
    assert orders["10000003"]["status"] == "skipped" and "организация" in orders["10000003"]["reason"]
    assert orders["10000006"]["status"] == "skipped" and "не попадает" in orders["10000006"]["reason"]
    assert orders["10000004"]["status"] == "already" and "тариф" in orders["10000004"]["reason"]

    kinds = set()
    while not events.empty():
        kinds.add(events.get()["kind"])
    assert {"log", "state", "status", "response"} <= kinds

    # Второй запуск: всё уже просмотрено — новых откликов нет.
    before = len(_Handler.submissions)
    engine2 = Engine(_cfg(chrome), _selectors(site), storage)
    _run_until(engine2, lambda: False, timeout=4)
    assert len(_Handler.submissions) == before
    storage.close()


def test_dry_run_and_paid_limit(site, chrome, tmp_path):
    _Handler.submissions.clear()
    storage = Storage(tmp_path / "db.sqlite")
    cfg = _cfg(chrome, browser={"dry_run": True})
    engine = Engine(cfg, _selectors(site), storage)
    _Handler.continues.clear()
    _run_until(engine, lambda: storage.is_seen("10000005"))
    assert _Handler.submissions == []
    # В тестовом режиме «Продолжить» нажимается только для комиссии (открывает панель),
    # а «Отправить сообщение» — никогда.
    assert sorted(_Handler.continues) == [("10000001", "commission"), ("10000005", "commission")]
    assert {r["status"] for r in storage.recent_responses()} == {"dry_run"}

    # Боевой режим: заказы из dry run снова доступны; платные запрещены лимитом бюджета
    # (150 ₽ > 100 ₽), поэтому заказ 2 (только платный) пропускается.
    cfg = _cfg(chrome, limits={"budget_per_day": 100})
    engine = Engine(cfg, _selectors(site), storage)
    _run_until(engine, lambda: len(_Handler.submissions) >= 2)
    assert {s["order"] for s in _Handler.submissions} == {"10000001", "10000005"}
    row = dict(storage._query("SELECT * FROM orders WHERE id='10000002'")[0])
    assert row["status"] == "skipped" and "лимит платных" in row["reason"]
    storage.close()


def test_autostop_on_limit(site, chrome, tmp_path):
    _Handler.submissions.clear()
    storage = Storage(tmp_path / "db.sqlite")
    engine = Engine(_cfg(chrome, limits={"per_day": 1, "on_limit": "stop"}), _selectors(site), storage)
    engine.start()
    engine.join(60)
    assert engine.state == STOPPED
    assert len(_Handler.submissions) == 1
    assert any("автостоп" in e["message"] for e in storage.recent_events())
    storage.close()


def test_dump(site, chrome, tmp_path):
    from profi_bot import dump

    _Handler.submissions.clear()
    _Handler.continues.clear()
    sel_path = tmp_path / "selectors.yaml"
    import yaml

    yaml.safe_dump(_selectors(site), sel_path.open("w", encoding="utf-8"), allow_unicode=True)
    out = tmp_path / "dump"
    dump.main(["--cdp", chrome, "--selectors", str(sel_path), "--out", str(out)])
    names = {p.name for p in out.iterdir()}
    assert {"feed_1.html", "feed_1.png", "feed_1_parsed.json", "feed_1_links.txt", "order.html",
            "order.png", "report.txt"} <= names
    assert (tmp_path / "dump.zip").exists()
    assert _Handler.submissions == [] and _Handler.continues == []  # ничего не нажато
    feed = (out / "feed_1.html").read_text(encoding="utf-8")
    assert "SECRET123" not in feed and "916 123-45-67" not in feed and "_order-snippet" in feed
    parsed = json.loads((out / "feed_1_parsed.json").read_text(encoding="utf-8"))
    assert [o["client_name"] for o in parsed] == ["Анна", "Олег", "ООО Ромашка", "Ирина", "Пётр", "Глеб"]
    assert parsed[2]["budget_max"] is None  # «700 ₽/час» в описании — не бюджет
    report = (out / "report.txt").read_text(encoding="utf-8")
    assert "бот распознал заказов: 6" in report
    assert re.search(r"type_paid\s+найдено\s+1", report) and re.search(r"continue_button\s+найдено\s+1", report)


def test_dump_current(site, chrome, tmp_path):
    """--current снимает вкладку пользователя как есть, без переходов и кликов."""
    from playwright.sync_api import sync_playwright

    from profi_bot import dump

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(chrome)
        page = browser.contexts[0].new_page()
        page.goto(f"{site}/backoffice/n.php?o=10000001")
        page.locator("text=Продолжить").click()  # пользователь сам дошёл до формы
        page.wait_for_selector("textarea", state="visible")
        # Подменяем адрес, чтобы вкладка считалась страницей Profi.ru.
        page.evaluate("history.replaceState(null, '', '/profi/backoffice/n.php?o=10000001')")
        out = tmp_path / "cur"
        # Снимщик — отдельный процесс в реальности; здесь отдельный поток (свой Playwright).
        t = threading.Thread(target=dump.main, args=(["--cdp", chrome, "--current", "--out", str(out)],))
        t.start()
        t.join(60)
        url_after = page.url
        page.close()
    assert url_after.endswith("/profi/backoffice/n.php?o=10000001")
    assert (out / "current_1.html").exists() and (out / "current_1.png").exists()
    controls = (out / "current_1_controls.txt").read_text(encoding="utf-8")
    assert "textarea" in controls and "Отправить" in controls


def test_response_timing(site, chrome, tmp_path):
    """Открыл заказ → (delay_before) → тариф → (tariff_to_send) → «Отправить сообщение»."""
    _Handler.submissions.clear()
    storage = Storage(tmp_path / "db.sqlite")
    cfg = _cfg(chrome, filters={"categories": ["балкон"]})
    cfg["timing"]["delay_before"] = {"min": 1.5, "max": 1.5, "unit": "sec"}
    cfg["timing"]["tariff_to_send"] = {"min": 4, "max": 4, "unit": "sec"}
    engine = Engine(cfg, _selectors(site), storage)
    _run_until(engine, lambda: len(_Handler.submissions) >= 1)
    sub = _Handler.submissions[0]
    elapsed = sub["at"] - _Handler.opened[sub["order"]]
    assert 5.5 <= elapsed <= 12, elapsed
    storage.close()
