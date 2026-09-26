"""Tkinter-панель: вкладки Фильтры / Шаблоны / Цены / Лимиты / Логи + Старт/Пауза/Стоп."""
from __future__ import annotations

import copy
import queue
import subprocess
import sys
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .. import config as config_mod
from ..engine import PAUSED, RUNNING, STOPPED, Engine
from ..models import Order
from ..pricing import FormulaError, calc_price, eval_formula
from ..response_type import LABELS
from ..storage import Storage
from ..templates import VARIABLES, build_variables, cleanup, render
from .binder import Binder

PAD = {"padx": 6, "pady": 3}
STATUS_LABELS = {"sent": "отправлен", "dry_run": "тест", "error": "ошибка"}
WEEKDAYS = {1: "Пн", 2: "Вт", 3: "Ср", 4: "Чт", 5: "Пт", 6: "Сб", 7: "Вс"}
UNITS = {"sec": "сек", "min": "мин"}


class App:
    def __init__(self, root: tk.Tk, settings_path: Path, selectors_path: Path):
        self.root = root
        self.settings_path = settings_path
        self.selectors_path = selectors_path
        self.cfg = config_mod.load_settings(settings_path)
        self.storage = Storage(config_mod.resolve_path(self.cfg["storage"]["db_path"]))
        self.events: queue.Queue = queue.Queue()
        self.engine = Engine(self.cfg, config_mod.load_selectors(selectors_path), self.storage, self.events)
        self.b = Binder(self.cfg)
        self.templates: list[dict] = copy.deepcopy(self.cfg["templates"]["items"])
        self._tpl_index: int | None = None

        root.title("Profi.ru — автоотклики")
        root.geometry("980x700")
        root.minsize(820, 560)
        self._build_topbar()
        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        for title, builder in (
            ("Фильтры", self._tab_filters),
            ("Шаблоны", self._tab_templates),
            ("Цены", self._tab_prices),
            ("Лимиты", self._tab_limits),
            ("Логи", self._tab_logs),
        ):
            frame = ttk.Frame(nb, padding=8)
            nb.add(frame, text=title)
            builder(frame)
        self._build_statusbar()

        self._load_events_history()
        self._refresh_responses()
        self._refresh_stats()
        self._update_buttons(STOPPED)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(300, self._poll_events)

    # ------------------------------------------------------------------ верх/низ
    def _build_topbar(self) -> None:
        bar = ttk.Frame(self.root, padding=(6, 6))
        bar.pack(fill="x")
        self.btn_start = ttk.Button(bar, text="▶ Старт", command=self.on_start)
        self.btn_pause = ttk.Button(bar, text="⏸ Пауза", command=self.on_pause)
        self.btn_stop = ttk.Button(bar, text="⏹ Стоп", command=self.on_stop)
        for w in (self.btn_start, self.btn_pause, self.btn_stop):
            w.pack(side="left", padx=3)
        ttk.Button(bar, text="💾 Сохранить", command=self.on_save).pack(side="left", padx=(12, 3))

        bar = ttk.Frame(self.root, padding=(6, 0, 6, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text="Chrome CDP:").pack(side="left", padx=(3, 3))
        self.b.entry(bar, "browser.cdp_url", width=24, label="Chrome CDP").pack(side="left")
        ttk.Label(bar, text="Опрос, сек:").pack(side="left", padx=(10, 3))
        self.b.entry(bar, "browser.poll_interval_sec", "int", width=5, label="Опрос").pack(side="left")
        self.b.check(bar, "browser.dry_run", "Тестовый режим (не отправлять)").pack(side="left", padx=10)

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self.root, padding=(6, 2))
        bar.pack(fill="x", side="bottom")
        self.state_label = ttk.Label(bar, text="", width=14, font=("TkDefaultFont", 9, "bold"))
        self.state_label.pack(side="left")
        self.status_var = tk.StringVar(value="Готов")
        ttk.Label(bar, textvariable=self.status_var).pack(side="left", padx=8)

    # ------------------------------------------------------------------ вкладки
    def _tab_filters(self, tab) -> None:
        left = ttk.Frame(tab)
        left.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(tab)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))

        def block(parent, title, path, hint):
            ttk.Label(parent, text=title, font=("TkDefaultFont", 10, "bold")).pack(anchor="w", pady=(6, 0))
            ttk.Label(parent, text=hint, foreground="gray").pack(anchor="w")
            self.b.lines(parent, path, height=5).pack(fill="both", expand=True, pady=2)

        block(left, "Категории", "filters.categories", "по одной на строку, ищется в заголовке заказа; пусто — любые")
        block(left, "Ключевые слова (нужно хотя бы одно)", "filters.keywords_include", "ищутся в названии и описании")
        block(left, "Стоп-слова", "filters.keywords_exclude", "заказы с этими словами пропускаются")

        budget = ttk.LabelFrame(right, text="Бюджет КЛИЕНТА в заказе, ₽ (какие заказы брать)", padding=6)
        budget.pack(fill="x", pady=(6, 4))
        ttk.Label(budget, text="от").grid(row=0, column=0, **PAD)
        self.b.entry(budget, "filters.budget_min", "optfloat", 10, "Бюджет от").grid(row=0, column=1, **PAD)
        ttk.Label(budget, text="до").grid(row=0, column=2, **PAD)
        self.b.entry(budget, "filters.budget_max", "optfloat", 10, "Бюджет до").grid(row=0, column=3, **PAD)
        self.b.check(budget, "filters.allow_no_budget", "Брать заказы без бюджета").grid(
            row=1, column=0, columnspan=4, sticky="w", **PAD)

        fmt = ttk.LabelFrame(right, text="Формат заказа", padding=6)
        fmt.pack(fill="x", pady=4)
        self.b.combo(fmt, "filters.remote_mode", {
            "remote_only": "Только дистанционные",
            "any": "Любые",
            "offline_only": "Только очные",
        }, width=26).pack(anchor="w")

        age = ttk.LabelFrame(right, text="Давность заказа (0 — не важно)", padding=6)
        age.pack(fill="x", pady=4)
        ttk.Label(age, text="Обновлён не раньше, ч назад:").grid(row=0, column=0, sticky="e", **PAD)
        self.b.entry(age, "filters.max_updated_hours", "float", 6, "Обновлён не раньше").grid(
            row=0, column=1, sticky="w", **PAD)
        ttk.Label(age, text="Создан не раньше, ч назад:").grid(row=1, column=0, sticky="e", **PAD)
        self.b.entry(age, "filters.max_created_hours", "float", 6, "Создан не раньше").grid(
            row=1, column=1, sticky="w", **PAD)
        ttk.Label(age, text="«Обновлён» — время в ленте («5 минут назад»), клиент может поднимать старый "
                            "заказ. «Создан» — «Заказ оставлен…» на странице заказа.",
                  foreground="gray", wraplength=420, justify="left").grid(row=2, column=0, columnspan=2, sticky="w")

        geo = ttk.LabelFrame(right, text="География", padding=6)
        geo.pack(fill="both", expand=True, pady=4)
        ttk.Label(geo, text="Город по одному на строку, напр. «Москва»; пусто — любой",
                  foreground="gray").pack(anchor="w")
        self.b.lines(geo, "filters.geo", height=3).pack(fill="both", expand=True, pady=2)

        client = ttk.LabelFrame(right, text="Тип клиента (ничего не выбрано — любой)", padding=6)
        client.pack(fill="x", pady=4)
        for w in self.b.multi(client, "filters.client_types", {"private": "Частное лицо", "company": "Компания"}):
            w.pack(side="left", padx=6)

    def _tab_templates(self, tab) -> None:
        top = ttk.Frame(tab)
        top.pack(fill="x")
        ttk.Label(top, text="Ротация:").pack(side="left")
        self.b.combo(top, "templates.rotation", {"round_robin": "По очереди", "random": "Случайно"},
                     width=12).pack(side="left", padx=4)
        ttk.Label(top, text="Ваше имя {my_name}:").pack(side="left", padx=(12, 3))
        self.b.entry(top, "templates.my_name", width=16).pack(side="left")
        ttk.Label(top, text="Срок {deadline}:").pack(side="left", padx=(12, 3))
        self.b.entry(top, "templates.deadline", width=14).pack(side="left")

        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True, pady=8)
        left = ttk.Frame(body)
        left.pack(side="left", fill="y")
        self.tpl_list = tk.Listbox(left, width=24, height=14, exportselection=False)
        self.tpl_list.pack(fill="y", expand=True)
        self.tpl_list.bind("<<ListboxSelect>>", self._on_tpl_select)
        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=4)
        ttk.Button(btns, text="+ Добавить", command=self._tpl_add).pack(side="left")
        ttk.Button(btns, text="− Удалить", command=self._tpl_delete).pack(side="left", padx=4)

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        row = ttk.Frame(right)
        row.pack(fill="x")
        ttk.Label(row, text="Название:").pack(side="left")
        self.tpl_name = tk.StringVar()
        ttk.Entry(row, textvariable=self.tpl_name, width=30).pack(side="left", padx=4)
        self.tpl_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="Активен", variable=self.tpl_enabled).pack(side="left", padx=8)
        self.tpl_text = tk.Text(right, height=10, wrap="word", undo=True)
        self.tpl_text.pack(fill="both", expand=True, pady=4)
        hint = "Переменные: " + ", ".join(f"{{{k}}} — {v}" for k, v in VARIABLES.items())
        ttk.Label(right, text=hint, foreground="gray", wraplength=620, justify="left").pack(anchor="w")
        ttk.Button(right, text="Предпросмотр", command=self._tpl_preview).pack(anchor="w", pady=4)
        self.tpl_preview = ttk.Label(right, text="", wraplength=620, justify="left", foreground="#1a5")
        self.tpl_preview.pack(anchor="w", fill="x")

        self._tpl_refresh_list(select=0)

    def _tab_prices(self, tab) -> None:
        price = ttk.LabelFrame(tab, text="Цена в отклике", padding=8)
        price.pack(fill="x")
        modes = {"fixed": "Фиксированная", "from": "«от» суммы", "range": "Диапазон", "formula": "Формула от бюджета"}
        radios = ttk.Frame(price)
        radios.grid(row=0, column=0, columnspan=6, sticky="w")
        for r in self.b.radio(radios, "pricing.mode", modes, command=self._price_test):
            r.pack(side="left", padx=6)

        ttk.Label(price, text="Сумма (фикс / «от»), ₽:").grid(row=1, column=0, sticky="e", **PAD)
        self.b.entry(price, "pricing.fixed", "float", 10, "Сумма").grid(row=1, column=1, sticky="w", **PAD)
        ttk.Label(price, text="Диапазон, ₽:").grid(row=2, column=0, sticky="e", **PAD)
        self.b.entry(price, "pricing.range_min", "float", 10, "Диапазон от").grid(row=2, column=1, sticky="w", **PAD)
        ttk.Label(price, text="—").grid(row=2, column=2)
        self.b.entry(price, "pricing.range_max", "float", 10, "Диапазон до").grid(row=2, column=3, sticky="w", **PAD)
        ttk.Label(price, text="Формула:").grid(row=3, column=0, sticky="e", **PAD)
        self.b.entry(price, "pricing.formula", width=30).grid(row=3, column=1, columnspan=3, sticky="w", **PAD)
        ttk.Label(price, text="переменные: budget, budget_min, budget_max; функции min, max, round. "
                              "Пример: budget * 0.9 (бюджет −10%)",
                  foreground="gray").grid(row=4, column=0, columnspan=6, sticky="w", **PAD)
        ttk.Label(price, text="Если у заказа нет бюджета, ₽:").grid(row=5, column=0, sticky="e", **PAD)
        self.b.entry(price, "pricing.fallback_price", "float", 10, "Цена без бюджета").grid(
            row=5, column=1, sticky="w", **PAD)
        ttk.Label(price, text="Округлять до:").grid(row=6, column=0, sticky="e", **PAD)
        self.b.entry(price, "pricing.round_to", "float", 10, "Округление").grid(row=6, column=1, sticky="w", **PAD)
        ttk.Label(price, text="Мин / макс цена:").grid(row=7, column=0, sticky="e", **PAD)
        self.b.entry(price, "pricing.min_price", "optfloat", 10, "Мин цена").grid(row=7, column=1, sticky="w", **PAD)
        ttk.Label(price, text="—").grid(row=7, column=2)
        self.b.entry(price, "pricing.max_price", "optfloat", 10, "Макс цена").grid(row=7, column=3, sticky="w", **PAD)

        ttk.Label(price, text="Это ВАША цена, которую бот впишет в поле «Стоимость занятия». В поле одно число: "
                              "для «от» и диапазона вписывается нижняя граница, а «от 1 200» / «800–2 000» "
                              "попадает только в текст через {price}.",
                  foreground="gray", wraplength=860, justify="left").grid(
            row=8, column=0, columnspan=6, sticky="w", **PAD)
        test = ttk.Frame(price)
        test.grid(row=9, column=0, columnspan=6, sticky="w", pady=(6, 0))
        ttk.Label(test, text="Проверка: бюджет заказа").pack(side="left")
        self.test_budget = tk.StringVar(value="5000")
        ttk.Entry(test, textvariable=self.test_budget, width=10).pack(side="left", padx=4)
        ttk.Button(test, text="Рассчитать", command=self._price_test).pack(side="left")
        self.price_result = ttk.Label(test, text="", foreground="#1a5")
        self.price_result.pack(side="left", padx=8)

        rt = ttk.LabelFrame(tab, text="Тип отклика", padding=8)
        rt.pack(fill="x", pady=8)
        self.b.check(rt, "response.allow_paid", "Обычный (платный)").grid(row=0, column=0, sticky="w", **PAD)
        self.b.check(rt, "response.allow_commission", "За комиссию").grid(row=0, column=1, sticky="w", **PAD)
        ttk.Label(rt, text="Приоритет:").grid(row=1, column=0, sticky="e", **PAD)
        self.b.combo(rt, "response.priority", {
            ("commission", "paid"): "Сначала за комиссию, иначе платный",
            ("paid", "commission"): "Сначала платный, иначе за комиссию",
        }, width=36).grid(row=1, column=1, columnspan=3, sticky="w", **PAD)
        ttk.Label(rt, text="Макс. комиссия, ₽:").grid(row=2, column=0, sticky="e", **PAD)
        self.b.entry(rt, "response.max_commission", "float", 8, "Макс. комиссия").grid(
            row=2, column=1, sticky="w", **PAD)
        ttk.Label(rt, text="сумму комиссии считает Profi.ru; 0 — без ограничения",
                  foreground="gray").grid(row=2, column=2, columnspan=3, sticky="w")
        ttk.Label(rt, text="Платный отклик стоит, ₽: от").grid(row=3, column=0, sticky="e", **PAD)
        self.b.entry(rt, "response.paid_cost_min", "float", 8, "Платный отклик от").grid(
            row=3, column=1, sticky="w", **PAD)
        ttk.Label(rt, text="до").grid(row=3, column=2, sticky="e", **PAD)
        self.b.entry(rt, "response.paid_cost_max", "float", 8, "Платный отклик до").grid(
            row=3, column=3, sticky="w", **PAD)
        ttk.Label(rt, text="Стоимость («Отклик • 90 ₽») бот читает на странице каждого заказа. Если она вне "
                            "диапазона — платно не откликается (0 — без ограничения).",
                  foreground="gray", wraplength=860, justify="left").grid(
            row=4, column=0, columnspan=6, sticky="w", **PAD)

    def _tab_limits(self, tab) -> None:
        lim = ttk.LabelFrame(tab, text="Лимиты (0 — без ограничения)", padding=8)
        lim.pack(fill="x")
        rows = [
            ("Откликов в день:", "limits.per_day", "int"),
            ("Откликов в час:", "limits.per_hour", "int"),
            ("Платных откликов в день:", "limits.paid_per_day", "int"),
            ("Бюджет на платные в день, ₽:", "limits.budget_per_day", "float"),
        ]
        for i, (label, path, kind) in enumerate(rows):
            ttk.Label(lim, text=label).grid(row=i, column=0, sticky="e", **PAD)
            self.b.entry(lim, path, kind, 8, label.rstrip(":")).grid(row=i, column=1, sticky="w", **PAD)
        ttk.Label(lim, text="При достижении лимита:").grid(row=4, column=0, sticky="e", **PAD)
        self.b.combo(lim, "limits.on_limit", {
            "stop": "Автостоп бота",
            "pause": "Пауза до освобождения лимита",
        }, width=30).grid(row=4, column=1, sticky="w", **PAD)
        ttk.Label(lim, text="Если исчерпаны только платные лимиты, бот продолжит откликаться за комиссию.",
                  foreground="gray").grid(row=5, column=0, columnspan=3, sticky="w", **PAD)

        tm = ttk.LabelFrame(tab, text="Время отклика (каждый раз случайное значение в диапазоне)", padding=8)
        tm.pack(fill="x", pady=8)
        for i, (label, key) in enumerate((("Открыл заказ → нажал тариф:", "delay_before"),
                                           ("Нажал тариф → «Отправить»:", "tariff_to_send"),
                                           ("После отклика до следующего:", "interval_between"))):
            ttk.Label(tm, text=label).grid(row=i, column=0, sticky="e", **PAD)
            ttk.Label(tm, text="от").grid(row=i, column=1)
            self.b.entry(tm, f"timing.{key}.min", "float", 6, label).grid(row=i, column=2, **PAD)
            ttk.Label(tm, text="до").grid(row=i, column=3)
            self.b.entry(tm, f"timing.{key}.max", "float", 6, label).grid(row=i, column=4, **PAD)
            self.b.combo(tm, f"timing.{key}.unit", UNITS, width=5).grid(row=i, column=5, **PAD)

        wh = ttk.LabelFrame(tab, text="Рабочие часы", padding=8)
        wh.pack(fill="x")
        self.b.check(wh, "timing.work_hours.enabled", "Работать только в указанное время").grid(
            row=0, column=0, columnspan=6, sticky="w", **PAD)
        ttk.Label(wh, text="с").grid(row=1, column=0, sticky="e", **PAD)
        self.b.entry(wh, "timing.work_hours.start", width=6).grid(row=1, column=1, sticky="w", **PAD)
        ttk.Label(wh, text="до").grid(row=1, column=2, **PAD)
        self.b.entry(wh, "timing.work_hours.end", width=6).grid(row=1, column=3, sticky="w", **PAD)
        ttk.Label(wh, text="(ЧЧ:ММ, можно ночное окно 22:00–06:00)", foreground="gray").grid(
            row=1, column=4, columnspan=3, sticky="w")
        days = ttk.Frame(wh)
        days.grid(row=2, column=0, columnspan=8, sticky="w", **PAD)
        for w in self.b.multi(days, "timing.work_hours.days", WEEKDAYS, cast=int):
            w.pack(side="left", padx=3)

    def _tab_logs(self, tab) -> None:
        self.stats_var = tk.StringVar()
        ttk.Label(tab, textvariable=self.stats_var, font=("TkDefaultFont", 10, "bold")).pack(anchor="w")

        btns = ttk.Frame(tab)
        btns.pack(fill="x", pady=4)
        ttk.Button(btns, text="Экспорт CSV", command=lambda: self._export("csv")).pack(side="left")
        ttk.Button(btns, text="Экспорт Excel", command=lambda: self._export("xlsx")).pack(side="left", padx=4)
        ttk.Button(btns, text="Обновить", command=self._refresh_all).pack(side="left")

        tg = ttk.LabelFrame(btns, text="Telegram", padding=(6, 2))
        tg.pack(side="right")
        self.b.check(tg, "telegram.enabled", "Вкл").pack(side="left")
        ttk.Label(tg, text="Token:").pack(side="left", padx=(6, 2))
        self.b.entry(tg, "telegram.token", width=22, show="•").pack(side="left")
        ttk.Label(tg, text="Chat ID:").pack(side="left", padx=(6, 2))
        self.b.entry(tg, "telegram.chat_id", width=12).pack(side="left")
        ttk.Button(tg, text="Тест", command=self._telegram_test).pack(side="left", padx=4)

        pane = ttk.PanedWindow(tab, orient="vertical")
        pane.pack(fill="both", expand=True)
        tree_frame = ttk.Frame(pane)
        cols = ("ts", "type", "status", "price", "title")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=8)
        for col, text, width in (("ts", "Время", 160), ("type", "Тип", 100), ("status", "Статус", 80),
                                 ("price", "Цена", 110), ("title", "Заказ", 480)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, stretch=col == "title")
        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        pane.add(tree_frame, weight=1)

        log_frame = ttk.Frame(pane)
        self.log_text = tk.Text(log_frame, height=10, wrap="word", state="disabled", font=("TkFixedFont", 9))
        for tag, color in (("error", "#c0392b"), ("warning", "#b9770e"), ("info", "black"), ("debug", "gray")):
            self.log_text.tag_configure(tag, foreground=color)
        sb2 = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb2.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb2.pack(side="left", fill="y")
        pane.add(log_frame, weight=1)

    # ------------------------------------------------------------------ шаблоны
    def _tpl_store_current(self) -> None:
        if self._tpl_index is None or self._tpl_index >= len(self.templates):
            return
        t = self.templates[self._tpl_index]
        t["name"] = self.tpl_name.get().strip() or f"Шаблон {self._tpl_index + 1}"
        t["enabled"] = bool(self.tpl_enabled.get())
        t["text"] = self.tpl_text.get("1.0", "end").rstrip()

    def _tpl_refresh_list(self, select: int | None = None) -> None:
        self.tpl_list.delete(0, "end")
        for t in self.templates:
            self.tpl_list.insert("end", ("✓ " if t.get("enabled", True) else "✗ ") + t.get("name", ""))
        if select is not None and self.templates:
            select = max(0, min(select, len(self.templates) - 1))
            self.tpl_list.selection_set(select)
            self._tpl_show(select)
        elif not self.templates:
            self._tpl_index = None
            self.tpl_name.set("")
            self.tpl_text.delete("1.0", "end")

    def _tpl_show(self, index: int) -> None:
        self._tpl_index = index
        t = self.templates[index]
        self.tpl_name.set(t.get("name", ""))
        self.tpl_enabled.set(t.get("enabled", True))
        self.tpl_text.delete("1.0", "end")
        self.tpl_text.insert("1.0", t.get("text", ""))

    def _on_tpl_select(self, _event=None) -> None:
        sel = self.tpl_list.curselection()
        if not sel or sel[0] == self._tpl_index:
            return
        self._tpl_store_current()
        index = sel[0]
        self._tpl_refresh_list()
        self.tpl_list.selection_set(index)
        self._tpl_show(index)

    def _tpl_add(self) -> None:
        self._tpl_store_current()
        self.templates.append({"name": f"Шаблон {len(self.templates) + 1}", "enabled": True,
                               "text": "Здравствуйте, {name}! "})
        self._tpl_refresh_list(select=len(self.templates) - 1)

    def _tpl_delete(self) -> None:
        if self._tpl_index is None:
            return
        del self.templates[self._tpl_index]
        index = self._tpl_index
        self._tpl_index = None
        self._tpl_refresh_list(select=index)

    def _tpl_preview(self) -> None:
        self._tpl_store_current()
        self.b.collect()
        order = Order(id="0", url="", title="Ремонт ванной комнаты", category="Ремонт",
                      budget_min=20000, budget_max=30000, client_name="Анна")
        quote = calc_price(order, self.cfg["pricing"])
        text = cleanup(render(self.tpl_text.get("1.0", "end").strip(),
                              build_variables(order, quote, self.cfg["templates"])))
        self.tpl_preview.configure(text=text)

    # ------------------------------------------------------------------ цены
    def _price_test(self) -> None:
        self.b.collect()
        try:
            budget = float(self.test_budget.get().replace(" ", "").replace(",", "."))
        except ValueError:
            self.price_result.configure(text="введите число", foreground="#c0392b")
            return
        cfg = self.cfg["pricing"]
        if cfg["mode"] == "formula":
            try:
                eval_formula(cfg["formula"], {"budget": budget, "budget_min": budget, "budget_max": budget})
            except FormulaError as exc:
                self.price_result.configure(text=f"ошибка: {exc}", foreground="#c0392b")
                return
        quote = calc_price(Order(id="0", url="", budget_min=budget, budget_max=budget), cfg)
        self.price_result.configure(text=f"→ {quote.text} ₽", foreground="#1a5")

    # ------------------------------------------------------------------ управление
    def _collect(self) -> bool:
        self._tpl_store_current()
        errors = self.b.collect()
        self.cfg["templates"]["items"] = copy.deepcopy(self.templates)
        if not errors:
            errors = config_mod.validate(self.cfg)
        if errors:
            messagebox.showerror("Ошибки в настройках", "\n".join(errors))
            return False
        return True

    def on_save(self, quiet: bool = False) -> bool:
        if not self._collect():
            return False
        config_mod.save_settings(self.cfg, self.settings_path)
        self.engine.update_config(self.cfg)
        self._tpl_refresh_list(select=self._tpl_index or 0)
        if not quiet:
            self.status_var.set(f"Настройки сохранены в {self.settings_path.name}")
        return True

    def on_start(self) -> None:
        if self.engine.state == PAUSED:
            self.engine.resume()
            return
        if not self.on_save(quiet=True):
            return
        if not self.cfg["browser"]["dry_run"] and not messagebox.askyesno(
            "Боевой режим", "Тестовый режим выключен — отклики будут отправляться по-настоящему. Продолжить?"
        ):
            return
        self.engine.selectors = config_mod.load_selectors(self.selectors_path)
        self.engine.start()

    def on_pause(self) -> None:
        if self.engine.state == RUNNING:
            self.engine.pause()
        elif self.engine.state == PAUSED:
            self.engine.resume()

    def on_stop(self) -> None:
        self.engine.stop()
        self.status_var.set("Остановка…")

    def _update_buttons(self, state: str) -> None:
        text = {RUNNING: ("● Работает", "#1a5"), PAUSED: ("❚❚ Пауза", "#b9770e"),
                STOPPED: ("■ Остановлен", "gray")}[state]
        self.state_label.configure(text=text[0], foreground=text[1])
        self.btn_start.configure(state="normal" if state != RUNNING else "disabled",
                                 text="▶ Продолжить" if state == PAUSED else "▶ Старт")
        self.btn_pause.configure(state="normal" if state != STOPPED else "disabled",
                                 text="▶ Продолжить" if state == PAUSED else "⏸ Пауза")
        self.btn_stop.configure(state="normal" if state != STOPPED else "disabled")

    # ------------------------------------------------------------------ логи
    def _append_log(self, ts: str, level: str, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{ts}  {message}\n", level)
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 2000:
            self.log_text.delete("1.0", f"{lines - 2000}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _load_events_history(self) -> None:
        for e in self.storage.recent_events(200):
            self._append_log(e["ts"].replace("T", " ")[5:], e["level"], e["message"])

    def _refresh_responses(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for r in self.storage.recent_responses(300):
            self.tree.insert("", "end", values=(
                r["ts"].replace("T", " "), LABELS.get(r["type"] or "", "—"),
                STATUS_LABELS.get(r["status"], r["status"]), r["price_text"] or "", r["title"] or "",
            ))

    def _refresh_stats(self) -> None:
        s = self.storage.stats()
        self.stats_var.set(
            f"Сегодня: {s['sent_today']} откликов (за час {s['sent_hour']}) · платных {s['paid_today']}, "
            f"за комиссию {s['commission_today']} · потрачено {s['spent_today']:g} ₽ · "
            f"тестовых {s['dry_run_today']} · ошибок {s['errors_today']}\n"
            f"Всего отправлено {s['total_sent']}, просмотрено заказов {s['orders_seen']}"
        )

    def _refresh_all(self) -> None:
        self._refresh_responses()
        self._refresh_stats()

    def _poll_events(self) -> None:
        refresh = False
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev["kind"]
                if kind == "log":
                    if ev["level"] != "debug":
                        self._append_log(ev["ts"], ev["level"], ev["message"])
                elif kind == "status":
                    self.status_var.set(ev["text"])
                elif kind == "state":
                    self._update_buttons(ev["state"])
                elif kind == "response":
                    refresh = True
        except queue.Empty:
            pass
        if refresh:
            self._refresh_all()
        self.root.after(300, self._poll_events)

    def _export(self, fmt: str) -> None:
        out_dir = config_mod.resolve_path(self.cfg["storage"]["export_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        default = f"profi_responses_{datetime.now():%Y%m%d_%H%M}.{fmt}"
        path = filedialog.asksaveasfilename(
            initialdir=out_dir, initialfile=default, defaultextension=f".{fmt}",
            filetypes=[("CSV", "*.csv")] if fmt == "csv" else [("Excel", "*.xlsx")],
        )
        if not path:
            return
        try:
            result = self.storage.export_csv(path) if fmt == "csv" else self.storage.export_xlsx(path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Экспорт", f"Не удалось сохранить: {exc}")
            return
        self.status_var.set(f"Экспортировано: {result}")
        _open_folder(Path(result).parent)

    def _telegram_test(self) -> None:
        self.b.collect()
        cfg = self.cfg["telegram"]
        if not cfg["token"] or not cfg["chat_id"]:
            messagebox.showwarning("Telegram", "Укажите token и chat_id")
            return
        ok = self.engine.notifier._post(cfg, "🔔 Тестовое уведомление Profi-бота")
        (messagebox.showinfo if ok else messagebox.showerror)(
            "Telegram", "Сообщение отправлено" if ok else "Не удалось отправить — проверьте token/chat_id и логи")

    def _on_close(self) -> None:
        if self.engine.is_alive:
            if not messagebox.askyesno("Выход", "Бот работает. Остановить и выйти?"):
                return
            self.engine.stop()
            self.engine.join(10)
        self.storage.close()
        self.root.destroy()


def _open_folder(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            import os

            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
    except Exception:  # noqa: BLE001
        pass


def run(settings_path: Path, selectors_path: Path) -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam" if sys.platform.startswith("linux") else ttk.Style().theme_use())
    except tk.TclError:
        pass
    App(root, settings_path, selectors_path)
    root.mainloop()
