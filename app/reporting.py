"""Text-only renderers for Academy reports."""

import re

from app.models import AcademyReport


def format_timestamp(seconds: float) -> str:
    """Format seconds as MM:SS, switching to H:MM:SS for hour-long media."""
    total_seconds = max(0, round(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{remaining_seconds:02d}"
    return f"{minutes:02d}:{remaining_seconds:02d}"


def _csv(items: list[str]) -> str:
    return ", ".join(items) if items else "Nessuno"


def _speaker_name(report: AcademyReport, speaker_id: str) -> str:
    return next(
        (speaker.display_name for speaker in report.speakers if speaker.id == speaker_id),
        speaker_id,
    )


def render_markdown(report: AcademyReport) -> str:
    """Render a stable, human-readable Markdown representation of a report."""
    lines = [
        f"# {report.title}",
        "",
        f"Durata: {format_timestamp(report.duration_seconds)}",
        f"Lingua rilevata: {report.detected_language}",
        "",
        "## Sintesi",
        report.synopsis,
        "",
        "## Descrizione estesa",
        report.extended_description,
        "",
        "## Pubblico e obiettivi",
        f"- Destinatari: {_csv(report.target_audience)}",
        f"- Prerequisiti: {_csv(report.prerequisites)}",
        f"- Obiettivi: {_csv(report.learning_objectives)}",
        "",
        "## Relatori",
    ]
    for speaker in report.speakers:
        role = f" — {speaker.role}" if speaker.role else ""
        lines.append(f"- {speaker.display_name}{role} (confidenza: {speaker.confidence})")
        for evidence in speaker.evidence:
            timestamp = (
                f" [{format_timestamp(evidence.timestamp_seconds)}]"
                if evidence.timestamp_seconds is not None
                else ""
            )
            lines.append(f"  - Evidenza{timestamp}: {evidence.note}")

    lines.extend(["", "## Capitoli"])
    for chapter in report.chapters:
        lines.append(
            f"- {format_timestamp(chapter.start_seconds)}–{format_timestamp(chapter.end_seconds)}: "
            f"{chapter.title} — {chapter.summary}"
        )

    lines.extend(["", "## Interventi"])
    for intervention in report.interventions:
        speakers = ", ".join(
            _speaker_name(report, speaker_id) for speaker_id in intervention.speaker_ids
        )
        lines.append(
            f"- {format_timestamp(intervention.start_seconds)}–{format_timestamp(intervention.end_seconds)}: "
            f"{speakers} — {intervention.summary}"
        )

    lines.extend(["", "## Slide"])
    for slide in report.slides:
        title = slide.title or "Senza titolo"
        content = f" — {_csv(slide.visible_content)}" if slide.visible_content else ""
        lines.append(
            f"- {format_timestamp(slide.timestamp_seconds)}: {title}{content} "
            f"(confidenza: {slide.confidence})"
        )

    lines.extend(
        [
            "",
            "## Argomenti e parole chiave",
            f"- Argomenti: {_csv(report.topics)}",
            f"- Parole chiave: {_csv(report.keywords)}",
            "",
            "## Punti chiave",
        ]
    )
    lines.extend(f"- {takeaway}" for takeaway in report.key_takeaways)
    lines.extend(["", "## Incertezze"])
    lines.extend(f"- {uncertainty}" for uncertainty in report.uncertainties)
    lines.extend(
        [
            "",
            "## Stima costi (USD)",
            f"- Intervallo stimato: ${report.cost.estimated_low_usd:.4f}–${report.cost.estimated_high_usd:.4f}",
            f"- Banda Bunny: ${report.cost.bunny_bandwidth_usd:.4f}",
            f"- Trascrizione: ${report.cost.transcription_usd:.4f}",
            f"- Analisi: ${report.cost.analysis_usd:.4f}",
            f"- Base della stima: {report.cost.basis}",
        ]
    )
    return "\n".join(lines)


def render_text(report: AcademyReport) -> str:
    """Render the report as plain text without Markdown syntax."""
    markdown = render_markdown(report)
    without_headings = re.sub(r"(?m)^#{1,6}\s+", "", markdown)
    return re.sub(r"(?m)^\s*-\s+", "", without_headings)
