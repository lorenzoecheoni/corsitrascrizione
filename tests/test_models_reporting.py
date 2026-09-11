import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.costs import estimate_cost
from app.models import AcademyReport, SpeakerProfile
from app.reporting import format_timestamp, render_markdown, render_text


def load_report() -> AcademyReport:
    data = json.loads(Path("tests/fixtures/report.json").read_text())
    return AcademyReport.model_validate(data)


def test_report_fixture_covers_multi_speaker_session() -> None:
    report = load_report()
    assert len(report.speakers) == 3
    assert len(report.interventions[1].speaker_ids) == 2
    assert report.slides[0].timestamp_seconds == 95


def test_one_hour_cost_is_in_approved_range() -> None:
    cost = estimate_cost(duration_seconds=3600, downloaded_bytes=450_000_000)
    assert 0.40 <= cost.estimated_low_usd <= cost.estimated_high_usd <= 0.70


def test_markdown_and_text_are_exportable() -> None:
    report = load_report()
    markdown = render_markdown(report)
    plain = render_text(report)
    assert "## Relatori" in markdown
    assert "01:35" in markdown
    assert "Relatori" in plain
    assert format_timestamp(3661) == "1:01:01"


def test_speaker_rejects_personal_name_with_inference_only() -> None:
    with pytest.raises(ValidationError, match="evidenza ammessa"):
        SpeakerProfile.model_validate(
            {
                "id": "relatore-3",
                "display_name": "Elena Verdi",
                "confidence": "bassa",
                "evidence": [
                    {
                        "kind": "inferenza",
                        "note": "Voce distinta senza identificazione supportata.",
                    }
                ],
            }
        )


def test_speaker_accepts_generic_label_without_admissible_evidence() -> None:
    speaker = SpeakerProfile.model_validate(
        {
            "id": "relatore-3",
            "display_name": "Relatore 3",
            "confidence": "bassa",
            "evidence": [
                {
                    "kind": "inferenza",
                    "note": "Voce distinta senza identificazione supportata.",
                }
            ],
        }
    )
    assert speaker.display_name == "Relatore 3"


@pytest.mark.parametrize("field,value", [("duration_seconds", -1), ("duration_seconds", float("nan")),
    ("duration_seconds", float("inf"))])
def test_report_rejects_nonfinite_negative_duration(field, value):
    data = load_report().model_dump()
    data[field] = value
    with pytest.raises(ValueError):
        AcademyReport.model_validate(data)


@pytest.mark.parametrize("section,field,value", [
    ("chapters", "end_seconds", -1), ("interventions", "end_seconds", -1),
    ("slides", "timestamp_seconds", float("inf")),
])
def test_report_rejects_invalid_timestamps(section, field, value):
    data = load_report().model_dump()
    data[section][0][field] = value
    with pytest.raises(ValueError):
        AcademyReport.model_validate(data)


@pytest.mark.parametrize("section", ["chapters", "interventions"])
def test_report_rejects_reversed_positive_intervals(section):
    data = load_report().model_dump()
    data[section][0].update(start_seconds=5, end_seconds=4)
    with pytest.raises(ValueError):
        AcademyReport.model_validate(data)


def test_cost_rejects_negative_nan_and_reversed_bounds():
    for changes in ({"estimated_low_usd": -1}, {"analysis_usd": float("nan")},
                    {"estimated_low_usd": 10, "estimated_high_usd": 1}):
        data = load_report().model_dump()
        data["cost"].update(changes)
        with pytest.raises(ValueError):
            AcademyReport.model_validate(data)


def test_original_title_usage_and_refined_cost_are_rendered():
    from app.models import APIUsage, ProviderUsage, UsageEntry
    usage = APIUsage(transcription=ProviderUsage(entries=[UsageEntry(input_tokens=1000, output_tokens=100,
        request_audio_seconds=3600)]), responses=ProviderUsage(entries=[UsageEntry(input_tokens=2000, output_tokens=200)]))
    report = load_report()
    report.bunny_title = "Titolo originale Bunny"
    report.usage = usage
    report.cost = estimate_cost(3600, 0, usage=usage)
    assert report.cost.transcription_usd == .0035
    assert report.cost.analysis_usd == .0006
    for rendered in (render_markdown(report), render_text(report)):
        assert "Titolo originale Bunny" in rendered
        assert "1000" in rendered and "2000" in rendered
        assert "fattura" in rendered
