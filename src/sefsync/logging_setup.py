"""Konfiguracija logovanja."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import get_settings

FORMAT = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"


def setup_logging(level: str | None = None, naziv: str = "sefsync-cli") -> None:
    """Podesava logovanje; `naziv` odredjuje u koji fajl se pise.

    Servis i komandna linija pisu u RAZLICITE fajlove. Na Windowsu se otvoren
    fajl ne moze preimenovati, pa bi rotacija pukla svaki put kad neko pokrene
    komandu dok servis radi - i svaki zapis bi vukao za sobom PermissionError.
    """
    settings = get_settings()
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(level or settings.log_level)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):  # pragma: no cover
        pass
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(stream)

    log_file = settings.log_file or (settings.storage_dir / f"{naziv}.log")
    try:
        file_handler = RotatingFileHandler(
            log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(file_handler)
    except OSError:
        root.warning("Ne mogu da otvorim log fajl %s", log_file)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
