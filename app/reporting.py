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


def render_markdown(report: AcademyReport) -> str:
    """Render a stable, human-readable Markdown representation of a report."""
    lines = [
        f"# {report.title}",
        "",
        f"Titolo originale Bunny: {report.bunny_title or 'Non disponibile'}",
        f"Durata: {format_timestamp(report.duration_seconds)}",
        f"Lingua rilevata: {report.detected_language}",
        "",
        "## Sintesi",
        report.synopsis,
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

    lines.extend(["", "## Slide"])
    for slide in report.slides:
        title = slide.title or "Senza titolo"
        content = f" — {_csv(slide.visible_content)}" if slide.visible_content else ""
        lines.append(
            f"- {format_timestamp(slide.timestamp_seconds)}: {title}{content} "
            f"(confidenza: {slide.confidence})"
        )

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
    lines.extend(["", "## Utilizzo API restituito"])
    for name, usage in (("Trascrizione", report.usage.transcription), ("Responses", report.usage.responses)):
        def shown(value):
            return str(value) if value is not None else "non disponibile"
        lines.append(f"- {name}: {usage.requests} richieste; token input {shown(usage.input_tokens)}; "
                     f"token output {shown(usage.output_tokens)}.")
        lines.append(f"  Contatori mancanti: input in {usage.missing_input_requests} richieste, "
                     f"output in {usage.missing_output_requests} richieste.")
        if not usage.entries:
            lines.append("  Utilizzo del provider non disponibile; stima da durata.")
        if name == "Trascrizione":
            lines.append(f"  Secondi audio restituiti dal provider: {shown(usage.provider_audio_seconds)}.")
    lines.append("Il costo monetario è una stima applicativa e non sostituisce la fattura dei fornitori.")
    return "\n".join(lines)


def render_text(report: AcademyReport) -> str:
    """Render the report as plain text without Markdown syntax."""
    markdown = render_markdown(report)
    without_headings = re.sub(r"(?m)^#{1,6}\s+", "", markdown)
    return re.sub(r"(?m)^\s*-\s+", "", without_headings)
