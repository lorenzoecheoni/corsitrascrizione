# Audio-aligned intervention boundaries

## Purpose

Every internal boundary in a per-video intermediate report must be a verified
editorial cut, not merely a rounded transcription or slide timestamp. The cut
must respect the end of a complete thought and be positioned using the actual
speech pause in the audio. Each cut must also carry enough neighboring words
for a human or the Academy converter to inspect it without listening again.

This design applies to every newly analyzed video. Existing saved reports do
not contain word-level or silence evidence and must be reanalyzed before their
JSON can claim conformance. The existing Governance video will be reanalyzed
once after deployment.

## Approved product rules

- Boundaries are derived from speech and semantic completion. Slide changes do
  not define boundaries.
- A segment may not end inside a sentence, example, explanation, or answer to a
  question. If the thought continues, the semantic group extends to its close.
- A moderator between two interventions is a separate segment. The preceding
  intervention ends before the moderator's first word, never after it.
- Every internal transition between two consecutive final segments has one
  `CONFINE` warning. If a report has `N` segments, it has exactly `N - 1`
  `CONFINE` warnings.
- The absolute start at `0:00:00` and the final end of the video are excluded.
- Segment boundaries are contiguous: the previous `fine` equals the following
  `inizio` at the same whole second.
- For a speech pause of at least two seconds, the spoken segment ends one second
  after its last word. A pause segment occupies the remaining silent middle and
  ends one second before the next spoken word.
- For a speech pause shorter than two seconds, two spoken segments meet at the
  midpoint of the pause.
- With no speech pause, both segments meet at the end of the previous segment's
  last word.
- The selected position is expressed to the nearest whole second, kept within
  the measured pause whenever a whole second exists inside it.
- Boundary evidence uses transcript words verbatim. A side with fewer than five
  available words or adjacent silence includes the available words and the
  marker `(pausa)`.
- If complete word timing or audio-silence evidence is unavailable, the analysis
  fails with fixed application text. It must not publish invented or merely
  text-derived boundaries.

## Chosen architecture

The existing single analysis pass is extended rather than adding a provider or
a second downloadable artifact:

1. AssemblyAI's existing diarized response supplies word-level `text`, `start`,
   `end`, confidence, and speaker fields for every utterance. These values stay
   in memory during analysis.
2. FFmpeg's existing visual extraction pass decodes the audio stream through
   `silencedetect` while still writing audio to the null muxer. It records only
   compact silence intervals; no audio file is created by the fast path.
3. The existing AI window analysis continues to group complete chronological
   transcript utterances into semantic interventions. Its prompt is strengthened
   to forbid a group boundary inside a sentence, example, explanation, answer,
   or unfinished thought, and to place moderator speech in its own appropriate
   `saluti`, `domande`, or `cambio_relatore` segment.
4. A deterministic local boundary aligner combines those semantic groups,
   word timestamps, and measured silence intervals. It materializes contiguous
   final segments and compact boundary evidence before the temporary transcript
   and media objects are destroyed.
5. Only the final `AcademyReport`, including its compact boundary evidence, is
   saved in SQLite. Full transcript text, word arrays, audio, frames, and FFmpeg
   diagnostics remain ephemeral.

AssemblyAI documents word-level timestamps inside diarized utterances at
<https://www.assemblyai.com/docs/pre-recorded-audio/label-speakers>. FFmpeg
documents `silencedetect` as a metadata filter at
<https://www.ffmpeg.org/ffmpeg-filters.html#silencedetect>.

This design requires the configured AssemblyAI fast path for conforming new
reports. The OpenAI diarized fallback does not provide the required persisted
word evidence in the application's current response format and therefore fails
closed for this workflow instead of approximating it.

## Data model

### Ephemeral transcription evidence

`TranscriptWord` is added to `app.transcription`:

```python
class TranscriptWord(BaseModel):
    text: str
    start_seconds: float
    end_seconds: float
    diarization_label: str
    confidence: float
```

`TranscriptSegment` gains a stable `source_utterance_id` and
`words: list[TranscriptWord]`. The word list is excluded from
`TranscriptWindow.to_payload()`: the AI continues to receive only the bounded
utterance text, timing, speaker label, and source utterance ID. AssemblyAI parsing
requires a valid, ordered, non-empty word list for every non-empty utterance.
The word text concatenation need not reproduce punctuation byte-for-byte, but
all words must fall within their utterance interval and use the utterance's
speaker label. Invalid or missing word timing raises the existing fixed
transcription-response text in the dedicated `WordEvidenceError` subtype; the
pipeline maps that subtype to the fixed boundary-verification failure.

`TranscriptionResult` retains words only in memory. They are never included in
AI requests or repair prompts and never written to logs or SQLite directly.

### Ephemeral silence evidence

`SilenceInterval` is added to `app.media`:

```python
@dataclass(frozen=True)
class SilenceInterval:
    start_seconds: float
    end_seconds: float
```

`MediaArtifacts` gains `silence_intervals: list[SilenceInterval]`. The fast
FFmpeg command uses one audio decode with `silencedetect=n=-45dB:d=0.15` and a
null output. The parser accepts only finite, ordered pairs inside the media
duration; an open silence at end-of-file is closed at the verified duration.
Malformed, incomplete, or contradictory filter events cause a fixed media
error. Raw FFmpeg lines are never stored or logged. Silence parser failures and
missing audio/filter evidence use `SilenceEvidenceError`, mapped to the same
boundary failure by the pipeline. The duration-consistency tolerance is the
sum of half-rounding units of the three printed numbers (at least six
significant digits, plus float ULPs), not an arbitrary time allowance. Missing
positive scan progress is invalid.

The thresholds are deterministic application constants, not model choices. A
150 ms minimum detects brief speech pauses while the boundary rules decide
whether the measured pause is shorter than or at least two seconds.

### Persisted compact evidence

`AcademyReport` gains `boundaries`, defaulting to an empty list so historical
rows remain readable:

```python
class BoundaryEvidence(ReportModel):
    previous_intervention_id: str
    next_intervention_id: str
    boundary_seconds: int
    words_before: list[str]  # zero to five, verbatim
    words_after: list[str]   # zero to five, verbatim
    pause_before: bool
    pause_after: bool
    rule: Literal["long_pause", "short_pause", "no_pause"]
```

The model requires a whole nonnegative second, at most five non-empty words on
each side, different consecutive IDs, and one entry for each internal pair.
The relationship between boundary evidence and interventions is validated by a
local completeness function because historical `AcademyReport` rows are still
allowed to deserialize with `boundaries=[]`.

No transcript, full sentence, waveform, silence list, or audio reference is
persisted.

`AcademyContent` also carries application-owned `audio_boundary_version`, an
optional strict integer whose only verified value is `1`. It is assigned only
to successfully aligned analysis content, validated again by the pipeline,
and persisted with the completed report. Historical JSON defaults to `None`;
the version is required even when one segment has zero internal boundaries.
It is not an AI output field or a new field in the intermediate v1 envelope.

## Semantic grouping

The AI remains responsible only for editorial meaning, not the final time:

- Each `WindowInterventionDraft.segment_indexes` is an ordered run of complete
  provider utterances.
- The prompt explicitly requires grouping all utterances that form one sentence,
  example, explanation, answer, or continuous reasoning.
- A moderator's utterances are never appended to the preceding substantive
  intervention. They form a granular segment typed from the existing enum.
- The existing local partition validator continues to require every transcript
  segment exactly once and in chronological order.
- Each window call after the first includes up to 3,000 serialized characters
  of `previous_context`: the preceding local group's type, title, summary and
  final utterances, excluding word arrays. Any omitted prefix is explicit.
  Payload splitting reserves that space inside the unchanged 12,000-character
  request cap; the 600-second cap remains on the current window.
- `WindowAnalysis.previous_continuity` must say `continue` or `separate` for
  distinct-source margins. Only `continue` joins the neighboring groups before
  alignment; speaker equality alone never does. `None` or `unresolved` fails
  closed without a repair that lacks transcript context. Copies of the same
  source utterance remain deterministically indivisible; a merge involving
  additional sources requires an explicit continuity decision. Incompatible
  group types fail closed, preserving a separately classified moderator.
- AI drafts contain spoken types only. A `pausa` draft is invalid, and the
  aligner also rejects supplied pause groups. Only its measured-silence rule
  may generate final pause segments; an AI label never discards spoken words.
- If payload bounding divides one unusually long provider utterance into pieces,
  all pieces keep the same `source_utterance_id`. Adjacent drafts containing
  pieces of that same utterance are merged locally before alignment, including
  across window boundaries, so no final cut can split the source utterance.
- The local aligner never splits an utterance. If the AI cannot produce a valid
  complete partition after the existing repair policy, analysis fails.

This is intentionally conservative: a single long utterance may yield a longer
intervention rather than an unsafe cut inside it.

## Deterministic boundary alignment

The new `app.boundaries` module exposes a pure function conceptually equivalent
to:

```python
align_intervention_boundaries(
    duration_seconds,
    semantic_groups,
    transcript_segments,
    silence_intervals,
) -> tuple[list[Intervention], list[BoundaryEvidence]]
```

The algorithm operates in chronological order:

1. Map each semantic group to its first and last transcript word. A spoken group
   without both values is invalid.
2. Preserve explicitly classified spoken groups, including moderator segments.
3. Between each pair of spoken groups, find the measured silence interval that
   overlaps the open range from the previous last-word end to the next first-word
   start. Slide timestamps are not consulted.
4. If measured silence is at least two seconds, end the previous spoken segment
   one second after its last word and begin the next spoken segment one second
   before its first word. When at least one whole second remains between those
   positions, materialize one `pausa` segment for that middle interval.
5. If measured silence is positive but shorter than two seconds, do not create a
   separate pause; choose the midpoint.
6. If there is no measured pause, choose the previous last-word end.
7. Convert the candidate to one integer second. Prefer the nearest integer inside
   the measured silence; ties choose the earlier second. Clamp only within the
   adjacent spoken groups and reject an empty or reversed final segment.
8. Set `previous.end_seconds == next.start_seconds` for every final neighboring
   pair, including generated pauses.
9. Generate exactly one `BoundaryEvidence` for every final neighboring pair.

For a generated pause there are two boundaries. On the spoken-to-pause boundary,
the after side is marked `(pausa)`; on the pause-to-spoken boundary, the before
side is marked `(pausa)`. For a cut within a shorter silence, both sides retain
the nearest transcript words and the relevant pause flag.

The aligner does not mutate provider transcription, semantic drafts, or media
artifacts.

## Five-word evidence

For each final internal boundary:

- `words_before` contains the nearest zero to five transcript words belonging to
  spoken content before the boundary, in original order.
- `words_after` contains the nearest zero to five transcript words belonging to
  spoken content after the boundary, in original order.
- A side is marked as a pause when the adjacent final segment is a pause, when no
  word occurs in the adjacent one-second region, or when fewer than five words
  exist on that side.
- Text is copied verbatim from AssemblyAI word objects. It is not regenerated,
  corrected, or translated by an LLM.

The intermediate JSON does not add a second boundary collection. The compact
evidence is rendered as verification requests.

## Intermediate-report contract

`CONFINE` is added to the exact warning-code literal. It is invalid with
`livello: critico`.

For each internal pair, the v1.1 builder emits:

```json
{
  "livello": "avviso",
  "codice": "CONFINE",
  "video": "v1",
  "intervento": "v1-i004",
  "campo": "inizio",
  "messaggio": "Confine 0:42:17; termina v1-i003. Prima: «ultime cinque parole». Dopo: «prime cinque parole»."
}
```

The warning is attached to the segment that starts. The message contains the
same whole-second boundary, the exported ID of the segment that ends, and both
word excerpts. When a side has silence or fewer than five words, its clause ends
with ` (pausa)`. With no available words the clause is `Prima: (pausa)` or
`Dopo: (pausa)`.

IDs in stored evidence are mapped to the exported chronological `v1-iNNN` IDs
after all deterministic segment materialization. Deduplication may not collapse
two different internal pairs. `CONFINE` warnings do not change `stato` to
`da_verificare`; they are expected review evidence.

The report still uses integer `versione: 1`, remains strictly per video, and
retains all previously approved v1.1 fields and rules.

## Historical-report behavior

Historical `AcademyReport` JSON remains readable because `boundaries` defaults
to an empty list. However, a completed report is boundary-conformant only when:

- it has `audio_boundary_version == 1`, including a one-segment report;
- it has exactly one evidence item per internal intervention pair;
- every evidence item names the exact consecutive stored IDs;
- its second equals both the previous end and next start; and
- every item has valid word/pause evidence.

The JSON route returns fixed HTTP 409 text for a historical or incomplete report:
`Rianalisi necessaria per verificare i confini sull’audio`. It does not emit a
partially conforming JSON. Markdown and TXT remain downloadable as historical
human-readable reports.

The job and saved-report interfaces display a visible `Rianalisi necessaria`
state beside historical JSON exports and offer the existing per-video analysis
flow. No background mass reanalysis is started automatically.

## Failures and privacy

Boundary alignment is fail-closed. These conditions fail the analysis with the
fixed message `Verifica audio dei confini non riuscita; riprova`:

- AssemblyAI omits or returns invalid word timings;
- the video has no usable audio stream;
- FFmpeg silence events are malformed or incomplete;
- semantic groups cannot map to complete word-bearing utterances;
- integer alignment would create an empty/reversed segment;
- final adjacency or boundary-evidence completeness fails.

Provider response bodies, transcript words, FFmpeg diagnostics, Bunny URLs, and
audio data never appear in error messages or application logs. Cancellation
still terminates FFmpeg and deletes the remote AssemblyAI transcript. Temporary
workspaces keep their existing guaranteed cleanup.

## Cost and performance

No new API or subscription is required. Word timestamps are part of the existing
AssemblyAI diarization response, and FFmpeg performs silence detection in the
existing media pass. The change can increase Railway CPU time because the fast
visual path decodes audio instead of packet-copying it to the null muxer, but it
does not add a second Bunny download or API charge.

Reanalyzing a historical video, including Governance, consumes one normal
AssemblyAI transcription and the existing OpenAI analysis calls. Merely
downloading an already conforming JSON performs no provider analysis and spends
no transcription or AI credit.

Continuity is decided in the existing window call, with the same usage, cost
and cancellation tracking; there is no additional request per window margin.
The reserved previous-context budget can increase the number of windows for
dense transcripts. Bounded context can also require a safe failure when a
model cannot establish completion at a margin.

## Testing and acceptance

Tests are written before production changes and cover:

1. AssemblyAI word parsing, ordering, speaker mapping, malformed/missing words,
   cancellation, and remote transcript deletion.
2. FFmpeg `silencedetect` parsing without stored audio, including long silence,
   short silence, no silence, end-of-file silence, malformed events, cancellation,
   and safe diagnostics.
3. Pure boundary alignment with a pause of at least two seconds, a shorter pause,
   no pause, integer tie-breaking, generated pause segments, moderator segments,
   a continued thought, and invalid empty intervals.
4. Verbatim five-word extraction and `(pausa)` behavior for zero through five
   available words.
5. Exact adjacency and `N - 1` evidence for multiple segment types.
6. `CONFINE` model/severity validation, mapping to `v1-iNNN`, message text,
   nonblocking status, and non-deduplication of distinct pairs.
7. Historical report deserialization plus HTTP 409/UI behavior; Markdown/TXT
   compatibility remains green.
8. Persistence/privacy assertions proving no transcript, word array, audio path,
   provider body, or FFmpeg diagnostic escapes into SQLite or logs.
9. Full portable Python suite, media integration suite where FFmpeg is installed,
   all Node UI suites, distributable build, and authenticated production smoke.

Production acceptance for the Governance video requires a newly completed job
and verifies:

- the expected five reconciled speakers remain present;
- every pair of consecutive segments is exactly adjacent;
- the number of `CONFINE` warnings is `len(interventi) - 1`;
- every warning is attached to the next segment and names the previous exported
  ID, exact boundary second, and before/after text or `(pausa)`;
- no intervention boundary equals a slide timestamp merely because of the slide;
- stored boundary evidence survives a server restart and repeated downloads
  produce byte-stable JSON without reanalysis;
- audio/transcript artifacts are absent after completion.

Only after these checks pass is the historical Governance report superseded for
Academy use.
