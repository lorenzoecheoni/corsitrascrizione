import json
from pathlib import Path

from app.costs import estimate_cost
from app.models import AcademyReport
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
