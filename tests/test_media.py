"""Real local media checks; FFmpeg/FFprobe must be installed on PATH."""

from concurrent.futures import CancelledError
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
from threading import Event, Thread
import time

from PIL import Image
import pytest

from app.media import FFmpegProcessor, FrameCandidate, MediaError, deduplicate_frames, temporary_workspace
from app import media


def test_silence_events_pair_and_close_end_of_file():
    events = media._SilenceEvents()
    events.consume("[silencedetect @ 0x1] silence_start: 2.25")
    events.consume("[silencedetect @ 0x1] silence_end: 4.75 | silence_duration: 2.5")
    events.consume("[silencedetect @ 0x1] silence_start: 9")

    assert events.finish(10) == [
        media.SilenceInterval(2.25, 4.75),
        media.SilenceInterval(9, 10),
    ]


@pytest.mark.parametrize("diagnostics, duration", [
    (["[silencedetect @ 0x1] silence_end: 4.75 | silence_duration: 2.5"], 10),
    (["[silencedetect @ 0x1] silence_start: 2", "[silencedetect @ 0x1] silence_start: 3"], 10),
    (["[silencedetect @ 0x1] silence_start: 4", "[silencedetect @ 0x1] silence_end: 3 | silence_duration: 1"], 10),
    (["[silencedetect @ 0x1] silence_start: nan"], 10),
    (["[silencedetect @ 0x1] silence_start: 2", "[silencedetect @ 0x1] silence_end: 4 | silence_duration: 2", "[silencedetect @ 0x1] silence_start: 3"], 10),
    (["[silencedetect @ 0x1] silence_start: 2", "[silencedetect @ 0x1] silence_end: 11 | silence_duration: 9"], 10),
    (["[silencedetect @ 0x1] silence_start: 9"], 8),
    (["[silencedetect @ 0x1] silence_start: not-a-number"], 10),
])
def test_silence_events_reject_invalid_or_malformed_diagnostics(diagnostics, duration):
    events = media._SilenceEvents()

    with pytest.raises(MediaError) as caught:
        for diagnostic in diagnostics:
            events.consume(diagnostic)
        events.finish(duration)

    assert str(caught.value) == "Impossibile verificare le pause audio"


@pytest.mark.parametrize("start, end", [
    (-1, 0), (0, -1), (float("nan"), 1), (0, float("inf")), (2, 1),
])
def test_silence_interval_requires_finite_nonnegative_ordered_bounds(start, end):
    with pytest.raises(ValueError):
        media.SilenceInterval(start, end)


@pytest.mark.parametrize("diagnostic", [
    "HTTP error 403 Forbidden https://private.invalid/?token=secret",
    "Server returned 401 Unauthorized secret",
    "SAMPLE-AES encryption is not supported secret",
    "DRM protected content secret",
    "Referrer denied secret",
])
def test_authorization_failures_have_a_safe_protected_category(diagnostic):
    script = f"import sys; sys.stderr.write({diagnostic!r}); sys.exit(1)"
    with pytest.raises(media.MediaProtectedError) as caught:
        media._run_process([sys.executable, "-c", script], Event(), lambda name, line: None)
    assert "secret" not in str(caught.value)
    assert "https://" not in str(caught.value)


def test_frame_numbers_do_not_misclassify_decode_failure_as_protected():
    script = "import sys; sys.stderr.write('frame=401 decode error'); sys.exit(1)"
    with pytest.raises(MediaError) as caught:
        media._run_process([sys.executable, "-c", script], Event(), lambda name, line: None)
    assert not isinstance(caught.value, media.MediaProtectedError)


@pytest.mark.parametrize("outcome", [None, RuntimeError, CancelledError])
def test_workspace_is_removed_on_every_exit(tmp_path: Path, outcome) -> None:
    created = None
    try:
        with temporary_workspace(tmp_path) as workspace:
            created = workspace
            (workspace / "audio.m4a").write_bytes(b"temporary")
            if outcome:
                raise outcome("stop")
    except (RuntimeError, CancelledError):
        pass
    assert created is not None and not created.exists()


def test_near_identical_frames_are_deduplicated(tmp_path: Path) -> None:
    first, second = tmp_path / "one.jpg", tmp_path / "two.jpg"
    Image.new("RGB", (64, 64), "white").save(first)
    Image.new("RGB", (64, 64), (254, 254, 254)).save(second)
    assert deduplicate_frames([
        FrameCandidate(first, 10), FrameCandidate(second, 12),
    ]) == [FrameCandidate(first, 10)]


def noise_frame(tmp_path: Path, number: int) -> Path:
    path = tmp_path / f"noise-{number}.png"
    Image.frombytes("L", (32, 32), random.Random(number).randbytes(1024)).save(path)
    return path


@pytest.mark.parametrize("patch_value, expected_count", [(157, 1), (176, 2)])
def test_phash_distance_eight_is_duplicate_but_ten_is_distinct(tmp_path, patch_value, expected_count) -> None:
    first = noise_frame(tmp_path, 4)
    second = tmp_path / "modified.png"
    with Image.open(first) as image:
        image.paste(patch_value, (0, 0, 4, 4))
        image.save(second)
    # These hand-checked edits have pHash distances 8 and 10 respectively.
    assert len(deduplicate_frames([
        FrameCandidate(first, 0), FrameCandidate(second, 2),
    ])) == expected_count


def test_dedup_orders_frames_enforces_gap_and_preserves_return_to_slide(tmp_path: Path) -> None:
    first, second, third = [noise_frame(tmp_path, i) for i in range(3)]
    assert deduplicate_frames([
        FrameCandidate(first, 10), FrameCandidate(third, 5),
        FrameCandidate(second, 1), FrameCandidate(first, 0),
    ]) == [FrameCandidate(first, 0), FrameCandidate(third, 5), FrameCandidate(first, 10)]


@pytest.mark.parametrize("limit", ["runtime", "inactivity", "storage"])
def test_media_limit_terminates_reaps_and_cleans_real_process(tmp_path, monkeypatch, limit):
    processes = []
    real_popen = subprocess.Popen
    def record(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(subprocess, "Popen", record)
    script = ("import time; time.sleep(5)" if limit != "storage" else
              "import pathlib,time,sys; pathlib.Path(sys.argv[1]).write_bytes(b'x'*20000); time.sleep(5)")
    with temporary_workspace(tmp_path) as workspace:
        started = time.monotonic()
        with pytest.raises(MediaError):
            media._run_process([sys.executable, "-u", "-c", script, str(workspace / "output")],
                Event(), lambda *_: None, workspace=workspace,
                runtime_seconds=.2 if limit == "runtime" else 2,
                inactivity_seconds=.2 if limit == "inactivity" else 2,
                max_workspace_bytes=1000 if limit == "storage" else 100000)
        assert time.monotonic() - started < 1.5
    assert processes and processes[0].poll() is not None
    assert not workspace.exists()


def test_over_600_distinct_frames_are_sampled_across_the_whole_video(tmp_path: Path) -> None:
    candidates = [FrameCandidate(noise_frame(tmp_path, i), i * 2) for i in range(605)]
    kept = deduplicate_frames(candidates)
    assert len(kept) == 600
    assert kept[0] == candidates[0] and kept[-1] == candidates[-1]
    gaps = [b.timestamp_seconds - a.timestamp_seconds for a, b in zip(kept, kept[1:])]
    assert set(gaps) == {2, 4}


def test_cancellation_is_checked_during_perceptual_deduplication(tmp_path, monkeypatch) -> None:
    event = Event()
    real_phash = media.imagehash.phash

    def cancel_after_first_hash(image):
        event.set()
        return real_phash(image)

    monkeypatch.setattr(media.imagehash, "phash", cancel_after_first_hash)
    with pytest.raises(CancelledError):
        deduplicate_frames([
            FrameCandidate(noise_frame(tmp_path, 0), 0),
            FrameCandidate(noise_frame(tmp_path, 1), 2),
        ], cancellation_event=event)


@pytest.fixture(scope="module")
def media_tools() -> tuple[str, str]:
    binaries = tuple(shutil.which(tool) for tool in ("ffmpeg", "ffprobe"))
    if not all(binaries):
        pytest.fail("Installare ffmpeg e ffprobe e aggiungerli al PATH (macOS: brew install ffmpeg; Debian: apt-get install ffmpeg).")
    return binaries


@pytest.fixture(scope="module")
def synthetic_video(tmp_path_factory, media_tools) -> Path:
    output = tmp_path_factory.mktemp("source") / "three-scenes.mp4"
    ffmpeg, _ = media_tools
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=red:s=1280x720:r=10:d=4,drawbox=x=50:y=50:w=300:h=300:color=white:t=fill",
        "-f", "lavfi", "-i", "color=blue:s=1280x720:r=10:d=4,drawbox=x=850:y=50:w=300:h=300:color=white:t=fill",
        "-f", "lavfi", "-i", "color=white:s=1280x720:r=10:d=4,drawbox=x=500:y=350:w=300:h=300:color=black:t=fill",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=12",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-map", "3:a", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-g", "10", "-c:a", "aac", str(output),
    ], check=True, capture_output=True)
    return output


def probe(ffprobe: str, path: Path) -> dict:
    result = subprocess.run([
        ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
    ], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def test_extract_single_input_audio_scene_timestamps_and_byte_estimate(
    tmp_path, synthetic_video, media_tools, monkeypatch,
) -> None:
    ffmpeg, ffprobe = media_tools
    calls = []
    real_popen = subprocess.Popen

    def record_popen(args, **kwargs):
        calls.append((args, kwargs))
        return real_popen(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", record_popen)
    progress = []
    with temporary_workspace(tmp_path) as workspace:
        result = FFmpegProcessor(ffmpeg, ffprobe).extract(
            str(synthetic_video), workspace, progress.append, Event(),
        )
        assert len(result.audio_chunks) == 1
        chunk = result.audio_chunks[0]
        data = probe(ffprobe, chunk.path)
        stream = data["streams"][0]
        assert stream["codec_name"] == "aac"
        assert stream["channels"] == 1 and stream["sample_rate"] == "16000"
        assert 0 < int(stream["bit_rate"]) < 32000
        assert chunk.path.stat().st_size < 25_000_000
        assert chunk.start_seconds == 0
        assert sum(c.duration_seconds for c in result.audio_chunks) == pytest.approx(12, abs=1)
        assert [f.timestamp_seconds for f in result.frame_candidates] == pytest.approx([0, 4, 8], abs=.11)
        for frame in result.frame_candidates:
            assert frame.path.is_relative_to(workspace)
            with Image.open(frame.path) as image:
                assert image.format == "JPEG" and image.width <= 960
        assert progress == sorted(progress) and progress[-1] >= 11.9
        # Payload accounting is an estimate with 20% overhead, not output size.
        packets = subprocess.run([
            ffprobe, "-v", "error", "-show_packets", "-show_entries", "packet=size",
            "-of", "json", str(synthetic_video),
        ], check=True, capture_output=True, text=True)
        payload_bytes = sum(int(p["size"]) for p in json.loads(packets.stdout)["packets"])
        assert result.downloaded_bytes == (payload_bytes * 120 + 99) // 100
        extraction_calls = [args for args, _ in calls if args[0] == ffmpeg]
        assert len(extraction_calls) == 1
        assert extraction_calls[0].count("-i") == 1
        assert extraction_calls[0].count(str(synthetic_video)) == 1
        assert all(kwargs.get("shell") is False for args, kwargs in calls if args[0] == ffmpeg)
    assert not workspace.exists()


def test_invalid_source_error_does_not_expose_url_or_upstream_diagnostics(tmp_path, media_tools, caplog) -> None:
    secret = "https://127.0.0.1:1/private?token=secret-token"
    with temporary_workspace(tmp_path) as workspace:
        with pytest.raises(MediaError) as error:
            FFmpegProcessor(*media_tools).extract(secret, workspace, lambda _: None, Event())
    assert "secret-token" not in str(error.value) + caplog.text
    assert secret not in str(error.value) + caplog.text
    assert error.value.__cause__ is None


def test_visual_only_extraction_reads_source_once_without_writing_audio(tmp_path, monkeypatch) -> None:
    commands = []

    def fake_run(args, event, consume):
        commands.append(args)
        template = Path(next(value for value in args if "frame-%06d.jpg" in value))
        Image.new("RGB", (32, 18), "white").save(Path(str(template).replace("%06d", "000001")))
        consume("stderr", "[Parsed_showinfo_0] n:   0 pts_time:0")
        consume("stderr", "Input stream #0:0 1 packets read (100 bytes)")
        consume("stderr", "[silencedetect @ 0x1] silence_start: 12.5")
        consume("stderr", "[silencedetect @ 0x1] silence_end: 14.25 | silence_duration: 1.75")
        consume("stdout", "out_time_us=60000000")

    monkeypatch.setattr(media, "_run_process", fake_run)
    progress = []
    with temporary_workspace(tmp_path) as workspace:
        result = FFmpegProcessor().extract_visual(
            "https://cdn.example/video/play_240p.mp4", workspace, progress.append, Event(),
        )
        assert result.audio_chunks == []
        assert result.silence_intervals == [media.SilenceInterval(12.5, 14.25)]
        assert not list(workspace.rglob("*.m4a"))
        assert not list(workspace.rglob("audio.csv"))
        assert [frame.timestamp_seconds for frame in result.frame_candidates] == [0]
        assert result.downloaded_bytes == 120
        assert progress == [60]

    assert len(commands) == 1
    assert commands[0].count("https://cdn.example/video/play_240p.mp4") == 1
    assert commands[0][-8:] == [
        "-map", "0:a:0", "-vn", "-af", "silencedetect=noise=-45dB:d=0.15",
        "-f", "null", os.devnull,
    ]
    assert "copy" not in commands[0]
    assert not any(value.endswith((".m4a", "audio.csv")) for value in commands[0])


def test_visual_only_extraction_rejects_malformed_silence_diagnostics_safely(tmp_path, monkeypatch) -> None:
    def fake_run(args, event, consume):
        template = Path(next(value for value in args if "frame-%06d.jpg" in value))
        Image.new("RGB", (32, 18), "white").save(Path(str(template).replace("%06d", "000001")))
        consume("stderr", "[Parsed_showinfo_0] n:   0 pts_time:0")
        consume("stderr", "Input stream #0:0 1 packets read (100 bytes)")
        consume("stderr", "[silencedetect @ 0x1] silence_start: private diagnostic")
        consume("stdout", "out_time_us=60000000")

    monkeypatch.setattr(media, "_run_process", fake_run)
    with temporary_workspace(tmp_path) as workspace:
        with pytest.raises(MediaError) as caught:
            FFmpegProcessor().extract_visual("private", workspace, lambda _: None, Event())

    assert str(caught.value) == "Impossibile verificare le pause audio"


def test_visual_only_extraction_detects_synthetic_speech_pauses_without_audio_files(tmp_path, media_tools) -> None:
    ffmpeg, _ = media_tools
    source = tmp_path / "speech-pauses.mp4"
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=blue:s=32x32:r=1:d=6.2",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=2.4",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=.8",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
        "-filter_complex", "[1:a][2:a][3:a][4:a][5:a]concat=n=5:v=0:a=1[a]",
        "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", str(source),
    ], check=True, capture_output=True)

    with temporary_workspace(tmp_path) as workspace:
        result = FFmpegProcessor(*media_tools).extract_visual(
            str(source), workspace, lambda _: None, Event(),
        )
        assert [(item.start_seconds, item.end_seconds) for item in result.silence_intervals] == pytest.approx([
            (1, 3.4), (4.4, 5.2),
        ], abs=.15)
        assert result.audio_chunks == []
        assert not list(workspace.rglob("*.m4a"))
        assert not list(workspace.rglob("audio.csv"))


def test_visual_only_cancellation_before_start_never_launches_ffmpeg(tmp_path) -> None:
    event = Event()
    event.set()
    with temporary_workspace(tmp_path) as workspace:
        with pytest.raises(CancelledError):
            FFmpegProcessor("missing-ffmpeg", "missing-ffprobe").extract_visual(
                "private", workspace, lambda _: None, event,
            )


def test_cancelled_before_start_never_launches_ffmpeg(tmp_path) -> None:
    event = Event()
    event.set()
    with temporary_workspace(tmp_path) as workspace:
        with pytest.raises(CancelledError):
            FFmpegProcessor("missing-ffmpeg", "missing-ffprobe").extract(
                "private", workspace, lambda _: None, event,
            )


@pytest.mark.parametrize("ignore_term", [False, True])
def test_cancellation_interrupts_silent_process_and_reaps_it(tmp_path, monkeypatch, ignore_term) -> None:
    # A real process with no pipe activity catches blocking readline regressions;
    # ignoring SIGTERM exercises the hard kill after the five-second grace period.
    event = Event()
    processes = []
    real_popen = subprocess.Popen
    script = (
        "import signal,time; "
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN); " if ignore_term else "")
        + "print('ready', flush=True); time.sleep(60)"
    )

    def record_popen(args, **kwargs):
        process = real_popen(args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", record_popen)
    started = time.monotonic()
    with temporary_workspace(tmp_path) as workspace:
        (workspace / "partial.m4a").write_bytes(b"partial")
        with pytest.raises(CancelledError):
            media._run_process(
                [sys.executable, "-u", "-c", script], event,
                lambda name, line: event.set() if line == "ready" else None,
            )
    elapsed = time.monotonic() - started
    assert processes[0].poll() is not None
    assert elapsed < 7
    if ignore_term:
        assert elapsed >= 5
    assert not workspace.exists()


def test_oversized_chunk_is_split_locally_without_a_second_source_read(
    tmp_path, synthetic_video, media_tools, monkeypatch,
) -> None:
    processor = FFmpegProcessor(*media_tools)
    real_read = processor._read_chunks
    original_path = None
    commands = []
    real_run = media._run_process

    def record_run(args, event, callback):
        commands.append(args)
        return real_run(args, event, callback)

    monkeypatch.setattr(media, "_run_process", record_run)

    def inflate_first_chunk(output, event):
        nonlocal original_path
        original_path = next(output.glob("audio-*.m4a"))
        # A valid MP4 free atom simulates unexpectedly oversized muxed output
        # without a 2-hour fixture. FFprobe and splitting still run for real.
        with original_path.open("ab") as file:
            file.write((24_000_001).to_bytes(4, "big") + b"free")
            file.truncate(file.tell() + 24_000_001 - 8)
        return real_read(output, event)

    monkeypatch.setattr(processor, "_read_chunks", inflate_first_chunk)
    with temporary_workspace(tmp_path) as workspace:
        result = processor.extract(str(synthetic_video), workspace, lambda _: None, Event())
        assert len(result.audio_chunks) >= 2
        assert all(chunk.path.stat().st_size <= 24_000_000 for chunk in result.audio_chunks)
        assert sum(chunk.duration_seconds for chunk in result.audio_chunks) == pytest.approx(12, abs=1)
        assert result.audio_chunks[0].start_seconds == 0
        assert result.audio_chunks[-1].start_seconds > 5
        assert not original_path.exists()
        ffmpeg_inputs = [args[args.index("-i") + 1] for args in commands if args[0] == media_tools[0]]
        assert ffmpeg_inputs[0] == str(synthetic_video)
        assert len(ffmpeg_inputs) >= 2
        assert all(Path(source).is_relative_to(workspace) for source in ffmpeg_inputs[1:])


def test_hls_timestamps_are_relative_to_video_start_and_segments_read_once(
    tmp_path, synthetic_video, media_tools,
) -> None:
    from collections import Counter
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    playlist = tmp_path / "stream.m3u8"
    subprocess.run([
        media_tools[0], "-v", "error", "-i", str(synthetic_video), "-c", "copy",
        "-hls_time", "4", "-hls_list_size", "0", str(playlist),
    ], check=True, capture_output=True)
    requests = Counter()

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            requests[self.path] += 1
            super().do_GET()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(tmp_path)))
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        with temporary_workspace(tmp_path) as workspace:
            result = FFmpegProcessor(*media_tools).extract(
                f"http://127.0.0.1:{server.server_port}/stream.m3u8", workspace,
                lambda _: None, Event(),
            )
            # MPEG-TS uses a nonzero initial timestamp (normally 1.4 seconds).
            assert [f.timestamp_seconds for f in result.frame_candidates] == pytest.approx([0, 4, 8], abs=.11)
            assert result.audio_chunks[0].start_seconds == 0
            assert sum(c.duration_seconds for c in result.audio_chunks) == pytest.approx(12, abs=1)
            assert result.downloaded_bytes > 0
        assert requests["/stream0.ts"] == 1
        assert requests["/stream1.ts"] == 1
        assert requests["/stream2.ts"] == 1
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_long_audio_segments_keep_offsets_and_stay_within_transcription_target(tmp_path, media_tools) -> None:
    source = tmp_path / "long.mp4"
    subprocess.run([
        media_tools[0], "-v", "error", "-f", "lavfi", "-i", "color=blue:s=32x32:r=1:d=601",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=601",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-b:a", "24k", str(source),
    ], check=True, capture_output=True)
    with temporary_workspace(tmp_path) as workspace:
        result = FFmpegProcessor(*media_tools).extract(str(source), workspace, lambda _: None, Event())
        assert len(result.audio_chunks) >= 2
        assert all(
            c.duration_seconds <= 601 and c.path.stat().st_size < 25_000_000
            for c in result.audio_chunks
        )
        assert sum(c.duration_seconds for c in result.audio_chunks) == pytest.approx(601, abs=1)
        assert result.audio_chunks[0].start_seconds == 0
        for first, second in zip(result.audio_chunks, result.audio_chunks[1:]):
            assert second.start_seconds == pytest.approx(first.start_seconds + first.duration_seconds, abs=.13)
        assert result.audio_chunks[-1].start_seconds >= 599.9
