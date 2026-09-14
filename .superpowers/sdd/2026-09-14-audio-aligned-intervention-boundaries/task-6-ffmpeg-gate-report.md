# Task 6 — real FFmpeg gate correction report

Date: 2026-09-14

Base: `cf989704e05557b6ad29e58466912e03d546366f`

Worktree: `.worktrees/audio-aligned-boundaries`

## Root-cause confirmation (recorded before modifications)

The prescribed real gate was reproduced with FFmpeg 7.0 and the pinned project
environment:

```text
pytest -q tests/test_media.py tests/test_end_to_end.py::test_form_to_report_with_real_ffmpeg_and_ephemeral_cleanup
3 failed, 53 passed, 2 warnings in 9.77s
```

1. **Synthetic pause fixture syntax.** Running the fixture's exact lavfi value
   `anullsrc=r=48000:cl=mono:d=.8` against the gate binary reports
   `Unable to parse option value ".8" as duration` and `Invalid argument`.
   The failure occurs while constructing the test fixture, before production
   extraction. `0.8` expresses the same duration with portable syntax; the
   scenario and its timing tolerance need no change.
2. **Terminal media progress.** The 12-second real fixture completes one locally
   probed AAC chunk whose verified durations total approximately 12 seconds and
   produces frames at 0, 4, and 8 seconds, but the callback list is `[8.1]`.
   `FFmpegProcessor._extract` currently forwards only `out_time_us` from FFmpeg's
   global progress stream. With the sparse frame output finishing at the last
   selected frame, that clock is not a reliable terminal duration. The same
   function already obtains verified local audio chunk durations through
   `_read_chunks`; no additional source read or source probe is needed.
3. **Responses usage accounting.** A diagnostic wrapper around the real offline
   smoke fake observed, in order, `SlideBatchResult`, `WindowAnalysis`, and
   `ConsolidatedTextReport`. `OpenAIAnalyzer.analyze` passes the same
   `ProviderUsage` through slide classification, window analysis, and
   consolidation, and existing focused analysis tests assert three requests for
   that full path. The produced count of 3 is intentional and complete; only the
   stale smoke expectation says 2. Repository search found no second occurrence
   of that false report expectation.

After correcting only the fixture literal and the audio-path terminal callback,
the three-test gate exposed the same FFmpeg 7 progress-clock defect in the
previously unreachable visual-only extraction: `silencedetect` emitted closed
intervals through 5.200021 seconds while `out_time_us` ended at 1.0 second, the
duration of the sole sparse JPEG frame. Reordering the audio-null and JPEG outputs
did not change the terminal value. A diagnostic run showed that appending a
fixed-rate `aresample` plus end-of-stream `astats` to the existing decoded audio
leg reports 6208 samples at 1000 Hz for the 6.2-second fixture. This provides a
compact duration from the same decode and source read, without retained audio or
an extra probe, and avoids weakening silence bounds.

## TDD log

### RED

- Prescribed real gate: **3 failed, 53 passed**. The failures were terminal
  audio progress (`[8.1]` for a 12-second local chunk), FFmpeg 7 rejecting
  `d=.8`, and the stale smoke request count.
- Added
  `test_audio_extraction_finishes_progress_from_verified_local_chunk_duration`.
  Before production modification it failed as intended: expected `[8, 12]`,
  observed `[8.0]`.
- After the first minimal audio-path fix and fixture correction, the isolated
  visual test became reachable and failed with `SilenceEvidenceError`: FFmpeg
  emitted silence through 5.200021 seconds but terminal progress was 1.0.

### GREEN

- New focused audio-progress regression: **1 passed**.
- Final three previously failing tests: **3 passed**, twice; the final run after
  adding the explicit visual terminal-progress assertion took 1.47 seconds.
- Full media plus real smoke gate: **57 passed**, twice; final run 9.26 seconds.

Mutation checks: removing the local-chunk terminal callback restores `[8.0]`
and fails the new focused test; removing the in-pass sample duration restores
the visual-only failure (and its new `progress[-1] >= 6.1` assertion protects
the terminal callback); restoring `d=.8` fails fixture construction on FFmpeg
7; restoring request count 2 fails the completed offline report.

## Implementation

- `app/media.py`: after cancellation-safe extraction/deduplication, terminal
  progress is raised monotonically to the maximum verified local audio-chunk
  end for `include_audio=True`.
- `app/media.py`: the visual-only audio-null leg now chains fixed-rate
  `aresample=1000` and end-of-stream `astats` after `silencedetect`. Only the
  validated integer sample count is retained; it supplies the duration for
  strict silence bounds and terminal progress. This remains one FFmpeg source
  read, performs no source probe, and writes no audio artifact.
- `tests/test_media.py`: portable `0.8` lavfi duration; focused local-chunk
  regression; same-pass duration diagnostics in controlled mocks; explicit
  monotonic terminal progress in the real synthetic pause gate.
- `tests/test_end_to_end.py`: corrected the full analysis-path response usage
  expectation from 2 to 3. Production accounting was intentionally unchanged.

## Verification

All commands used the prescribed FFmpeg 7 PATH and pinned uv environment.

```text
pytest -q <three previously failing node IDs>
3 passed, 2 warnings in 1.47s

pytest -q tests/test_media.py tests/test_end_to_end.py::test_form_to_report_with_real_ffmpeg_and_ephemeral_cleanup
57 passed, 2 warnings in 9.26s

pytest -q tests/test_pipeline.py tests/test_analysis.py tests/test_analysis_chunks.py tests/test_models_reporting.py
218 passed in 1.90s

env -u RUN_LIVE_BUNNY -u RUN_LIVE_SYNTHETIC_ANALYSIS ... pytest -q --tb=short
972 passed, 3 skipped, 2 warnings in 31.59s

python -m compileall -q app tests
exit 0

git diff --check
exit 0
```

The warnings are the two pre-existing FastAPI/Starlette deprecations. No Node
gate was run because no frontend/Node asset changed. No live tests, provider
calls, network access, installs, push, deploy, or reanalysis were performed.

## Self-review

- **Scope/minimality:** production changes are limited to terminal-duration
  recovery for the two extraction modes. No usage behavior or unrelated code
  was changed.
- **One source read:** the FFmpeg command still contains exactly one source URL;
  the visual duration is produced by the existing decoded audio-null leg and
  the audio path reuses already-probed local chunks.
- **Privacy/storage:** parsers retain only integer sample count and compact
  silence intervals. Raw FFmpeg diagnostics, URLs, audio, and provider bodies
  are not returned, logged, or persisted. Visual-only tests confirm no `.m4a`
  or `audio.csv` output.
- **Cancellation/resources:** the existing checks before launch, after process,
  during local probes/deduplication, and immediately before terminal callback
  remain intact; `_run_process` still owns stop/reap on parser errors.
- **Monotonicity/accounting:** terminal callbacks occur only when the verified
  duration exceeds observed progress. Download byte estimation and the single
  extraction command are unchanged. Complete response usage remains three.
- **Portability/fail-closed behavior:** FFmpeg 7 real integration and stereo
  `astats` diagnostics were checked. Missing, non-positive, fractional, or
  contradictory duration sample evidence cannot authorize silence boundaries.
