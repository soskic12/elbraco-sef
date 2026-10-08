"""Model podataka aplikacije (nezavisan od ERP baze)."""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
    Unicode,
    UnicodeText,
)
from sqlalchemy.dialects import mssql

# Namerno Unicode, ne String: na MS SQL-u String daje VARCHAR, koji ne moze da
# sacuva cirilicu ("ЈП Водоканал Бечеј") ni nasa slova van kodne strane baze.
# Unicode daje NVARCHAR i ne zavisi od kolacije; na SQLite-u je svejedno.
String = Unicode
# NTEXT je zastareo u SQL Serveru, pa duga polja idu kao NVARCHAR(max).
Text = UnicodeText().with_variant(mssql.NVARCHAR(None), "mssql")
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


def utcnow() -> datetime:
    """Naivni UTC - sve kolone tipa DateTime cuvaju UTC bez timezone oznake."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# Sifarnici
# --------------------------------------------------------------------------- #


class UnitKind(str, enum.Enum):
    RETAIL = "maloprodaja"
    WAREHOUSE = "magacin"
    HQ = "uprava"
    SERVICE = "servis"
    OTHER = "ostalo"


class BusinessUnit(Base):
    """Poslovna jedinica: maloprodajni objekat, magacin, uprava..."""

    __tablename__ = "business_unit"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[UnitKind] = mapped_column(Enum(UnitKind), default=UnitKind.RETAIL)
    # Sifra iste PJ u ERP-u (za fazu 2 - magacin/skladiste na kalkulaciji)
    erp_code: Mapped[str | None] = mapped_column(String(32), default=None)
    # Prijave ljudi koji rade u ovoj jedinici, zarezom odvojene.
    #
    # `kontakti_osobe.SifraPovezanogObjekta` koristi i projekat LOGISTIKA, pa se
    # u njega ne sme upisivati nasa sema: sifra `010` tamo znaci nesto drugo, a
    # jednog dana ce biti i nova prodavnica. Zato vezu covek-jedinica drzimo kod
    # sebe, gde je mozemo menjati bez posledica po tudju aplikaciju.
    #
    # Kad je prazno, osoba se i dalje nalazi preko `SifraPovezanogObjekta` ->
    # `erp_code`, kako rade prodavnice.
    logins: Mapped[str | None] = mapped_column(String(500), default=None)

    def login_list(self) -> list[str]:
        return [x.strip().lower() for x in (self.logins or "").split(",") if x.strip()]
    address: Mapped[str | None] = mapped_column(String(300), default=None)
    city: Mapped[str | None] = mapped_column(String(100), default=None)
    postal_code: Mapped[str | None] = mapped_column(String(10), default=None)
    emails: Mapped[str | None] = mapped_column(String(500), default=None)  # zarezom odvojeno
    phones: Mapped[str | None] = mapped_column(String(300), default=None)  # za Viber/SMS
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Tehnicke jedinice (reklamacije, izlazne e-fakture, "USLUGA" prodajna mesta)
    # nisu odrediste isporuke - automatsko razvrstavanje ih preskace,
    # ali se mogu dodeliti rucno i mogu biti cilj pravila.
    routable: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    documents: Mapped[list[Document]] = relationship(back_populates="business_unit")

    def email_list(self) -> list[str]:
        return [e.strip() for e in (self.emails or "").split(",") if e.strip()]

    def phone_list(self) -> list[str]:
        return [p.strip() for p in (self.phones or "").split(",") if p.strip()]

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BusinessUnit {self.code} {self.name}>"


class DocumentType(str, enum.Enum):
    INVOICE = "Invoice"              # 380 - faktura
    CREDIT_NOTE = "CreditNote"       # 381 - knjizno odobrenje
    DEBIT_NOTE = "DebitNote"         # 383 - knjizno zaduzenje
    PREPAYMENT = "Prepayment"        # 386 - avansni racun
    OTHER = "Other"


class MatchField(str, enum.Enum):
    """Polje dokumenta nad kojim pravilo trazi."""

    DELIVERY_ADDRESS = "delivery_address"      # cac:Delivery/DeliveryLocation/Address
    DELIVERY_CITY = "delivery_city"
    DELIVERY_NAME = "delivery_name"            # naziv lokacije isporuke
    BUYER_ADDRESS = "buyer_address"            # AccountingCustomerParty adresa (kad nema Delivery)
    BUYER_CITY = "buyer_city"
    BUYER_REFERENCE = "buyer_reference"        # cbc:BuyerReference
    ORDER_REFERENCE = "order_reference"        # cac:OrderReference/cbc:ID
    CONTRACT_REFERENCE = "contract_reference"
    ADDITIONAL_REFERENCE = "additional_reference"  # cac:AdditionalDocumentReference/cbc:ID
    BILLING_REFERENCE = "billing_reference"        # cac:BillingReference -> broj ispravljene fakture
    # Koliko faktura dokument ispravlja: "nema" / "jedna" / "vise". Mereno na 923
    # odobrenja: tamo gde ostala pravila cute, "jedna" znaci povracaj (010) u
    # 90,2 %, a "vise" finansijsko odobrenje (088) u 87,5 %.
    BILLING_REFERENCE_KIND = "billing_reference_kind"
    ATTACHMENT_TEXT = "attachment_text"            # tekst iz PDF priloga
    NOTE = "note"                              # cbc:Note
    SUPPLIER_VAT = "supplier_vat"
    SUPPLIER_NAME = "supplier_name"
    DOCUMENT_NUMBER = "document_number"
    ITEM_TEXT = "item_text"                    # spojeni nazivi stavki
    ANY_TEXT = "any_text"                      # sva gornja polja spojena


class MatchOp(str, enum.Enum):
    EQUALS = "equals"
    CONTAINS = "contains"
    STARTSWITH = "startswith"
    REGEX = "regex"


class RoutingRule(Base):
    """Pravilo razvrstavanja dokumenta na poslovnu jedinicu.

    Pravila se primenjuju redom po `priority` (manji broj = ranije).
    Prvo pravilo koje se poklopi odredjuje PJ.
    """

    __tablename__ = "routing_rule"

    id: Mapped[int] = mapped_column(primary_key=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    field: Mapped[MatchField] = mapped_column(Enum(MatchField))
    op: Mapped[MatchOp] = mapped_column(Enum(MatchOp), default=MatchOp.CONTAINS)
    pattern: Mapped[str] = mapped_column(String(500))
    # Dodatno suzavanje: pravilo vazi samo za ovog dobavljaca (PIB)
    supplier_vat: Mapped[str | None] = mapped_column(String(20), default=None)
    # ... i/ili samo za jednu vrstu dokumenta (npr. rabatna knjizna odobrenja)
    document_type: Mapped[DocumentType | None] = mapped_column(
        Enum(DocumentType), default=None
    )
    business_unit_id: Mapped[int] = mapped_column(ForeignKey("business_unit.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    comment: Mapped[str | None] = mapped_column(String(300), default=None)
    # Koliko puta je pravilo odlucilo, i koliko puta je operater tu odluku
    # ispravio. Drugi broj je jedini pouzdan znak da pravilo ne valja.
    hits: Mapped[int] = mapped_column(Integer, default=0)
    misses: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    business_unit: Mapped[BusinessUnit] = relationship()

    @property
    def accuracy(self) -> float | None:
        """Udeo potvrdjenih odluka; None dok nema nijedne provere."""
        ukupno = self.hits + self.misses
        return None if ukupno == 0 else 100.0 * self.hits / ukupno

    __table_args__ = (Index("ix_rule_priority", "priority", "active"),)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<RoutingRule #{self.priority} {self.field.value} {self.op.value}>"


# --------------------------------------------------------------------------- #
# Dokumenti
# --------------------------------------------------------------------------- #


class SefStatus(str, enum.Enum):
    NEW = "New"
    SEEN = "Seen"
    RENOTIFIED = "ReNotified"
    APPROVED = "Approved"
    REJECTED = "Rejected"
    STORNO = "Storno"
    UNKNOWN = "Unknown"


class ProcessState(str, enum.Enum):
    """Interni tok obrade."""

    NEW = "new"                  # preuzet overview zapis
    FETCHED = "fetched"          # UBL preuzet i isparsiran
    ROUTED = "routed"            # dodeljena PJ
    UNASSIGNED = "unassigned"    # nije uspelo razvrstavanje - ceka operatera
    NOTIFIED = "notified"        # PJ obavestena
    ACCEPTED = "accepted"        # prihvacen na SEF-u
    REJECTED = "rejected"        # odbijen na SEF-u
    CALCULATED = "calculated"    # faza 2: kreirana ulazna kalkulacija
    ERROR = "error"


class RoutingSource(str, enum.Enum):
    RULE = "rule"
    SUPPLIER_DEFAULT = "supplier_default"
    MANUAL = "manual"
    SINGLE_UNIT = "single_unit"  # firma ima samo jednu aktivnu PJ
    NONE = "none"


class Document(Base):
    """Ulazni dokument sa SEF-a (faktura, KO, KZ, avans)."""

    __tablename__ = "document"

    id: Mapped[int] = mapped_column(primary_key=True)
    sef_invoice_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    glob_uniq_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    cir_invoice_id: Mapped[str | None] = mapped_column(String(32), default=None)

    document_number: Mapped[str | None] = mapped_column(String(100), index=True, default=None)
    document_type: Mapped[DocumentType] = mapped_column(Enum(DocumentType), default=DocumentType.INVOICE)
    invoice_type_code: Mapped[str | None] = mapped_column(String(8), default=None)

    supplier_name: Mapped[str | None] = mapped_column(String(300), default=None)
    supplier_vat: Mapped[str | None] = mapped_column(String(20), index=True, default=None)
    supplier_reg_no: Mapped[str | None] = mapped_column(String(20), default=None)

    amount: Mapped[float | None] = mapped_column(Float, default=None)          # ukupno sa PDV
    sum_without_vat: Mapped[float | None] = mapped_column(Float, default=None)
    vat_amount: Mapped[float | None] = mapped_column(Float, default=None)
    rounding_amount: Mapped[float | None] = mapped_column(Float, default=None)
    currency: Mapped[str | None] = mapped_column(String(3), default="RSD")
    # Faktura u stranoj valuti nosi i obracun u dinarima. Kalkulacija ide u
    # dinarima - bez ovoga bi 1.350,60 EUR otislo u ERP kao 1.350,60 dinara.
    sum_without_vat_rsd: Mapped[float | None] = mapped_column(Float, default=None)
    vat_amount_rsd: Mapped[float | None] = mapped_column(Float, default=None)

    issue_date: Mapped[date | None] = mapped_column(Date, default=None)
    delivery_date: Mapped[date | None] = mapped_column(Date, default=None)
    due_date: Mapped[date | None] = mapped_column(Date, default=None)
    prepayment_date: Mapped[date | None] = mapped_column(Date, default=None)
    sent_date: Mapped[datetime | None] = mapped_column(DateTime, default=None, index=True)

    sef_status: Mapped[SefStatus] = mapped_column(Enum(SefStatus), default=SefStatus.NEW, index=True)
    sef_version: Mapped[int | None] = mapped_column(Integer, default=None)
    state: Mapped[ProcessState] = mapped_column(Enum(ProcessState), default=ProcessState.NEW, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)

    # Razvrstavanje
    business_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("business_unit.id"), default=None, index=True
    )
    routing_source: Mapped[RoutingSource] = mapped_column(Enum(RoutingSource), default=RoutingSource.NONE)
    routing_rule_id: Mapped[int | None] = mapped_column(ForeignKey("routing_rule.id"), default=None)
    routing_note: Mapped[str | None] = mapped_column(String(500), default=None)
    # Covek je video predlog i prihvatio ga (prosledjivanjem ili izricito).
    routing_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    routing_confirmed_by: Mapped[str | None] = mapped_column(String(100), default=None)
    # Predlog je bio pogresan pa ga je operater ispravio - iz ovoga se uci.
    routing_corrected: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # Tekstualni "hint" podaci iz UBL-a nad kojima rade pravila (i analiza)
    delivery_name: Mapped[str | None] = mapped_column(String(300), default=None)
    delivery_address: Mapped[str | None] = mapped_column(String(300), default=None)
    delivery_city: Mapped[str | None] = mapped_column(String(120), default=None)
    buyer_address: Mapped[str | None] = mapped_column(String(300), default=None)
    buyer_city: Mapped[str | None] = mapped_column(String(120), default=None)
    buyer_reference: Mapped[str | None] = mapped_column(String(200), default=None)
    order_reference: Mapped[str | None] = mapped_column(String(200), default=None)
    contract_reference: Mapped[str | None] = mapped_column(String(200), default=None)
    # Neki dobavljaci ovde upisuju objekat (KIM-TEC: "Elbraco - Sombor").
    additional_reference: Mapped[str | None] = mapped_column(String(500), default=None)
    # Knjizno odobrenje pokazuje na fakturu koju ispravlja (cac:BillingReference).
    # Broj veza je signal koji deli dve vrste odobrenja: tacno jedna veza znaci
    # povracaj robe po jednoj fakturi (magacin 010), a nijedna ili vise njih
    # finansijsko odobrenje na promet (088). Mereno: 234 odluke, 85 % tacnih.
    billing_reference: Mapped[str | None] = mapped_column(String(500), default=None)
    billing_reference_count: Mapped[int] = mapped_column(Integer, default=0)
    # Period na koji se dokument odnosi (cac:InvoicePeriod). Potreban je kod
    # obavestenja o prethodnom porezu: kad odobrenje pokriva vise faktura, SEF
    # trazi pocetak i kraj perioda kao OBAVEZNA polja - bez njih vraca 400.
    period_start: Mapped[date | None] = mapped_column(Date, default=None)
    period_end: Mapped[date | None] = mapped_column(Date, default=None)
    # Ziro racun dobavljaca. Kod javnih preduzeca SEF salje identifikator koji
    # nije maticni broj (Srbijagas: 86132 umesto 20084600), pa je racun jedini
    # pouzdan nacin da se dobavljac nadje u NAZIVI - popunjen je na 37.411 od
    # 44.949 redova, naspram 3.351 sa maticnim brojem.
    payment_account: Mapped[str | None] = mapped_column(String(64), default=None)
    # Tekst PDF priloga - CANDY objekat upisuje samo tamo ("Poslovnica B000067561").
    attachment_text: Mapped[str | None] = mapped_column(Text, default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)

    ubl_path: Mapped[str | None] = mapped_column(String(400), default=None)
    # Kad je fajl premesten u arhivu. Razlika izmedju „nikad nije ni bio“ i
    # „sklonjen namerno“; sadrzaj se po potrebi skida ponovo sa SEF-a.
    ubl_arhiviran: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    pdf_path: Mapped[str | None] = mapped_column(String(400), default=None)
    raw_overview: Mapped[dict | None] = mapped_column(JSON, default=None)

    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    accepted_by: Mapped[str | None] = mapped_column(String(100), default=None)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    # "Nepregledano" kod nas, umesto statusa "Nova" na SEF-u. Preuzimanje UBL-a
    # obara "Nova" na portalu, a bez UBL-a se razvrsta samo 15 % dokumenata
    # (naspram 79 % sa njim) - mereno 17.09. na 4.000 dokumenata. Zato oznaku
    # vodimo sami: kolege je vide u panelu, a app sme da preuzme UBL odmah.
    pregledano_at: Mapped[datetime | None] = mapped_column(DateTime, default=None, index=True)
    pregledano_by: Mapped[str | None] = mapped_column(String(100), default=None)

    # Tok rada operatera: dokument ceka u redu dok ga operater ne prosledi
    # poslovnoj jedinici, a poslovodja potvrdjuje da je roba stigla.
    forwarded_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    forwarded_by: Mapped[str | None] = mapped_column(String(100), default=None)
    received_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    received_by: Mapped[str | None] = mapped_column(String(100), default=None)
    received_note: Mapped[str | None] = mapped_column(String(500), default=None)
    # Istorija preuzeta pre pustanja u rad ne trazi nicije odobrenje.
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    business_unit: Mapped[BusinessUnit | None] = relationship(back_populates="documents")
    lines: Mapped[list[DocumentLine]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="DocumentLine.line_no"
    )
    notifications: Mapped[list[Notification]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    @property
    def po_starom(self) -> bool:
        """Dokument koji je u panel stigao pre prelaska na novi nacin rada.

        Za njega kalkulaciju pravi covek u ERP-u, kao i do sada, pa panel ne
        nudi ni potvrdu prijema ni upis. Granica je dan iz `NOVI_TOK_OD`; bez
        nje nista nije po starom.
        """
        from .config import get_settings

        granica = get_settings().novi_tok_od
        if granica is None:
            return False
        return self.created_at is not None and self.created_at.date() < granica

    @property
    def nepregledan(self) -> bool:
        """Niko ga jos nije video - ono sto je na portalu bio status "Nova".

        Prosledjen dokument se racuna kao vidjen i kad oznaka nije upisana: ta
        polja postoje tek od 17.09., a istorija je prosledjivana i pre toga. Bez
        ovoga bi prikaz "Nepregledano" prvog dana pokazao ceo arhiv.
        """
        return self.pregledano_at is None and self.forwarded_at is None

    @property
    def is_credit_note(self) -> bool:
        return self.document_type is DocumentType.CREDIT_NOTE

    def routing_fields(self) -> dict[str, str | None]:
        """Polja nad kojima rade pravila - isto sto i `UblDocument.routing_fields`.

        Jedno mesto namerno: spisak je ranije postojao i u ingestu i u merenju, pa
        je novo polje upisano u jedan a u drugom je nedostajalo. Merenje je onda
        pokazalo da novo pravilo "nista ne menja" - a ono se prosto nije okinulo.
        """
        from .ubl.parser import vrsta_veze

        return {
            "delivery_address": self.delivery_address,
            "delivery_city": self.delivery_city,
            "delivery_name": self.delivery_name,
            "buyer_address": self.buyer_address,
            "buyer_city": self.buyer_city,
            "buyer_reference": self.buyer_reference,
            "order_reference": self.order_reference,
            "contract_reference": self.contract_reference,
            "additional_reference": self.additional_reference,
            "billing_reference": self.billing_reference,
            "billing_reference_kind": vrsta_veze(self.billing_reference_count),
            "attachment_text": self.attachment_text,
            "note": self.note,
            "supplier_vat": self.supplier_vat,
            "supplier_name": self.supplier_name,
            "document_number": self.document_number,
            "item_text": " | ".join(
                [x.name for x in self.lines if x.name] + [x.note for x in self.lines if x.note]
            ) or None,
            "document_type": self.document_type.value,
        }

    @property
    def sef_decided(self) -> bool:
        """Na SEF-u je odluceno - prihvacena, odbijena ili stornirana."""
        return self.sef_status in (SefStatus.APPROVED, SefStatus.REJECTED, SefStatus.STORNO)

    @property
    def needs_action(self) -> bool:
        """Stoji u redu operatera dok se i SEF i prosledjivanje ne zavrse."""
        if self.archived:
            return False
        return not self.sef_decided or self.forwarded_at is None

    @property
    def bez_identifikacije(self) -> bool:
        """Faktura bez PIB-a i bez maticnog broja nije ispravna.

        Bez oba se dobavljac ne moze pouzdano prepoznati - ni u nasem sifarniku
        (NAZIVI), ni u mapiranju artikala, ni u poreskoj evidenciji. Takav
        dokument ne sme dalje od operatera: on ga resava sa dobavljacem ili
        odbija na SEF-u, a ne prosledjuje poslovodji.
        """
        return not (self.supplier_vat or "").strip() and not (self.supplier_reg_no or "").strip()

    @property
    def ubl_pending(self) -> bool:
        """UBL namerno nije preuzet, da se ne obori status "Nova" na SEF-u.

        Takav dokument se u redu vidi iz pregleda (dobavljac, iznos, datum),
        ali nema stavke ni adresu isporuke dok ga neko ne otvori na portalu
        ili dok se SEF_PRESERVE_NEW ne ugasi.
        """
        return self.ubl_path is None and self.sef_status is SefStatus.NEW

    @property
    def waiting_for_unit(self) -> bool:
        """Prosledjen poslovnoj jedinici, ceka potvrdu da je roba stigla."""
        return self.forwarded_at is not None and self.received_at is None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Document {self.sef_invoice_id} {self.document_number}>"


class DocumentLine(Base):
    """Stavka dokumenta - puni se iz UBL-a, osnova za fazu 2 (ulazna kalkulacija)."""

    __tablename__ = "document_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id", ondelete="CASCADE"), index=True)

    line_no: Mapped[int] = mapped_column(Integer)           # pozicija u dokumentu
    line_ref: Mapped[str | None] = mapped_column(String(64), default=None)  # cbc:ID dobavljaca
    name: Mapped[str | None] = mapped_column(String(500), default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    sellers_item_id: Mapped[str | None] = mapped_column(String(100), default=None)
    buyers_item_id: Mapped[str | None] = mapped_column(String(100), default=None)
    standard_item_id: Mapped[str | None] = mapped_column(String(100), default=None)  # GTIN/EAN
    quantity: Mapped[float | None] = mapped_column(Float, default=None)
    unit_code: Mapped[str | None] = mapped_column(String(16), default=None)      # UN/ECE Rec 20 (H87, KGM...)
    price: Mapped[float | None] = mapped_column(Float, default=None)             # jed. cena bez PDV
    base_quantity: Mapped[float | None] = mapped_column(Float, default=None)
    line_amount: Mapped[float | None] = mapped_column(Float, default=None)       # osnovica stavke
    allowance_amount: Mapped[float | None] = mapped_column(Float, default=None)  # rabat
    charge_amount: Mapped[float | None] = mapped_column(Float, default=None)
    vat_percent: Mapped[float | None] = mapped_column(Float, default=None)
    vat_category: Mapped[str | None] = mapped_column(String(8), default=None)    # S, AE, O, Z, E...
    note: Mapped[str | None] = mapped_column(Text, default=None)

    # Faza 2: mapiranje na ERP artikal
    erp_item_code: Mapped[str | None] = mapped_column(String(50), default=None)
    erp_mapping_source: Mapped[str | None] = mapped_column(String(30), default=None)
    # Stavka koja nije artikal i nikad nece imati nasu sifru: "Odobrenje po
    # Saglasnosti broj 277102/2026" (Eurotehna), troskovi transporta i slicno.
    # Bez ovoga takva stavka zauvek stoji na spisku "ceka sifru" i zatrpava ga.
    nije_artikal: Mapped[bool] = mapped_column(Boolean, default=False)
    nije_artikal_by: Mapped[str | None] = mapped_column(String(100), default=None)

    document: Mapped[Document] = relationship(back_populates="lines")

    __table_args__ = (UniqueConstraint("document_id", "line_no", name="uq_line_no"),)


class ItemMapping(Base):
    """Faza 2: veza sifre artikla dobavljaca -> sifra artikla u ERP-u."""

    __tablename__ = "item_mapping"

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_vat: Mapped[str] = mapped_column(String(20), index=True)
    supplier_item_id: Mapped[str | None] = mapped_column(String(100), default=None)
    barcode: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    supplier_item_name: Mapped[str | None] = mapped_column(String(500), default=None)
    erp_item_code: Mapped[str] = mapped_column(String(50))
    unit_factor: Mapped[float] = mapped_column(Float, default=1.0)  # dobavljac salje kutije, ERP komade
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # Odakle veza: "barkod", "kalkulacija", "oba" (dva nezavisna puta se slozila),
    # "rucno". "oba" je najjaci - mereno 99,7% slaganja na 1.756 parova.
    source: Mapped[str | None] = mapped_column(String(20), default=None)
    # Iz koliko zaknjizenih kalkulacija je izvedeno. Jedan nalaz nije dokaz.
    evidence: Mapped[int] = mapped_column(Integer, default=0)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    confirmed_by: Mapped[str | None] = mapped_column(String(100), default=None)
    # Isti brojaci kao kod pravila razvrstavanja: bez njih se ne vidi koje
    # mapiranje je pogresno postavljeno, a dobavljaci umeju da recikliraju sifre.
    hits: Mapped[int] = mapped_column(Integer, default=0)
    misses: Mapped[int] = mapped_column(Integer, default=0)

    @property
    def pouzdano(self) -> bool:
        """Sme li da se koristi bez ljudske potvrde."""
        if self.confirmed_at is not None:
            return True
        return self.source == "oba" or (self.source == "kalkulacija" and self.evidence >= 2)

    __table_args__ = (Index("ix_item_map_lookup", "supplier_vat", "supplier_item_id"),)


# --------------------------------------------------------------------------- #
# Notifikacije / audit / sync
# --------------------------------------------------------------------------- #


class NotifyChannel(str, enum.Enum):
    EMAIL = "email"
    PUSH = "push"
    DASHBOARD = "dashboard"


class NotifyStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class Notification(Base):
    __tablename__ = "notification"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id", ondelete="CASCADE"), index=True)
    business_unit_id: Mapped[int | None] = mapped_column(ForeignKey("business_unit.id"), default=None)
    channel: Mapped[NotifyChannel] = mapped_column(Enum(NotifyChannel))
    target: Mapped[str | None] = mapped_column(String(300), default=None)
    status: Mapped[NotifyStatus] = mapped_column(Enum(NotifyStatus), default=NotifyStatus.PENDING)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    document: Mapped[Document] = relationship(back_populates="notifications")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(100), default="system")
    action: Mapped[str] = mapped_column(String(60))
    document_id: Mapped[int | None] = mapped_column(ForeignKey("document.id"), default=None, index=True)
    detail: Mapped[str | None] = mapped_column(Text, default=None)


class SyncRun(Base):
    __tablename__ = "sync_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    date_from: Mapped[date | None] = mapped_column(Date, default=None)
    date_to: Mapped[date | None] = mapped_column(Date, default=None)
    seen: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    message: Mapped[str | None] = mapped_column(Text, default=None)


class Setting(Base):
    """Trajne vrednosti (npr. poslednji uspesan sync)."""

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, default=None)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class StanjeNacrta(str, enum.Enum):
    """Dokle je stigla kalkulacija koju aplikacija sklapa."""

    SPREMAN = "spreman"          # sve stoji, moze u ERP cim poslovodja potvrdi prijem
    CEKA_SIFRU = "ceka_sifru"    # fali nasa sifra za jednu ili vise stavki
    PROBLEM = "problem"          # nesto drugo (dobavljac van sifarnika, sifra bez artikla)
    UPISAN = "upisan"            # vec je u ERP-u
    # Avansni racun ne prolazi kroz kalkulaciju - mereno 18.09.: nijedan od 39
    # avansa u istoriji nema kalkulaciju u ERP-u, idu pravo racunovodstvu.
    NE_TREBA = "ne_treba"


class NacrtKalkulacije(Base):
    """Stanje kalkulacije koju aplikacija sklapa za jedan dokument.

    Sam nacrt se uvek moze izvesti iznova iz dokumenta i ERP-a, ali to je desetak
    upita po dokumentu - prespro za ekran koji se osvezava. Zato se ovde cuva
    ishod poslednjeg racunanja, a puni nacrt se pravi tek kad zatreba.

    Ono sto covek uradi ne cuva se ovde nego tamo gde i pripada: veza sifre ide u
    `item_mapping`, pa vredi i za sve buduce fakture istog dobavljaca.
    """

    __tablename__ = "nacrt_kalkulacije"

    document_id: Mapped[int] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), primary_key=True
    )
    stanje: Mapped[StanjeNacrta] = mapped_column(
        Enum(StanjeNacrta), default=StanjeNacrta.PROBLEM, index=True
    )
    magacin: Mapped[str | None] = mapped_column(String(10), default=None)
    tabela: Mapped[str | None] = mapped_column(String(20), default=None)   # ULKALKUL / MKALKUL
    iznos: Mapped[float | None] = mapped_column(Float, default=None)
    # Sta zaustavlja, u obliku u kom se prikazuje coveku.
    problemi: Mapped[list | None] = mapped_column(JSON, default=None)
    # Po jedna stavka koja nema nasu sifru: line_no, naziv, sifra dobavljaca, EAN...
    nemapirane: Mapped[list | None] = mapped_column(JSON, default=None)
    # Ono sto ne zaustavlja upis, ali covek treba da pogleda pre knjizenja.
    # Za sada: ID PDV evidencije kod dobavljaca kod kog se javlja i 21.
    napomene: Mapped[list | None] = mapped_column(JSON, default=None)
    racunato_u: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    # Upis u ERP (faza 2): broj kalkulacije i ko je pustio
    erp_broj: Mapped[str | None] = mapped_column(String(20), default=None)
    # Zasto mislimo da je to ta kalkulacija - broj odsecen na 15 znakova, druga
    # sifra istog dobavljaca... Stoji odvojeno jer `erp_broj` je varchar(20).
    erp_napomena: Mapped[str | None] = mapped_column(String(250), default=None)
    upisano_u: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    upisao: Mapped[str | None] = mapped_column(String(100), default=None)

    # Predlog po iznosu i datumu kad se broj racuna ne poklapa (ljudska greska u
    # kucanju). Nikad ne sklanja fakturu sam - ceka da covek kaze jeste ili nije.
    sumnja: Mapped[dict | None] = mapped_column(JSON, default=None)
    sumnja_odbio: Mapped[str | None] = mapped_column(String(100), default=None)
    sumnja_odbijeno_u: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    # Nesto sto je covek morao da uradi u ERP-u jer app ne sme sama. Za sada
    # jedan slucaj: dokument je prebacen na drugu jedinicu POSLE upisa, pa
    # kalkulacija stoji u pogresnom magacinu. App je ne dira - samo javi.
    upozorenje: Mapped[str | None] = mapped_column(String(250), default=None)

    # Sifra je ispravljena POSLE sto je kalkulacija vec otisla u ERP. App tamo
    # ne sme nista da menja (ima samo INSERT), pa ovde stoji sta tacno treba
    # ispraviti i u kakvom je stanju kalkulacija bila u tom trenutku:
    # {stara, nova, naziv_stara, naziv_nova, kalkulacija, proknjizeno,
    #  trazio, u}. Stoji dok covek ne kaze da je ispravljeno.
    ispravka: Mapped[dict | None] = mapped_column(JSON, default=None)
    ispravka_zatvorio: Mapped[str | None] = mapped_column(String(100), default=None)
    ispravka_zatvorena_u: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    document: Mapped[Document] = relationship()

    @property
    def broj_nemapiranih(self) -> int:
        return len(self.nemapirane or [])


class Slika(Base):
    """Slika koju je poslovodja poslao uz dokument - najcesce ambalaza novog artikla.

    Do sada su te slike isle u Viber grupu, gde nisu vezane ni za sta: covek koji
    otvara sifru mora sam da spoji sliku sa fakturom. Viber nema API za grupe, pa
    se slika prima tamo gde joj je mesto - uz stavku kojoj fali nasa sifra.

    Fajl stoji na disku (`STORAGE_DIR/slike/<godina>/<mesec>/`), u bazi je samo
    putanja - isto kao UBL i PDF prilozi.
    """

    __tablename__ = "slika"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), index=True
    )
    # Koja stavka, kad se zna - tako Dusan vidi sliku bas uz artikal koji otvara.
    line_no: Mapped[int | None] = mapped_column(Integer, default=None)
    putanja: Mapped[str] = mapped_column(String(400))
    naziv_fajla: Mapped[str | None] = mapped_column(String(260), default=None)
    # Slika je o ARTIKLU, a ne o fakturi: isti artikal stize na vise objekata, pa
    # kad ga jedan poslovodja slika, ostali to moraju da vide i da ne slikaju
    # ponovo. Zato se prepisuje ono po cemu se artikal prepoznaje.
    ean: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    sifra_dobavljaca: Mapped[str | None] = mapped_column(String(100), default=None)
    supplier_vat: Mapped[str | None] = mapped_column(String(20), default=None)
    naziv_artikla: Mapped[str | None] = mapped_column(String(300), default=None)
    napomena: Mapped[str | None] = mapped_column(String(300), default=None)
    poslao: Mapped[str] = mapped_column(String(100))
    poslato_u: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    document: Mapped[Document] = relationship()


class OdlukaManjka(str, enum.Enum):
    """Sta je dobavljac rekao kad je stiglo manje nego sto faktura kaze."""

    KASNIJE = "kasnije"      # dovoze sledecom turom
    STORNO = "storno"        # salju ispravljenu fakturu
    OPERATER = "operater"    # poslovodja ne zna, neka odluci operater


class PrijavaManjka(Base):
    """Poslovodja je otvorio kamion i stiglo je manje nego sto faktura kaze.

    Polje `Document.received_note` za ovo ne valja: postoji od pocetka i u
    7.727 dokumenata nije popunjeno **nijednom**. Slobodan tekst se ne
    popunjava, a i da se popuni, ni upis kalkulacije ni kasnije knjizno
    odobrenje ne mogu nista s njim - zato manjak ide po stavkama.

    Sta app radi zavisi od `odluka`:

    * `KASNIJE` - kalkulacija se upisuje za **punu fakturu**. Kalkulacija za
      manju kolicinu ne bi se slagala ni sa `PDVEVIDX` ni sa saldom
      dobavljaca, a ostatak ne bi imao uz sta da se knjizi. Upis ne pomera
      zalihu (dira samo zaglavlje, stavke i `PDVEVIDX`, nikad `ARTPROD`), pa
      kalkulacija sme da stoji neproknjizena koliko treba - crvena linija na
      ekranu kaze poslovodji da ne knjizi dok ostatak ne dodje.
    * `STORNO` i `OPERATER` - **ne upisuje se nista**, faktura tek treba da
      nestane ili da se o njoj odluci.

    Mereno 26.09.: odbijenih na SEF-u 7, storniranih 32, knjiznih odobrenja
    967 - treci put je daleko najcesci.
    """

    __tablename__ = "prijava_manjka"

    document_id: Mapped[int] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), primary_key=True
    )
    # Po jedna stavka koja ne valja: line_no, naziv, fakturisano, stiglo.
    stavke: Mapped[list] = mapped_column(JSON, default=list)
    odluka: Mapped[OdlukaManjka] = mapped_column(Enum(OdlukaManjka))
    prijavio: Mapped[str] = mapped_column(String(100))
    prijavljeno_u: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # Trag zatvaranja: ostatak stigao, knjizno odobrenje, ili storno.
    zatvorio: Mapped[str | None] = mapped_column(String(100), default=None)
    zatvoreno_u: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    zatvoreno_kako: Mapped[str | None] = mapped_column(String(60), default=None)

    document: Mapped[Document] = relationship()

    @property
    def otvoren(self) -> bool:
        return self.zatvoreno_u is None

    @property
    def sazetak(self) -> str:
        """„stiglo 9 od 10“ - ono sto stoji u crvenoj liniji."""
        delovi = [
            f"{s.get('naziv', '')[:28]}: stiglo {s.get('stiglo')} od {s.get('fakturisano')}"
            for s in (self.stavke or [])
        ]
        return "; ".join(delovi)


class ObavestenjeOPrethodnomPorezu(Base):
    """Obavestenje koje kupac salje dobavljacu uz knjizno odobrenje.

    Njime se javlja koliko PDV-a nije koristeno kao prethodni porez. Na SEF
    portalu je to dugme „Kreiraj obavestenje o prethodnom porezu“ na samom
    dokumentu; ovde se pamti da je poslato, sa brojem koji je SEF vratio.

    Broj obavestenja je **broj samog knjiznog odobrenja**, doslovno - tako je
    Bane trazio (30.09.), i tako se uvek vidi na sta se odnosi.

    Slanje je prema dobavljacu i nepovratno, pa se cuva i trag ko ga je pustio.
    """

    __tablename__ = "obavestenje_prethodni_porez"

    document_id: Mapped[int] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), primary_key=True
    )
    # Broj koji smo poslali = broj odobrenja; SEF vraca svoj Id i status.
    broj: Mapped[str] = mapped_column(String(200))
    sef_notice_id: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str | None] = mapped_column(String(30), default=None)
    iznos_pdv: Mapped[float | None] = mapped_column(Float, default=None)
    vrsta_veze: Mapped[str | None] = mapped_column(String(50), default=None)
    poslao: Mapped[str] = mapped_column(String(100))
    poslato_u: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    greska: Mapped[str | None] = mapped_column(String(500), default=None)

    # Kad slanje padne, covek obavestenje napravi na portalu - to se belezi
    # ovde. Bez toga bi izvestaj zauvek prijavljivao gresku za nesto sto je
    # odavno zavrseno, a neko bi pre ili kasnije kliknuo "posalji ponovo" i
    # poslao dobavljacu drugo obavestenje (Bane, 07.10.).
    rucno_potvrdio: Mapped[str | None] = mapped_column(String(100), default=None)
    rucno_u: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    document: Mapped[Document] = relationship()

    @property
    def uspelo(self) -> bool:
        return self.sef_notice_id is not None and not self.greska

    @property
    def zavrseno(self) -> bool:
        """Posla li ga app ili covek rucno - svejedno, dobavljac ga ima."""
        return self.uspelo or self.rucno_u is not None


class PopustZaPrenos(Base):
    """Naknadni popust koji je dobavljac odobrio za jedan uredjaj.

    Knjizno odobrenje ume da bude dve razlicite stvari. Kad dobavljac odobri ceo
    iznos uredjaja, roba se vraca i ide na 010. Kad odobri samo deo - jer je
    uredjaj stigao ostecen - roba ostaje kod nas i prodaje se jeftinije, pa
    odobrenje ide na 088 kao finansijsko.

    Problem je sto to drugo zavrsi na 088, gde ga Ilijana ne vidi, a ona je ta
    koja popust prenosi objektu koji taj ostecen uredjaj prodaje. Bane, 24.09.:
    "takav dokument po meni treba da zavrsi u 088, ali da ilijana o istom bude
    obavestena, jer taj dodatni popust ona dalje treba da prenese u mpo koji
    prodaje takav ostecen uredjaj".

    Zato se takvo odobrenje ovde zapise i ostane otvoreno dok se ne prenese -
    mejl kaze da je stiglo, a ekran cuva trag sta je uradjeno.
    """

    __tablename__ = "popust_za_prenos"

    document_id: Mapped[int] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), primary_key=True
    )
    # Uredjaj na koji se popust odnosi - nadjen preko broja fakture iz napomene.
    erp_item_code: Mapped[str | None] = mapped_column(String(50), default=None)
    naziv_artikla: Mapped[str | None] = mapped_column(String(300), default=None)
    # Iznos odobrenja i nabavna cena tog uredjaja: odnos je ono sto razdvaja
    # popust od povrata, pa stoji uz zapis da se moze proveriti.
    iznos: Mapped[float | None] = mapped_column(Float, default=None)
    nabavna: Mapped[float | None] = mapped_column(Float, default=None)
    broj_racuna: Mapped[str | None] = mapped_column(String(100), default=None)
    baza_godine: Mapped[str | None] = mapped_column(String(40), default=None)
    otkriveno_u: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # Trag: kome je popust prenet i kada.
    preneto_u_objekat: Mapped[str | None] = mapped_column(String(20), default=None)
    preneto_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    preneto_by: Mapped[str | None] = mapped_column(String(100), default=None)
    napomena: Mapped[str | None] = mapped_column(String(500), default=None)

    document: Mapped[Document] = relationship()

    @property
    def otvoren(self) -> bool:
        return self.preneto_at is None

    @property
    def udeo(self) -> float | None:
        """Koliki deo vrednosti uredjaja je odobren - 0,37 znaci 37 %."""
        if not self.nabavna or self.iznos is None:
            return None
        return abs(self.iznos) / abs(self.nabavna)
