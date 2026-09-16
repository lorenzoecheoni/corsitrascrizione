"""Strict interchange models for the Academy intermediate report v2."""

import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .intermediate_models import UnitConfidence, parse_hms
from .models import InterventionKind, ReportModel


V2_AREAS = frozenset({
    "Governance", "Fiscalità", "Passaggio generazionale", "Patrimonio", "Compliance",
})
Access = Literal["pubblico", "iscritti"]
VerificationLevelV2 = Literal["bloccante", "avviso"]
VerificationCodeV2 = Literal[
    "RELATORE_NON_IDENTIFICATO", "TEMPI_INCOERENTI",
    "RELATORE_NON_NEL_REGISTRO", "INTERVENTO_BREVE",
    "CONFIDENZA_BASSA", "MATERIALE_NON_RAGGIUNGIBILE",
    "INTERVENTO_LUNGO", "SLIDE_NON_ABBINATA", "ALIAS_RELATORE_AMBIGUO",
    "RELATORE_INFERITO", "RELATORE_NON_COERENTE", "ANTEPRIMA_ASSENTE",
    "INTERVENTO_SENZA_SLIDE", "RELATORE_SENZA_MATERIALE",
]

_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_DIDACTIC_KINDS = {"intervento", "domande"}


class FaqV2(ReportModel):
    domanda: str
    risposta: str


class CourseV2(ReportModel):
    codice: str = Field(min_length=1)
    titolo: str
    sottotitolo: str
    area: str
    sinossi: str
    competenze: list[str] = Field(min_length=5, max_length=6)
    profili: list[str] = Field(min_length=3, max_length=3)
    faq: list[FaqV2] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def validate_editorial_fields(self) -> "CourseV2":
        if self.area not in V2_AREAS:
            raise ValueError("area non ammessa")
        if any("|" not in profile for profile in self.profili):
            raise ValueError("ogni profilo deve usare il formato Categoria | frase")
        return self


class SpeakerV2(ReportModel):
    slug: str
    nome: str
    ruolo: str | None = None
    organizzazione: str | None = None
    confidenza: UnitConfidence

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        if not _SLUG.fullmatch(value):
            raise ValueError("slug non valido")
        return value


class BlockRefV2(ReportModel):
    id: str
    titolo: str


class InterventionV2(ReportModel):
    id: str
    inizio: str
    fine: str
    inizio_secondi: int = Field(ge=0)
    fine_secondi: int = Field(ge=0)
    tipo: InterventionKind
    relatori: list[str] = Field(default_factory=list)
    blocco: BlockRefV2 | None = None
    titolo: str
    titolo_slide: str | None = None
    descrizione: str | None = None
    sintesi: str
    punti_chiave: list[str] = Field(default_factory=list)
    casi: list[str] = Field(default_factory=list)
    riferimenti: list[str] = Field(default_factory=list)
    accesso: Access
    confidenza: UnitConfidence

    @model_validator(mode="after")
    def validate_segment(self) -> "InterventionV2":
        start, end = parse_hms(self.inizio), parse_hms(self.fine)
        if start != self.inizio_secondi or end != self.fine_secondi:
            raise ValueError("i secondi devono corrispondere ai tempi h:mm:ss")
        if end <= start:
            raise ValueError("fine deve essere successiva a inizio")
        if self.tipo in _DIDACTIC_KINDS:
            if not self.descrizione:
                raise ValueError("un intervento didattico richiede la descrizione")
            if len(self.titolo) > 70:
                raise ValueError("il titolo di lezione deve stare sotto i 70 caratteri")
        if self.tipo in {"pausa", "logistica"} and self.blocco is not None:
            raise ValueError("pausa e logistica non appartengono a blocchi")
        return self


class SlideV2(ReportModel):
    inizio: str
    inizio_secondi: int = Field(ge=0)
    intervento: str
    titolo: str
    testo: str = Field(max_length=2000)
    materiale: str | None = None
    pagina: int | None = Field(default=None, ge=1)
    confidenza: UnitConfidence

    @model_validator(mode="after")
    def validate_consistency(self) -> "SlideV2":
        if parse_hms(self.inizio) != self.inizio_secondi:
            raise ValueError("i secondi devono corrispondere al tempo h:mm:ss")
        return self


class MaterialV2(ReportModel):
    id: str
    titolo: str
    relatore: str | None = None
    url: str | None = None
    file: str | None = None
    pagine: int | None = Field(default=None, ge=1)
    accesso: Access = "iscritti"
    interventi: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_source(self) -> "MaterialV2":
        if not _SLUG.fullmatch(self.id):
            raise ValueError("id materiale non valido")
        if (self.url is None) == (self.file is None):
            raise ValueError("un materiale richiede esattamente una sorgente")
        return self


class VideoV2(ReportModel):
    chiave: str
    guid: str
    titolo_bunny: str
    durata_secondi: int = Field(ge=0)
    ordine: int = Field(ge=1)
    lingua: str
    sinossi: str
    interventi: list[InterventionV2]
    slide: list[SlideV2] = Field(default_factory=list)
    materiali: list[MaterialV2] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references(self) -> "VideoV2":
        intervention_ids = [item.id for item in self.interventi]
        if len(intervention_ids) != len(set(intervention_ids)):
            raise ValueError("gli id degli interventi devono essere univoci")
        ids = set(intervention_ids)
        for slide in self.slide:
            if slide.intervento not in ids:
                raise ValueError("una slide deve riferirsi a un intervento presente")
        material_ids = [material.id for material in self.materiali]
        if len(material_ids) != len(set(material_ids)):
            raise ValueError("gli id dei materiali devono essere univoci")
        for slide in self.slide:
            if slide.materiale is not None and slide.materiale not in set(material_ids):
                raise ValueError("una slide deve riferirsi a un materiale presente")
        for material in self.materiali:
            if any(item not in ids for item in material.interventi):
                raise ValueError("un materiale deve riferirsi a interventi presenti")
        if self.interventi:
            if self.interventi[0].inizio_secondi != 0:
                raise ValueError("la timeline deve iniziare a zero")
            for previous, following in zip(self.interventi, self.interventi[1:]):
                if following.inizio_secondi != previous.fine_secondi:
                    raise ValueError("la timeline degli interventi deve essere continua")
            if self.interventi[-1].fine_secondi != self.durata_secondi:
                raise ValueError("la timeline deve coprire l'intera durata del video")
        return self


class VerificationV2(ReportModel):
    livello: VerificationLevelV2
    codice: VerificationCodeV2
    video: str | None = None
    intervento: str | None = None
    campo: str | None = None
    messaggio: str


class BoundaryNoteV2(ReportModel):
    video: str
    intervento: str
    messaggio: str


class CostV2(ReportModel):
    valuta: str
    minimo: float
    massimo: float
    banda_bunny: float
    trascrizione: float
    analisi: float
    criterio: str


class DiagnosticsV2(ReportModel):
    confini: list[BoundaryNoteV2] = Field(default_factory=list)
    costo_stimato: CostV2 | None = None


class ReportV2(ReportModel):
    versione: Literal[2] = 2
    stato: Literal["verificato", "da_verificare"]
    corso: CourseV2
    relatori: list[SpeakerV2]
    video: list[VideoV2] = Field(min_length=1)
    verifiche_richieste: list[VerificationV2] = Field(default_factory=list)
    diagnostica: DiagnosticsV2 = Field(default_factory=DiagnosticsV2)

    @model_validator(mode="after")
    def validate_course(self) -> "ReportV2":
        slugs = [speaker.slug for speaker in self.relatori]
        names = [speaker.nome for speaker in self.relatori]
        if len(slugs) != len(set(slugs)) or len(names) != len(set(names)):
            raise ValueError("slug e nomi dei relatori devono essere univoci")
        known = set(slugs)
        for video in self.video:
            for item in video.interventi:
                if any(slug not in known for slug in item.relatori):
                    raise ValueError("un intervento cita uno slug non dichiarato")
            for material in video.materiali:
                if material.relatore is not None and material.relatore not in known:
                    raise ValueError("un materiale cita uno slug non dichiarato")
        if self.stato == "verificato" and any(
            item.livello == "bloccante" for item in self.verifiche_richieste
        ):
            raise ValueError("uno stato verificato non ammette verifiche bloccanti")
        return self


__all__ = [
    "Access",
    "BlockRefV2",
    "BoundaryNoteV2",
    "CostV2",
    "CourseV2",
    "DiagnosticsV2",
    "FaqV2",
    "InterventionV2",
    "MaterialV2",
    "ReportV2",
    "SlideV2",
    "SpeakerV2",
    "V2_AREAS",
    "VerificationCodeV2",
    "VerificationLevelV2",
    "VerificationV2",
    "VideoV2",
]
