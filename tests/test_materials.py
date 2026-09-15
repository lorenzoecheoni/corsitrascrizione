"""Synthetic decks and hostile boundaries; no external network or real content."""

from concurrent.futures import CancelledError
from pathlib import Path
import socket
import random
import subprocess
import sys
from threading import Event, Timer
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from app.config import Settings
from app.materials import (
    DeckPage, MaterialError, MaterialProcessor, _download_https,
    _PinnedHTTPSConnection, extract_deck_pages, fetch_deck, match_slides_to_material,
)
from app.models import ReportMaterial, SlideChange


URL = "https://www.assoholding.it/deck.pptx"
TITLE = "Slide · Furio D'Andrea"
PUBLIC = "93.184.216.34"


def write_test_pptx(path, pages):
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for number, lines in enumerate(pages, start=1):
            runs = "".join(f"<a:r><a:t>{escape(line)}</a:t></a:r>" for line in lines)
            archive.writestr(
                f"ppt/slides/slide{number}.xml",
                '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                f"<p:cSld><a:p>{runs}</a:p></p:cSld></p:sld>",
            )
    return path


def write_test_pdf(path, pages):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=600, height=800)
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
        })
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 40 750 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as output:
        writer.write(output)
    return path


def slide(text="Decisioni assembleari", content=None, time=10, **extra):
    return SlideChange(timestamp_seconds=time, title=text,
                       visible_content=content or [], confidence="alta", **extra)


def test_pptx_pages_match_and_preserve_observation(tmp_path):
    deck = write_test_pptx(tmp_path / "deck.pptx", [
        ["PREMESSE", "Poteri e responsabilità"],
        ["DECISIONI ASSEMBLEARI", "Quorum e maggioranze"],
    ])
    original = slide(content=["Quorum e maggioranze"])
    pages = extract_deck_pages(deck)
    result = match_slides_to_material([original], ReportMaterial(titolo=TITLE, file=str(deck)), pages)
    assert [(p.number, p.text) for p in pages] == [
        (1, "PREMESSE Poteri e responsabilità"),
        (2, "DECISIONI ASSEMBLEARI Quorum e maggioranze"),
    ]
    assert result[0].material_title == TITLE
    assert result[0].page == 2
    assert result[0].model_dump(exclude={"material_title", "page"}) == original.model_dump(
        exclude={"material_title", "page"})
    assert original.page is None


def test_pdf_text_is_extracted_in_page_order(tmp_path):
    deck = write_test_pdf(tmp_path / "deck.pdf", ["Premesse", "Decisioni assembleari"])
    pages = extract_deck_pages(deck)
    assert [(p.number, p.text.strip()) for p in pages] == [
        (1, "Premesse"), (2, "Decisioni assembleari"),
    ]


@pytest.mark.parametrize("pages", [
    [DeckPage(1, "Un argomento completamente diverso")],
    [DeckPage(1, "Decisioni assembleari"), DeckPage(2, "Decisioni assembleari")],
    [DeckPage(1, "Decisioni assembleari quorum"), DeckPage(2, "Decisioni assembleari quorums")],
])
def test_low_score_or_ambiguous_runner_up_clears_unverified_links(pages):
    result = match_slides_to_material(
        [slide(material_title="unverified", page=99)], ReportMaterial(titolo=TITLE, url=URL), pages)
    assert result[0].material_title is None
    assert result[0].page is None


def test_matching_uses_both_token_overlap_and_sequence_similarity():
    # Jaccard .5 + sequence 20/33 => .54242, below the .55 threshold.
    result = match_slides_to_material(
        [slide("alpha beta")], ReportMaterial(titolo=TITLE, url=URL),
        [DeckPage(1, "alpha beta gamma deltas")])
    assert result[0].page is None
    # Jaccard .5 + sequence 20/32 => exactly .55, accepted at the boundary.
    result = match_slides_to_material(
        [slide("alpha beta")], ReportMaterial(titolo=TITLE, url=URL),
        [DeckPage(1, "alpha beta gamma delta")])
    assert result[0].page == 1


def test_page_order_is_chronological_stable_and_never_falls_back_to_worse_match():
    observed = [slide("Decisioni", time=20), slide("Premesse", time=10),
                slide("Premesse", time=30), slide("Decisioni", time=40)]
    result = match_slides_to_material(observed, ReportMaterial(titolo=TITLE, url=URL),
                                     [DeckPage(2, "Decisioni"), DeckPage(1, "Premesse")])
    assert [s.page for s in result] == [2, 1, None, 2]
    assert [s.timestamp_seconds for s in result] == [20, 10, 30, 40]


class Response:
    def __init__(self, status=200, chunks=(), headers=None):
        self.status, self.chunks, self.headers = status, iter(chunks), headers or {}

    def getheader(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def read1(self, size):
        item = next(self.chunks, b"")
        if isinstance(item, BaseException):
            raise item
        return item


def network(responses, seen):
    class Connection:
        def __init__(self, host, addresses, timeout):
            seen.append((host, addresses))

        def request(self, method, target, headers):
            assert method == "GET"
            assert headers["Accept-Encoding"] == "identity"
            seen.append(target)

        def getresponse(self):
            return next(responses)

        def close(self):
            pass
    return Connection


def dns(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC, port))]


def download(url, dest, responses=(), hosts=("www.assoholding.it",), resolver=dns):
    seen = []
    _download_https(url, dest, hosts, resolver=resolver,
                    connection_factory=network(iter(responses), seen))
    return seen


@pytest.mark.parametrize("url", [
    "http://www.assoholding.it/deck.pptx", "https://user:secret@www.assoholding.it/deck.pptx",
    "https://elsewhere.example/deck.pptx", "https://www.assoholding.it.evil.example/deck.pptx",
    "https://www.assoholding.it:8443/deck.pptx", "https://www.assoholding.it./deck.pptx",
    "https://localhost/deck.pptx", "https://127.0.0.1/deck.pptx",
    "https://[::1]/deck.pptx", "https://169.254.169.254/latest/meta-data/",
    "https://www.assoholding.it\\@elsewhere.example/deck.pptx",
    "https://www.assoholding.it/deck\r\n.pptx",
])
def test_unsafe_urls_are_rejected_before_writing(tmp_path, url):
    with pytest.raises(MaterialError):
        download(url, tmp_path / "deck", [Response(chunks=[b"would succeed if allowed"])])
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.1", "172.16.1.1", "192.168.1.1", "169.254.169.254",
    "0.0.0.0", "100.100.100.200", "224.0.0.1", "::1", "fe80::1", "fc00::1",
    "::ffff:127.0.0.1",
    "64:ff9b::a00:1", "::ffff:0:127.0.0.1", "400::1",
])
def test_any_unsafe_dns_answer_rejects_even_mixed_public_answers(tmp_path, address):
    def mixed(host, port, **kwargs):
        return dns(host, port) + [(socket.AF_INET6 if ":" in address else socket.AF_INET,
                                  socket.SOCK_STREAM, 6, "", (address, port))]
    with pytest.raises(MaterialError):
        download(URL, tmp_path / "deck", [Response(chunks=[b"would succeed if allowed"])], resolver=mixed)
    assert list(tmp_path.iterdir()) == []


def test_redirects_revalidate_host_dns_and_relative_targets(tmp_path):
    seen = download(URL, tmp_path / "deck", [
        Response(302, headers={"location": "/actual;deck.pptx"}), Response(chunks=[b"deck"]),
    ])
    assert (tmp_path / "deck").read_bytes() == b"deck"
    assert seen == [("www.assoholding.it", (PUBLIC,)), "/deck.pptx",
                    ("www.assoholding.it", (PUBLIC,)), "/actual;deck.pptx"]


@pytest.mark.parametrize("target", ["https://evil.example/deck", "http://www.assoholding.it/deck",
                                     "https://secret@www.assoholding.it/deck"])
def test_redirect_to_forbidden_target_cleans_download(tmp_path, target):
    with pytest.raises(MaterialError):
        download(URL, tmp_path / "deck", [Response(302, headers={"location": target}),
                                         Response(chunks=[b"would succeed if allowed"])])
    assert list(tmp_path.iterdir()) == []


def test_redirect_dns_rebinding_is_rejected(tmp_path):
    answers = iter([PUBLIC, "127.0.0.1"])
    def rebinding(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), port))]
    with pytest.raises(MaterialError):
        download(URL, tmp_path / "deck", [Response(302, headers={"location": "/second"}),
                                         Response(chunks=[b"would succeed if allowed"])],
                 resolver=rebinding)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("headers,chunks", [
    ({"content-length": str(50 * 1024 * 1024 + 1)}, []),
    ({}, [b"x" * (1024 * 1024)] * 51),
    ({"content-encoding": "gzip"}, [b"small compressed bomb"]),
    ({}, [b"partial", OSError("FAKE_SECRET_BODY")]),
])
def test_stream_limits_and_errors_leave_no_files_or_error_body(tmp_path, headers, chunks, caplog):
    with pytest.raises(MaterialError) as exc:
        download(URL, tmp_path / "deck", [Response(headers=headers, chunks=chunks)])
    assert "FAKE_SECRET_BODY" not in str(exc.value) + caplog.text
    assert exc.value.__suppress_context__
    assert list(tmp_path.iterdir()) == []


def test_download_is_bounded_even_at_exact_limit(tmp_path):
    download(URL, tmp_path / "deck", [Response(chunks=[b"x" * 1024 * 1024] * 50)])
    assert (tmp_path / "deck").stat().st_size == 50 * 1024 * 1024


def test_tls_uses_pinned_address_but_original_hostname(monkeypatch):
    import app.materials as module
    calls = []
    class Socket:
        def close(self):
            pass
        def sendall(self, data):
            calls.append(data)
    raw = Socket()
    def connect(address, timeout, **kwargs):
        calls.append(address)
        return raw
    monkeypatch.setattr(module.socket, "create_connection", connect)
    connection = _PinnedHTTPSConnection("www.assoholding.it", (PUBLIC,), 20)
    def wrap(sock, server_hostname):
        assert sock is raw
        calls.append(server_hostname)
        return raw
    monkeypatch.setattr(connection._context, "wrap_socket", wrap)
    connection.connect()
    assert calls == [(PUBLIC, 443), "www.assoholding.it"]
    assert connection.host == "www.assoholding.it"
    connection.request("GET", "/deck.pptx")
    assert b"Host: www.assoholding.it\r\n" in calls[-1]
    assert PUBLIC.encode() not in calls[-1]


@pytest.mark.parametrize("entry,body", [
    ("../escape.xml", b"bad"), ("/escape.xml", b"bad"), ("C:/escape.xml", b"bad"),
    ("ppt\\evil.xml", b"bad"), ("ppt/vbaProject.bin", b"macro"),
    ("ppt/embeddings/object.bin", b"executable"), ("ppt/activeX/control.xml", b"control"),
    ("ppt/slides/_rels/slide1.xml.rels", b'<Relationships><Relationship TargetMode="External" Target="https://private/"/></Relationships>'),
    ("ppt/slides/_rels/slide1.xml.rels", b'<Relationships><Relationship Target="../../../../escape"/></Relationships>'),
    ("ppt/slides/slide2.xml", b'<!DOCTYPE a [<!ENTITY x "boom">]><a>&x;</a>'),
    ("ppt/slides/slide2.xml", b"<malformed>"),
])
def test_hostile_pptx_is_rejected_without_extracting_members(tmp_path, entry, body):
    deck = write_test_pptx(tmp_path / "bad.pptx", [["safe"]])
    with ZipFile(deck, "a") as archive:
        archive.writestr(entry, body)
    with pytest.raises(MaterialError):
        extract_deck_pages(deck)
    assert list(tmp_path.iterdir()) == [deck]


def test_zip_bomb_and_symlink_are_rejected(tmp_path):
    deck = write_test_pptx(tmp_path / "bomb.pptx", [["safe"]])
    with ZipFile(deck, "a", ZIP_DEFLATED) as archive:
        archive.writestr("ppt/media/bomb.png", b"x" * 2_000_000)
    with pytest.raises(MaterialError):
        extract_deck_pages(deck)
    link = write_test_pptx(tmp_path / "link.pptx", [["safe"]])
    with ZipFile(link, "a") as archive:
        info = ZipInfo("ppt/media/link.png")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "/etc/passwd")
    with pytest.raises(MaterialError):
        extract_deck_pages(link)


def test_pptx_uncompressed_limit_counts_unread_media(tmp_path):
    deck = write_test_pptx(tmp_path / "large.pptx", [["safe"]])
    # Ratio stays below 100 and the ZIP is under 50 MB. Unread media alone
    # pushes the central-directory uncompressed total past 100 MB.
    block = random.Random(1).randbytes(16 * 1024) * 64
    with ZipFile(deck, "a", ZIP_DEFLATED) as archive:
        for number in range(100):
            archive.writestr(f"ppt/media/image{number}.png", block)
    with pytest.raises(MaterialError):
        extract_deck_pages(deck)


@pytest.mark.parametrize("content", [
    '<Types><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml"/></Types>',
    '<Types><Override PartName="/ppt/safe.dat" ContentType="application/vnd.openxmlformats-officedocument.oleObject"/></Types>',
])
def test_pptx_rejects_execution_declared_by_content_type(tmp_path, content):
    deck = write_test_pptx(tmp_path / "macro.pptx", [["safe"]])
    with ZipFile(deck, "a") as archive:
        archive.writestr("[Content_Types].xml", content)
    with pytest.raises(MaterialError):
        extract_deck_pages(deck)


def test_pptx_rejects_embedded_execution_relationship(tmp_path):
    deck = write_test_pptx(tmp_path / "ole.pptx", [["safe"]])
    with ZipFile(deck, "a") as archive:
        archive.writestr("ppt/slides/_rels/slide1.xml.rels", '<Relationships><Relationship '
                         'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject" '
                         'Target="../object.dat"/></Relationships>')
    with pytest.raises(MaterialError):
        extract_deck_pages(deck)


def test_pptx_numeric_page_order_and_empty_pages(tmp_path):
    deck = write_test_pptx(tmp_path / "deck.pptx", [[str(n)] for n in range(1, 12)] + [[]])
    pages = extract_deck_pages(deck)
    assert [p.number for p in pages] == list(range(1, 13))
    assert pages[-1].text == ""


def test_pdf_active_content_and_encryption_are_rejected(tmp_path):
    from pypdf import PdfWriter
    for kind in ["javascript", "attachment", "encrypted"]:
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        if kind == "javascript":
            writer.add_js("FAKE_PRIVATE_SCRIPT")
        elif kind == "attachment":
            writer.add_attachment("secret.exe", b"FAKE_PRIVATE_BODY")
        else:
            writer.encrypt("password")
        deck = tmp_path / f"{kind}.pdf"
        writer.write(deck)
        with pytest.raises(MaterialError) as exc:
            extract_deck_pages(deck)
        assert "FAKE_PRIVATE" not in str(exc.value)


def test_pdf_file_backed_stream_is_rejected(tmp_path):
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import NameObject, TextStringObject
    deck = write_test_pdf(tmp_path / "file.pdf", ["safe"])
    writer = PdfWriter(clone_from=PdfReader(deck))
    stream = writer.pages[0]["/Contents"]
    stream[NameObject("/F")] = TextStringObject("https://private.example/stream")
    writer.write(deck)
    with pytest.raises(MaterialError):
        extract_deck_pages(deck)


def test_pdf_text_never_invokes_an_optional_external_image_decoder(tmp_path, monkeypatch):
    import app.materials as module
    from pypdf import PdfReader, PdfWriter, filters
    from pypdf.generic import NameObject
    deck = write_test_pdf(tmp_path / "codec.pdf", ["safe"])
    writer = PdfWriter(clone_from=PdfReader(deck))
    writer.pages[0]["/Contents"][NameObject("/Filter")] = NameObject("/JBIG2Decode")
    writer.write(deck)
    def forbidden_decoder(*args, **kwargs):
        raise AssertionError("Optional external decoder reached")
    monkeypatch.setattr(filters.JBIG2Decode, "decode", forbidden_decoder)
    with pytest.raises(MaterialError):
        module._pdf_pages(deck)


@pytest.mark.parametrize("kind", ["pages", "text"])
def test_pdf_resource_limits_are_enforced(tmp_path, kind):
    deck = write_test_pdf(tmp_path / "oversized.pdf", [""] * 1001 if kind == "pages" else ["x" * 100_001])
    with pytest.raises(MaterialError):
        extract_deck_pages(deck)


def test_settings_produce_an_exact_normalized_host_allowlist():
    settings = Settings(bunny_library_id=1, bunny_stream_api_key="fake", bunny_cdn_hostname="fake",
                        openai_api_key="fake", app_password="fake", _env_file=None,
                        material_allowed_hosts=" WWW.Assoholding.IT, docs.example.com ,www.assoholding.it")
    assert settings.parsed_material_allowed_hosts == ("www.assoholding.it", "docs.example.com")


def test_processor_promotes_only_parseable_candidates_and_cleans_workspace(tmp_path):
    source = write_test_pptx(tmp_path / "source.pptx", [["Decisioni assembleari"]])
    workspace = tmp_path / "job"
    workspace.mkdir()
    def fetcher(url, workspace, allowed_hosts, cancellation_event=None):
        target = workspace / "download.pptx"
        target.write_bytes(source.read_bytes() if "good" in url else b"FAKE_ERROR_BODY")
        return target
    result = MaterialProcessor(fetcher=fetcher).process(
        ["Slide Furio D’Andrea | https://www.assoholding.it/good.pptx",
         "Bad | https://www.assoholding.it/bad.pptx", "plain missing label"],
        [slide()], workspace, Event())
    assert len(result.materials) == 1
    assert result.materials[0].titolo == TITLE
    assert result.materials[0].relatore == "Furio D'Andrea"
    assert result.materials[0].pagine == 1
    assert result.materials[0].file is None
    assert result.slides[0].page == 1
    assert len(result.failures) == 2
    assert "FAKE_ERROR_BODY" not in repr(result)
    assert list(workspace.iterdir()) == []


def test_processor_compares_all_materials_before_accepting(tmp_path):
    one = write_test_pptx(tmp_path / "one.pptx", [["Decisioni assembleari"]])
    two = write_test_pptx(tmp_path / "two.pptx", [["Decisioni assembleari"]])
    workspace = tmp_path / "job"
    workspace.mkdir()
    result = MaterialProcessor().process([str(one), str(two)], [slide()], workspace)
    assert len(result.materials) == 2
    assert result.slides[0].page is None
    assert result.slides[0].material_title is None
    assert list(workspace.iterdir()) == []


def test_matching_deadline_keeps_verified_metadata_but_drops_unfinished_links(tmp_path, monkeypatch):
    import app.materials as module
    source = write_test_pptx(tmp_path / "source.pptx", [["Decisioni assembleari"]])
    workspace = tmp_path / "job"
    workspace.mkdir()
    real_popen = subprocess.Popen
    processes = []
    def stalled_matcher(args, **kwargs):
        if args[4] == "match":
            process = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
            processes.append(process)
            return process
        return real_popen(args, **kwargs)
    monkeypatch.setattr(module.subprocess, "Popen", stalled_matcher)
    monkeypatch.setattr(module, "MATCH_TIMEOUT_SECONDS", .15, raising=False)
    result = MaterialProcessor().process([str(source)], [slide()], workspace)
    assert len(result.materials) == 1
    assert result.slides[0].page is None
    assert result.slides[0].material_title is None
    assert processes[0].poll() is not None
    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize("cancel", [False, True])
def test_processor_cleans_partial_download_on_error_and_cancellation(tmp_path, cancel):
    def fetcher(url, workspace, allowed_hosts, cancellation_event=None):
        (workspace / "partial").write_bytes(b"transient")
        if cancel:
            raise CancelledError()
        raise OSError("FAKE_SECRET_BODY")
    processor = MaterialProcessor(fetcher=fetcher)
    if cancel:
        with pytest.raises(CancelledError):
            processor.process([URL], [slide()], tmp_path, Event())
    else:
        result = processor.process([URL], [slide()], tmp_path)
        assert result.materials == ()
        assert len(result.failures) == 1
        assert "FAKE_SECRET_BODY" not in repr(result)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("during_fetch", [False, True])
def test_cancellation_is_never_swallowed_when_there_are_no_slides(tmp_path, during_fetch):
    event = Event()
    def fetcher(url, workspace, allowed_hosts, cancellation_event=None):
        event.set()
        raise OSError("interrupted transfer")
    if not during_fetch:
        event.set()
    with pytest.raises(CancelledError):
        MaterialProcessor(fetcher=fetcher).process([URL] if during_fetch else [], [], tmp_path, event)
    assert list(tmp_path.iterdir()) == []


def test_processor_rejects_local_symlinks_without_deleting_source(tmp_path):
    source = write_test_pptx(tmp_path / "source.pptx", [["safe"]])
    link = tmp_path / "link.pptx"
    link.symlink_to(source)
    workspace = tmp_path / "job"
    workspace.mkdir()
    result = MaterialProcessor().process([str(link)], [], workspace)
    assert not result.materials
    assert result.failures
    assert source.is_file() and link.is_symlink()
    assert list(workspace.iterdir()) == []


def test_fetch_worker_is_killed_on_cancellation_and_removes_partial_file(tmp_path, monkeypatch):
    import app.materials as module
    real_popen = subprocess.Popen
    processes = []
    def slow_worker(args, **kwargs):
        # The slow external process replaces a stalled DNS/TLS/read operation.
        Path(args[6]).write_bytes(b"partial deck")
        process = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(module.subprocess, "Popen", slow_worker)
    event = Event()
    timer = Timer(.2, event.set)
    timer.start()
    try:
        with pytest.raises(CancelledError):
            fetch_deck(URL, tmp_path, ("www.assoholding.it",), event)
    finally:
        timer.cancel()
    assert processes and processes[0].poll() is not None
    assert list(tmp_path.iterdir()) == []


def test_parser_worker_cancel_reaps_child_and_removes_page_output(tmp_path, monkeypatch):
    import app.materials as module
    deck = write_test_pptx(tmp_path / "source.pptx", [["safe"]])
    real_popen = subprocess.Popen
    processes = []
    def slow_worker(args, **kwargs):
        Path(args[6]).write_bytes(b"private page text")
        process = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(module.subprocess, "Popen", slow_worker)
    event = Event()
    timer = Timer(.15, event.set)
    timer.start()
    try:
        with pytest.raises(CancelledError):
            extract_deck_pages(deck, cancellation_event=event)
    finally:
        timer.cancel()
    assert processes[0].poll() is not None
    assert list(tmp_path.iterdir()) == [deck]


def test_worker_total_deadline_kills_stalled_work(tmp_path, monkeypatch):
    import app.materials as module
    real_popen = subprocess.Popen
    processes = []
    def slow_worker(args, **kwargs):
        process = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(module.subprocess, "Popen", slow_worker)
    monkeypatch.setattr(module, "DOWNLOAD_TIMEOUT_SECONDS", .15)
    with pytest.raises(MaterialError):
        fetch_deck(URL, tmp_path, ("www.assoholding.it",))
    assert processes and processes[0].poll() is not None
    assert list(tmp_path.iterdir()) == []
