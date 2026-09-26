"""Связывание полей Tkinter с путями во вложенном dict настроек ("filters.budget_min")."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk


def get_path(cfg: dict, path: str):
    node = cfg
    for part in path.split("."):
        node = node[part]
    return node


def set_path(cfg: dict, path: str, value) -> None:
    parts = path.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _fmt_num(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class Binder:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._fields: list[tuple[str, str, object, str]] = []  # path, kind, holder, label

    # --- фабрики виджетов ---
    def entry(self, parent, path: str, kind: str = "str", width: int = 12, label: str = "", **kw) -> ttk.Entry:
        """kind: str | int | float | optfloat (пусто → None)."""
        value = get_path(self.cfg, path)
        var = tk.StringVar(value=_fmt_num(value) if kind != "str" else (value or ""))
        widget = ttk.Entry(parent, textvariable=var, width=width, **kw)
        self._fields.append((path, kind, var, label or path))
        return widget

    def check(self, parent, path: str, text: str) -> ttk.Checkbutton:
        var = tk.BooleanVar(value=bool(get_path(self.cfg, path)))
        self._fields.append((path, "bool", var, text))
        return ttk.Checkbutton(parent, text=text, variable=var)

    def lines(self, parent, path: str, height: int = 4, width: int = 40) -> tk.Text:
        widget = tk.Text(parent, height=height, width=width, wrap="word", undo=True)
        widget.insert("1.0", "\n".join(get_path(self.cfg, path) or []))
        self._fields.append((path, "lines", widget, path))
        return widget

    def combo(self, parent, path: str, options: dict, width: int = 20) -> ttk.Combobox:
        """options: {значение_в_конфиге: подпись}. Значения могут быть списками (сравнение по ==)."""
        current = get_path(self.cfg, path)
        labels = list(options.values())
        label = next((lbl for val, lbl in options.items() if _as_key(val) == _as_key(current)), labels[0])
        var = tk.StringVar(value=label)
        widget = ttk.Combobox(parent, textvariable=var, values=labels, state="readonly", width=width)
        self._fields.append((path, "combo", (var, options), path))
        return widget

    def radio(self, parent, path: str, options: dict, command=None) -> list[ttk.Radiobutton]:
        var = tk.StringVar(value=get_path(self.cfg, path))
        self._fields.append((path, "radio", var, path))
        return [ttk.Radiobutton(parent, text=lbl, value=val, variable=var, command=command)
                for val, lbl in options.items()]

    def radio_var(self, path: str) -> tk.StringVar | None:
        for p, kind, holder, _ in self._fields:
            if p == path and kind == "radio":
                return holder
        return None

    def multi(self, parent, path: str, options: dict, cast=str) -> list[ttk.Checkbutton]:
        """Набор чекбоксов → список выбранных значений."""
        selected = {str(v) for v in (get_path(self.cfg, path) or [])}
        vars_ = {}
        widgets = []
        for val, lbl in options.items():
            var = tk.BooleanVar(value=str(val) in selected)
            vars_[val] = var
            widgets.append(ttk.Checkbutton(parent, text=lbl, variable=var))
        self._fields.append((path, "multi", (vars_, cast), path))
        return widgets

    # --- сбор значений ---
    def collect(self) -> list[str]:
        """Переносит значения виджетов в cfg. Возвращает список ошибок ввода."""
        errors = []
        for path, kind, holder, label in self._fields:
            try:
                value = self._read(kind, holder)
            except ValueError:
                errors.append(f"«{label}»: ожидается число")
                continue
            set_path(self.cfg, path, value)
        return errors

    @staticmethod
    def _read(kind: str, holder):
        if kind == "str":
            return holder.get().strip()
        if kind == "int":
            return int(float(holder.get().strip().replace(",", ".") or 0))
        if kind == "float":
            return float(holder.get().strip().replace(",", ".") or 0)
        if kind == "optfloat":
            raw = holder.get().strip().replace(",", ".").replace(" ", "")
            return float(raw) if raw else None
        if kind == "bool":
            return bool(holder.get())
        if kind == "lines":
            return [ln.strip() for ln in holder.get("1.0", "end").splitlines() if ln.strip()]
        if kind == "combo":
            var, options = holder
            val = next((v for v, lbl in options.items() if lbl == var.get()), next(iter(options)))
            return list(val) if isinstance(val, tuple) else val
        if kind == "radio":
            return holder.get()
        if kind == "multi":
            vars_, cast = holder
            return [cast(val) for val, var in vars_.items() if var.get()]
        raise ValueError(kind)


def _as_key(value):
    return tuple(value) if isinstance(value, list) else value
