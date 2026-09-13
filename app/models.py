import re
from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator


Confidence = Literal["alta", "media", "bassa"]
GENERIC_SPEAKER_LABEL = re.compile(r"^Relatore [1-9]\d*$")
IDENTITY_EVIDENCE_KINDS = {"introduzione", "sottopancia", "slide", "metadata"}
Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Counter = Annotated[int, Field(ge=0, strict=True)]
UnitConfidence = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
InterventionKind = Literal[
    "intervento", "saluti", "logistica", "domande", "pausa", "cambio_relatore"
]
GENERIC_INTERVENTION_SPEAKER = re.compile(
    r"^(?:relatore|speaker)(?:[\s_-]*\d+)?$", re.IGNORECASE
)


def _parse_intervention_second(value: object) -> int | float:
    if isinstance(value, bool):
        raise ValueError("Il tempo non può essere booleano")
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str) or not re.fullmatch(r"\d+:[0-5]\d:[0-5]\d", value):
        raise ValueError("Il tempo deve essere h:mm:ss")
    hours, minutes, seconds = (int(part) for part in value.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def _format_intervention_second(value: float) -> str:
    seconds = int(value + 0.5)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


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


class SlideChange(ReportModel):
    timestamp_seconds: Nonnegative
    title: str | None = None
    visible_content: list[str] = Field(default_factory=list)
    confidence: Confidence


class Intervention(ReportModel):
    """One factual timeline segment, stored numerically and exported as h:mm:ss."""

    id: str
    start_seconds: Nonnegative = Field(
        validation_alias=AliasChoices("start_seconds", "inizio"), serialization_alias="inizio"
    )
    end_seconds: Nonnegative = Field(
        validation_alias=AliasChoices("end_seconds", "fine"), serialization_alias="fine"
    )
    tipo: InterventionKind
    relatori: list[str] = Field(default_factory=list)
    titolo: str
    sintesi: str
    punti_chiave: list[str] = Field(default_factory=list)
    confidenza: UnitConfidence

    @field_validator("start_seconds", "end_seconds", mode="before")
    @classmethod
    def parse_timestamp(cls, value: object) -> object:
        return _parse_intervention_second(value)

    @field_serializer("start_seconds", "end_seconds")
    def serialize_timestamp(self, value: float) -> str:
        return _format_intervention_second(value)

    @field_validator("relatori", mode="before")
    @classmethod
    def omit_generic_speaker_labels(cls, value: object) -> object:
        if value is None:
            return []
        if not isinstance(value, list):
            return value
        return [
            name.strip()
            for name in value
            if isinstance(name, str)
            and name.strip()
            and not GENERIC_INTERVENTION_SPEAKER.fullmatch(name.strip())
        ]

    @model_validator(mode="after")
    def validate_segment(self) -> "Intervention":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("fine deve essere successiva a inizio")
        if self.tipo == "intervento" and not 3 <= len(self.punti_chiave) <= 7:
            raise ValueError("un intervento richiede da 3 a 7 punti_chiave")
        if self.tipo != "intervento" and len(self.punti_chiave) > 7:
            raise ValueError("punti_chiave ammette al massimo 7 elementi")
        return self


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
    speakers: list[SpeakerProfile]
    slides: list[SlideChange]
    uncertainties: list[str]
    interventions: list[Intervention] = Field(default_factory=list)


class AcademyReport(AcademyContent):
    cost: CostEstimate
    bunny_title: str = ""
    usage: APIUsage = Field(default_factory=APIUsage)


class AnalysisResult(AcademyContent):
    """Application-owned usage, added after the strict AcademyContent parse."""
    usage: ProviderUsage = Field(default_factory=ProviderUsage)
