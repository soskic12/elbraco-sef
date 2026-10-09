"""Da li kalkulacija koju panel smatra upisanom jos uvek postoji u ERP-u.

Panel zapamti broj kalkulacije i od tada joj veruje zauvek. Ali covek u ERP-u
sme da obrise kalkulaciju - i to radi, kad je napravljena greskom. Panel to ne
sazna: dokument i dalje stoji kao „upisan", **ne moze da se vrati** (vracanje se
odbija dok je kalkulacija u ERP-u), i niko ga vise ne obradjuje.

Gore od toga: ERP broj koji se oslobodi **dobija neko drugi**. Provereno
09.10.2026. na `kp-2026248-POV` (#8279): panel je pisao `088/03205`, Bogdan je
kalkulaciju obrisao, a `088/03205` je u ERP-u u medjuvremenu postao MISON-ovo
odobrenje `26-2204-239378` na -1.080,00. Panel je mesec dana pokazivao na tudji
dokument i tvrdio da je posao zavrsen.

Ovde se to samo **cita i prijavljuje** - nista se ne menja. Odluku sta dalje
donosi covek: ako kalkulacije nema, `sefsync zaboravi-upis <id> --stvarno`
oslobadja dokument, pa ga operater dodeli kome treba.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select, text

from ..db import session_scope
from ..models import Document, NacrtKalkulacije, StanjeNacrta
from .mapiranje import kljuc_racuna

log = logging.getLogger(__name__)

STAVKE_ZA = {"ULKALKUL": "PRULKALK", "MKALKUL": "MPRULK"}
TABELE = tuple(STAVKE_ZA)


def _razlozi(erp_broj: str | None, tabela: str | None) -> tuple[str, str, str]:
    """Iz zapisa „ULKALKUL 010/00620" vadi tabelu, magacin i broj.

    Zapis ima dva oblika: kad app upise kalkulaciju stoji samo `088/03205`, a
    kad je prepozna kao vec unetu rukom stoji i ime tabele ispred. Prvo
    izvodjenje ove provere je cepalo samo po `/` i trazilo magacin
    „ULKALKUL 010" - pa je prijavilo 106 nepostojecih kalkulacija kojih nema
    jer ih nije ni trazilo gde treba (09.10.).
    """
    zapis = (erp_broj or "").strip()
    nadjena = (tabela or "").strip().upper()
    for ime in TABELE:
        if zapis.upper().startswith(ime):
            nadjena = ime
            zapis = zapis[len(ime):].strip()
            break
    if "/" not in zapis:
        return nadjena or "ULKALKUL", "", ""
    magacin, kalk = zapis.split("/", 1)
    return nadjena or "ULKALKUL", magacin.strip(), kalk.strip()


@dataclass
class Nalaz:
    provereno: int = 0
    u_redu: int = 0
    nema_je: list[dict] = field(default_factory=list)
    tudja: list[dict] = field(default_factory=list)
    nepotpun_zapis: list[dict] = field(default_factory=list)

    @property
    def sumnjivih(self) -> int:
        return len(self.nema_je) + len(self.tudja)


def proveri(engine=None) -> Nalaz:
    """Prolazi kroz nacrte u stanju „upisan" i pita ERP da li su jos tamo."""
    from ..erp.connection import get_erp_engine
    from ..erp.kalkulacija import _analitika_dobavljaca

    nalaz = Nalaz()
    eng = engine or get_erp_engine()

    with session_scope() as session:
        redovi = list(
            session.execute(
                select(NacrtKalkulacije, Document)
                .join(Document, Document.id == NacrtKalkulacije.document_id)
                .where(
                    NacrtKalkulacije.stanje == StanjeNacrta.UPISAN,
                    Document.archived == False,  # noqa: E712
                )
            )
        )
        with eng.connect() as conn:
            analitike: dict[str, str | None] = {}
            for red, doc in redovi:
                tabela, magacin, kalk = _razlozi(red.erp_broj, red.tabela)
                if not magacin:
                    nalaz.nepotpun_zapis.append(
                        {"id": doc.id, "broj": doc.document_number,
                         "erp": (red.erp_broj or "").strip()}
                    )
                    continue
                broj = f"{magacin}/{kalk}"
                nalaz.provereno += 1

                r = conn.execute(
                    text(
                        f"SELECT RTRIM(DOBAVLJAC) dob, RTRIM(ISNULL(BROJ_RN_DOB,'')) rn "
                        f"FROM dbo.{tabela} WHERE RTRIM(MAGACIN)=:m AND RTRIM(BROJ)=:b"
                    ),
                    {"m": magacin.strip(), "b": kalk.strip()},
                ).first()

                if r is None:
                    nalaz.nema_je.append(
                        {
                            "id": doc.id,
                            "broj": doc.document_number,
                            "dobavljac": doc.supplier_name,
                            "erp": f"{tabela} {broj}",
                            "upisao": red.upisao,
                        }
                    )
                    continue

                pib = (doc.supplier_vat or "").strip()
                if pib not in analitike:
                    analitike[pib] = _analitika_dobavljaca(conn, doc.supplier_reg_no, pib)
                nas = analitike[pib]
                # Broj racuna je dokaz; sifra dobavljaca je potvrda. Oboje se
                # poredi bez razmaka i bez obzira na velicinu slova - u ERP-u se
                # kuca rukom (vidi `26-fa001011891` naspram `26-FA001011891`).
                nas_broj = kljuc_racuna(doc.document_number)
                erp_broj_racuna = kljuc_racuna(r.rn)
                # ERP broj je `varchar(20)`, pa ljudi izbacuju crtice i secku
                # kraj. Zato se poredi isto kao svuda u aplikaciji, i priznaje
                # se i kad je jedan pocetak drugog.
                poklapa = bool(erp_broj_racuna) and bool(nas_broj) and (
                    erp_broj_racuna == nas_broj
                    or nas_broj.startswith(erp_broj_racuna)
                    or erp_broj_racuna.startswith(nas_broj)
                )
                if erp_broj_racuna and nas_broj and not poklapa:
                    nalaz.tudja.append(
                        {
                            "id": doc.id,
                            "broj": doc.document_number,
                            "dobavljac": doc.supplier_name,
                            "erp": f"{tabela} {broj}",
                            "tamo_stoji": r.rn,
                            "tamo_dobavljac": r.dob,
                            "nas_dobavljac": nas or "?",
                        }
                    )
                    continue
                nalaz.u_redu += 1

    return nalaz
