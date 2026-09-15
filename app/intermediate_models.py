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

from .models import (
    ChapterBoundaryOrigin,
    GENERIC_INTERVENTION_SPEAKER,
    InterventionKind,
    ReportModel,
)


VerificationCode = Literal[
    "RELATORE_NON_IDENTIFICATO", "TEMPI_INCOERENTI",
    "RELATORE_NON_NEL_REGISTRO", "INTERVENTO_BREVE",
    "CONFIDENZA_BASSA", "MATERIALE_NON_RAGGIUNGIBILE", "CONFINE",
    "INTERVENTO_LUNGO", "SLIDE_NON_ABBINATA", "ALIAS_RELATORE_AMBIGUO",
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
    "MATERIALE_NON_RAGGIUNGIBILE", "CONFINE", "INTERVENTO_LUNGO",
    "SLIDE_NON_ABBINATA", "ALIAS_RELATORE_AMBIGUO",
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


class IntermediateCostV11(_Model):
    valuta: Literal["USD"] = "USD"
    minimo: Nonnegative
    massimo: Nonnegative
    banda_bunny: Nonnegative
    trascrizione: Nonnegative
    analisi: Nonnegative
    criterio: str

    @model_validator(mode="after")
    def ordered_cost(self) -> "IntermediateCostV11":
        if self.massimo < self.minimo:
            raise ValueError("intervallo dei costi non valido")
        return self


class IntermediateSpeechBlockV11(_Model):
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
    sinossi: str

    @field_validator("start_seconds", "end_seconds", mode="before")
    @classmethod
    def parse_timestamp(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("Il tempo non può essere booleano")
        return parse_hms(value) if isinstance(value, str) else value

    @field_serializer("start_seconds", "end_seconds")
    def serialize_timestamp(self, value: float) -> str:
        return format_hms(value)

    @model_validator(mode="after")
    def validate_segment(self) -> "IntermediateSpeechBlockV11":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("fine deve essere successiva a inizio")
        return self


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


class IntermediateBoundaryOriginV11(ChapterBoundaryOrigin):
    slide_indizio_seconds: Nonnegative | None = Field(
        default=None,
        validation_alias=AliasChoices("slide_indizio_seconds", "slide_indizio"),
        serialization_alias="slide_indizio",
    )

    @field_validator("slide_indizio_seconds", mode="before")
    @classmethod
    def parse_timestamp(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("Il tempo non può essere booleano")
        return parse_hms(value) if isinstance(value, str) else value

    @field_serializer("slide_indizio_seconds")
    def serialize_timestamp(self, value: float | None) -> str | None:
        return format_hms(value) if value is not None else None


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
    blocco: str | None = None
    capitolo_numero: int | None = Field(default=None, ge=1)
    capitoli_blocco: int | None = Field(default=None, ge=1)
    confine_inizio: IntermediateBoundaryOriginV11 | None = None

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
        chapter_fields = (self.blocco, self.capitolo_numero, self.capitoli_blocco)
        has_chapter_fields = [value is not None for value in chapter_fields]
        if any(has_chapter_fields) and not all(has_chapter_fields):
            raise ValueError("blocco e numerazione capitolo devono essere presenti insieme")
        return self


class IntermediateSlideV11(_Model):
    start_seconds: Nonnegative = Field(
        validation_alias=AliasChoices("start_seconds", "inizio"), serialization_alias="inizio"
    )
    titolo: str
    testo_principale: str = Field(max_length=500)
    confidenza: UnitConfidence
    materiale: str | None = None
    pagina: int | None = Field(default=None, ge=1)

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
    pagine: int | None = Field(default=None, ge=1)
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
    blocchi_parlato: list[IntermediateSpeechBlockV11] = Field(default_factory=list)
    costo_stimato: IntermediateCostV11 | None = None
    slide: list[IntermediateSlideV11]
    materiali: list[IntermediateMaterialV11]

    @model_validator(mode="after")
    def validate_granular_contract(self) -> "IntermediateVideoV11":
        intervention_ids = [intervention.id for intervention in self.interventi]
        if len(intervention_ids) != len(set(intervention_ids)):
            raise ValueError("gli id degli interventi devono essere univoci")

        granular_fields = {"blocchi_parlato", "costo_stimato"}
        supplied_granular_fields = granular_fields & self.model_fields_set
        if not supplied_granular_fields:
            return self
        if (
            supplied_granular_fields != granular_fields
            or not self.blocchi_parlato
            or self.costo_stimato is None
        ):
            raise ValueError("il contratto granulare richiede blocchi e costo stimato")

        duration = self.durata_secondi
        segments = [*self.interventi, *self.blocchi_parlato]
        if any(segment.end_seconds > duration for segment in segments):
            raise ValueError("nessun estremo può superare la durata Bunny")
        if any(slide.start_seconds > duration for slide in self.slide) or any(
            chapter.confine_inizio is not None
            and chapter.confine_inizio.slide_indizio_seconds is not None
            and chapter.confine_inizio.slide_indizio_seconds > duration
            for chapter in self.interventi
        ):
            raise ValueError("nessun indizio slide può superare la durata Bunny")

        materials_by_title = {material.titolo: material for material in self.materiali}
        sources = {(material.url, material.file) for material in self.materiali}
        if len(materials_by_title) != len(self.materiali) or len(sources) != len(self.materiali):
            raise ValueError("i materiali richiedono titoli e sorgenti univoci")
        for slide in self.slide:
            if slide.materiale is None:
                continue
            material = materials_by_title.get(slide.materiale)
            if material is None:
                raise ValueError("la slide deve riferirsi a un materiale presente")
            if slide.pagina is not None and (material.pagine is None or slide.pagina > material.pagine):
                raise ValueError("la pagina deve esistere nel materiale indicato")

        block_ids = [block.id for block in self.blocchi_parlato]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("gli id dei blocchi parlato devono essere univoci")
        blocks_by_id = {block.id: block for block in self.blocchi_parlato}
        ordered_blocks = sorted(self.blocchi_parlato, key=lambda block: block.start_seconds)
        bridge_segments = sorted(
            (
                intervention for intervention in self.interventi
                if intervention.tipo in {"pausa", "logistica"}
            ),
            key=lambda intervention: intervention.start_seconds,
        )
        for intervention in bridge_segments:
            if any(
                value is not None
                for value in (
                    intervention.blocco,
                    intervention.capitolo_numero,
                    intervention.capitoli_blocco,
                )
            ) or intervention.confine_inizio is not None:
                raise ValueError("pausa e logistica non appartengono a capitoli")
        consumed_bridge_ids: set[str] = set()
        gaps = []
        # Edge logistics are factual timeline segments too. When present they
        # must cover the entire edge gap, just like an interior bridge. Keep
        # undeclared terminal timing discrepancies available to warning checks.
        if any(bridge.start_seconds < ordered_blocks[0].start_seconds for bridge in bridge_segments):
            gaps.append((0, ordered_blocks[0].start_seconds))
        for previous, following in zip(ordered_blocks, ordered_blocks[1:]):
            if following.start_seconds < previous.end_seconds:
                raise ValueError("i blocchi parlato non possono sovrapporsi")
            if following.start_seconds > previous.end_seconds:
                gaps.append((previous.end_seconds, following.start_seconds))
        if any(bridge.end_seconds > ordered_blocks[-1].end_seconds for bridge in bridge_segments):
            gaps.append((ordered_blocks[-1].end_seconds, duration))
        for gap_start, gap_end in gaps:
            covered_until = gap_start
            for bridge in bridge_segments:
                if bridge.id in consumed_bridge_ids:
                    continue
                if bridge.start_seconds < covered_until or bridge.end_seconds > gap_end:
                    continue
                if bridge.start_seconds != covered_until:
                    break
                covered_until = bridge.end_seconds
                consumed_bridge_ids.add(bridge.id)
                if covered_until == gap_end:
                    break
            if covered_until != gap_end:
                raise ValueError("un intervallo fuori dai blocchi richiede pausa o logistica")
        if consumed_bridge_ids != {bridge.id for bridge in bridge_segments}:
            raise ValueError("pausa e logistica devono coprire un solo intervallo fuori dai blocchi")
        spoken = [
            intervention for intervention in self.interventi
            if intervention.tipo not in {"pausa", "logistica"}
        ]
        chapters_by_block: dict[str, list[IntermediateInterventionV11]] = {}
        for chapter in spoken:
            if chapter.blocco is None:
                raise ValueError("copertura dei blocchi parlato incompleta")
            if chapter.blocco not in blocks_by_id:
                raise ValueError("un capitolo deve appartenere a un blocco parlato")
            chapters_by_block.setdefault(chapter.blocco, []).append(chapter)
        if set(chapters_by_block) != set(blocks_by_id):
            raise ValueError("ogni blocco parlato richiede almeno un capitolo")

        for block_id, block in blocks_by_id.items():
            chapters = sorted(chapters_by_block[block_id], key=lambda item: item.capitolo_numero or 0)
            declared_counts = {chapter.capitoli_blocco for chapter in chapters}
            expected_numbers = list(range(1, len(chapters) + 1))
            if (
                len(declared_counts) != 1
                or declared_counts.pop() != len(chapters)
                or [chapter.capitolo_numero for chapter in chapters] != expected_numbers
            ):
                raise ValueError("numerazione dei capitoli nel blocco incoerente")
            if chapters[0].start_seconds != block.start_seconds or chapters[-1].end_seconds != block.end_seconds:
                raise ValueError("copertura temporale del blocco incompleta")
            if any(
                previous.end_seconds != following.start_seconds
                for previous, following in zip(chapters, chapters[1:])
            ):
                raise ValueError("i capitoli di un blocco devono essere contigui")

        public_chapters = [chapter for chapter in self.interventi if chapter.accesso == "pubblico"]
        if len(public_chapters) != 1 or public_chapters[0].tipo != "intervento":
            raise ValueError("è richiesto esattamente un capitolo didattico pubblico")
        public_duration = public_chapters[0].end_seconds - public_chapters[0].start_seconds
        if not 480 <= public_duration <= 900:
            raise ValueError("il capitolo didattico pubblico deve durare 480-900 secondi")
        return self


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
    "IntermediateMaterialV11", "IntermediateSpeechBlockV11", "IntermediateCostV11",
    "VerificationRequestV11", "VerificationCode", "Access",
    "NameOrigin", "format_hms", "parse_hms",
]
