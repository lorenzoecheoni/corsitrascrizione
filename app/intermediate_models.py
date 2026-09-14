"""Strict interchange models for the Academy intermediate report v1.1."""

import re
from typing import Annotated, Literal

from pydantic import (
    AliasChoices,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from .models import GENERIC_INTERVENTION_SPEAKER, InterventionKind, ReportModel


VerificationCode = Literal[
    "RELATORE_NON_IDENTIFICATO", "TEMPI_INCOERENTI",
    "RELATORE_NON_NEL_REGISTRO", "INTERVENTO_BREVE",
    "CONFIDENZA_BASSA", "MATERIALE_NON_RAGGIUNGIBILE", "CONFINE",
]
Access = Literal["pubblico", "iscritti"]
NameOrigin = Literal["audio", "slide", "inventario", "metadata", "revisione"]
VerificationLevel = Literal["critico", "avviso"]
Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
UnitConfidence = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]

_HMS = re.compile(r"\d+:[0-5]\d:[0-5]\d")
_INTERVENTION_ID = re.compile(r"^v1-i[0-9]{3,}$")
_CRITICAL_CODES = {"RELATORE_NON_IDENTIFICATO", "TEMPI_INCOERENTI"}
_WARNING_CODES = {
    "RELATORE_NON_NEL_REGISTRO", "INTERVENTO_BREVE", "CONFIDENZA_BASSA",
    "MATERIALE_NON_RAGGIUNGIBILE", "CONFINE",
}


def parse_hms(value: str) -> int:
    """Parse a strict ``h:mm:ss`` timestamp into seconds."""
    if not isinstance(value, str) or not _HMS.fullmatch(value):
        raise ValueError("Il tempo deve essere h:mm:ss")
    hours, minutes, seconds = (int(part) for part in value.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def format_hms(seconds: float) -> str:
    """Format seconds using the canonical ``h:mm:ss`` representation."""
    if isinstance(seconds, bool) or seconds < 0:
        raise ValueError("Il tempo non può essere negativo")
    total = int(seconds + 0.5)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


class _Model(ReportModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class IntermediateCourseV11(_Model):
    titolo: str
    sinossi_corso: str


class IntermediateSpeakerV11(_Model):
    nome: str
    slug: str | None = None
    ruolo: str | None = None
    organizzazione: str | None = None
    confidenza: UnitConfidence
    origine_nome: list[NameOrigin] = Field(min_length=1)

    @field_validator("nome")
    @classmethod
    def require_named_speaker(cls, value: str) -> str:
        if not value.strip() or GENERIC_INTERVENTION_SPEAKER.fullmatch(value.strip()):
            raise ValueError("nome relatore non può essere generico")
        return value


class IntermediateInterventionV11(_Model):
    id: str
    start_seconds: Nonnegative = Field(
        validation_alias=AliasChoices("start_seconds", "inizio"), serialization_alias="inizio"
    )
    end_seconds: Nonnegative = Field(
        validation_alias=AliasChoices("end_seconds", "fine"), serialization_alias="fine"
    )
    tipo: InterventionKind
    relatori: list[str]
    titolo: str
    sintesi: str
    punti_chiave: list[str]
    accesso: Access
    confidenza: UnitConfidence

    @field_validator("start_seconds", "end_seconds", mode="before")
    @classmethod
    def parse_timestamp(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("Il tempo non può essere booleano")
        if isinstance(value, str):
            return parse_hms(value)
        return value

    @field_serializer("start_seconds", "end_seconds")
    def serialize_timestamp(self, value: float) -> str:
        return format_hms(value)

    @model_validator(mode="after")
    def validate_segment(self) -> "IntermediateInterventionV11":
        if not _INTERVENTION_ID.fullmatch(self.id) or int(self.id[5:]) < 1:
            raise ValueError("id intervento deve avere formato v1-iNNN")
        if self.end_seconds <= self.start_seconds:
            raise ValueError("fine deve essere successiva a inizio")
        if self.tipo != "intervento" and self.punti_chiave != []:
            raise ValueError("i tipi non intervento non ammettono punti_chiave")
        return self


class IntermediateSlideV11(_Model):
    start_seconds: Nonnegative = Field(
        validation_alias=AliasChoices("start_seconds", "inizio"), serialization_alias="inizio"
    )
    titolo: str
    testo_principale: str = Field(max_length=500)
    confidenza: UnitConfidence
    materiale: str | None = None
    pagina: int | None = None

    @field_validator("start_seconds", mode="before")
    @classmethod
    def parse_timestamp(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("Il tempo non può essere booleano")
        return parse_hms(value) if isinstance(value, str) else value

    @field_serializer("start_seconds")
    def serialize_timestamp(self, value: float) -> str:
        return format_hms(value)


class IntermediateMaterialV11(_Model):
    titolo: str
    relatore: str | None = None
    url: str | None = None
    file: str | None = None
    pagine: int | None = None
    accesso: Access = "iscritti"

    @model_validator(mode="after")
    def exactly_one_source(self) -> "IntermediateMaterialV11":
        if (self.url is None) == (self.file is None):
            raise ValueError("un materiale richiede esattamente una sorgente")
        return self


class IntermediateVideoV11(_Model):
    chiave: str
    guid: str
    titolo_bunny: str
    durata_secondi: int = Field(ge=0)
    ordine: int = Field(ge=1)
    lingua: str
    sinossi: str
    interventi: list[IntermediateInterventionV11]
    slide: list[IntermediateSlideV11]
    materiali: list[IntermediateMaterialV11]


class VerificationRequestV11(_Model):
    livello: VerificationLevel
    codice: VerificationCode
    video: str | None = None
    intervento: str | None = None
    campo: str | None = None
    messaggio: str

    @model_validator(mode="after")
    def matching_level(self) -> "VerificationRequestV11":
        critical = self.codice in _CRITICAL_CODES
        warning = self.codice in _WARNING_CODES
        if (critical and self.livello != "critico") or (warning and self.livello != "avviso"):
            raise ValueError("livello non coerente con il codice di verifica")
        return self


class IntermediateReportV11(_Model):
    versione: Literal[1]
    stato: Literal["verificato", "da_verificare"]
    corso: IntermediateCourseV11
    relatori: list[IntermediateSpeakerV11]
    video: list[IntermediateVideoV11] = Field(min_length=1, max_length=1)
    verifiche_richieste: list[VerificationRequestV11]

    @field_validator("versione", mode="before")
    @classmethod
    def require_exact_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("versione deve essere l'intero 1")
        return value

    @model_validator(mode="after")
    def validate_envelope(self) -> "IntermediateReportV11":
        video = self.video[0]
        if video.chiave != "v1" or video.ordine != 1:
            raise ValueError("la busta v1.1 richiede video v1 con ordine 1")
        names = [speaker.nome for speaker in self.relatori]
        if len(names) != len(set(names)):
            raise ValueError("i nomi dei relatori devono essere univoci")
        known = set(names)
        for intervention in video.interventi:
            if any(name not in known for name in intervention.relatori):
                raise ValueError("ogni relatore dell'intervento deve essere nel registro")
        critical = any(item.livello == "critico" for item in self.verifiche_richieste)
        if (self.stato == "da_verificare") != critical:
            raise ValueError("stato incoerente con le verifiche critiche")
        return self


__all__ = [
    "IntermediateReportV11", "IntermediateCourseV11", "IntermediateSpeakerV11",
    "IntermediateVideoV11", "IntermediateInterventionV11", "IntermediateSlideV11",
    "IntermediateMaterialV11", "VerificationRequestV11", "VerificationCode", "Access",
    "NameOrigin", "format_hms", "parse_hms",
]
