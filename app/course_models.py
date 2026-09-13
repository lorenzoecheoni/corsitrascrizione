from __future__ import annotations

import math
import re
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, field_serializer, field_validator, model_validator

from app.models import Intervention, ReportModel


ALLOWED_AREAS = {
    "Fiscalità",
    "Governance",
    "Operazioni straordinarie",
    "Patrimonio",
    "Sostenibilità",
    "Trust",
    "Compliance",
    "Tecnologia e innovazione",
    "Family business",
}
GENERIC_SPEAKER = re.compile(r"^(?:relatore|speaker)(?:[\s_-]*\d+)?$", re.IGNORECASE)
UnitConfidence = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
PositiveSeconds = Annotated[int, Field(gt=0, strict=True)]
NonnegativeSeconds = Annotated[int, Field(ge=0, strict=True)]


def format_hms(seconds: float) -> str:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ValueError("I secondi devono essere numerici")
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("I secondi devono essere finiti e non negativi")
    rounded = int(seconds + 0.5)
    hours, remainder = divmod(rounded, 3600)
    minutes, final_seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{final_seconds:02d}"


def parse_hms(value: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"\d+:[0-5]\d:[0-5]\d", value):
        raise ValueError("Il tempo deve essere nel formato canonico h:mm:ss")
    hours, minutes, seconds = (int(part) for part in value.split(":"))
    return hours * 3600 + minutes * 60 + seconds


class InventoryReference(ReportModel):
    foglio: str
    ordine: Annotated[int, Field(ge=1, strict=True)]


class IntermediateCourse(ReportModel):
    titolo: str
    sinossi_corso: str
    inventario: InventoryReference | None = None


class IntermediateSpeaker(ReportModel):
    nome: str
    slug: str | None = None
    ruolo: str | None = None
    organizzazione: str | None = None
    confidenza: UnitConfidence
    origine_nome: list[Literal["audio", "slide", "inventario", "metadata"]] = Field(
        min_length=1
    )

    @field_validator("nome")
    @classmethod
    def reject_generic_name(cls, value: str) -> str:
        name = value.strip()
        if not name or GENERIC_SPEAKER.fullmatch(name):
            raise ValueError("I relatori generici devono essere omessi")
        return name


class IntermediateSlide(ReportModel):
    start_seconds: NonnegativeSeconds = Field(
        validation_alias=AliasChoices("start_seconds", "inizio"), serialization_alias="inizio"
    )
    titolo: str | None = None
    testo_principale: Annotated[str, Field(max_length=500)] = ""
    confidenza: UnitConfidence

    @field_validator("start_seconds", mode="before")
    @classmethod
    def parse_start(cls, value: object) -> object:
        return parse_hms(value) if isinstance(value, str) else value

    @field_serializer("start_seconds")
    def serialize_start(self, value: int) -> str:
        return format_hms(value)


class IntermediateVideo(ReportModel):
    chiave: str
    guid: str
    titolo_bunny: str
    durata_secondi: PositiveSeconds
    ordine: Annotated[int, Field(ge=1, strict=True)]
    interventi: list[Intervention] = Field(min_length=1)
    slide: list[IntermediateSlide] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_timeline(self) -> "IntermediateVideo":
        expected_start = 0
        expected_prefix = f"{self.chiave}-i"
        seen_ids: set[str] = set()
        for item in self.interventi:
            if item.id in seen_ids or not item.id.startswith(expected_prefix):
                raise ValueError("Gli ID degli interventi devono essere unici e coerenti col video")
            seen_ids.add(item.id)
            if item.start_seconds != expected_start:
                raise ValueError("La timeline degli interventi deve essere continua")
            expected_start = item.end_seconds
        if expected_start != self.durata_secondi:
            raise ValueError("La timeline degli interventi deve coprire l'intera durata")
        if any(slide.start_seconds > self.durata_secondi for slide in self.slide):
            raise ValueError("Una slide è fuori dalla durata del video")
        return self


class VerificationRequest(ReportModel):
    livello: Literal["critico", "avviso"]
    codice: str
    video: str | None = None
    intervento: str | None = None
    campo: str | None = None
    messaggio: str


class IntermediateCourseReport(ReportModel):
    versione: Literal[1] = 1
    stato: Literal["da_verificare", "confermato", "verificato"] = "da_verificare"
    corso: IntermediateCourse
    relatori: list[IntermediateSpeaker] = Field(default_factory=list)
    video: list[IntermediateVideo] = Field(min_length=1)
    verifiche_richieste: list[VerificationRequest] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_course(self) -> "IntermediateCourseReport":
        keys = [video.chiave for video in self.video]
        orders = [video.ordine for video in self.video]
        if len(keys) != len(set(keys)) or keys != [f"v{number}" for number in range(1, len(keys) + 1)]:
            raise ValueError("Le chiavi video devono essere stabili e consecutive da v1")
        if orders != list(range(1, len(orders) + 1)):
            raise ValueError("L'ordine video deve essere consecutivo")
        speaker_names = [speaker.nome for speaker in self.relatori]
        if len(speaker_names) != len(set(speaker_names)):
            raise ValueError("I relatori devono essere unici")
        allowed = set(speaker_names)
        for video in self.video:
            for item in video.interventi:
                if any(name not in allowed for name in item.relatori):
                    raise ValueError("Un intervento cita un relatore non dichiarato")
        if self.stato in {"confermato", "verificato"} and any(
            item.livello == "critico" for item in self.verifiche_richieste
        ):
            raise ValueError("Un report con verifiche critiche non può essere confermato")
        return self


class AcademyFAQ(ReportModel):
    domanda: str
    risposta: str


class AcademyQuote(ReportModel):
    testo: str
    autore: str | None = None
    nota: str | None = None


class AcademyCourse(ReportModel):
    titolo: str
    slug: str | None = None
    sottotitolo: str
    lead: str | None = None
    area: str
    formato: str | None = None
    durata: str | None = None
    ore: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None
    prezzo: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    presentazione: str
    competenze: list[str] = Field(min_length=5, max_length=7)
    profili: list[str] = Field(min_length=1)
    faq: list[AcademyFAQ] = Field(default_factory=list)
    citazione: AcademyQuote | None = None

    @model_validator(mode="after")
    def validate_editorial_fields(self) -> "AcademyCourse":
        if self.area not in ALLOWED_AREAS:
            raise ValueError("area non ammessa")
        paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", self.presentazione)]
        if len([paragraph for paragraph in paragraphs if paragraph]) != 3:
            raise ValueError("presentazione deve contenere esattamente 3 paragrafi")
        if any("|" not in profile for profile in self.profili):
            raise ValueError("ogni profilo deve usare il formato Categoria | frase")
        return self


class AcademySpeaker(ReportModel):
    nome: str
    ruolo: str | None = None
    organizzazione: str | None = None
    slug: str | None = None


class AcademyVideo(ReportModel):
    chiave: str
    sorgente: Literal["bunny"] = "bunny"
    guid: str
    durata_secondi: PositiveSeconds
    titolo: str | None = None


class AcademyAnswer(ReportModel):
    testo: str
    corretta: bool = False


class AcademyQuestion(ReportModel):
    testo: str
    risposte: list[AcademyAnswer] = Field(min_length=4, max_length=4)
    spiegazione: str

    @model_validator(mode="after")
    def validate_correct_answer(self) -> "AcademyQuestion":
        if sum(answer.corretta for answer in self.risposte) != 1:
            raise ValueError("Ogni domanda richiede una sola risposta corretta")
        return self


class AcademyLesson(ReportModel):
    titolo: str
    tipo: Literal["video", "quiz", "testo"] = "video"
    video: str | None = None
    inizio: str | None = None
    fine: str | None = None
    relatori: list[str] = Field(default_factory=list)
    descrizione: str | None = None
    hero: bool = False
    anteprima: bool = False
    domande: list[AcademyQuestion] = Field(default_factory=list)
    corpo: str | None = None

    @field_validator("inizio", "fine")
    @classmethod
    def validate_time(cls, value: str | None) -> str | None:
        if value is not None:
            parse_hms(value)
        return value

    @model_validator(mode="after")
    def validate_type_fields(self) -> "AcademyLesson":
        if re.match(r"^\s*\[", self.titolo):
            raise ValueError("Il titolo non può iniziare con un prefisso tra parentesi quadre")
        if self.tipo == "video":
            if not self.video or self.inizio is None or self.fine is None:
                raise ValueError("Una lezione video richiede video, inizio e fine")
            if parse_hms(self.fine) <= parse_hms(self.inizio):
                raise ValueError("La fine della lezione deve seguire l'inizio")
            if self.domande or self.corpo is not None:
                raise ValueError("Una lezione video non può contenere quiz o testo")
        elif self.tipo == "quiz":
            if len(self.domande) != 3:
                raise ValueError("Un quiz richiede esattamente 3 domande")
            if self.video or self.inizio is not None or self.fine is not None or self.corpo is not None:
                raise ValueError("Un quiz non può contenere riferimenti video o testo")
        else:
            if not self.corpo:
                raise ValueError("Una lezione di testo richiede il corpo")
        if self.hero and not self.anteprima:
            raise ValueError("La hero deve essere anche anteprima")
        return self


class AcademyModule(ReportModel):
    titolo: str
    sommario: str | None = None
    lezioni: list[AcademyLesson]

    @model_validator(mode="after")
    def validate_lessons(self) -> "AcademyModule":
        video_lessons = [lesson for lesson in self.lezioni if lesson.tipo == "video"]
        if not 2 <= len(video_lessons) <= 6:
            raise ValueError("Un modulo richiede tra 2 e 6 lezioni video")
        quizzes = [index for index, lesson in enumerate(self.lezioni) if lesson.tipo == "quiz"]
        if quizzes != [len(self.lezioni) - 1]:
            raise ValueError("Ogni modulo deve terminare con un solo quiz")
        return self


class AcademyImport(ReportModel):
    versione: Literal[1] = 1
    corso: AcademyCourse
    relatori: list[AcademySpeaker]
    video: list[AcademyVideo] = Field(min_length=1)
    moduli: list[AcademyModule] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_references(self) -> "AcademyImport":
        video_keys = [video.chiave for video in self.video]
        if len(video_keys) != len(set(video_keys)):
            raise ValueError("Le chiavi video devono essere uniche")
        speaker_names = [speaker.nome for speaker in self.relatori]
        if len(speaker_names) != len(set(speaker_names)):
            raise ValueError("I relatori devono essere unici")
        return self


def academy_price(included_seconds: int) -> Decimal:
    if isinstance(included_seconds, bool) or not isinstance(included_seconds, int):
        raise ValueError("La durata inclusa deve essere un numero intero di secondi")
    if included_seconds <= 0 or included_seconds > 4 * 3600:
        raise ValueError("La durata inclusa deve essere compresa fra 1 secondo e 4 ore")
    if included_seconds <= 3600:
        return Decimal("97")
    if included_seconds <= 7200:
        return Decimal("147")
    if included_seconds <= 10800:
        return Decimal("197")
    return Decimal("247")


def validate_academy_import(
    report: AcademyImport, source: IntermediateCourseReport
) -> list[str]:
    errors: list[str] = []
    if source.stato not in {"confermato", "verificato"}:
        errors.append("Il report intermedio non è verificato.")
    if any(item.livello == "critico" for item in source.verifiche_richieste):
        errors.append("Il report intermedio contiene verifiche critiche.")

    source_videos = {video.chiave: video for video in source.video}
    report_videos = {video.chiave: video for video in report.video}
    if set(report_videos) != set(source_videos):
        errors.append("Le chiavi video non corrispondono al report intermedio.")
    for key, video in report_videos.items():
        source_video = source_videos.get(key)
        if source_video is None:
            continue
        if video.guid != source_video.guid:
            errors.append(f"Il GUID del video {key} non corrisponde alla fonte.")
        if video.durata_secondi != source_video.durata_secondi:
            errors.append(f"La durata del video {key} non corrisponde alla fonte.")

    allowed_speakers = {speaker.nome for speaker in source.relatori}
    declared_speakers = {speaker.nome for speaker in report.relatori}
    if not declared_speakers.issubset(allowed_speakers):
        errors.append("Il JSON Academy dichiara un relatore non presente nella fonte.")

    intervals: dict[str, list[tuple[int, int, str]]] = {}
    hero_lessons: list[AcademyLesson] = []
    included_seconds = 0
    for module in report.moduli:
        for lesson in module.lezioni:
            if lesson.tipo != "video":
                continue
            if lesson.video not in report_videos:
                errors.append(f"La lezione '{lesson.titolo}' usa un riferimento video sconosciuto.")
                continue
            unknown = set(lesson.relatori) - allowed_speakers
            if unknown:
                errors.append(f"La lezione '{lesson.titolo}' cita un relatore non presente nella fonte.")
            start = parse_hms(lesson.inizio or "0:00:00")
            end = parse_hms(lesson.fine or "0:00:00")
            duration = end - start
            included_seconds += duration
            if end > report_videos[lesson.video].durata_secondi:
                errors.append(f"La durata della lezione '{lesson.titolo}' supera il video.")
            if duration < 8 * 60 or duration > 40 * 60:
                errors.append(f"La durata della lezione '{lesson.titolo}' non è fra 8 e 40 minuti.")
            intervals.setdefault(lesson.video, []).append((start, end, lesson.titolo))
            if lesson.hero:
                hero_lessons.append(lesson)
                if duration < 8 * 60 or duration > 15 * 60:
                    errors.append("La lezione hero deve durare fra 8 e 15 minuti.")

    for video_key, video_intervals in intervals.items():
        ordered = sorted(video_intervals)
        for previous, current in zip(ordered, ordered[1:]):
            if current[0] < previous[1]:
                errors.append(f"Le lezioni del video {video_key} si sovrappongono.")
                break

    if len(hero_lessons) != 1:
        errors.append("Il JSON Academy deve contenere una sola lezione hero.")
    if included_seconds:
        try:
            expected_price = academy_price(included_seconds)
        except ValueError:
            errors.append("La durata totale inclusa supera le quattro ore ammesse.")
        else:
            if Decimal(str(report.corso.prezzo)) != expected_price:
                errors.append(f"Il prezzo deve essere {expected_price} euro per la durata inclusa.")
    return errors


__all__ = [
    "ALLOWED_AREAS",
    "AcademyImport",
    "Intervention",
    "IntermediateCourseReport",
    "academy_price",
    "format_hms",
    "parse_hms",
    "validate_academy_import",
]
