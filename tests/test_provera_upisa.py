"""Da li kalkulacija koju panel smatra upisanom još postoji u ERP-u.

Povod je `kp-2026248-POV` (#8279, 09.10.2026.): panel je pisao `088/03205`,
Bogdan je kalkulaciju obrisao u ERP-u, a taj broj je u međuvremenu dobilo
MISON-ovo odobrenje `26-2204-239378`. Panel je pokazivao na tuđi dokument i
tvrdio da je posao završen — a dokument se zbog toga nije mogao ni vratiti.
"""

from __future__ import annotations

from sefsync.services.provera_upisa import proveri


class LazniRed:
    """Red iz ERP-a, onoliko koliko provera čita."""

    def __init__(self, dob, rn):
        self.dob = dob
        self.rn = rn


class LazniErp:
    """Vraća unapred pripremljen red za (magacin, broj), ili ništa."""

    def __init__(self, redovi):
        self.redovi = redovi

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, upit, parametri=None):
        kljuc = (parametri or {}).get("m"), (parametri or {}).get("b")
        red = self.redovi.get(kljuc)

        class R:
            def first(self_):
                return red

        return R()


def _nacrt(db, *, doc_id, broj, pib, erp_broj, ime="Dobavljač"):
    from sefsync.models import Document, NacrtKalkulacije, StanjeNacrta

    with db.session_scope() as session:
        session.add(Document(
            id=doc_id, sef_invoice_id=900000 + doc_id, document_number=broj,
            supplier_vat=pib, supplier_name=ime,
        ))
        session.flush()
        session.add(NacrtKalkulacije(
            document_id=doc_id, stanje=StanjeNacrta.UPISAN,
            erp_broj=erp_broj, tabela="ULKALKUL", upisao="bogdan",
        ))


def test_obrisana_kalkulacija_se_prijavljuje(db, monkeypatch):
    from sefsync.services import provera_upisa as modul

    _nacrt(db, doc_id=1, broj="kp-2026248-POV", pib="101599656", erp_broj="088/03205")
    monkeypatch.setattr(
        "sefsync.erp.kalkulacija._analitika_dobavljaca", lambda *a, **k: "00908"
    )

    nalaz = proveri(engine=LazniErp({}))   # u ERP-u nema nicega

    assert nalaz.provereno == 1
    assert len(nalaz.nema_je) == 1
    assert nalaz.nema_je[0]["broj"] == "kp-2026248-POV"
    assert nalaz.sumnjivih == 1


def test_broj_koji_je_dobio_neko_drugi(db, monkeypatch):
    """Oslobođeni broj kalkulacije ERP s vremenom dodeli drugom dokumentu."""
    from sefsync.services import provera_upisa as modul

    _nacrt(db, doc_id=2, broj="kp-2026248-POV", pib="101599656", erp_broj="088/03205")
    monkeypatch.setattr(
        "sefsync.erp.kalkulacija._analitika_dobavljaca", lambda *a, **k: "00908"
    )

    erp = LazniErp({("088", "03205"): LazniRed("50457", "26-2204-239378")})
    nalaz = proveri(engine=erp)

    assert len(nalaz.tudja) == 1
    assert nalaz.tudja[0]["tamo_stoji"] == "26-2204-239378"
    assert nalaz.tudja[0]["tamo_dobavljac"] == "50457"


def test_kalkulacija_koja_je_tamo_gde_treba_ne_smeta(db, monkeypatch):
    _nacrt(db, doc_id=3, broj="kp-2026250-POV", pib="101599656", erp_broj="010/00633")
    monkeypatch.setattr(
        "sefsync.erp.kalkulacija._analitika_dobavljaca", lambda *a, **k: "00908"
    )

    erp = LazniErp({("010", "00633"): LazniRed("00908", "kp-2026250-POV")})
    nalaz = proveri(engine=erp)

    assert nalaz.u_redu == 1
    assert nalaz.sumnjivih == 0


def test_razlika_u_velicini_slova_nije_greska(db, monkeypatch):
    """U ERP-u se broj kuca rukom: `26-fa001011891` naspram `26-FA001011891`."""
    _nacrt(db, doc_id=4, broj="26-FA001011891", pib="105874681", erp_broj="088/03328")
    monkeypatch.setattr(
        "sefsync.erp.kalkulacija._analitika_dobavljaca", lambda *a, **k: "08589"
    )

    erp = LazniErp({("088", "03328"): LazniRed("08589", "26-fa001011891")})
    nalaz = proveri(engine=erp)

    assert nalaz.u_redu == 1
    assert nalaz.sumnjivih == 0


def test_zapis_koji_nosi_ime_tabele_se_razlaze(db, monkeypatch):
    """Kad app prepozna kalkulaciju koju je čovek uneo, zapis glasi
    „ULKALKUL 010/00620" — sa imenom tabele ispred.

    Prvo izvođenje provere je cepalo samo po „/" i tražilo magacin
    „ULKALKUL 010", pa je prijavilo 106 nepostojećih kalkulacija kojih nema
    jer ih nije ni tražilo gde treba.
    """
    _nacrt(db, doc_id=5, broj="26-3RF-003125", pib="100042618",
           erp_broj="ULKALKUL 010/00620")
    monkeypatch.setattr(
        "sefsync.erp.kalkulacija._analitika_dobavljaca", lambda *a, **k: "01234"
    )

    erp = LazniErp({("010", "00620"): LazniRed("01234", "26-3RF-003125")})
    nalaz = proveri(engine=erp)

    assert nalaz.u_redu == 1
    assert nalaz.sumnjivih == 0


def test_broj_skracen_u_ERP_u_nije_tudji(db, monkeypatch):
    """ERP čuva broj u varchar(20), pa ljudi izbacuju crtice i seku kraj:
    `SAODSO1002/2026-06256_Garden` postane `SAODSO1002202606256G`."""
    _nacrt(db, doc_id=6, broj="SAODSO1002/2026-06256_Garden", pib="100000001",
           erp_broj="088/03195")
    monkeypatch.setattr(
        "sefsync.erp.kalkulacija._analitika_dobavljaca", lambda *a, **k: "50041"
    )

    erp = LazniErp({("088", "03195"): LazniRed("50041", "SAODSO1002202606256G")})
    nalaz = proveri(engine=erp)

    assert nalaz.u_redu == 1
    assert nalaz.sumnjivih == 0
