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
    note: Mapped[str | None] = mapped_column(Text, default=None)

    ubl_path: Mapped[str | None] = mapped_column(String(400), default=None)
    pdf_path: Mapped[str | None] = mapped_column(String(400), default=None)
    raw_overview: Mapped[dict | None] = mapped_column(JSON, default=None)

    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    accepted_by: Mapped[str | None] = mapped_column(String(100), default=None)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

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
    def is_credit_note(self) -> bool:
        return self.document_type is DocumentType.CREDIT_NOTE

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
