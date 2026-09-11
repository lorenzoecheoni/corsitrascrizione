from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


Confidence = Literal["alta", "media", "bassa"]


class ReportModel(BaseModel):
    """Base model that rejects report fields outside the documented contract."""

    model_config = ConfigDict(extra="forbid")


class Evidence(ReportModel):
    kind: Literal["introduzione", "sottopancia", "slide", "metadata", "inferenza"]
    timestamp_seconds: float | None = None
    note: str


class SpeakerProfile(ReportModel):
    id: str
    display_name: str
    role: str | None = None
    confidence: Confidence
    evidence: list[Evidence] = Field(default_factory=list)


class Intervention(ReportModel):
    start_seconds: float
    end_seconds: float
    speaker_ids: list[str]
    summary: str


class Chapter(ReportModel):
    start_seconds: float
    end_seconds: float
    title: str
    summary: str


class SlideChange(ReportModel):
    timestamp_seconds: float
    title: str | None = None
    visible_content: list[str] = Field(default_factory=list)
    confidence: Confidence


class CostEstimate(ReportModel):
    estimated_low_usd: float
    estimated_high_usd: float
    bunny_bandwidth_usd: float
    transcription_usd: float
    analysis_usd: float
    basis: str


class AcademyContent(ReportModel):
    title: str
    duration_seconds: float
    detected_language: str
    synopsis: str
    extended_description: str
    target_audience: list[str]
    prerequisites: list[str]
    learning_objectives: list[str]
    speakers: list[SpeakerProfile]
    interventions: list[Intervention]
    chapters: list[Chapter]
    slides: list[SlideChange]
    topics: list[str]
    keywords: list[str]
    key_takeaways: list[str]
    uncertainties: list[str]


class AcademyReport(AcademyContent):
    cost: CostEstimate
