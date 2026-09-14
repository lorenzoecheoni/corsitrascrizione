"""Ephemeral media extraction. Only the workspace context owns media cleanup.

The caller must supply a trusted, regenerated Bunny URL, not an arbitrary user
URL. Progress callbacks receive monotonic processed seconds, not percentages.
FFmpeg diagnostics are consumed privately and never logged or included in errors.
"""

from collections.abc import Callable, Iterator
from concurrent.futures import CancelledError
from contextlib import contextmanager
from contextvars import ContextVar
import csv
from dataclasses import dataclass, field
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import tempfile
from threading import Event
from time import monotonic

import imagehash
from PIL import Image


_TRANSCRIPTION_SEGMENT_SECONDS = 600
_OPENAI_MAX_AUDIO_SECONDS = 1400
_OPENAI_MAX_AUDIO_BYTES = 24_000_000
_DURATION_SAMPLE_RATE = 1000


@dataclass(frozen=True)
class AudioChunk:
    path: Path
    start_seconds: float
    duration_seconds: float


@dataclass(frozen=True)
class FrameCandidate:
    path: Path
    timestamp_seconds: float


@dataclass(frozen=True)
class SilenceInterval:
    start_seconds: float
    end_seconds: float

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.start_seconds)
            or not math.isfinite(self.end_seconds)
            or self.start_seconds < 0
            or self.end_seconds < 0
            or self.end_seconds < self.start_seconds
        ):
            raise ValueError("I limiti della pausa devono essere finiti, non negativi e ordinati")


@dataclass(frozen=True)
class MediaArtifacts:
    audio_chunks: list[AudioChunk]
    frame_candidates: list[FrameCandidate]
    downloaded_bytes: int
    # Conservative estimate: ceil(encoded input packet bytes * 1.20), gathered
    # from FFmpeg's single-pass input summary. Includes a 20% allowance for HLS
    # container/HTTP overhead. It is NOT measured network traffic or a guaranteed
    # upper bound: playlists, retries, encryption and transport overhead vary.
    # Output media sizes must never be substituted for input traffic.
    silence_intervals: list[SilenceInterval] = field(default_factory=list)
    # Empty measured evidence is distinct from a media path that never ran
    # silencedetect. This flag remains transient with the other artifacts.
    silence_measured: bool = False


class MediaError(RuntimeError):
    """Safe application-authored media error without upstream diagnostics."""


class MediaProtectedError(MediaError):
    """FFmpeg encountered an authorization or unsupported protection failure."""


class SilenceEvidenceError(MediaError):
    """Malformed or unavailable audio silence evidence with fixed safe text."""

    def __init__(self) -> None:
        super().__init__("Impossibile verificare le pause audio")


_SILENCE_NUMBER = r"[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eE][+-]?\d+)?"
_SILENCE_START = re.compile(
    rf"^\[silencedetect @ [^\]]+\] silence_start: (?P<seconds>{_SILENCE_NUMBER})\s*$"
)
_SILENCE_END = re.compile(
    rf"^\[silencedetect @ [^\]]+\] silence_end: (?P<seconds>{_SILENCE_NUMBER})"
    rf" \| silence_duration: (?P<duration>{_SILENCE_NUMBER})\s*$"
)
_SILENCE_PREFIX = re.compile(r"^\[silencedetect @ [^\]]+\] silence_(?:start|end)\b")


class _SilenceEvents:
    """Validate the small, transient subset of silencedetect diagnostics we need."""

    def __init__(self) -> None:
        self._intervals: list[SilenceInterval] = []
        self._open_start: float | None = None
        self._open_rounding_error = 0.0

    @staticmethod
    def _rounding_error(value: str) -> float:
        # FFmpeg 6.x av_ts2timestr uses %.6g (trailing zeros are omitted).
        # Honor additional printed digits from newer versions. Summing half
        # units in the last significant place bounds serialization error only.
        number = Decimal(value)
        if not number:
            return 0.0
        digits = max(6, len(number.as_tuple().digits))
        return .5 * 10.0 ** (number.adjusted() - digits + 1)

    @staticmethod
    def _seconds(value: str) -> float:
        try:
            seconds = float(value)
        except ValueError:
            raise SilenceEvidenceError() from None
        if not math.isfinite(seconds) or seconds < 0:
            raise SilenceEvidenceError()
        return seconds

    def consume(self, line: str) -> None:
        if (re.search(
                r"\bNo such filter:\s*['\"](?:silencedetect|aresample|astats)['\"]", line,
            )
                or re.search(r"\bStream map ['\"]0:a:0['\"] matches no streams\.", line)):
            raise SilenceEvidenceError()
        start = _SILENCE_START.fullmatch(line)
        if start:
            if self._open_start is not None:
                raise SilenceEvidenceError()
            value = self._seconds(start["seconds"])
            if self._intervals and value < self._intervals[-1].end_seconds:
                raise SilenceEvidenceError()
            self._open_start = value
            self._open_rounding_error = self._rounding_error(start["seconds"])
            return

        end = _SILENCE_END.fullmatch(line)
        if end:
            if self._open_start is None:
                raise SilenceEvidenceError()
            end_seconds = self._seconds(end["seconds"])
            reported_duration = self._seconds(end["duration"])
            try:
                interval = SilenceInterval(self._open_start, end_seconds)
            except ValueError:
                raise SilenceEvidenceError() from None
            if not math.isclose(
                interval.end_seconds - interval.start_seconds, reported_duration,
                rel_tol=0,
                abs_tol=(self._open_rounding_error + self._rounding_error(end["seconds"])
                         + self._rounding_error(end["duration"])
                         + 4 * max(math.ulp(self._open_start), math.ulp(end_seconds),
                                   math.ulp(reported_duration))),
            ):
                raise SilenceEvidenceError()
            self._intervals.append(interval)
            self._open_start = None
            return

        if _SILENCE_PREFIX.match(line):
            raise SilenceEvidenceError()

    def finish(self, duration_seconds: float) -> list[SilenceInterval]:
        duration = self._seconds(str(duration_seconds))
        if duration <= 0:
            raise SilenceEvidenceError()
        if self._open_start is not None:
            try:
                self._intervals.append(SilenceInterval(self._open_start, duration))
            except ValueError:
                raise SilenceEvidenceError() from None
            self._open_start = None
        if any(interval.end_seconds > duration for interval in self._intervals):
            raise SilenceEvidenceError()
        return list(self._intervals)


@dataclass(frozen=True)
class MediaLimits:
    workspace: Path
    deadline: float
    inactivity_seconds: float
    max_workspace_bytes: int


_active_limits: ContextVar[MediaLimits | None] = ContextVar("media_limits", default=None)


@contextmanager
def temporary_workspace(root: Path | None = None) -> Iterator[Path]:
    workspace = Path(tempfile.mkdtemp(prefix="bunny-video-", dir=root))
    try:
        yield workspace
    finally:
        shutil.rmtree(workspace)


def deduplicate_frames(
    candidates: list[FrameCandidate], cancellation_event: Event | None = None,
) -> list[FrameCandidate]:
    """Deduplicate consecutive visual states by pHash, with a 2s gap.

    pHash measures structure, so flat screens differing only in color may merge.
    More than 600 surviving frames are sampled uniformly, including both ends.
    """
    kept: list[FrameCandidate] = []
    hashes: list[imagehash.ImageHash] = []
    for candidate in sorted(candidates, key=lambda item: item.timestamp_seconds):
        if cancellation_event is not None:
            _check_cancelled(cancellation_event)
        if kept and candidate.timestamp_seconds - kept[-1].timestamp_seconds < 2:
            continue
        with Image.open(candidate.path) as image:
            fingerprint = imagehash.phash(image)
        if hashes and fingerprint - hashes[-1] <= 8:
            continue
        kept.append(candidate)
        hashes.append(fingerprint)
    if cancellation_event is not None:
        _check_cancelled(cancellation_event)
    if len(kept) > 600:
        kept = [kept[round(i * (len(kept) - 1) / 599)] for i in range(600)]
    return kept


def _check_cancelled(event: Event) -> None:
    if event.is_set():
        raise CancelledError("Elaborazione annullata")


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _run_process(args: list[str], event: Event, on_line: Callable[[str, str], None], *,
                 workspace: Path | None = None, runtime_seconds: float = 21600,
                 inactivity_seconds: float = 120, max_workspace_bytes: int = 2_000_000_000) -> None:
    """Drain both pipes without blocking cancellation on a stalled input.

    Selectors support our macOS/Linux hosts. Bounded buffers discard overlong
    diagnostic lines; we only parse short numeric FFmpeg records.
    """
    _check_cancelled(event)
    limits = _active_limits.get()
    deadline = monotonic() + runtime_seconds
    if limits:
        workspace, deadline = limits.workspace, limits.deadline
        inactivity_seconds, max_workspace_bytes = limits.inactivity_seconds, limits.max_workspace_bytes
    last_activity = monotonic()
    last_size = 0
    last_progress = -1

    def check_limits() -> None:
        nonlocal last_activity, last_size
        now = monotonic()
        if now > deadline:
            raise MediaError("Tempo massimo di elaborazione media superato")
        if workspace is not None:
            size = 0
            for root, _, files in os.walk(workspace):
                for filename in files:
                    try:
                        size += (Path(root) / filename).stat().st_size
                    except FileNotFoundError:
                        continue
                    if size > max_workspace_bytes:
                        raise MediaError("Limite dello spazio temporaneo superato")
            if size != last_size:
                last_size, last_activity = size, now
        if now - last_activity > inactivity_seconds:
            raise MediaError("Elaborazione media interrotta per inattività")

    check_limits()
    try:
        process = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, shell=False,
        )
    except OSError:
        raise MediaError("Impossibile avviare FFmpeg/FFprobe; verificare l'installazione") from None
    buffers: dict[str, bytes] = {"stdout": b"", "stderr": b""}
    protected = False

    def consume(name: str, line: str) -> None:
        nonlocal protected, last_progress, last_activity
        if name == "stdout" and line.startswith("out_time_us="):
            try:
                progress = int(line.split("=", 1)[1])
                if progress > last_progress:
                    last_progress, last_activity = progress, monotonic()
            except ValueError:
                pass
        if name == "stderr" and re.search(
            r"\b(?:forbidden|unauthorized|drm|sample-aes)\b|"
            r"(?:http error|server returned)\s+(?:401|403)\b|"
            r"referr?er.*(?:denied|forbidden|invalid)",
            line, re.IGNORECASE,
        ):
            protected = True
        on_line(name, line)

    try:
        with selectors.DefaultSelector() as selector:
            for pipe, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map():
                _check_cancelled(event)
                check_limits()
                for key, _ in selector.select(timeout=.1):
                    data = os.read(key.fd, 65536)
                    name = key.data
                    if not data:
                        selector.unregister(key.fileobj)
                        if buffers[name]:
                            consume(name, buffers[name].decode("utf-8", "replace"))
                        continue
                    lines = (buffers[name] + data).split(b"\n")
                    buffers[name] = lines.pop()[-16384:]
                    for line in lines:
                        if len(line) <= 16384:
                            consume(name, line.decode("utf-8", "replace"))
            while process.poll() is None:
                _check_cancelled(event)
                check_limits()
                event.wait(.1)
            _check_cancelled(event)
            check_limits()
            if process.returncode:
                if protected:
                    raise MediaProtectedError("Accesso al video negato o protezione non supportata")
                raise MediaError("Impossibile elaborare il contenuto multimediale")
    finally:
        _stop_process(process)
        process.stdout.close()
        process.stderr.close()


_FRAME_INFO = re.compile(r"\[.*showinfo.*\].*\bn:\s*(\d+).*\bpts_time:([\d.eE+-]+)")
_INPUT_BYTES = re.compile(r"Input stream #0:\d+.*?\d+ packets read \((\d+) bytes\)")
_AUDIO_SAMPLES = re.compile(
    r"^\[Parsed_astats_\d+ @ [^\]]+\] Number of samples: (\d+(?:\.0+)?)\s*$"
)


class FFmpegProcessor:
    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe", *,
                 runtime_seconds: float = 21600, inactivity_seconds: float = 120,
                 max_workspace_bytes: int = 2_000_000_000) -> None:
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.runtime_seconds = runtime_seconds
        self.inactivity_seconds = inactivity_seconds
        self.max_workspace_bytes = max_workspace_bytes

    def extract(
        self, source_url: str, workspace: Path,
        progress_callback: Callable[[float], None], cancellation_event: Event,
    ) -> MediaArtifacts:
        """Decode the source once; all later probes operate on local outputs."""
        token = _active_limits.set(MediaLimits(workspace, monotonic() + self.runtime_seconds,
                                              self.inactivity_seconds, self.max_workspace_bytes))
        try:
            return self._extract(source_url, workspace, progress_callback, cancellation_event, include_audio=True)
        finally:
            _active_limits.reset(token)

    def extract_visual(
        self, source_url: str, workspace: Path,
        progress_callback: Callable[[float], None], cancellation_event: Event,
        *, duration_seconds: float | None = None,
    ) -> MediaArtifacts:
        """Extract slide candidates without writing temporary audio files."""
        if duration_seconds is not None and (
            not math.isfinite(duration_seconds) or duration_seconds <= 0
        ):
            raise SilenceEvidenceError()
        token = _active_limits.set(MediaLimits(workspace, monotonic() + self.runtime_seconds,
                                              self.inactivity_seconds, self.max_workspace_bytes))
        try:
            return self._extract(
                source_url, workspace, progress_callback, cancellation_event,
                include_audio=False, duration_seconds=duration_seconds,
            )
        finally:
            _active_limits.reset(token)

    def _extract(
        self, source_url, workspace, progress_callback, cancellation_event, *, include_audio: bool,
        duration_seconds: float | None = None,
    ) -> MediaArtifacts:
        _check_cancelled(cancellation_event)
        output = Path(tempfile.mkdtemp(prefix="media-", dir=workspace))
        timestamps: dict[int, float] = {}
        input_bytes = 0
        saw_input_bytes = False
        processed_seconds = 0.0
        audio_samples: int | None = None
        silence_events = _SilenceEvents()

        def consume(name: str, line: str) -> None:
            nonlocal input_bytes, saw_input_bytes, processed_seconds, audio_samples
            if name == "stderr":
                frame = _FRAME_INFO.search(line)
                if frame:
                    timestamps[int(frame[1])] = float(frame[2])
                count = _INPUT_BYTES.search(line)
                if count:
                    input_bytes += int(count[1])
                    saw_input_bytes = True
                if not include_audio:
                    silence_events.consume(line)
                    sample_count = _AUDIO_SAMPLES.fullmatch(line)
                    if sample_count:
                        samples = float(sample_count[1])
                        if not samples.is_integer() or samples <= 0:
                            raise SilenceEvidenceError()
                        if audio_samples is not None and audio_samples != int(samples):
                            raise SilenceEvidenceError()
                        audio_samples = int(samples)
            elif line.startswith("out_time_us="):
                try:
                    seconds = max(0.0, int(line.split("=", 1)[1]) / 1_000_000)
                except ValueError:
                    return
                if seconds > processed_seconds:
                    processed_seconds = seconds
                    progress_callback(seconds)

        command = [
            self.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "verbose",
            "-nostats", "-progress", "pipe:1", "-i", source_url,
        ]
        if include_audio:
            command.extend([
                "-map", "0:a:0", "-vn", "-c:a", "aac", "-ac", "1", "-ar", "16000",
                "-b:a", "24k", "-f", "segment",
                "-segment_time", str(_TRANSCRIPTION_SEGMENT_SECONDS),
                "-reset_timestamps", "1", "-segment_format", "mp4",
                "-segment_list", str(output / "audio.csv"), "-segment_list_type", "csv",
                str(output / "audio-%05d.m4a"),
            ])
        command.extend([
            "-map", "0:v:0", "-an", "-vf",
            "select='eq(n,0)+gt(scene,0.18)',scale=w='min(960,iw)':h=-2,showinfo",
            "-fps_mode", "vfr", "-c:v", "mjpeg", "-q:v", "3",
            str(output / "frame-%06d.jpg"),
        ])
        if not include_audio:
            # Decode through silencedetect in the existing source pass. Bunny's
            # validated metadata is the canonical export timeline in production;
            # the astats fallback remains only for direct callers without it.
            audio_filter = "silencedetect=noise=-45dB:d=0.15"
            if duration_seconds is None:
                audio_filter += f",aresample={_DURATION_SAMPLE_RATE},astats=metadata=0:reset=0"
            command.extend([
                "-map", "0:a:0", "-vn",
                "-af", audio_filter,
                "-f", "null", os.devnull,
            ])
        _run_process(command, cancellation_event, consume)
        _check_cancelled(cancellation_event)
        frames = sorted(output.glob("frame-*.jpg"))
        if (
            not saw_input_bytes or not frames
            or sorted(timestamps) != list(range(len(frames)))
            or any(not math.isfinite(t) or t < 0 for t in timestamps.values())
            or any(timestamps[i] <= timestamps[i - 1] for i in range(1, len(frames)))
        ):
            raise MediaError("Impossibile verificare i fotogrammi o la lettura del video")
        audio_chunks = self._read_chunks(output, cancellation_event) if include_audio else []
        if include_audio:
            terminal_seconds = max(
                chunk.start_seconds + chunk.duration_seconds for chunk in audio_chunks
            )
            silence_intervals = []
        else:
            if duration_seconds is not None:
                terminal_seconds = duration_seconds
            else:
                if audio_samples is None:
                    raise SilenceEvidenceError()
                terminal_seconds = audio_samples / _DURATION_SAMPLE_RATE
            silence_intervals = silence_events.finish(terminal_seconds)
        frame_candidates = deduplicate_frames([
            FrameCandidate(path, timestamps[i]) for i, path in enumerate(frames)
        ], cancellation_event)
        _check_cancelled(cancellation_event)
        if terminal_seconds > processed_seconds:
            processed_seconds = terminal_seconds
            progress_callback(terminal_seconds)
        return MediaArtifacts(
            audio_chunks, frame_candidates, (input_bytes * 120 + 99) // 100, silence_intervals,
            silence_measured=not include_audio,
        )

    def _read_chunks(self, output: Path, event: Event) -> list[AudioChunk]:
        chunks = []
        with (output / "audio.csv").open(newline="") as file:
            for filename, start, _ in csv.reader(file):
                path = output / Path(filename).name
                duration, size = self._probe_audio(path, event)
                chunks.extend(self._fit_chunk(AudioChunk(path, float(start), duration), size, event))
        if not chunks:
            raise MediaError("Il video non contiene audio utilizzabile")
        return chunks

    def _fit_chunk(self, chunk: AudioChunk, size: int, event: Event) -> list[AudioChunk]:
        """Remux only local AAC if a segment breaches either delivery limit.

        The segment muxer cuts at packet boundaries, so the 600s target can
        run slightly long. Recheck every child against OpenAI's hard limits.
        """
        _check_cancelled(event)
        if (
            size <= _OPENAI_MAX_AUDIO_BYTES
            and chunk.duration_seconds <= _OPENAI_MAX_AUDIO_SECONDS
        ):
            return [chunk]
        if chunk.duration_seconds <= .128:
            raise MediaError("Il segmento audio supera il limite consentito")
        parts = max(
            2,
            math.ceil(size / _OPENAI_MAX_AUDIO_BYTES),
            math.ceil(chunk.duration_seconds / _TRANSCRIPTION_SEGMENT_SECONDS),
        )
        output = Path(tempfile.mkdtemp(prefix="split-", dir=chunk.path.parent))
        _run_process([
            self.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
            "-i", str(chunk.path), "-map", "0:a:0", "-c:a", "copy",
            "-f", "segment", "-segment_time", str(chunk.duration_seconds / parts),
            "-reset_timestamps", "1", "-segment_format", "mp4",
            "-segment_list", str(output / "audio.csv"), "-segment_list_type", "csv",
            str(output / "audio-%05d.m4a"),
        ], event, lambda name, line: None)
        children = []
        with (output / "audio.csv").open(newline="") as file:
            for filename, start, _ in csv.reader(file):
                path = output / Path(filename).name
                duration, child_size = self._probe_audio(path, event)
                if duration >= chunk.duration_seconds or child_size >= size:
                    raise MediaError("Impossibile suddividere il segmento audio")
                child = AudioChunk(path, chunk.start_seconds + float(start), duration)
                children.extend(self._fit_chunk(child, child_size, event))
        if not children:
            raise MediaError("Impossibile suddividere il segmento audio")
        chunk.path.unlink()
        return children

    def _probe_audio(self, path: Path, event: Event) -> tuple[float, int]:
        lines = []
        _run_process([
            self.ffprobe, "-v", "error", "-show_entries", "format=duration,size",
            "-of", "json", str(path),
        ], event, lambda name, line: lines.append(line) if name == "stdout" else None)
        try:
            data = json.loads("\n".join(lines))["format"]
            duration, size = float(data["duration"]), int(data["size"])
            if not math.isfinite(duration) or duration <= 0 or size <= 0:
                raise ValueError
            return duration, size
        except (ValueError, KeyError, TypeError):
            raise MediaError("Impossibile verificare il segmento audio") from None
