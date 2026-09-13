"""Generate a validated Academy v1 import from a verified factual report."""

from __future__ import annotations

from concurrent.futures import CancelledError
import json
from threading import Event
from typing import Any

from pydantic import BaseModel, ValidationError

from app.academy_prompt import ACADEMY_REPAIR_PROMPT, ACADEMY_SYSTEM_PROMPT
from app.course_models import (
    AcademyImport,
    IntermediateCourseReport,
    academy_price,
    parse_hms,
    validate_academy_import,
)
from app.retry import check_cancelled


class AcademyGenerationError(Exception):
    """Safe, user-facing generation failure."""


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


def _apply_price(data: dict[str, Any]) -> dict[str, Any]:
    included_seconds = 0
    for module in data.get("moduli", []):
        for lesson in module.get("lezioni", []):
            if lesson.get("tipo", "video") != "video":
                continue
            included_seconds += parse_hms(lesson["fine"]) - parse_hms(lesson["inizio"])
    updated = json.loads(json.dumps(data, ensure_ascii=False))
    updated["corso"]["prezzo"] = float(academy_price(included_seconds))
    return updated


class AcademyGenerator:
    MODEL = "gpt-4o-mini"
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
                text_format=AcademyImport,
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
                candidate = AcademyImport.model_validate(_apply_price(data))
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


__all__ = ["AcademyGenerationError", "AcademyGenerator"]
