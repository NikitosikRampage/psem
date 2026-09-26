"""Сквозной тест: фейковый «Profi.ru» на localhost + настоящий Chrome, к которому
движок подключается по CDP (как к браузеру пользователя) с селекторами из config/selectors.yaml."""
from __future__ import annotations

import copy
import json
import os
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
                     geo="Москва, м. Сокол", cat="Ремонт", client="Анна", ctype="Частное лицо",
                     types=["paid", "commission"]),
    "10000002": dict(title="Сборка шкафа", desc="Нужна сборка мебели, тел. +7 916 123-45-67", budget="3 000 ₽",
                     geo="Москва", cat="Сборка мебели", client="Олег", ctype="Частное лицо", types=["paid"]),
    "10000003": dict(title="Покраска стен", desc="Срочно, дёшево", budget="1 000 ₽",
                     geo="Москва", cat="Ремонт", client="ООО Ромашка", ctype="Компания", types=["paid"]),
    "10000004": dict(title="Ремонт кухни", desc="Плитка на фартук", budget="от 15 000 ₽",
                     geo="Москва", cat="Ремонт", client="Ирина", ctype="Частное лицо", types=["paid"],
                     already=True),
    "10000005": dict(title="Ремонт балкона", desc="Плитка на пол", budget="10 000 – 20 000 ₽",
                     geo="Дистанционно", cat="Ремонт", client="Пётр", ctype="Частное лицо",
                     types=["commission"]),
}


def _feed_html() -> str:
    cards = "".join(
        f"""<div data-testid="order-snippet">
              <a href="/backoffice/o.php?o={oid}"><h3>{o['title']}</h3></a>
              <p>{o['desc']}</p>
              <span data-testid="order-price">{o['budget']}</span>
              <span data-testid="order-geo">{o['geo']}</span>
              <span data-testid="order-category">{o['cat']}</span>
              <span data-testid="client-name">{o['client']}</span>
              <span data-testid="client-type">{o['ctype']}</span>
            </div>"""
        for oid, o in ORDERS.items()
    )
    return (f"<html><head><meta name='csrf-token' content='SECRET123'></head><body><h1>Заказы</h1>{cards}"
            "<script>window.__STATE__ = {token: 'SECRET123'}</script></body></html>")


def _order_html(oid: str) -> str:
    o = ORDERS[oid]
    if o.get("already"):
        return f"<html><body><h1>{o['title']}</h1><div>Вы откликнулись на этот заказ</div></body></html>"
    types = ""
    if "paid" in o["types"]:
        types += '<label><input type="radio" name="kind" value="paid"> Платный отклик</label>'
    if "commission" in o["types"]:
        types += '<label><input type="radio" name="kind" value="commission"> За комиссию</label>'
    return f"""<html><body>
      <h1>{o['title']}</h1><div data-testid="client-name">{o['client']}</div>
      <button id="open" onclick="document.getElementById('f').style.display='block'">Откликнуться</button>
      <form id="f" style="display:none" onsubmit="return send(event)">
        {types}
        <div data-testid="response-price">Отклик стоит 150 ₽</div>
        <input name="commission" value="">
        <textarea name="message"></textarea>
        <input name="price"> <input name="price_max">
        <button type="submit">Отправить</button>
      </form>
      <div id="ok" style="display:none">Отклик отправлен</div>
      <script>
        async function send(e) {{
          e.preventDefault();
          const f = new FormData(document.getElementById('f'));
          await fetch('/submit?o={oid}', {{method: 'POST', body: JSON.stringify(Object.fromEntries(f))}});
          document.getElementById('f').style.display = 'none';
          document.getElementById('ok').style.display = 'block';
          return false;
        }}
      </script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    submissions: list = []

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
            return self._send(_feed_html())
        if url.path == "/backoffice/o.php":
            return self._send(_order_html(parse_qs(url.query)["o"][0]))
        self._send("not found", 404)

    def do_POST(self):
        url = urlparse(self.path)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.submissions.append({"order": parse_qs(url.query)["o"][0], **json.loads(body)})
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
    cfg["timing"]["work_hours"]["enabled"] = False
    cfg["filters"].update(categories=["ремонт", "мебел"], keywords_exclude=["дёшево"])
    cfg["templates"].update(my_name="Никита", deadline="3 дня", items=[
        {"name": "t1", "enabled": True, "text": "Здравствуйте, {name}! {title} за {price} ₽, {deadline}. {my_name}"},
        {"name": "t2", "enabled": True, "text": "Добрый день! Цена {price} ₽"},
    ])
    cfg["pricing"].update(mode="formula", formula="budget * 0.9", round_to=100)
    cfg["response"].update(priority=["commission", "paid"], commission_percent=12)
    cfg["limits"].update(per_day=10, per_hour=10, paid_per_day=5, budget_per_day=1000)
    for key, value in overrides.items():
        cfg[key].update(value)
    return cfg


def _selectors(site: str) -> dict:
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
    storage = Storage(tmp_path / "db.sqlite")
    events: queue.Queue = queue.Queue()
    engine = Engine(_cfg(chrome), _selectors(site), storage, events)
    _run_until(engine, lambda: len(storage.recent_responses()) >= 3
               and storage.is_seen("10000004") and storage.is_seen("10000003"))

    subs = {s["order"]: s for s in _Handler.submissions}
    assert set(subs) == {"10000001", "10000002", "10000005"}

    # Заказ 1: доступны оба типа, приоритет — комиссия; цена = 30 000 * 0.9.
    s1 = subs["10000001"]
    assert s1["kind"] == "commission" and s1["commission"] == "12" and s1["price"] == "27000"
    assert s1["message"] == "Здравствуйте, Анна! Ремонт ванной комнаты за 27 000 ₽, 3 дня. Никита"
    # Заказ 2: только платный; второй шаблон по ротации.
    s2 = subs["10000002"]
    assert s2["kind"] == "paid" and s2["price"] == "2700" and s2["message"] == "Добрый день! Цена 2 700 ₽"
    # Заказ 5: диапазон бюджета 10–20 тыс., гео «дистанционно».
    assert subs["10000005"]["kind"] == "commission" and subs["10000005"]["price"] == "18000"

    rows = {r["order_id"]: r for r in storage.recent_responses()}
    assert rows["10000002"]["cost"] == 150  # стоимость прочитана со страницы
    assert all(r["status"] == "sent" for r in rows.values())
    stats = storage.stats()
    assert stats["sent_today"] == 3 and stats["paid_today"] == 1 and stats["spent_today"] == 150

    orders = {r["id"]: r for r in (dict(x) for x in storage._query("SELECT * FROM orders"))}
    assert orders["10000003"]["status"] == "skipped" and "дёшево" in orders["10000003"]["reason"]
    assert orders["10000004"]["status"] == "already"

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
    _run_until(engine, lambda: storage.is_seen("10000005"))
    assert _Handler.submissions == []
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
    sel_path = tmp_path / "selectors.yaml"
    import yaml

    yaml.safe_dump(_selectors(site), sel_path.open("w", encoding="utf-8"), allow_unicode=True)
    out = tmp_path / "dump"
    dump.main(["--cdp", chrome, "--selectors", str(sel_path), "--out", str(out), "--yes"])
    names = {p.name for p in out.iterdir()}
    assert {"feed_1.html", "feed_1.png", "feed_1_parsed.json", "feed_links.txt", "order.html",
            "form.html", "form.png", "report.txt"} <= names
    assert (tmp_path / "dump.zip").exists()
    assert _Handler.submissions == []  # отклик не отправлен
    feed = (out / "feed_1.html").read_text(encoding="utf-8")
    assert "SECRET123" not in feed and "916 123-45-67" not in feed and "data-testid=\"order-snippet\"" in feed
    assert len(json.loads((out / "feed_1_parsed.json").read_text(encoding="utf-8"))) == 5
    report = (out / "report.txt").read_text(encoding="utf-8")
    assert "бот распознал заказов: 5" in report and "message_input" in report
