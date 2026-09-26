"""Загрузка/сохранение настроек из YAML.

Настройки хранятся как обычный вложенный dict: значения из файла накладываются
поверх DEFAULTS, поэтому отсутствующие в YAML ключи всегда имеют значение.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "config"
SETTINGS_PATH = CONFIG_DIR / "settings.yaml"
SELECTORS_PATH = CONFIG_DIR / "selectors.yaml"

RESPONSE_TYPES = ("paid", "commission")
ROTATION_MODES = ("round_robin", "random")
TIME_UNITS = ("sec", "min")

DEFAULTS: dict[str, Any] = {
    "browser": {
        "cdp_url": "http://127.0.0.1:9222",
        "poll_interval_sec": 60,
        "dry_run": True,
    },
    "filters": {
        "categories": [],
        "keywords_include": [],
        "keywords_exclude": [],
        "remote_mode": "remote_only",  # remote_only | any | offline_only
        "max_updated_minutes": 0,  # заказ обновлён не раньше N минут назад (время в ленте); 0 — не важно
        "max_created_minutes": 0,  # заказ создан не раньше N минут назад («Заказ оставлен…»); 0 — не важно
    },
    "templates": {
        "rotation": "round_robin",
        "my_name": "",
        "deadline": "",
        "items": [
            {
                "name": "Базовый",
                "enabled": True,
                "text": "Здравствуйте, {name}! Готов взяться за «{title}». "
                "Стоимость: {price} ₽, срок: {deadline}. {my_name}",
            }
        ],
    },
    # Цена в поле «Цена» отклика: первая строка, в которую попал бюджет клиента.
    # Бюджет вне всех строк — заказ не подходит. budget_max пусто/0 — без верхней границы.
    "pricing": {
        "rules": [
            {"budget_min": 0, "budget_max": 1000, "price": 1000},
            {"budget_min": 1000, "budget_max": 1500, "price": 1300},
            {"budget_min": 1500, "budget_max": 4000, "price": 1500},
        ],
        "take_no_budget": True,
        "no_budget_price": 1200,
    },
    "response": {
        "priority": ["commission", "paid"],
        "allow_paid": True,
        "allow_commission": True,
        "max_commission": 0,  # ₽; 0 — без ограничения. Сумму комиссии считает Profi.ru
        "paid_cost_min": 0,  # ₽; платный отклик берётся, только если его цена в диапазоне (0 — без границы)
        "paid_cost_max": 0,
        "paid_cost_estimate": 100,  # ₽, если стоимость не прочиталась со страницы (и не задан максимум)
    },
    "timing": {
        # От открытия страницы заказа до нажатия на тариф («Комиссия»/«Отклик»).
        "delay_before": {"min": 5, "max": 20, "unit": "sec"},
        # От нажатия на тариф до «Отправить сообщение» (включая ввод текста).
        "tariff_to_send": {"min": 15, "max": 45, "unit": "sec"},
        "interval_between": {"min": 1, "max": 3, "unit": "min"},
        "work_hours": {
            "enabled": True,
            "start": "09:00",
            "end": "21:00",
            "days": [1, 2, 3, 4, 5, 6, 7],
        },
    },
    "limits": {
        "per_day": 30,
        "per_hour": 6,
        "paid_per_day": 10,
        "budget_per_day": 1000,
        "on_limit": "stop",  # stop — автостоп; pause — ждать, пока лимит освободится
    },
    "telegram": {
        "enabled": False,
        "token": "",
        "chat_id": "",
    },
    "storage": {
        "db_path": "data/profi_bot.db",
        "log_path": "data/profi_bot.log",
        "export_dir": "exports",
    },
}


def deep_merge(base: dict, override: dict | None) -> dict:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# Секции, из которых при загрузке выкидываются устаревшие ключи старых версий.
_STRICT_SECTIONS = ("filters", "pricing", "response")


def load_settings(path: Path | str = SETTINGS_PATH) -> dict:
    cfg = deep_merge(DEFAULTS, _read_yaml(Path(path)))
    for section in _STRICT_SECTIONS:
        for key in list(cfg[section]):
            if key not in DEFAULTS[section]:
                del cfg[section][key]
    return cfg


def save_settings(cfg: dict, path: Path | str = SETTINGS_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, allow_unicode=True, sort_keys=False)


def load_selectors(path: Path | str = SELECTORS_PATH) -> dict:
    return _read_yaml(Path(path))


def resolve_path(p: str) -> Path:
    """Относительные пути из конфига считаются от корня проекта."""
    path = Path(p)
    return path if path.is_absolute() else ROOT_DIR / path


def validate(cfg: dict) -> list[str]:
    """Возвращает список ошибок (пустой — всё в порядке)."""
    errors: list[str] = []
    p = cfg["pricing"]
    if not p.get("rules"):
        errors.append("Таблица цен пуста — добавьте хотя бы одну строку")
    for i, rule in enumerate(p.get("rules") or [], 1):
        low, high = float(rule.get("budget_min") or 0), float(rule.get("budget_max") or 0)
        if high and low > high:
            errors.append(f"Цены, строка {i}: бюджет «от» больше «до»")
        if float(rule.get("price") or 0) <= 0:
            errors.append(f"Цены, строка {i}: не указана ваша цена")
    if p.get("take_no_budget") and float(p.get("no_budget_price") or 0) <= 0:
        errors.append("Цены: укажите цену для заказов без бюджета")

    r = cfg["response"]
    if not r["allow_paid"] and not r["allow_commission"]:
        errors.append("Не разрешён ни один тип отклика")
    if not set(r["priority"]) <= set(RESPONSE_TYPES):
        errors.append("Приоритет откликов должен содержать только paid/commission")
    pmin, pmax = float(r.get("paid_cost_min") or 0), float(r.get("paid_cost_max") or 0)
    if pmin < 0 or pmax < 0:
        errors.append("Стоимость платного отклика не может быть отрицательной")
    elif pmax and pmin > pmax:
        errors.append("Стоимость платного отклика: «от» больше «до»")
    if float(r.get("max_commission") or 0) < 0:
        errors.append("Максимальная комиссия не может быть отрицательной")

    if cfg["templates"]["rotation"] not in ROTATION_MODES:
        errors.append("Неизвестный режим ротации шаблонов")
    if not [t for t in cfg["templates"]["items"] if t.get("enabled", True) and t.get("text", "").strip()]:
        errors.append("Нет ни одного активного шаблона отклика")

    for key in ("delay_before", "tariff_to_send", "interval_between"):
        d = cfg["timing"][key]
        if float(d["min"]) > float(d["max"]):
            errors.append(f"Задержка {key}: минимум больше максимума")
        if d["unit"] not in TIME_UNITS:
            errors.append(f"Задержка {key}: единица должна быть sec или min")

    from .scheduler import parse_hhmm

    wh = cfg["timing"]["work_hours"]
    for key in ("start", "end"):
        try:
            parse_hhmm(wh[key])
        except ValueError:
            errors.append(f"Рабочие часы: неверное время «{wh[key]}», нужно ЧЧ:ММ")

    f = cfg["filters"]
    from .filters import REMOTE_MODES

    if f.get("remote_mode") not in REMOTE_MODES:
        errors.append("Неизвестный режим формата заказа (дистанционно/очно)")
    for key in ("max_updated_minutes", "max_created_minutes"):
        if float(f.get(key) or 0) < 0:
            errors.append("Давность заказа не может быть отрицательной")

    tg = cfg["telegram"]
    if tg["enabled"] and (not tg["token"] or not tg["chat_id"]):
        errors.append("Telegram включён, но не заданы token/chat_id")
    return errors
