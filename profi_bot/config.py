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
PRICE_MODES = ("fixed", "from", "range", "formula")
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
        "budget_min": None,
        "budget_max": None,
        "allow_no_budget": True,
        "geo": [],
        "remote_mode": "remote_only",  # remote_only | any | offline_only
        "max_updated_hours": 0,  # заказ обновлён не раньше N часов назад (время в ленте); 0 — не важно
        "max_created_hours": 0,  # заказ создан не раньше N часов назад (со страницы заказа); 0 — не важно
        "client_types": [],  # private / company; пусто = любой
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
    "pricing": {
        "mode": "fixed",
        "fixed": 1000,
        "range_min": 1000,
        "range_max": 2000,
        "formula": "budget * 0.9",
        "fallback_price": 1000,
        "round_to": 100,
        "min_price": None,
        "max_price": None,
    },
    "response": {
        "priority": ["commission", "paid"],
        "allow_paid": True,
        "allow_commission": True,
        "max_commission": 0,  # ₽; 0 — без ограничения. Сумму комиссии считает Profi.ru
        "paid_cost_estimate": 100,
    },
    "timing": {
        "delay_before": {"min": 20, "max": 90, "unit": "sec"},
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


def load_settings(path: Path | str = SETTINGS_PATH) -> dict:
    return deep_merge(DEFAULTS, _read_yaml(Path(path)))


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
    if p["mode"] not in PRICE_MODES:
        errors.append(f"Неизвестный режим цены: {p['mode']}")
    if p["mode"] == "range" and float(p["range_min"]) > float(p["range_max"]):
        errors.append("Цена: минимум диапазона больше максимума")
    if p["mode"] == "formula":
        from .pricing import FormulaError, eval_formula

        try:
            eval_formula(p["formula"], {"budget": 1000, "budget_min": 1000, "budget_max": 1000})
        except FormulaError as exc:
            errors.append(f"Ошибка в формуле цены: {exc}")

    r = cfg["response"]
    if not r["allow_paid"] and not r["allow_commission"]:
        errors.append("Не разрешён ни один тип отклика")
    if not set(r["priority"]) <= set(RESPONSE_TYPES):
        errors.append("Приоритет откликов должен содержать только paid/commission")
    if float(r.get("max_commission") or 0) < 0:
        errors.append("Максимальная комиссия не может быть отрицательной")

    if cfg["templates"]["rotation"] not in ROTATION_MODES:
        errors.append("Неизвестный режим ротации шаблонов")
    if not [t for t in cfg["templates"]["items"] if t.get("enabled", True) and t.get("text", "").strip()]:
        errors.append("Нет ни одного активного шаблона отклика")

    for key in ("delay_before", "interval_between"):
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
    for key in ("max_updated_hours", "max_created_hours"):
        if float(f.get(key) or 0) < 0:
            errors.append("Давность заказа не может быть отрицательной")
    if f["budget_min"] is not None and f["budget_max"] is not None and f["budget_min"] > f["budget_max"]:
        errors.append("Фильтр бюджета: минимум больше максимума")

    tg = cfg["telegram"]
    if tg["enabled"] and (not tg["token"] or not tg["chat_id"]):
        errors.append("Telegram включён, но не заданы token/chat_id")
    return errors
