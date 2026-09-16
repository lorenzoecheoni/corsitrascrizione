import copy
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.editorial import (
    EditorialDraft,
    EditorialEnricher,
    EditorialGenerationError,
    validate_editorial_draft,
)
from app.intermediate_report import build_intermediate_report

from granular_support import governance_report, valid_editorial_draft


GUID = UUID("00000000-0000-0000-0000-000000000001")


def source():
    return build_intermediate_report(governance_report(), GUID)


class FakeClient:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.responses = self
        self.with_raw_response = SimpleNamespace(parse=self.raw_parse)

    def raw_parse(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        response = SimpleNamespace(status="completed", output_parsed=outcome)
        return SimpleNamespace(headers={}, parse=lambda: response)


def test_valid_draft_passes_without_repair():
    client = FakeClient(valid_editorial_draft())

    draft = EditorialEnricher(client).enrich(source())

    assert isinstance(draft, EditorialDraft)
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["model"] == "gpt-5.6-luna"
    assert call["store"] is False
    assert call["text_format"].__name__ == "EditorialDraft"


def test_unknown_intervention_id_triggers_repair():
    broken = valid_editorial_draft()
    broken["interventi"][0]["id"] = "v1-i999"
    client = FakeClient(broken, valid_editorial_draft())

    draft = EditorialEnricher(client).enrich(source())

    assert isinstance(draft, EditorialDraft)
    assert len(client.calls) == 2
    repair = client.calls[1]
    assert "Errori da correggere" in repair["instructions"]
    assert "non presenti nel report" in repair["instructions"]


def test_area_outside_the_closed_list_triggers_repair():
    broken = valid_editorial_draft()
    broken["corso"]["area"] = "Area inventata"
    client = FakeClient(broken, valid_editorial_draft())

    EditorialEnricher(client).enrich(source())

    assert len(client.calls) == 2
    assert "area" in client.calls[1]["instructions"]


def test_two_invalid_drafts_raise_safe_error():
    broken = valid_editorial_draft()
    broken["corso"]["area"] = "Area inventata"
    client = FakeClient(broken, broken)

    with pytest.raises(EditorialGenerationError, match="contratto"):
        EditorialEnricher(client).enrich(source())


def test_openai_failure_raises_safe_error_without_details():
    client = FakeClient(RuntimeError("private token abc123"))

    with pytest.raises(EditorialGenerationError) as excinfo:
        EditorialEnricher(client).enrich(source())

    assert "abc123" not in str(excinfo.value)


def test_validate_editorial_draft_reports_missing_coverage():
    draft = EditorialDraft.model_validate(valid_editorial_draft())
    draft.interventi = draft.interventi[1:]

    errors = validate_editorial_draft(draft, source())

    assert any("non copre tutti gli interventi" in error for error in errors)


def test_validate_editorial_draft_reports_missing_block_coverage():
    draft = EditorialDraft.model_validate(valid_editorial_draft())
    draft.blocchi = draft.blocchi[:1]

    errors = validate_editorial_draft(draft, source())

    assert any("non copre tutti i blocchi" in error for error in errors)


def test_validate_editorial_draft_reports_long_lesson_title():
    draft = EditorialDraft.model_validate(valid_editorial_draft())
    draft.interventi[0].titolo_lezione = "Titolo " + "lungo " * 20

    errors = validate_editorial_draft(draft, source())

    assert any("70 caratteri" in error for error in errors)
