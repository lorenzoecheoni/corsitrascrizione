import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Confidence = Literal["alta", "media", "bassa"]
GENERIC_SPEAKER_LABEL = re.compile(r"^Relatore [1-9]\d*$")
IDENTITY_EVIDENCE_KINDS = {"introduzione", "sottopancia", "slide", "metadata"}
Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Counter = Annotated[int, Field(ge=0, strict=True)]


class ReportModel(BaseModel):
    """Base model that rejects report fields outside the documented contract."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Evidence(ReportModel):
    kind: Literal["introduzione", "sottopancia", "slide", "metadata", "inferenza"]
    timestamp_seconds: Nonnegative | None = None
    note: str


class SpeakerProfile(ReportModel):
    id: str
    display_name: str
    role: str | None = None
    confidence: Confidence
    evidence: list[Evidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def requires_identity_evidence_for_personal_name(self) -> "SpeakerProfile":
        """Allow named speakers only when the report contains permitted evidence."""
        if GENERIC_SPEAKER_LABEL.fullmatch(self.display_name):
            return self
        if any(evidence.kind in IDENTITY_EVIDENCE_KINDS for evidence in self.evidence):
            return self
        raise ValueError(
            "Un nome personale richiede almeno un'evidenza ammessa: "
            "introduzione, sottopancia, slide o metadata."
        )


class TimeInterval(ReportModel):
    start_seconds: Nonnegative
    end_seconds: Nonnegative

    @model_validator(mode="after")
    def ordered_interval(self):
        if self.end_seconds < self.start_seconds:
            raise ValueError("La fine precede l'inizio")
        return self


class Intervention(TimeInterval):
    speaker_ids: list[str]
    summary: str


class Chapter(TimeInterval):
    title: str
    summary: str


class SlideChange(ReportModel):
    timestamp_seconds: Nonnegative
    title: str | None = None
    visible_content: list[str] = Field(default_factory=list)
    confidence: Confidence


class CostEstimate(ReportModel):
    estimated_low_usd: Nonnegative
    estimated_high_usd: Nonnegative
    bunny_bandwidth_usd: Nonnegative
    transcription_usd: Nonnegative
    analysis_usd: Nonnegative
    basis: str

    @model_validator(mode="after")
    def ordered_cost(self):
        if self.estimated_high_usd < self.estimated_low_usd:
            raise ValueError("Intervallo dei costi non valido")
        return self


class UsageEntry(ReportModel):
    """One attempted API request; unknown counters remain absent, never zero."""
    input_tokens: Counter | None = None
    output_tokens: Counter | None = None
    audio_input_tokens: Counter | None = None
    provider_audio_seconds: Nonnegative | None = None
    request_audio_seconds: Nonnegative = 0


class ProviderUsage(ReportModel):
    entries: list[UsageEntry] = Field(default_factory=list)

    @property
    def requests(self) -> int:
        return len(self.entries)

    def _sum(self, field: str):
        values = [getattr(entry, field) for entry in self.entries if getattr(entry, field) is not None]
        return sum(values) if values else None

    @property
    def input_tokens(self) -> int | None:
        return self._sum("input_tokens")

    @property
    def output_tokens(self) -> int | None:
        return self._sum("output_tokens")

    @property
    def provider_audio_seconds(self) -> float | None:
        return self._sum("provider_audio_seconds")

    @property
    def missing_input_requests(self) -> int:
        return sum(entry.input_tokens is None for entry in self.entries)

    @property
    def missing_output_requests(self) -> int:
        return sum(entry.output_tokens is None for entry in self.entries)


class APIUsage(ReportModel):
    transcription: ProviderUsage = Field(default_factory=ProviderUsage)
    responses: ProviderUsage = Field(default_factory=ProviderUsage)


class AcademyContent(ReportModel):
    title: str
    duration_seconds: Nonnegative
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
    bunny_title: str = ""
    usage: APIUsage = Field(default_factory=APIUsage)


class AnalysisResult(AcademyContent):
    """Application-owned usage, added after the strict AcademyContent parse."""
    usage: ProviderUsage = Field(default_factory=ProviderUsage)
