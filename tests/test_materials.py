"""Synthetic decks and hostile boundaries; no external network or real content."""

from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path
import os
import signal
import socket
import random
import subprocess
import sys
from threading import Event, Timer
import time
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from app.config import Settings
from app.materials import (
    DeckPage, MaterialError, MaterialProcessor, _download_https,
    _PinnedHTTPSConnection, extract_deck_pages, fetch_deck, match_slides_to_material,
)
from app.models import ReportMaterial, SlideChange
from app.storage import BunnyStorageError


URL = "https://www.assoholding.it/deck.pptx"
TITLE = "Slide · Furio D'Andrea"
PUBLIC = "93.184.216.34"


def write_test_pptx(path, pages, *, hidden=()):
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for number, lines in enumerate(pages, start=1):
            runs = "".join(f"<a:r><a:t>{escape(line)}</a:t></a:r>" for line in lines)
            archive.writestr(
                f"ppt/slides/slide{number}.xml",
                ('<p:sld show="0" ' if number in hidden else '<p:sld ') +
                'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                f"<p:cSld><a:p>{runs}</a:p></p:cSld></p:sld>",
            )
    return path


def add_test_presentation(path, order, relationships=None):
    relationships = relationships if relationships is not None else [
        ("r1", "slides/slide1.xml"), ("r2", "slides/slide2.xml"),
    ]
    ids = "".join(f'<p:sldId id="{256 + n}" r:id="{rid}"/>' for n, rid in enumerate(order))
    rels = "".join(
        f'<Relationship Id="{rid}" Target="{target}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"/>'
        for rid, target in relationships)
    with ZipFile(path, "a") as archive:
        archive.writestr("ppt/presentation.xml", '<p:presentation '
                         'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                         'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                         f'<p:sldIdLst>{ids}</p:sldIdLst></p:presentation>')
        archive.writestr("ppt/_rels/presentation.xml.rels", '<Relationships '
                         'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         f'{rels}</Relationships>')
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


def test_matching_accepts_exact_point_fifty_five_despite_weighted_float_roundoff():
    from difflib import SequenceMatcher
    text, candidate = "a b c", "c b a ddddddddddddd"
    jaccard = len(set(text.split()) & set(candidate.split())) / len(set(text.split()) | set(candidate.split()))
    sequence = SequenceMatcher(None, text, candidate).ratio()
    assert (jaccard, sequence) == (.75, .25)
    assert .6 * jaccard + .4 * sequence == .5499999999999999
    result = match_slides_to_material(
        [slide(text)], ReportMaterial(titolo=TITLE, url=URL), [DeckPage(1, candidate)])
    assert result[0].page == 1


@pytest.mark.parametrize("score,accepted", [(.5499999999999999, True), (.55, True),
                                            (.55 - 1e-14, False), (.54999999, False)])
def test_score_floor_tolerance_only_covers_float_roundoff(score, accepted):
    from app.materials import _score_at_least_floor
    assert _score_at_least_floor(score) is accepted


def test_page_order_is_chronological_stable_and_never_falls_back_to_worse_match():
    observed = [slide("Decisioni", time=20), slide("Premesse", time=10),
                slide("Premesse", time=30), slide("Decisioni", time=40)]
    result = match_slides_to_material(observed, ReportMaterial(titolo=TITLE, url=URL),
                                     [DeckPage(2, "Decisioni"), DeckPage(1, "Premesse")])
    assert [s.page for s in result] == [2, 1, None, 2]
    assert [s.timestamp_seconds for s in result] == [20, 10, 30, 40]


def test_matching_accepts_exact_one_tenth_margin_for_scores_one_and_point_nine():
    # Same token set; 12 matching characters out of 16 => sequence .75,
    # runner-up .6 * 1 + .4 * .75 = .9. The best page scores exactly 1.
    result = match_slides_to_material(
        [slide("one two three xx")], ReportMaterial(titolo=TITLE, url=URL),
        [DeckPage(1, "one two three xx"), DeckPage(2, "one three two xx")])
    assert result[0].page == 1


@pytest.mark.parametrize("runner_up,accepted", [(.9, True), (.90000000000001, False), (.90000001, False)])
def test_margin_tolerance_only_covers_float_roundoff(runner_up, accepted):
    from app.materials import _margin_at_least_tenth
    assert _margin_at_least_tenth(1.0, runner_up) is accepted


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


@pytest.mark.parametrize("suffix", ["?token=PRIVATE", "#PRIVATE"])
def test_sensitive_redirect_cannot_promote_public_source(tmp_path, suffix):
    with pytest.raises(MaterialError):
        download(URL, tmp_path / "deck", [
            Response(302, headers={"location": "/signed.pptx" + suffix}),
            Response(chunks=[b"PRIVATE-DECK"]),
        ])
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


def test_pptx_page_numbers_follow_presentation_relationship_order(tmp_path):
    deck = add_test_presentation(write_test_pptx(tmp_path / "ordered.pptx", [["Premesse"], ["Decisioni"]]), ["r2", "r1"])
    pages = extract_deck_pages(deck)
    assert [(p.number, p.text) for p in pages] == [(1, "Decisioni"), (2, "Premesse")]
    matched = match_slides_to_material([slide("Decisioni")], ReportMaterial(titolo=TITLE, file=str(deck)), pages)
    assert matched[0].page == 1


def test_pptx_hidden_slides_do_not_consume_displayed_page_numbers(tmp_path):
    deck = add_test_presentation(write_test_pptx(tmp_path / "hidden.pptx", [["Premesse"], ["Hidden"]], hidden=(2,)), ["r2", "r1"])
    assert [(p.number, p.text) for p in extract_deck_pages(deck)] == [(1, "Premesse")]


@pytest.mark.parametrize("order,relationships", [
    (["r1"], [("r1", "slides/missing.xml")]),
    (["r3"], [("r1", "slides/slide1.xml")]),
    (["r1"], [("r1", "slides/slide1.xml"), ("r1", "slides/slide2.xml")]),
    (["r1", "r1"], [("r1", "slides/slide1.xml")]),
    (["r1"], [("r1", "../../../escape.xml")]),
    (["r1"], [("r1", "https://private.example/slides.xml")]),
])
def test_pptx_rejects_missing_ambiguous_or_unsafe_presentation_references(tmp_path, order, relationships):
    deck = add_test_presentation(write_test_pptx(tmp_path / "bad-order.pptx", [["one"], ["two"]]), order, relationships)
    with pytest.raises(MaterialError):
        extract_deck_pages(deck)


def test_pptx_real_package_never_falls_back_when_presentation_relationships_are_missing(tmp_path):
    deck = write_test_pptx(tmp_path / "incomplete.pptx", [["one"]])
    with ZipFile(deck, "a") as archive:
        archive.writestr("ppt/presentation.xml", '<presentation/>')
    with pytest.raises(MaterialError):
        extract_deck_pages(deck)


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
    if hasattr(filters, "JBIG2Decode"):
        monkeypatch.setattr(filters.JBIG2Decode, "decode", forbidden_decoder)
    with pytest.raises(MaterialError):
        module._pdf_pages(deck)


@pytest.mark.parametrize("kind", ["pages", "text"])
def test_pdf_resource_limits_are_enforced(tmp_path, kind):
    deck = write_test_pdf(tmp_path / "oversized.pdf", [""] * 1001 if kind == "pages" else ["x" * 100_001])
    with pytest.raises(MaterialError):
        extract_deck_pages(deck)


def write_disguised_pdf_contents(path, *, count=1, stream_bytes=32, codec="/FlateDecode", indirect_array=False):
    import zlib
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, EncodedStreamObject, NameObject
    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")}))
    for _ in range(count):
        page = writer.add_blank_page(width=100, height=100)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        content = EncodedStreamObject()
        program = b"BT /F1 12 Tf 10 10 Td (safe) Tj ET" if stream_bytes > 100 else b""
        content._data = zlib.compress(b"%" + b"x" * (stream_bytes - len(program) - 2) + b"\n" + program)
        content[NameObject("/Filter")] = NameObject(codec)
        content[NameObject("/Subtype")] = NameObject("/Image")
        reference = writer._add_object(content)
        page[NameObject("/Contents")] = writer._add_object(ArrayObject([reference])) if indirect_array else reference
    writer.write(path)
    return path


def write_object_stream_pdf(path, *, count=12, stream_bytes=9 * 1024 * 1024 + 22,
                            rooted=True, direct_references=False, subtype="", cyclic=False):
    """Valid PDF 1.5 xref/object streams; each container holds two small objects.

    Padding compresses well but must count against the decoded byte budget.
    Containers are indexed by xref entries, not necessarily trailer-reachable.
    """
    import zlib
    data = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    containers = list(range(6, 6 + count))
    first_embedded = 6 + count
    xref_number = first_embedded + 2 * count
    program = b"BT /F1 12 Tf 10 10 Td (safe) Tj ET"

    def add(number, body):
        offsets[number] = len(data)
        data.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")

    probe = b""
    if rooted:
        probe += b"/Probe [" + b" ".join(f"{i} 0 R".encode() for i in range(first_embedded, xref_number)) + b"]"
    if direct_references:
        probe += b"/Containers [" + b" ".join(f"{i} 0 R {i} 0 R".encode() for i in [*containers, xref_number]) + b"]"
    add(1, b"<< /Type /Catalog /Pages 2 0 R " + probe + b" >>")
    add(2, b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>")
    add(3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] "
           b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>")
    add(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    add(5, f"<< /Length {len(program)} >>\nstream\n".encode() + program + b"\nendstream")
    for index, number in enumerate(containers):
        embedded = first_embedded + 2 * index
        body = f"<< /Next {embedded} 0 R >>".encode() if cyclic else b"<< /Value 0 >>"
        header = f"{embedded} 0 {embedded + 1} {len(body) + 1} ".encode()
        decoded = (header + body + b" << /Value 1 >>").ljust(stream_bytes, b" ")
        assert len(decoded) == stream_bytes
        compressed = zlib.compress(decoded)
        add(number, f"<< /Type /ObjStm /N 2 /First {len(header)} /Filter /FlateDecode "
            f"{subtype} /Length {len(compressed)} >>\nstream\n".encode() + compressed + b"\nendstream")
    xref_offset = len(data)
    offsets[xref_number] = xref_offset
    entries = bytearray(b"\x00\x00\x00\x00\x00\xff\xff")
    for number in range(1, xref_number + 1):
        if first_embedded <= number < xref_number:
            index, position = divmod(number - first_embedded, 2)
            entries.extend(b"\x02" + containers[index].to_bytes(4, "big") + position.to_bytes(2, "big"))
        else:
            entries.extend(b"\x01" + offsets[number].to_bytes(4, "big") + b"\x00\x00")
    add(xref_number, f"<< /Type /XRef /Size {xref_number + 1} /Root 1 0 R "
        f"/W [1 4 2] /Length {len(entries)} >>\nstream\n".encode() + entries + b"\nendstream")
    data.extend(f"startxref\n{xref_offset}\n%%EOF\n".encode())
    path.write_bytes(data)
    return path


@pytest.mark.parametrize("rooted", [True, False])
def test_pdf_object_streams_cannot_bypass_cumulative_100_mib_budget(tmp_path, rooted):
    # Production-scale probe: 12 * 9,437,206 = 113,246,472 decoded bytes,
    # despite a ~112 KiB PDF and each container below the 10 MiB stream cap.
    deck = write_object_stream_pdf(tmp_path / "object-stream-budget.pdf", rooted=rooted)
    assert deck.stat().st_size < 120 * 1024
    with pytest.raises(MaterialError, match="^MATERIALE_NON_RAGGIUNGIBILE$"):
        extract_deck_pages(deck)
    assert list(tmp_path.iterdir()) == [deck]


def test_pdf_initialization_xref_streams_share_the_cumulative_decoded_budget(tmp_path):
    import zlib
    deck = write_object_stream_pdf(tmp_path / "xref-stream-budget.pdf", count=0)
    data = bytearray(deck.read_bytes())
    previous = int(data.split(b"startxref\n")[-1].splitlines()[0])
    for number in range(7, 19):
        offset = len(data)
        entry = b"\x01" + offset.to_bytes(4, "big") + b"\x00\x00"
        compressed = zlib.compress(entry.ljust(9 * 1024 * 1024, b"\x00"))
        data.extend(f"{number} 0 obj\n<< /Type /XRef /Size {number + 1} /Root 1 0 R "
                    f"/Prev {previous} /W [1 4 2] /Index [{number} 1] /Filter /FlateDecode "
                    f"/Length {len(compressed)} >>\nstream\n".encode() + compressed + b"\nendstream\nendobj\n")
        data.extend(f"startxref\n{offset}\n%%EOF\n".encode())
        previous = offset
    deck.write_bytes(data)
    assert deck.stat().st_size < 120 * 1024
    with pytest.raises(MaterialError, match="^MATERIALE_NON_RAGGIUNGIBILE$"):
        extract_deck_pages(deck)
    assert list(tmp_path.iterdir()) == [deck]


def test_pdf_object_stream_budget_is_checked_before_page_extraction(tmp_path, monkeypatch):
    import app.materials as module
    from pypdf._page import PageObject
    deck = write_object_stream_pdf(tmp_path / "before-extraction.pdf", count=3, stream_bytes=80,
                                   rooted=False, subtype="/Subtype /Image")
    monkeypatch.setattr(module, "MAX_UNCOMPRESSED_BYTES", 200)
    def forbidden_extract(*args, **kwargs):
        raise AssertionError("Unaccounted object stream reached extraction")
    monkeypatch.setattr(PageObject, "extract_text", forbidden_extract)
    with pytest.raises(MaterialError):
        module._pdf_pages(deck)


def test_pdf_object_streams_count_once_across_xref_and_trailer_with_cycles(tmp_path, monkeypatch):
    import app.materials as module
    from pypdf import PdfReader
    deck = write_object_stream_pdf(tmp_path / "shared-objects.pdf", count=2, stream_bytes=80,
                                   direct_references=True, cyclic=True)
    # Both members in each stream share one container; direct duplicate
    # container refs and self-references must not double count or loop.
    reader = PdfReader(deck, strict=True)
    assert len(reader.xref_objStm) == 4
    assert len({value[0] for value in reader.xref_objStm.values()}) == 2
    monkeypatch.setattr(module, "MAX_UNCOMPRESSED_BYTES", 300)
    assert module._pdf_pages(deck)[0].text.strip() == "safe"


@pytest.mark.parametrize("extra_byte", [0, 1])
def test_pdf_object_and_content_streams_share_one_inclusive_budget(tmp_path, monkeypatch, extra_byte):
    import app.materials as module
    deck = write_object_stream_pdf(tmp_path / "combined-budget.pdf", count=2, stream_bytes=80,
                                   direct_references=True)
    # Two 80-byte object containers, one content stream, one 13-entry xref
    # stream with 7 bytes per entry. Shared trailer refs do not add copies.
    budget = 160 + len(b"BT /F1 12 Tf 10 10 Td (safe) Tj ET") + 13 * 7
    monkeypatch.setattr(module, "MAX_UNCOMPRESSED_BYTES", budget - extra_byte)
    if extra_byte:
        with pytest.raises(MaterialError):
            module._pdf_pages(deck)
    else:
        assert module._pdf_pages(deck)[0].text.strip() == "safe"


@pytest.mark.parametrize("table", [{1: (1, 0)}, {1: (2, 0), 2: (1, 0)},
                                    {1: (999, 0)}, {1: (6, -1)}, None])
def test_pdf_invalid_or_cyclic_object_stream_tables_fail_closed_without_resolution(table):
    from types import SimpleNamespace
    from app.materials import _pdf_object_streams
    # No get_object API: resolution itself would fail this test. Cycle/nesting
    # and unknown table shapes must be rejected before pypdf can recurse.
    reader = SimpleNamespace(xref_objStm=table, xref={0: {1: 10, 2: 20, 6: 60}})
    with pytest.raises(MaterialError):
        tuple(_pdf_object_streams(reader))


def test_disguised_pdf_contents_cannot_bypass_100_mib_decoded_limit(tmp_path):
    # Eleven distinct 10 MiB streams: each is at its individual limit, the
    # compressed file is small, and only the cumulative limit must reject it.
    deck = write_disguised_pdf_contents(tmp_path / "110-mib.pdf", count=11, stream_bytes=10 * 1024 * 1024)
    with pytest.raises(MaterialError):
        extract_deck_pages(deck)
    assert list(tmp_path.iterdir()) == [deck]


def test_disguised_pdf_contents_at_exact_100_mib_are_accounted_and_read(tmp_path):
    deck = write_disguised_pdf_contents(tmp_path / "100-mib.pdf", count=10, stream_bytes=10 * 1024 * 1024)
    pages = extract_deck_pages(deck)
    assert len(pages) == 10
    assert all(page.text.strip() == "safe" for page in pages)
    assert list(tmp_path.iterdir()) == [deck]


def test_disguised_font_streams_are_counted_before_text_extraction(tmp_path, monkeypatch):
    import zlib
    import app.materials as module
    from pypdf import PdfWriter
    from pypdf._page import PageObject
    from pypdf.generic import DictionaryObject, EncodedStreamObject, NameObject
    writer = PdfWriter()
    for _ in range(11):
        page = writer.add_blank_page(width=100, height=100)
        cmap = EncodedStreamObject()
        cmap._data = zlib.compress(b"%" + b"x" * (10 * 1024 * 1024 - 2) + b"\n")
        cmap[NameObject("/Filter")] = NameObject("/FlateDecode")
        cmap[NameObject("/Subtype")] = NameObject("/Image")
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica"),
            NameObject("/ToUnicode"): writer._add_object(cmap)})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
            DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    deck = tmp_path / "font-110-mib.pdf"
    writer.write(deck)
    def forbidden_extract(*args, **kwargs):
        raise AssertionError("Oversized disguised font stream reached extraction")
    monkeypatch.setattr(PageObject, "extract_text", forbidden_extract)
    with pytest.raises(MaterialError):
        module._pdf_pages(deck)


def test_disguised_pdf_contents_are_checked_through_indirect_arrays(tmp_path, monkeypatch):
    import app.materials as module
    from pypdf import filters
    deck = write_disguised_pdf_contents(tmp_path / "array.pdf", codec="/JBIG2Decode", indirect_array=True)
    def forbidden_decode(*args, **kwargs):
        raise AssertionError("Unvalidated disguised content reached the decoder")
    if hasattr(filters, "JBIG2Decode"):
        monkeypatch.setattr(filters.JBIG2Decode, "decode", forbidden_decode)
    with pytest.raises(MaterialError):
        module._pdf_pages(deck)


@pytest.mark.parametrize("legacy_api", [False, True])
def test_pdf_disables_external_decoders_before_reader_parses_any_objects(tmp_path, monkeypatch, legacy_api):
    import pypdf
    import app.materials as module
    from pypdf import filters
    from pypdf.generic import EncodedStreamObject, NameObject
    from contextlib import nullcontext
    deck = write_test_pdf(tmp_path / "safe.pdf", ["safe"])
    reader = pypdf.PdfReader
    def reader_with_encoded_object(*args, **kwargs):
        # A malicious object stream can be decoded inside PdfReader before
        # application traversal sees the page or /Contents dictionary.
        encoded = EncodedStreamObject()
        encoded._data = b"untrusted JBIG2 bytes"
        encoded[NameObject("/Filter")] = NameObject("/JBIG2Decode")
        encoded.get_data()
        return reader(*args, **kwargs)
    monkeypatch.setattr(pypdf, "PdfReader", reader_with_encoded_object)
    monkeypatch.setattr(filters, "JBIG2DEC_BINARY", "/untrusted/decoder", raising=False)
    attempted = []
    def forbidden_subprocess(*args, **kwargs):
        attempted.append("subprocess")
        raise AssertionError("External execution attempted")
    monkeypatch.setattr(subprocess, "run", forbidden_subprocess)
    temporary = []
    def forbidden_temp(*args, **kwargs):
        temporary.append("temporary")
        raise AssertionError("Decoder created a directory outside workspace")
    monkeypatch.setattr(filters, "TemporaryDirectory", forbidden_temp, raising=False)
    context = pypdf.apply_configuration(jbig2dec_binary="/untrusted/decoder", disable_legacy_handling=False) if hasattr(pypdf, "apply_configuration") else nullcontext()
    with context:
        if legacy_api:
            monkeypatch.delattr(pypdf, "overwrite_configuration", raising=False)
        with pytest.raises(MaterialError):
            module._pdf_pages(deck)
    assert attempted == [] and temporary == []


def test_pdf_cyclic_contents_array_is_rejected_without_text_extraction(tmp_path, monkeypatch):
    import app.materials as module
    from pypdf import PdfWriter
    from pypdf._page import PageObject
    from pypdf.generic import ArrayObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    contents = ArrayObject()
    reference = writer._add_object(contents)
    contents.append(reference)
    page[NameObject("/Contents")] = reference
    deck = tmp_path / "cycle.pdf"
    writer.write(deck)
    def forbidden_extract(*args, **kwargs):
        raise AssertionError("Cyclic contents reached extraction")
    monkeypatch.setattr(PageObject, "extract_text", forbidden_extract)
    with pytest.raises(MaterialError):
        module._pdf_pages(deck)


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


def test_processor_rehosts_fetched_decks_and_reports_the_cdn_url(tmp_path):
    source = write_test_pptx(tmp_path / "source.pptx", [["Decisioni assembleari"]])
    workspace = tmp_path / "job"
    workspace.mkdir()
    def fetcher(url, workspace, allowed_hosts, cancellation_event=None):
        deck = workspace / "download.pptx"
        deck.write_bytes(source.read_bytes())
        return deck
    uploads = []
    def hoster(deck, key):
        uploads.append((Path(deck).read_bytes(), key))
        return f"https://academy-decks.b-cdn.net/{key}"
    result = MaterialProcessor(fetcher=fetcher, hoster=hoster).process(
        ["Slide Furio D’Andrea | https://www.assoholding.it/good.pptx"],
        [slide()], workspace, Event(), hosting_prefix="video-123")
    assert uploads == [(source.read_bytes(), "video-123/slide-furio-dandrea.pptx")]
    assert result.hosted == ((
        "https://www.assoholding.it/good.pptx",
        "https://academy-decks.b-cdn.net/video-123/slide-furio-dandrea.pptx",
    ),)
    assert result.materials[0].url == "https://www.assoholding.it/good.pptx"


def test_processor_hosting_failure_keeps_the_original_source(tmp_path):
    source = write_test_pptx(tmp_path / "source.pptx", [["Decisioni assembleari"]])
    workspace = tmp_path / "job"
    workspace.mkdir()
    def fetcher(url, workspace, allowed_hosts, cancellation_event=None):
        deck = workspace / "download.pptx"
        deck.write_bytes(source.read_bytes())
        return deck
    def hoster(deck, key):
        raise BunnyStorageError()
    result = MaterialProcessor(fetcher=fetcher, hoster=hoster).process(
        ["Slide Furio D’Andrea | https://www.assoholding.it/good.pptx"],
        [slide()], workspace, Event(), hosting_prefix="video-123")
    assert result.hosted == ()
    assert len(result.materials) == 1
    assert result.failures == ()


def test_processor_without_hosting_prefix_never_uploads(tmp_path):
    source = write_test_pptx(tmp_path / "source.pptx", [["Decisioni assembleari"]])
    workspace = tmp_path / "job"
    workspace.mkdir()
    def fetcher(url, workspace, allowed_hosts, cancellation_event=None):
        deck = workspace / "download.pptx"
        deck.write_bytes(source.read_bytes())
        return deck
    def hoster(deck, key):
        raise AssertionError("Senza hosting_prefix non si carica nulla")
    result = MaterialProcessor(fetcher=fetcher, hoster=hoster).process(
        ["Slide Furio D’Andrea | https://www.assoholding.it/good.pptx"],
        [slide()], workspace, Event())
    assert result.hosted == ()
    assert len(result.materials) == 1


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


def test_distinct_sources_with_identical_titles_never_get_title_only_links(tmp_path):
    one, two, workspace = tmp_path / "one", tmp_path / "two", tmp_path / "job"
    one.mkdir()
    two.mkdir()
    workspace.mkdir()
    first = write_test_pptx(one / "Slide Furio D’Andrea.pptx", [["Decisioni assembleari"]])
    second = write_test_pptx(two / "Slide Furio D'Andrea.pptx", [["Comunicazioni logistiche"]])
    result = MaterialProcessor().process([str(first), str(second)], [slide()], workspace)
    assert [m.titolo for m in result.materials] == [TITLE, TITLE]
    assert result.slides[0].material_title is None and result.slides[0].page is None
    assert result.failures == ("MATERIALE_NON_RAGGIUNGIBILE",)
    assert list(workspace.iterdir()) == []


def test_duplicate_source_urls_deduplicate_before_title_ambiguity(tmp_path):
    source = write_test_pptx(tmp_path / "source.pptx", [["Decisioni assembleari"]])
    workspace = tmp_path / "job"
    workspace.mkdir()
    def fetcher(url, workspace, allowed_hosts, cancellation_event=None):
        deck = workspace / "deck"
        deck.write_bytes(source.read_bytes())
        return deck
    first = f"{TITLE} | https://www.assoholding.it/a.pptx"
    result = MaterialProcessor(fetcher=fetcher).process(
        [first, first, "Other label | https://WWW.ASSOHOLDING.IT:443/a.pptx#section"],
        [slide()], workspace)
    assert len(result.materials) == 1
    assert result.materials[0].titolo == TITLE
    assert result.slides[0].page == 1
    assert result.failures == ()
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


@pytest.mark.parametrize("mode", ["fetch", "parse", "match"])
@pytest.mark.parametrize("internal", [True, False])
def test_worker_exit_tags_distinguish_internal_from_operational_errors(tmp_path, monkeypatch, mode, internal):
    import app.materials as module
    real_popen = subprocess.Popen
    script = """
import sys
from pathlib import Path
import app.materials as m
mode, source, output, internal = sys.argv[1:]
def fail(*args, **kwargs):
    if internal == 'True':
        raise TypeError('PRIVATE-WORKER-TRACE https://user:password@host/?token')
    raise m.MaterialError()
m._download_https = fail
m._extract_pages = fail
m._match_materials = fail
sys.argv = ['worker', mode, source, output, '[]']
raise SystemExit(m._worker_main())
"""
    source = tmp_path / "input.json"
    source.write_text('{"slides": [], "decks": []}')
    def worker(args, **kwargs):
        return real_popen([sys.executable, "-B", "-c", script, mode, str(source), args[6], str(internal)], **kwargs)
    monkeypatch.setattr(module.subprocess, "Popen", worker)
    with pytest.raises(RuntimeError if internal else MaterialError) as caught:
        module._run_worker(mode, str(source), tmp_path / "result", timeout=3)
    assert str(caught.value) == ("MATERIAL_INTERNAL_ERROR" if internal else "MATERIALE_NON_RAGGIUNGIBILE")
    assert list(tmp_path.iterdir()) == [source]


def test_worker_supervisor_does_not_convert_programming_errors(tmp_path, monkeypatch):
    import app.materials as module
    def broken_popen(*args, **kwargs):
        raise TypeError("PRIVATE-SUPERVISOR")
    monkeypatch.setattr(module.subprocess, "Popen", broken_popen)
    with pytest.raises(TypeError):
        module._run_worker("fetch", URL, tmp_path / "result", timeout=3)


def test_untagged_worker_startup_failure_is_internal(tmp_path, monkeypatch):
    import app.materials as module
    real_popen = subprocess.Popen
    def startup_failure(args, **kwargs):
        # Import/initialization failures exit 1 before the worker protocol runs.
        return real_popen([sys.executable, "-c", "raise SystemExit(1)"], **kwargs)
    monkeypatch.setattr(module.subprocess, "Popen", startup_failure)
    with pytest.raises(RuntimeError, match="^MATERIAL_INTERNAL_ERROR$"):
        module._run_worker("parse", "unused", tmp_path / "result", timeout=3)


def test_downloader_does_not_convert_internal_connection_error(tmp_path):
    def broken_connection(*args, **kwargs):
        raise TypeError("PRIVATE-CONNECTION")
    with pytest.raises(TypeError):
        _download_https(URL, tmp_path / "result", ("www.assoholding.it",),
                        resolver=dns, connection_factory=broken_connection)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("mode,payload", [
    ("fetch", None), ("parse", None), ("match", None), ("fetch", ""),
    ("fetch", "<directory>"),
    ("parse", '[{"number":2,"text":"private"}]'),
    ("parse", '[{"number":1,"text":"private"},{"number":1,"text":"private"}]'),
    ("parse", '{"PRIVATE": true}'), ("parse", 'PRIVATE-NOT-JSON'),
    ("parse", '[{"number":true,"text":"private"}]'),
    ("parse", '[{"number":1,"text":null}]'),
    ("parse", '[{"number":1,"text":"private","extra":"PRIVATE"}]'),
    pytest.param("parse", "[" * (sys.getrecursionlimit() + 100) + "0" + "]" * (sys.getrecursionlimit() + 100), id="deep_json"),
    pytest.param("parse", "[" + "9" * 5000 + "]", id="oversized_integer"),
    ("match", 'PRIVATE-NOT-JSON'), ("match", '{"PRIVATE": true}'),
    ("match", '[["Slide · Furio D\'Andrea",true]]'),
    ("match", '[["Slide · Furio D\'Andrea",1.0]]'),
    ("match", '[["Slide · Furio D\'Andrea"]]'),
    ("match", '[[null,1]]'), ("match", '[]'),
])
def test_successful_worker_requires_complete_typed_result(tmp_path, monkeypatch, mode, payload):
    import app.materials as module
    source = write_test_pptx(tmp_path / "source.pptx", [["Decisioni assembleari"]])
    workspace = tmp_path / "job"
    workspace.mkdir()
    def worker(stage, source, output, **kwargs):
        assert stage == mode
        if payload == "<directory>":
            output.mkdir()
        elif payload is not None:
            output.write_text(payload)
    monkeypatch.setattr(module, "_run_worker", worker)
    with pytest.raises(module.MaterialInternalError, match="^MATERIAL_INTERNAL_ERROR$"):
        if mode == "fetch":
            fetch_deck(URL, workspace, ("www.assoholding.it",))
        elif mode == "parse":
            extract_deck_pages(source)
        else:
            module._match_in_workspace([slide()], [(ReportMaterial(titolo=TITLE, url=URL, pagine=1),
                (DeckPage(1, "Decisioni assembleari"),))], workspace, Event())
    assert list(workspace.iterdir()) == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["job", "source.pptx"]


@pytest.mark.parametrize("boundary", ["connection", "stream"])
def test_downloader_internal_valueerror_propagates(tmp_path, boundary):
    def connection(*args, **kwargs):
        raise ValueError("PRIVATE-INTERNAL-CONNECTION-BUG")
    with pytest.raises(ValueError, match="PRIVATE-INTERNAL-CONNECTION-BUG"):
        if boundary == "connection":
            _download_https(URL, tmp_path / "result", ("www.assoholding.it",),
                            resolver=dns, connection_factory=connection)
        else:
            download(URL, tmp_path / "result", [Response(chunks=[b"partial", ValueError("PRIVATE-INTERNAL-CONNECTION-BUG")])])
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("url,headers", [
    ("https://www.assoholding.it:invalid/deck", {}),
    ("https://[broken/deck", {}), (URL, {"content-length": "PRIVATE-NOT-A-NUMBER"}),
    (URL, {"content-length": "1.5"}),
])
def test_external_url_and_header_format_errors_remain_operational(tmp_path, url, headers):
    with pytest.raises(MaterialError, match="^MATERIALE_NON_RAGGIUNGIBILE$"):
        download(url, tmp_path / "result", [Response(headers=headers)])
    assert list(tmp_path.iterdir()) == []


def test_worker_deadline_also_stops_descendants(tmp_path, monkeypatch):
    import app.materials as module
    real_popen = subprocess.Popen
    marker, pidfile = tmp_path / "escaped", tmp_path / "child.pid"
    child_script = "import sys,time; from pathlib import Path; time.sleep(.6); Path(sys.argv[1]).write_text('escaped')"
    worker_script = (
        "import subprocess,sys,time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); "
        "Path(sys.argv[3]).write_text(str(child.pid)); time.sleep(60)"
    )
    def parent_and_child(args, **kwargs):
        return real_popen([sys.executable, "-c", worker_script, child_script, str(marker), str(pidfile)], **kwargs)
    monkeypatch.setattr(module.subprocess, "Popen", parent_and_child)
    try:
        with pytest.raises(MaterialError):
            module._run_worker("parse", "unused", tmp_path / "output", timeout=.25)
        assert pidfile.exists()
        time.sleep(.7)
        assert not marker.exists(), "Worker child outlived the enforced deadline"
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_concurrent_jobs_keep_materials_and_cleanup_isolated(tmp_path):
    one = write_test_pdf(tmp_path / "one.pdf", ["Decisioni assembleari"])
    two = write_test_pptx(tmp_path / "two.pptx", [["Premesse"]])
    workspaces = [tmp_path / "job-one", tmp_path / "job-two"]
    for workspace in workspaces:
        workspace.mkdir()
    processor = MaterialProcessor()
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(processor.process, [str(source)], [slide(title)], workspace)
                   for source, title, workspace in zip([one, two], ["Decisioni assembleari", "Premesse"], workspaces)]
        results = [future.result(timeout=10) for future in futures]
    assert [result.materials[0].titolo for result in results] == ["one", "two"]
    assert [result.slides[0].page for result in results] == [1, 1]
    assert all(result.failures == () for result in results)
    assert all(list(workspace.iterdir()) == [] for workspace in workspaces)
    assert one.is_file() and two.is_file()
