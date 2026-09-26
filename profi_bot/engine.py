"""Движок автооткликов: работает в отдельном потоке, управляется Старт/Пауза/Стоп."""
from __future__ import annotations

import copy
import logging
import queue
import threading
import time
from datetime import datetime

from .browser import AlreadyResponded, BrowserClient, FormNotFound
from .filters import created_too_old, match, too_old
from .limits import Limits, LimitStatus
from .models import Order
from .pricing import pick_price, price_skip_reason
from .response_type import LABELS, choose_type, paid_cost_ok
from .scheduler import in_work_hours, next_work_start, random_delay
from .storage import Storage
from .templates import build_variables, cleanup, pick_template, render
from .notifier import TelegramNotifier

logger = logging.getLogger("profi_bot")

STOPPED, RUNNING, PAUSED = "stopped", "running", "paused"
MAX_FEED_ERRORS = 3


class StopRequested(Exception):
    pass


class Engine:
    def __init__(
        self,
        cfg: dict,
        selectors: dict,
        storage: Storage,
        events: queue.Queue | None = None,
        browser_factory=None,
        now=datetime.now,
    ):
        self._cfg = copy.deepcopy(cfg)
        self.selectors = selectors
        self.storage = storage
        self.events = events
        self.limits = Limits(storage)
        self.notifier = TelegramNotifier(lambda: self._cfg["telegram"], self.log)
        self._browser_factory = browser_factory or (
            lambda: BrowserClient(self._cfg["browser"]["cdp_url"], self.selectors, self.log)
        )
        self._now = now
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        self.state = STOPPED
        self.browser: BrowserClient | None = None
        self._auto_stop_reason: str | None = None

    # --- управление ---
    @property
    def cfg(self) -> dict:
        return self._cfg

    def update_config(self, cfg: dict) -> None:
        """Новые настройки применяются со следующего шага цикла."""
        self._cfg = copy.deepcopy(cfg)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            self.resume()
            return
        self._stop.clear()
        self._resume.set()
        self._auto_stop_reason = None
        self._thread = threading.Thread(target=self._run, name="profi-engine", daemon=True)
        self._thread.start()

    def pause(self) -> None:
        if self.state == RUNNING:
            self._resume.clear()
            self._set_state(PAUSED)
            self.log("info", "Пауза")

    def resume(self) -> None:
        if self.state == PAUSED:
            self._resume.set()
            self._set_state(RUNNING)
            self.log("info", "Продолжение работы")

    def stop(self) -> None:
        self._stop.set()
        self._resume.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    @property
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # --- события ---
    def _emit(self, kind: str, **payload) -> None:
        if self.events is not None:
            self.events.put({"kind": kind, **payload})

    def _set_state(self, state: str) -> None:
        self.state = state
        self._emit("state", state=state)

    def status(self, text: str) -> None:
        self._emit("status", text=text)

    def log(self, level: str, message: str) -> None:
        getattr(logger, level if level in ("debug", "info", "warning", "error") else "info")(message)
        if level != "debug":
            self.storage.add_event(level, message)
        self._emit("log", level=level, message=message, ts=self._now().strftime("%H:%M:%S"))

    def notify(self, text: str) -> None:
        self.notifier.send(text)

    # --- ожидание с учётом паузы/стопа ---
    def _check(self) -> None:
        if self._stop.is_set():
            raise StopRequested()
        if not self._resume.is_set():
            self.status("На паузе")
            while not self._resume.wait(0.3):
                if self._stop.is_set():
                    raise StopRequested()
            if self._stop.is_set():
                raise StopRequested()

    def _sleep(self, seconds: float, text: str = "") -> None:
        end = time.monotonic() + max(0.0, seconds)
        while True:
            self._check()
            left = end - time.monotonic()
            if left <= 0:
                return
            if text:
                self.status(f"{text}: {_human(left)}")
            self._stop.wait(min(1.0, left))

    # --- основной цикл ---
    def _run(self) -> None:
        self._set_state(RUNNING)
        cfg = self._cfg
        mode = "ТЕСТОВЫЙ режим (dry run, без отправки)" if cfg["browser"]["dry_run"] else "БОЕВОЙ режим"
        self.log("info", f"Старт. {mode}")
        self.notify(f"▶️ Profi-бот запущен: {mode}")
        if not cfg["browser"]["dry_run"]:
            n = self.storage.forget_dry_run_orders()
            if n:
                self.log("info", f"{n} заказов из тестового режима снова доступны для откликов")
        reason = "остановлен пользователем"
        try:
            self._connect()
            self._loop()
        except StopRequested:
            pass
        except Exception as exc:  # noqa: BLE001 — любая ошибка останавливает бота с уведомлением
            reason = f"ошибка: {exc}"
            logger.exception("engine crashed")
            self.log("error", f"Критическая ошибка: {exc}")
        finally:
            if self.browser:
                self.browser.close()
                self.browser = None
            if self._auto_stop_reason:
                reason = self._auto_stop_reason
            self.log("info", f"Бот остановлен ({reason})")
            self.notify(f"⏹ Profi-бот остановлен: {reason}")
            self._set_state(STOPPED)
            self.status("Остановлен")

    def _connect(self) -> None:
        self.status("Подключение к Chrome…")
        self.browser = self._browser_factory()
        try:
            self.browser.connect()
        except Exception as exc:
            raise RuntimeError(
                f"не удалось подключиться к Chrome по {self._cfg['browser']['cdp_url']}. "
                f"Запустите Chrome с --remote-debugging-port ({exc})"
            ) from None
        self.log("info", "Подключено к Chrome")

    def _auto_stop(self, reason: str) -> None:
        self._auto_stop_reason = reason
        self._stop.set()
        raise StopRequested()

    def _wait_work_hours(self) -> None:
        wh = self._cfg["timing"]["work_hours"]
        now = self._now()
        if in_work_hours(now, wh):
            return
        resume = next_work_start(now, wh)
        self.log("info", f"Вне рабочих часов, ожидание до {resume:%d.%m %H:%M}")
        while not in_work_hours(self._now(), self._cfg["timing"]["work_hours"]):
            self._sleep(min(60, max(1, (resume - self._now()).total_seconds())), "Вне рабочих часов")

    def _handle_limit(self, st: LimitStatus) -> None:
        msg = f"Достигнут лимит: {st.reason}"
        if self._cfg["limits"].get("on_limit", "stop") == "stop":
            self.log("warning", msg + " — автостоп")
            self._auto_stop(msg)
        resume = st.resume_at or self._now()
        self.log("warning", f"{msg} — ожидание до {resume:%d.%m %H:%M}")
        self.notify(f"⏸ {msg}. Продолжу в {resume:%H:%M}")
        while True:
            left = (resume - self._now()).total_seconds()
            if left <= 0:
                break
            self._sleep(min(60, left), "Лимит, ожидание")

    def _check_limits(self) -> bool:
        """True — можно работать; иначе лимит обработан (пауза или стоп)."""
        st = self.limits.check_global(
            self._cfg["limits"], self._now(), commission_allowed=self._cfg["response"]["allow_commission"]
        )
        if st.blocked:
            self._handle_limit(st)
            return False
        return True

    def _loop(self) -> None:
        feed_errors = 0
        while True:
            self._check()
            self._wait_work_hours()
            if not self._check_limits():
                continue
            self.status("Загрузка ленты заказов…")
            try:
                orders = self.browser.fetch_orders()
                feed_errors = 0
            except StopRequested:
                raise
            except Exception as exc:  # noqa: BLE001
                feed_errors += 1
                self.log("error", f"Ошибка загрузки ленты: {exc}")
                if feed_errors >= MAX_FEED_ERRORS:
                    self.log("warning", "Переподключение к Chrome…")
                    self.browser.close()
                    self._connect()
                    feed_errors = 0
                self._sleep(30, "Повтор через")
                continue

            new_orders = [o for o in orders if not self.storage.is_seen(o.id)]
            self.log("info", f"В ленте {len(orders)} заказов, новых {len(new_orders)}")
            for order in new_orders:
                self._check()
                cfg = self._cfg
                ok, reason = match(order, cfg["filters"], self._now(), cfg["pricing"])
                if not ok:
                    status = "too_old" if too_old(order, cfg["filters"], self._now()) else "skipped"
                    self.storage.mark_order(order, status, reason)
                    self.log("debug", f"Пропуск «{order.title[:60]}»: {reason}")
                    continue
                if not in_work_hours(self._now(), cfg["timing"]["work_hours"]):
                    break
                if not self._check_limits():
                    break
                self.log("info", f"Подходит по ленте: «{order.title[:80]}»")
                if self._process(order):
                    pause = random_delay(self._cfg["timing"]["interval_between"])
                    self._sleep(pause, "Интервал между откликами")

            self._sleep(float(self._cfg["browser"]["poll_interval_sec"]), "Следующая проверка ленты")

    def _process(self, order: Order) -> bool:
        """Откликается на заказ. True — была попытка отклика (для интервала)."""
        cfg = self._cfg
        dry_run = bool(cfg["browser"]["dry_run"])
        quote = pick_price(order, cfg["pricing"])
        if quote is None:  # настройки поменяли на ходу — заказ уже не подходит по цене
            self.storage.mark_order(order, "skipped", price_skip_reason(order, cfg["pricing"]))
            return False
        record = dict(order_id=order.id, title=order.title, url=order.url, price_text=quote.text,
                      price_value=quote.value)
        try:
            self.status(f"Открываю заказ «{order.title[:40]}»")
            form = self.browser.open_order(order)
            if form.client_name and not order.client_name:
                order.client_name = form.client_name
            old = created_too_old(form.created_at, cfg["filters"], self._now())
            if old:
                self.storage.mark_order(order, "skipped", old)
                self.log("info", f"Пропуск «{order.title[:60]}»: {old}")
                return False

            r = cfg["response"]
            # Стоимость не прочиталась — для лимита бюджета берём верхнюю границу диапазона (осторожно).
            cost = form.paid_cost if form.paid_cost is not None else float(
                r.get("paid_cost_max") or r.get("paid_cost_estimate") or 0)
            paid_ok = self.limits.paid_allowed(cfg["limits"], cost, self._now())
            rtype = choose_type(form.available_types, r, paid_ok, form.commission_cost, form.paid_cost)
            if rtype is None:
                notes = [f"доступно: {', '.join(LABELS[t] for t in sorted(form.available_types))}"]
                if not paid_ok:
                    notes.append("лимит платных исчерпан")
                if "paid" in form.available_types and not paid_cost_ok(form.paid_cost, r):
                    notes.append(f"платный отклик стоит {form.paid_cost:g} ₽ — вне диапазона")
                max_c = float(cfg["response"].get("max_commission") or 0)
                if max_c and form.commission_cost is not None and form.commission_cost > max_c:
                    notes.append(f"комиссия {form.commission_cost:g} ₽ больше {max_c:g} ₽")
                why = f"нет подходящего типа отклика ({'; '.join(notes)})"
                self.storage.mark_order(order, "skipped", why)
                self.log("info", f"Пропуск «{order.title[:60]}»: {why}")
                return False

            delay = random_delay(cfg["timing"]["delay_before"])
            send_after = random_delay(cfg["timing"]["tariff_to_send"])
            self.log("info", f"Откликаюсь на «{order.title[:60]}» ({LABELS[rtype]}): выбор тарифа через "
                             f"{_human(delay)}, отправка ещё через {_human(send_after)}")
            self._sleep(delay, "Читаю заказ, выбор тарифа через")
            template = pick_template(cfg["templates"], self.storage.next_counter("template_rr"))
            message = cleanup(render(template["text"], build_variables(order, quote, cfg["templates"])))
            sent = self.browser.submit_response(rtype, message, quote, dry_run, send_after, self._sleep)
            status = "sent" if sent else "dry_run"
            record.update(
                type=rtype, template=template.get("name", ""), message=message, status=status,
                cost=cost if rtype == "paid" else form.commission_cost,
            )
            self.storage.add_response(**record)
            self.storage.mark_order(order, "responded" if sent else "dry_run")
            detail = f"{LABELS[rtype]}, цена {quote.text} ₽ ({quote.reason})"
            if rtype == "commission" and form.commission_cost is not None:
                detail += f", комиссия {form.commission_cost:g} ₽"
            elif rtype == "paid":
                detail += f", стоимость отклика {cost:g} ₽"
            if sent:
                self.log("info", f"✅ Отклик отправлен: «{order.title[:60]}» ({detail})")
                self.notify(f"✅ Отклик: {order.title}\n{detail}\n{order.url}")
            else:
                self.log("info", f"🧪 [dry run] Не отправлено (тестовый режим): «{order.title[:60]}» ({detail})")
            self._emit("response", row=record)
            return True
        except AlreadyResponded as exc:
            why = str(exc) or "уже есть отклик"
            self.storage.mark_order(order, "already", why)
            self.log("info", f"Пропуск «{order.title[:60]}»: {why}")
            return False
        except StopRequested:
            raise
        except Exception as exc:  # noqa: BLE001
            error = str(exc) if isinstance(exc, FormNotFound) else f"{type(exc).__name__}: {exc}"
            record.update(status="error", error=error[:500])
            self.storage.add_response(**record)
            self.storage.mark_order(order, "error", error[:200])
            self.log("error", f"Ошибка отклика на «{order.title[:60]}»: {error[:300]}")
            self.notify(f"⚠️ Ошибка отклика: {order.title}\n{error[:300]}")
            self._emit("response", row=record)
            return True


def _human(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} с"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m} мин {s} с"
    h, m = divmod(m, 60)
    return f"{h} ч {m} мин"
