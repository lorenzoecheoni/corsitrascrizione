"""One validated LLM pass that writes the editorial layer of the v2 report."""

from __future__ import annotations

from concurrent.futures import CancelledError
import json
from threading import Event
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.editorial_prompt import EDITORIAL_REPAIR_PROMPT, EDITORIAL_SYSTEM_PROMPT
from app.intermediate_models import IntermediateReportV11
from app.intermediate_models_v2 import V2_AREAS
from app.models import ReportModel
from app.retry import check_cancelled


class EditorialGenerationError(Exception):
    """Safe, user-facing enrichment failure."""


class EditorialFaq(ReportModel):
    domanda: str
    risposta: str


class EditorialIntervention(ReportModel):
    id: str
    titolo_lezione: str
    descrizione: str
    casi: list[str] = Field(default_factory=list)
    riferimenti: list[str] = Field(default_factory=list)


class EditorialBlock(ReportModel):
    id: str
    titolo_modulo: str


class EditorialCourse(ReportModel):
    sottotitolo: str
    area: str
    competenze: list[str] = Field(default_factory=list)
    profili: list[str] = Field(default_factory=list)
    faq: list[EditorialFaq] = Field(default_factory=list)


class EditorialDraft(ReportModel):
    """Editorial layer without reference validators, so one repair remains possible."""

    corso: EditorialCourse
    interventi: list[EditorialIntervention]
    blocchi: list[EditorialBlock]


_DIDACTIC_KINDS = {"intervento", "domande"}


def didactic_intervention_ids(source: IntermediateReportV11) -> list[str]:
    return [
        item.id
        for video in source.video
        for item in video.interventi
        if item.tipo in _DIDACTIC_KINDS
    ]


def validate_editorial_draft(draft: EditorialDraft, source: IntermediateReportV11) -> list[str]:
    """Machine-check the draft against the factual source before it is trusted."""
    errors: list[str] = []
    expected_interventions = didactic_intervention_ids(source)
    covered = [item.id for item in draft.interventi]
    if len(covered) != len(set(covered)):
        errors.append("Un intervento è coperto più di una volta.")
    if set(covered) - set(expected_interventions):
        errors.append("La bozza copre interventi non presenti nel report.")
    if set(expected_interventions) - set(covered):
        errors.append("La bozza non copre tutti gli interventi didattici.")
    for item in draft.interventi:
        if not item.titolo_lezione.strip() or not item.descrizione.strip():
            errors.append(f"L'intervento {item.id} richiede titolo e descrizione non vuoti.")
        if len(item.titolo_lezione) > 70:
            errors.append(f"Il titolo di {item.id} supera i 70 caratteri.")
    expected_blocks = [block.id for video in source.video for block in video.blocchi_parlato]
    covered_blocks = [block.id for block in draft.blocchi]
    if len(covered_blocks) != len(set(covered_blocks)):
        errors.append("Un blocco è coperto più di una volta.")
    if set(covered_blocks) - set(expected_blocks):
        errors.append("La bozza copre blocchi non presenti nel report.")
    if set(expected_blocks) - set(covered_blocks):
        errors.append("La bozza non copre tutti i blocchi di parlato.")
    for block in draft.blocchi:
        if not block.titolo_modulo.strip():
            errors.append(f"Il blocco {block.id} richiede un titolo non vuoto.")
    if draft.corso.area not in V2_AREAS:
        errors.append("L'area non è fra quelle ammesse.")
    if not 5 <= len(draft.corso.competenze) <= 6:
        errors.append("Le competenze devono essere 5 o 6.")
    if len(draft.corso.profili) != 3 or any("|" not in p for p in draft.corso.profili):
        errors.append("Servono esattamente 3 profili nel formato Categoria | frase.")
    if not 2 <= len(draft.corso.faq) <= 4:
        errors.append("Le faq devono essere fra 2 e 4.")
    if not draft.corso.sottotitolo.strip():
        errors.append("Il sottotitolo non può essere vuoto.")
    return errors


def _as_detached_json(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if not isinstance(value, dict):
        raise ValueError("output_not_object")
    return json.loads(json.dumps(value, ensure_ascii=False))


class EditorialEnricher:
    # Same Responses API compatibility constraint as the analysis pipeline.
    MODEL = "gpt-5.6-luna"
    MAX_OUTPUT_TOKENS = 10_000

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
                text_format=EditorialDraft,
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
            raise EditorialGenerationError(
                "Il servizio OpenAI è temporaneamente non disponibile; riprova più tardi"
            ) from None
        check_cancelled(cancellation_event)
        if getattr(response, "status", "completed") != "completed":
            raise EditorialGenerationError(
                "Il servizio OpenAI è temporaneamente non disponibile; riprova più tardi"
            )
        return getattr(response, "output_parsed", None)

    def enrich(
        self, source: IntermediateReportV11, cancellation_event: Event | None = None,
    ) -> EditorialDraft:
        payload = source.model_dump_json(by_alias=True, exclude_none=True)
        instructions = EDITORIAL_SYSTEM_PROMPT
        for attempt in range(2):
            parsed = self._request(
                instructions=instructions,
                payload=payload,
                cancellation_event=cancellation_event,
            )
            errors: list[str]
            try:
                draft = EditorialDraft.model_validate(_as_detached_json(parsed))
            except (ValidationError, ValueError, TypeError):
                errors = ["JSON non valido o fuori schema"]
            else:
                errors = validate_editorial_draft(draft, source)
                if not errors:
                    return draft
            instructions = (
                f"{EDITORIAL_REPAIR_PROMPT}\n\nErrori da correggere:\n"
                + "\n".join(f"- {error}" for error in errors)
            )
        raise EditorialGenerationError(
            "La bozza editoriale non rispetta il contratto dopo la correzione"
        )


__all__ = [
    "EditorialBlock",
    "EditorialCourse",
    "EditorialDraft",
    "EditorialEnricher",
    "EditorialFaq",
    "EditorialGenerationError",
    "EditorialIntervention",
    "didactic_intervention_ids",
    "validate_editorial_draft",
]
