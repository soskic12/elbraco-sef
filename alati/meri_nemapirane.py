r"""Zasto stavka nema nasu sifru - po uzroku, ne po utisku.

Isti nalaz daje i `sefsync nemapirane` na serveru; ovde je zbog merenja na
razvojnoj masini. Logika je u `services/nacrti.razlozi_cekanja`, na jednom mestu.

    .\.venv\Scripts\python.exe alati\meri_nemapirane.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from sefsync.services.nacrti import razlozi_cekanja  # noqa: E402


def main() -> None:
    nalaz = razlozi_cekanja()
    print(f"Nacrta koji cekaju sifru: {nalaz['nacrta']}, stavki u njima: {nalaz['stavki']}")
    print()
    for uzrok, n in nalaz["uzroci"].most_common():
        print(f"  {n:>4}  {uzrok}")
        for primer in nalaz["primeri"].get(uzrok, []):
            print(f"        {primer}")
    print()
    print("--- po dobavljacu ---")
    for ime, n in nalaz["po_dobavljacu"].most_common(10):
        print(f"  {n:>4}  {ime}")


if __name__ == "__main__":
    main()
