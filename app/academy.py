"""Generate a validated Academy v1 import from a verified factual report."""

from __future__ import annotations

from concurrent.futures import CancelledError
import json
from threading import Event
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.academy_prompt import ACADEMY_REPAIR_PROMPT, ACADEMY_SYSTEM_PROMPT
from app.course_models import (
    AcademyImport,
    IntermediateCourseReport,
    academy_price,
    parse_hms,
    validate_academy_import,
)
from app.retry import check_cancelled
from app.models import ReportModel


class AcademyGenerationError(Exception):
    """Safe, user-facing generation failure."""


class DraftFAQ(ReportModel):
    domanda: str
    risposta: str


class DraftQuote(ReportModel):
    testo: str
    autore: str | None = None
    nota: str | None = None


class DraftCourse(ReportModel):
    titolo: str
    slug: str | None = None
    sottotitolo: str
    lead: str | None = None
    area: str
    formato: str | None = None
    durata: str | None = None
    ore: float | None = None
    prezzo: float | None = None
    presentazione: str
    competenze: list[str]
    profili: list[str]
    faq: list[DraftFAQ] = Field(default_factory=list)
    citazione: DraftQuote | None = None


class DraftSpeaker(ReportModel):
    nome: str
    ruolo: str | None = None
    organizzazione: str | None = None
    slug: str | None = None


class DraftVideo(ReportModel):
    chiave: str
    sorgente: Literal["bunny"] = "bunny"
    guid: str
    durata_secondi: int
    titolo: str | None = None


class DraftAnswer(ReportModel):
    testo: str
    corretta: bool = False


class DraftQuestion(ReportModel):
    testo: str
    risposte: list[DraftAnswer]
    spiegazione: str


class DraftLesson(ReportModel):
    titolo: str
    tipo: Literal["video", "quiz", "testo"] = "video"
    video: str | None = None
    inizio: str | None = None
    fine: str | None = None
    relatori: list[str] = Field(default_factory=list)
    descrizione: str | None = None
    hero: bool = False
    anteprima: bool = False
    domande: list[DraftQuestion] = Field(default_factory=list)
    corpo: str | None = None


class DraftModule(ReportModel):
    titolo: str
    sommario: str | None = None
    lezioni: list[DraftLesson]


class AcademyDraft(ReportModel):
    """Strict JSON shape without editorial validators, so one repair remains possible."""

    versione: Literal[1] = 1
    corso: DraftCourse
    relatori: list[DraftSpeaker]
    video: list[DraftVideo]
    moduli: list[DraftModule]


def _pydantic_errors(error: ValidationError) -> list[dict[str, Any]]:
    return [
        {
            "percorso": [str(item) for item in detail.get("loc", ())],
            "tipo": str(detail.get("type", "validation_error")),
            "messaggio": str(detail.get("msg", "Valore non valido")),
        }
        for detail in error.errors(include_url=False, include_input=False)
    ]


def _as_detached_json(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if not isinstance(value, dict):
        raise ValueError("output_not_object")
    return json.loads(json.dumps(value, ensure_ascii=False))


def _apply_computed_fields(data: dict[str, Any]) -> dict[str, Any]:
    included_seconds = 0
    for module in data.get("moduli", []):
        for lesson in module.get("lezioni", []):
            if lesson.get("tipo", "video") != "video":
                continue
            included_seconds += parse_hms(lesson["fine"]) - parse_hms(lesson["inizio"])
    updated = json.loads(json.dumps(data, ensure_ascii=False))
    updated["corso"]["prezzo"] = float(academy_price(included_seconds))
    ore = max(0.5, round(included_seconds / 1800) / 2)
    updated["corso"]["ore"] = ore
    module_count = len(updated.get("moduli", []))
    updated["corso"]["durata"] = (
        f"{module_count} modul{'o' if module_count == 1 else 'i'} · ~{ore:g}h"
    )
    return updated


class AcademyGenerator:
    # Same Responses API compatibility constraint as the analysis pipeline.
    MODEL = "gpt-5.6-luna"
    MAX_OUTPUT_TOKENS = 12_000

    def __init__(self, client: Any) -> None:
        self._client = client

    def _request(
        self, *, instructions: str, payload: str, cancellation_event: Event | None,
    ) -> Any:
        check_cancelled(cancellation_event)
        try:
            raw = self._client.responses.with_raw_response.parse(
                model=self.MODEL,
                store=False,
                text_format=AcademyDraft,
                instructions=instructions,
                input=payload,
                max_output_tokens=self.MAX_OUTPUT_TOKENS,
            )
            try:
                response = raw.parse()
            finally:
                del raw
        except CancelledError:
            raise
        except Exception:
            raise AcademyGenerationError(
                "Il servizio OpenAI è temporaneamente non disponibile; riprova più tardi"
            ) from None
        check_cancelled(cancellation_event)
        if getattr(response, "status", "completed") != "completed":
            raise AcademyGenerationError(
                "Il servizio OpenAI è temporaneamente non disponibile; riprova più tardi"
            )
        return getattr(response, "output_parsed", None)

    def generate(
        self, source: IntermediateCourseReport,
        cancellation_event: Event | None = None,
    ) -> AcademyImport:
        if any(item.livello == "critico" for item in source.verifiche_richieste):
            raise AcademyGenerationError("Risolvi le verifiche critiche prima di generare il JSON")
        if source.stato not in {"confermato", "verificato"}:
            raise AcademyGenerationError("Il report intermedio deve essere verificato")

        payload = source.model_dump_json(by_alias=True, exclude_none=True)
        instructions = ACADEMY_SYSTEM_PROMPT
        for attempt in range(2):
            parsed = self._request(
                instructions=instructions,
                payload=payload,
                cancellation_event=cancellation_event,
            )
            data: dict[str, Any] | None = None
            errors: list[dict[str, Any] | str]
            try:
                data = _as_detached_json(parsed)
                candidate = AcademyImport.model_validate(_apply_computed_fields(data))
            except (ValidationError, ValueError, TypeError, KeyError) as error:
                if isinstance(error, ValidationError):
                    errors = _pydantic_errors(error)
                else:
                    errors = [{"tipo": "validation_error", "messaggio": "JSON non valido"}]
            else:
                errors = validate_academy_import(candidate, source)
                if not errors:
                    check_cancelled(cancellation_event)
                    return candidate
                data = candidate.model_dump(mode="json", by_alias=True, exclude_none=True)

            if attempt == 1 or data is None:
                raise AcademyGenerationError(
                    "Non è stato possibile creare un JSON Academy valido; il report intermedio resta salvato"
                )
            payload = json.dumps(
                {"errors": errors, "previous_json": data},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            instructions = ACADEMY_REPAIR_PROMPT

        raise AcademyGenerationError("Non è stato possibile creare un JSON Academy valido")


__all__ = ["AcademyDraft", "AcademyGenerationError", "AcademyGenerator"]
