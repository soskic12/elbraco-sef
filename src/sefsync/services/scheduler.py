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

            # Sifra se otvara u ERP-u, van panela, pa se zapeli nacrti moraju
            # preracunati sami - inace covek uradi posao a ekran i dalje kaze
            # "ceka sifru", dok neko rucno ne pusti `nacrti --osvezi`.
            if self.settings.erp_upis_kalkulacije:
                try:
                    from .nacrti import osvezi_zapele

                    ishod = osvezi_zapele(limit=self.settings.osvezi_zapele_limit)
                    if ishod["odmrznuto"]:
                        log.info("Odmrznuto nacrta: %(odmrznuto)s od %(pregledano)s", ishod)
                except Exception:  # noqa: BLE001 - ERP ume da bude nedostupan
                    log.exception("Osvežavanje zapelih nacrta nije prošlo")

                # Potvrda prijema pokusava upis odmah; ako nacrt tada nije bio
                # spreman, upis se odbio i niko ga nije ponavljao. Faktura je
                # tako tiho stajala dva dana (Vitel R-1382/26VP, 02.10.). Covek
                # JESTE kliknuo "potvrdi prijem" - ovo dovrsava tu nameru.
                try:
                    from .nacrti import dovrsi_upise

                    ishod = dovrsi_upise(
                        limit=self.settings.dovrsi_upise_limit,
                        produkcija=self.settings.erp_allow_production_write,
                    )
                    if ishod["nasao"]:
                        log.info("Dovrseno upisa: %(upisao)s od %(nasao)s", ishod)
                except Exception:  # noqa: BLE001 - ciklus ne sme da stane zbog ovoga
                    log.exception("Dovršavanje zaostalih upisa nije prošlo")
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
