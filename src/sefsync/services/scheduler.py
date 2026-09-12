"""Radnik koji periodicno sinhronizuje SEF (Windows servis / systemd / konzola)."""

from __future__ import annotations

import logging
import signal
import threading
import time

from ..config import Settings, get_settings
from .ingest import IngestService

log = logging.getLogger(__name__)


class SyncWorker:
    def __init__(self, service: IngestService | None = None, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.service = service or IngestService(settings=self.settings)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #

    def run_forever(self, install_signal_handlers: bool = True) -> None:
        interval = max(1, self.settings.poll_interval_minutes) * 60
        if install_signal_handlers:
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    signal.signal(sig, lambda *_: self.stop())
                except (ValueError, OSError):  # nije glavni thread / Windows
                    pass

        log.info("Radnik pokrenut, interval %s min", self.settings.poll_interval_minutes)
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                stats = self.service.sync()
                log.info("Ciklus zavrsen: %s", stats.summary())
            except Exception:  # noqa: BLE001 - radnik ne sme da umre zbog jednog ciklusa
                log.exception("Neocekivana greska u ciklusu sinhronizacije")
            elapsed = time.monotonic() - started
            self._stop.wait(max(5.0, interval - elapsed))
        log.info("Radnik zaustavljen.")

    def start_background(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self.run_forever, kwargs={"install_signal_handlers": False},
            name="sef-sync", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
