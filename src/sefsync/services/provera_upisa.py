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

log = logging.getLogger(__name__)

STAVKE_ZA = {"ULKALKUL": "PRULKALK", "MKALKUL": "MPRULK"}


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
                broj = (red.erp_broj or "").strip()
                tabela = (red.tabela or "").strip() or "ULKALKUL"
                if not broj or "/" not in broj:
                    nalaz.nepotpun_zapis.append(
                        {"id": doc.id, "broj": doc.document_number, "erp": broj}
                    )
                    continue
                magacin, kalk = broj.split("/", 1)
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
                nas_broj = (doc.document_number or "").replace(" ", "").upper()
                erp_broj_racuna = (r.rn or "").replace(" ", "").upper()
                if erp_broj_racuna and nas_broj and erp_broj_racuna != nas_broj:
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
