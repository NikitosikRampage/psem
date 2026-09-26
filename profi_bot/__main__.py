"""Запуск: python -m profi_bot [--headless] [--config PATH] [--selectors PATH]."""
from __future__ import annotations

import argparse
import logging
import queue
import shutil
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import config as config_mod


def setup_logging(log_path: Path, console: bool) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3,
                                                           encoding="utf-8")]
    if console:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


def run_headless(settings_path: Path, selectors_path: Path) -> int:
    from .engine import Engine
    from .storage import Storage

    cfg = config_mod.load_settings(settings_path)
    errors = config_mod.validate(cfg)
    if errors:
        print("Ошибки в настройках:\n  " + "\n  ".join(errors), file=sys.stderr)
        return 2
    storage = Storage(config_mod.resolve_path(cfg["storage"]["db_path"]))
    engine = Engine(cfg, config_mod.load_selectors(selectors_path), storage, queue.Queue())
    signal.signal(signal.SIGINT, lambda *_: engine.stop())
    signal.signal(signal.SIGTERM, lambda *_: engine.stop())
    engine.start()
    while engine.is_alive:
        engine.join(0.5)
    storage.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="profi_bot", description="Автоотклики на Profi.ru")
    parser.add_argument("--headless", action="store_true", help="без интерфейса, по настройкам из YAML")
    parser.add_argument("--config", type=Path, default=config_mod.SETTINGS_PATH, help="путь к settings.yaml")
    parser.add_argument("--selectors", type=Path, default=config_mod.SELECTORS_PATH, help="путь к selectors.yaml")
    args = parser.parse_args(argv)

    example = config_mod.CONFIG_DIR / "settings.example.yaml"
    if not args.config.exists():
        args.config.parent.mkdir(parents=True, exist_ok=True)
        if example.exists():
            shutil.copyfile(example, args.config)
        else:
            config_mod.save_settings(config_mod.DEFAULTS, args.config)
    cfg = config_mod.load_settings(args.config)
    setup_logging(config_mod.resolve_path(cfg["storage"]["log_path"]), console=args.headless)

    if args.headless:
        return run_headless(args.config, args.selectors)
    from .ui.app import run

    run(args.config, args.selectors)
    return 0


if __name__ == "__main__":
    sys.exit(main())
