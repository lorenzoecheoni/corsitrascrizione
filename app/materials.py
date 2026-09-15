"""Verify and inspect declared decks only inside an ephemeral job workspace.

Network and parser workers are killable: DNS, TLS, decompression and text
extraction cannot extend the parent's deadline or outlive cancellation. No deck
content is logged. Only verified metadata and conservative slide links escape
``MaterialProcessor.process``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from collections import Counter
from concurrent.futures import CancelledError
from dataclasses import dataclass
from difflib import SequenceMatcher
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
from threading import Event
from time import monotonic
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from uuid import UUID, uuid4
from xml.etree import ElementTree
from zipfile import ZipFile

from app.config import parse_material_allowed_hosts
from app.material_registry import canonical_material_title, resolve_material_sources
from app.models import ReportMaterial, SlideChange
from app.retry import check_cancelled


DOWNLOAD_TIMEOUT_SECONDS = 20.0
PARSE_TIMEOUT_SECONDS = 20.0
MATCH_TIMEOUT_SECONDS = 20.0
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_XML_BYTES = 10 * 1024 * 1024
MAX_PAGES = 1000
MAX_PAGE_TEXT = 100_000
MAX_TOTAL_TEXT = 2_000_000
MAX_RESULT_BYTES = 12 * MAX_TOTAL_TEXT
MAX_ARCHIVE_ENTRIES = 10_000
MAX_MATERIALS = 32
_FAILURE = "MATERIALE_NON_RAGGIUNGIBILE"
_SLIDE_PATH = re.compile(r"ppt/slides/slide([1-9][0-9]*)\.xml\Z")
_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


class MaterialError(Exception):
    """Fixed application-authored error, never a provider/parser message."""

    def __init__(self) -> None:
        super().__init__(_FAILURE)


@dataclass(frozen=True)
class DeckPage:
    number: int
    text: str


@dataclass(frozen=True)
class MaterialAnalysis:
    materials: tuple[ReportMaterial, ...]
    slides: tuple[SlideChange, ...]
    failures: tuple[str, ...]


def _validate_url(url: str, allowed_hosts: Sequence[str]) -> tuple[str, str]:
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url) or "\\" in url:
        raise MaterialError()
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443) or host not in allowed_hosts):
        raise MaterialError()
    # Exact DNS names only, regardless of what an operator added to the allowlist.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise MaterialError()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise MaterialError()
    return host, urlunsplit(("", "", parsed.path or "/", parsed.query, ""))


def _public_addresses(host: str, resolver: Callable) -> tuple[str, ...]:
    answers = resolver(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    addresses: list[str] = []
    for family, socktype, protocol, _, address in answers:
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise MaterialError()
        ip = ipaddress.ip_address(address[0])
        if not ip.is_global or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise MaterialError()
        if isinstance(ip, ipaddress.IPv6Address) and (
            ip.ipv4_mapped is not None or ip.sixtofour is not None or ip.teredo is not None
        ):
            raise MaterialError()
        if str(ip) not in addresses:
            addresses.append(str(ip))
    if not addresses:
        raise MaterialError()
    return tuple(addresses)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to validated numeric addresses; use the DNS hostname for TLS/Host."""

    def __init__(self, host: str, addresses: tuple[str, ...], timeout: float):
        super().__init__(host, port=443, timeout=timeout)
        self._addresses = addresses

    def connect(self) -> None:
        for address in self._addresses:
            raw = None
            try:
                raw = socket.create_connection((address, 443), self.timeout)
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                return
            except OSError:
                if raw is not None:
                    raw.close()
        raise MaterialError()


def _download_https(
    url: str, destination: Path, allowed_hosts: Sequence[str], *,
    resolver: Callable | None = None, connection_factory: Callable | None = None,
) -> None:
    """Worker-only streamed transfer; the parent enforces a total 20s deadline.

    A fresh DNS check precedes every hop, including same-host redirects. The
    connection never resolves the hostname again, uses no proxy/netrc/cookies,
    and never downloads an error or redirect response body.
    """
    created = False
    connection = None
    deadline = monotonic() + DOWNLOAD_TIMEOUT_SECONDS
    try:
        hosts = parse_material_allowed_hosts(allowed_hosts)
        for hop in range(6):
            host, target = _validate_url(url, hosts)
            addresses = _public_addresses(host, resolver or socket.getaddrinfo)
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise MaterialError()
            connection = (connection_factory or _PinnedHTTPSConnection)(host, addresses, remaining)
            connection.request("GET", target, headers={
                "Accept-Encoding": "identity", "User-Agent": "BunnyVideoReport/1.0",
                "Connection": "close",
            })
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("location")
                if not location or hop == 5:
                    raise MaterialError()
                url = urljoin(url, location)
                connection.close()
                connection = None
                continue
            if response.status != 200:
                raise MaterialError()
            if response.getheader("content-encoding", "identity").lower() != "identity":
                raise MaterialError()
            length = response.getheader("content-length")
            declared = None if length is None else int(length)
            if declared is not None and not 0 <= declared <= MAX_DOWNLOAD_BYTES:
                raise MaterialError()
            total = 0
            with destination.open("xb") as output:
                created = True
                while True:
                    if monotonic() >= deadline:
                        raise MaterialError()
                    chunk = response.read1(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise MaterialError()
                    output.write(chunk)
            if total == 0 or declared is not None and total != declared:
                raise MaterialError()
            return
        raise MaterialError()
    except Exception:
        if created:
            destination.unlink(missing_ok=True)
        raise MaterialError() from None
    finally:
        if connection is not None:
            connection.close()


def _run_worker(
    mode: str, source: str, output: Path, *, allowed_hosts: Sequence[str] = (),
    timeout: float, cancellation_event: Event | None = None,
) -> None:
    check_cancelled(cancellation_event)
    deadline = monotonic() + timeout
    process = None
    try:
        # -B avoids generating bytecode outside the supplied job workspace.
        # Fixed argv, no shell, and DEVNULL prevent URLs/parser errors in logs.
        process = subprocess.Popen(
            [sys.executable, "-B", "-m", "app.materials", mode, source, str(output),
             json.dumps(list(allowed_hosts))],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=Path(__file__).resolve().parent.parent,
            start_new_session=True,
        )
        while True:
            check_cancelled(cancellation_event)
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise MaterialError()
            try:
                code = process.wait(timeout=min(.05, remaining))
            except subprocess.TimeoutExpired:
                continue
            check_cancelled(cancellation_event)
            if code != 0 or monotonic() > deadline:
                raise MaterialError()
            return
    except CancelledError:
        raise
    except Exception:
        raise MaterialError() from None
    finally:
        if process is not None:
            # Stop the session group even if the worker exited before its
            # descendants. This also covers timeout and cancellation.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def fetch_deck(
    url: str, workspace: Path, allowed_hosts: Sequence[str] | str,
    cancellation_event: Event | None = None,
) -> Path:
    """Fetch into ``workspace``; caller owns the returned temporary file."""
    destination = Path(workspace) / f"material-{uuid4().hex}.deck"
    try:
        hosts = parse_material_allowed_hosts(allowed_hosts)
        _validate_url(url, hosts)
        _run_worker("fetch", url, destination, allowed_hosts=hosts,
                    timeout=DOWNLOAD_TIMEOUT_SECONDS, cancellation_event=cancellation_event)
        if not destination.is_file() or not 0 < destination.stat().st_size <= MAX_DOWNLOAD_BYTES:
            raise MaterialError()
        return destination
    except CancelledError:
        destination.unlink(missing_ok=True)
        raise
    except Exception:
        destination.unlink(missing_ok=True)
        raise MaterialError() from None


def _xml(data: bytes) -> ElementTree.Element:
    # Forbid DTD/entity declarations and non-UTF8 XML before parsing. This also
    # blocks UTF16 variants that could bypass an ASCII declaration check.
    text = data.decode("utf-8-sig")
    if "\x00" in text or "<!doctype" in text.lower() or "<!entity" in text.lower():
        raise MaterialError()
    return ElementTree.fromstring(text)


def _pptx_display_order(archive, entries, slides, relationships) -> list[str]:
    names = {entry.filename for entry in entries}
    slide_names = {entry.filename for _, entry in slides}
    if "ppt/presentation.xml" not in names:
        # The brief's tiny synthetic fixtures contain slide XML only. A real
        # package with any other part must provide authoritative ordering.
        if any(not entry.is_dir() and entry.filename not in slide_names for entry in entries):
            raise MaterialError()
        return [entry.filename for _, entry in sorted(slides)]
    if relationships is None or archive.getinfo("ppt/presentation.xml").file_size > MAX_XML_BYTES:
        raise MaterialError()
    root = _xml(archive.read("ppt/presentation.xml"))
    if root.tag != f"{{{_P_NS}}}presentation":
        raise MaterialError()
    lists = root.findall(f"{{{_P_NS}}}sldIdLst")
    if len(lists) != 1 or not 1 <= len(lists[0]) <= MAX_PAGES:
        raise MaterialError()
    order = []
    seen_ids, seen_relationships = set(), set()
    for node in lists[0]:
        rid, slide_id = node.get(f"{{{_R_NS}}}id"), node.get("id")
        if (node.tag != f"{{{_P_NS}}}sldId" or not slide_id or not slide_id.isdigit()
                or slide_id in seen_ids or not rid or rid in seen_relationships
                or rid not in relationships):
            raise MaterialError()
        target, kind = relationships[rid]
        if target not in slide_names or kind != f"{_R_NS}/slide" or target in order:
            raise MaterialError()
        order.append(target)
        seen_ids.add(slide_id)
        seen_relationships.add(rid)
    return order


def _pptx_pages(path: Path) -> tuple[DeckPage, ...]:
    with ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_ENTRIES or sum(i.file_size for i in entries) > MAX_UNCOMPRESSED_BYTES:
            raise MaterialError()
        names: set[str] = set()
        slides = []
        presentation_relationships = None
        for entry in entries:
            name = entry.filename
            lower = name.lower()
            if (not name or name.startswith("/") or "\\" in name or ":" in name
                    or any(part in (".", "..") for part in name.split("/"))
                    or name in names or stat.S_ISLNK(entry.external_attr >> 16)
                    or entry.flag_bits & 1 or entry.compress_type not in (0, 8)
                    or entry.file_size > max(entry.compress_size, 1) * 100
                    or any(c in lower for c in ("vbaproject", "/embeddings/", "/activex/", "/ctrlprops/"))
                    or PurePosixPath(lower).suffix in {".bin", ".exe", ".dll", ".js", ".vbs", ".com", ".bat"}):
                raise MaterialError()
            names.add(name)
            if lower == "[content_types].xml":
                if entry.file_size > MAX_XML_BYTES:
                    raise MaterialError()
                root = _xml(archive.read(entry))
                if any(marker in node.attrib.get("ContentType", "").lower()
                       for node in root.iter()
                       for marker in ("macroenabled", "vba", "oleobject", "activex")):
                    raise MaterialError()
            if lower.endswith(".rels"):
                if entry.file_size > MAX_XML_BYTES:
                    raise MaterialError()
                root = _xml(archive.read(entry))
                relationships = {}
                if name == "ppt/_rels/presentation.xml.rels" and root.tag != f"{{{_PACKAGE_REL_NS}}}Relationships":
                    raise MaterialError()
                for relation in root.iter():
                    if relation.tag.rsplit("}", 1)[-1] != "Relationship":
                        continue
                    target = unquote(relation.attrib.get("Target", ""))
                    source_dir = posixpath.dirname(posixpath.dirname(name))
                    resolved = posixpath.normpath(posixpath.join(source_dir, target))
                    relation_type = relation.attrib.get("Type", "").rsplit("/", 1)[-1].lower()
                    if (not target or relation.attrib.get("TargetMode", "Internal").lower() != "internal"
                            or relation_type in {"oleobject", "package", "control", "vbaproject", "attachedtemplate"}
                            or urlsplit(target).scheme or target.startswith(("/", "\\"))
                            or "\\" in target or resolved == ".." or resolved.startswith("../")):
                        raise MaterialError()
                    if name == "ppt/_rels/presentation.xml.rels":
                        rid = relation.get("Id")
                        if not rid or rid in relationships:
                            raise MaterialError()
                        relationships[rid] = (resolved, relation.get("Type"))
                if name == "ppt/_rels/presentation.xml.rels":
                    presentation_relationships = relationships
            match = _SLIDE_PATH.fullmatch(name)
            if match:
                if entry.file_size > MAX_XML_BYTES:
                    raise MaterialError()
                slides.append((int(match[1]), entry))
        if not 1 <= len(slides) <= MAX_PAGES:
            raise MaterialError()
        order = _pptx_display_order(archive, entries, slides, presentation_relationships)
        parsed = {}
        total = 0
        for _, entry in sorted(slides):
            root = _xml(archive.read(entry))
            if root.tag != f"{{{_P_NS}}}sld" or root.get("show", "true") not in {"0", "1", "true", "false"}:
                raise MaterialError()
            text = " ".join(node.text or "" for node in root.iter(
                "{http://schemas.openxmlformats.org/drawingml/2006/main}t"))
            total += len(text)
            if len(text) > MAX_PAGE_TEXT or total > MAX_TOTAL_TEXT:
                raise MaterialError()
            parsed[entry.filename] = (text, root.get("show") in {"0", "false"})
        pages = []
        for name in order:
            text, hidden = parsed[name]
            if not hidden:
                pages.append(DeckPage(len(pages) + 1, text))
        if not pages:
            raise MaterialError()
        return tuple(pages)


def _reject_pdf_external_decoder(*args, **kwargs):
    raise MaterialError()


def _pdf_object_streams(reader):
    """Enumerate containers that resolving pypdf's compressed xrefs can decode.

    Containers are not normally trailer-reachable. Object streams cannot
    themselves be compressed objects (PDF 1.5); reject nested/cyclic tables
    before resolving any container. Unknown reader table shapes fail closed.
    """
    from pypdf.generic import IndirectObject, StreamObject
    table = getattr(reader, "xref_objStm", None)
    xref = getattr(reader, "xref", None)
    if not isinstance(table, dict) or not isinstance(xref, dict) or len(table) > 100_000:
        raise MaterialError()
    direct = xref.get(0, {})
    containers = set()
    for member, entry in table.items():
        if (not isinstance(member, int) or member <= 0
                or not isinstance(entry, (tuple, list)) or len(entry) != 2):
            raise MaterialError()
        number, index = entry
        if (not isinstance(number, int) or number <= 0 or not isinstance(index, int) or index < 0
                or number in table or number not in direct):
            raise MaterialError()
        containers.add(number)
    for number in sorted(containers):
        obj = IndirectObject(number, 0, reader).get_object()
        if not isinstance(obj, StreamObject) or obj.get("/Type") != "/ObjStm":
            raise MaterialError()
        yield obj


def _pdf_content_stream_ids(reader) -> set[int]:
    """Resolve page/font text streams by role, not their declared /Subtype."""
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NullObject, StreamObject
    streams: set[int] = set()
    visited = set()
    steps = 0
    font_stream_keys = {"/ToUnicode", "/FontFile", "/FontFile2", "/FontFile3"}
    stack = [(reader.trailer, frozenset(), False, 0)]
    stack.extend((page.get("/Contents"), frozenset(), True, 0) for page in reader.pages)
    while stack:
        obj, ancestors, required, depth = stack.pop()
        steps += 1
        if steps > 100_000 or depth > 100:
            raise MaterialError()
        references = set()
        while isinstance(obj, IndirectObject):
            key = (obj.idnum, obj.generation)
            if key in references or len(references) > 100:
                raise MaterialError()
            references.add(key)
            obj = obj.get_object()
        if obj is None or isinstance(obj, NullObject):
            continue
        if required and id(obj) in ancestors:
            raise MaterialError()
        state = (id(obj), required)
        if state in visited:
            continue
        visited.add(state)
        if required:
            if isinstance(obj, StreamObject):
                streams.add(id(obj))
            elif isinstance(obj, ArrayObject):
                stack.extend((child, ancestors | {id(obj)}, True, depth + 1) for child in obj)
                continue
            else:
                raise MaterialError()
        if isinstance(obj, DictionaryObject):
            stack.extend((child, frozenset(), key in font_stream_keys, depth + 1) for key, child in obj.items())
        elif isinstance(obj, ArrayObject):
            stack.extend((child, frozenset(), False, depth + 1) for child in obj)
    return streams


def _pdf_pages(path: Path) -> tuple[DeckPage, ...]:
    import pypdf
    from pypdf import filters
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, StreamObject

    # Install the deny gate before PdfReader can decode an object stream.
    # It works with both legacy constants and the newer 6.x configuration API,
    # and blocks decoder scratch files as well as subprocess execution.
    if hasattr(filters, "JBIG2DEC_BINARY"):
        filters.JBIG2DEC_BINARY = None
    if hasattr(filters, "JBIG2Decode"):
        filters.JBIG2Decode.decode = staticmethod(_reject_pdf_external_decoder)
    # pypdf 6 supports legacy limit constants; newer 6.x exposes a scoped
    # configuration API. Both execute only in this disposable worker.
    if hasattr(pypdf, "overwrite_configuration"):
        pypdf.overwrite_configuration(
            maximum_declared_stream_length=MAX_XML_BYTES,
            array_based_stream_maximum_output_length=MAX_XML_BYTES,
            lzw_maximum_output_length=MAX_XML_BYTES,
            run_length_maximum_output_length=MAX_XML_BYTES,
            zlib_maximum_output_length=MAX_XML_BYTES,
            page_tree_maximum_entries=MAX_PAGES * 2,
            xform_maximum_invocations_per_extraction=1000,
            jbig2dec_binary=None, disable_legacy_handling=True,
        )
    else:
        import pypdf.filters
        for name in ("ZLIB_MAX_OUTPUT_LENGTH", "LZW_MAX_OUTPUT_LENGTH", "RUN_LENGTH_MAX_OUTPUT_LENGTH",
                     "MAX_DECLARED_STREAM_LENGTH"):
            if hasattr(pypdf.filters, name):
                setattr(pypdf.filters, name, MAX_XML_BYTES)
    forbidden = {"/OpenAction", "/AA", "/JS", "/JavaScript", "/Launch", "/EmbeddedFiles",
                 "/EF", "/RichMedia", "/XFA", "/URI", "/GoToR", "/SubmitForm", "/ImportData"}
    with path.open("rb") as source:
        try:
            reader = pypdf.PdfReader(source, strict=True)
        except Exception:
            raise MaterialError() from None
        if reader.is_encrypted:
            raise MaterialError()
        decoded_bytes = 0
        decoded_streams: set[int] = set()

        def account_stream(obj):
            nonlocal decoded_bytes
            if "/F" in obj:
                raise MaterialError()
            if id(obj) in decoded_streams:
                return
            codecs = obj.get("/Filter")
            codecs = [] if codecs is None else codecs if isinstance(codecs, ArrayObject) else [codecs]
            if any(item not in {"/FlateDecode", "/Fl", "/LZWDecode", "/LZW", "/ASCII85Decode", "/A85",
                                "/ASCIIHexDecode", "/AHx", "/RunLengthDecode", "/RL"} for item in codecs):
                raise MaterialError()
            data = obj.get_data()
            decoded_bytes += len(data)
            if len(data) > MAX_XML_BYTES or decoded_bytes > MAX_UNCOMPRESSED_BYTES:
                raise MaterialError()
            decoded_streams.add(id(obj))

        containers = []
        for obj in _pdf_object_streams(reader):
            # /Subtype /Image cannot exempt an object-stream container.
            account_stream(obj)
            containers.append(obj)
        if not 1 <= len(reader.pages) <= MAX_PAGES:
            raise MaterialError()
        content_streams = _pdf_content_stream_ids(reader)
        stack = [(reader.trailer, 0), *((obj, 0) for obj in containers)]
        visited: set[tuple[int, int]] = set()
        steps = 0
        while stack:
            obj, depth = stack.pop()
            steps += 1
            if steps > 100_000 or depth > 100:
                raise MaterialError()
            if isinstance(obj, IndirectObject):
                key = (obj.idnum, obj.generation)
                if key in visited:
                    continue
                visited.add(key)
                obj = obj.get_object()
            if isinstance(obj, DictionaryObject):
                if forbidden.intersection(obj) or str(obj.get("/S", "")) in forbidden:
                    raise MaterialError()
                if isinstance(obj, StreamObject):
                    if "/F" in obj:
                        raise MaterialError()
                    # Image decoding is unnecessary; avoid optional external
                    # codecs. All text/font/form streams have bounded decoding.
                    if id(obj) in content_streams or obj.get("/Subtype") != "/Image":
                        account_stream(obj)
                stack.extend((child, depth + 1) for child in obj.values())
            elif isinstance(obj, ArrayObject):
                stack.extend((child, depth + 1) for child in obj)
        pages = []
        total = 0
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            total += len(text)
            if len(text) > MAX_PAGE_TEXT or total > MAX_TOTAL_TEXT:
                raise MaterialError()
            pages.append(DeckPage(number, text))
        return tuple(pages)


def _extract_pages(path: Path) -> tuple[DeckPage, ...]:
    if not 0 < path.stat().st_size <= MAX_DOWNLOAD_BYTES:
        raise MaterialError()
    with path.open("rb") as source:
        signature = source.read(8)
    if signature.startswith(b"PK\x03\x04"):
        return _pptx_pages(path)
    if signature.startswith(b"%PDF-"):
        return _pdf_pages(path)
    raise MaterialError()


def extract_deck_pages(path: Path, *, cancellation_event: Event | None = None) -> tuple[DeckPage, ...]:
    """Extract bounded transient page text in a time/memory-limited subprocess."""
    try:
        path = Path(path).resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix="deck-pages-", dir=path.parent) as directory:
            output = Path(directory) / "pages.json"
            _run_worker("parse", str(path), output, timeout=PARSE_TIMEOUT_SECONDS,
                        cancellation_event=cancellation_event)
            if output.stat().st_size > MAX_RESULT_BYTES:
                raise MaterialError()
            records = json.loads(output.read_text(encoding="utf-8"))
            if not 1 <= len(records) <= MAX_PAGES:
                raise MaterialError()
            pages = tuple(DeckPage(record["number"], record["text"]) for record in records)
            if (any(type(p.number) is not int or p.number < 1 or not isinstance(p.text, str)
                    or len(p.text) > MAX_PAGE_TEXT for p in pages)
                    or sum(len(p.text) for p in pages) > MAX_TOTAL_TEXT):
                raise MaterialError()
            return pages
    except CancelledError:
        raise
    except Exception:
        raise MaterialError() from None


def _normalized(text: str) -> tuple[str, set[str]]:
    normalized = " ".join("".join(c if c.isalnum() else " " for c in text.lower()).split())
    return normalized, set(normalized.split())


def _margin_at_least_tenth(best: float, runner_up: float) -> bool:
    margin = best - runner_up
    # One ulp at the maximum possible score covers subtraction roundoff;
    # it does not relax the editorial threshold for real score differences.
    return margin >= .10 or math.isclose(margin, .10, rel_tol=0, abs_tol=math.ulp(1.0))


def _score_at_least_floor(score: float) -> bool:
    # Use the same one-ulp allowance as the margin, for weighted-sum roundoff.
    return score >= .55 or math.isclose(score, .55, rel_tol=0, abs_tol=math.ulp(1.0))


def _match_materials(
    slides: Sequence[SlideChange], decks: Sequence[tuple[ReportMaterial, Sequence[DeckPage]]],
    cancellation_event: Event | None = None,
) -> tuple[SlideChange, ...]:
    check_cancelled(cancellation_event)
    title_counts = Counter(material.titolo for material, _ in decks)
    candidates = [(index, material, page, *_normalized(page.text))
                  for index, (material, pages) in enumerate(decks)
                  for page in sorted(pages, key=lambda page: page.number)]
    output = [s.model_copy(update={"material_title": None, "page": None}) for s in slides]
    last_page: dict[int, int] = {}
    for slide_index in sorted(range(len(slides)), key=lambda index: (slides[index].timestamp_seconds, index)):
        check_cancelled(cancellation_event)
        slide = slides[slide_index]
        text, tokens = _normalized(" ".join([slide.title or "", *slide.visible_content]))
        if not tokens:
            continue
        scores = []
        for index, material, page, page_text, page_tokens in candidates:
            check_cancelled(cancellation_event)
            if not page_tokens:
                continue
            jaccard = len(tokens & page_tokens) / len(tokens | page_tokens)
            score = .6 * jaccard + .4 * SequenceMatcher(None, text, page_text).ratio()
            scores.append((score, index, material, page))
        scores.sort(key=lambda candidate: (-candidate[0], candidate[1], candidate[3].number))
        if not scores:
            continue
        score, index, material, page = scores[0]
        runner_up = scores[1][0] if len(scores) > 1 else 0.0
        if (_score_at_least_floor(score) and _margin_at_least_tenth(score, runner_up)
                and title_counts[material.titolo] == 1 and page.number >= last_page.get(index, 0)):
            output[slide_index] = slide.model_copy(update={"material_title": material.titolo, "page": page.number})
            last_page[index] = page.number
    check_cancelled(cancellation_event)
    return tuple(output)


def match_slides_to_material(
    slides: Sequence[SlideChange], material: ReportMaterial, pages: Sequence[DeckPage],
) -> tuple[SlideChange, ...]:
    return _match_materials(slides, [(material, pages)])


def _match_in_workspace(slides, decks, workspace, cancellation_event) -> tuple[SlideChange, ...]:
    """Bound adversarial SequenceMatcher work without changing its formula."""
    check_cancelled(cancellation_event)
    if not slides or not decks:
        return _match_materials(slides, [], cancellation_event)
    try:
        with tempfile.TemporaryDirectory(prefix="material-match-", dir=workspace) as directory:
            input_path = Path(directory) / "input.json"
            output_path = Path(directory) / "links.json"
            payload = {
                "slides": [slide.model_dump() for slide in slides],
                "decks": [{"material": material.model_dump(),
                           "pages": [{"number": p.number, "text": p.text} for p in pages]}
                          for material, pages in decks],
            }
            total = 0
            with input_path.open("xb") as target:
                for chunk in json.JSONEncoder().iterencode(payload):
                    check_cancelled(cancellation_event)
                    encoded = chunk.encode("utf-8")
                    total += len(encoded)
                    if total > MAX_RESULT_BYTES:
                        raise MaterialError()
                    target.write(encoded)
            _run_worker("match", str(input_path), output_path, timeout=MATCH_TIMEOUT_SECONDS,
                        cancellation_event=cancellation_event)
            if output_path.stat().st_size > MAX_RESULT_BYTES:
                raise MaterialError()
            links = json.loads(output_path.read_text(encoding="utf-8"))
            title_counts = Counter(material.titolo for material, _ in decks)
            valid = {(material.titolo, page.number) for material, pages in decks for page in pages
                     if title_counts[material.titolo] == 1}
            if len(links) != len(slides) or any(tuple(link) not in valid | {(None, None)} for link in links):
                raise MaterialError()
            check_cancelled(cancellation_event)
            return tuple(slide.model_copy(update={"material_title": title, "page": page})
                         for slide, (title, page) in zip(slides, links))
    except CancelledError:
        raise
    except Exception:
        # Parsing already verified these materials; only unfinished links are
        # absent. The intermediate report emits SLIDE_NON_ABBINATA for them.
        return _match_materials(slides, [], cancellation_event)


def _copy_local(source: Path, target: Path, cancellation_event: Event | None) -> None:
    # Do not follow symlinks or block on named pipes/device files after a stat race.
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as input_file:
        info = os.fstat(input_file.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_DOWNLOAD_BYTES:
            raise MaterialError()
        total = 0
        with target.open("xb") as output:
            while chunk := input_file.read(64 * 1024):
                check_cancelled(cancellation_event)
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise MaterialError()
                output.write(chunk)


class MaterialProcessor:
    """Sole candidate-to-ReportMaterial promotion gate; all failures are nonfatal."""

    def __init__(self, allowed_hosts: Sequence[str] | str = "www.assoholding.it", *,
                 fetcher: Callable | None = None, extractor: Callable | None = None):
        self.allowed_hosts = parse_material_allowed_hosts(allowed_hosts)
        self.fetcher = fetcher or fetch_deck
        self.extractor = extractor or extract_deck_pages

    def process(self, sources: Sequence[str], slides: Sequence[SlideChange], workspace: Path,
                cancellation_event: Event | None = None) -> MaterialAnalysis:
        decks = []
        failures = []
        seen: set[tuple[str, ...]] = set()
        for position, source in enumerate(sources):
            check_cancelled(cancellation_event)
            try:
                if position >= MAX_MATERIALS:
                    failures.append(_FAILURE)
                    break
                declared = resolve_material_sources([source], UUID(int=0))
                if not declared:
                    raise MaterialError()
                candidate = declared[0]
                url = None
                local = None
                if " | " in candidate:
                    title, url = candidate.split(" | ", 1)
                elif candidate.startswith(("https://", "http://")):
                    url = candidate
                    title = Path(urlsplit(url).path).name or "Materiale"
                else:
                    local = Path(candidate)
                    title = local.stem
                key = ("url", *_validate_url(url, self.allowed_hosts)) if url is not None else ("file", os.path.abspath(local))
                if key in seen:
                    continue
                seen.add(key)
                title = canonical_material_title(title)
                with tempfile.TemporaryDirectory(prefix="material-", dir=workspace) as directory:
                    material_workspace = Path(directory)
                    if url is not None:
                        _validate_url(url, self.allowed_hosts)
                        deck = self.fetcher(url, material_workspace, self.allowed_hosts,
                                            cancellation_event=cancellation_event)
                        metadata = {"url": url}
                    else:
                        deck = material_workspace / "local.deck"
                        _copy_local(local, deck, cancellation_event)
                        metadata = {"file": str(local)}
                    if not Path(deck).resolve().is_relative_to(material_workspace.resolve()):
                        raise MaterialError()
                    pages = self.extractor(deck, cancellation_event=cancellation_event)
                    check_cancelled(cancellation_event)
                    if not pages:
                        raise MaterialError()
                    material = ReportMaterial(
                        titolo=title, relatore=title.removeprefix("Slide · ") if title.startswith("Slide · ") else None,
                        pagine=len(pages), **metadata,
                    )
                    decks.append((material, pages))
            except CancelledError:
                raise
            except Exception:
                failures.append(_FAILURE)
        failures.extend(_FAILURE for count in Counter(material.titolo for material, _ in decks).values() if count > 1)
        matched = _match_in_workspace(slides, decks, workspace, cancellation_event)
        return MaterialAnalysis(tuple(material for material, _ in decks), matched, tuple(failures))


def _worker_main() -> int:
    try:
        import resource
        # Railway/Linux provides a real address-space ceiling. macOS does not
        # support RLIMIT_AS reliably; CPU + parent wall limits still apply there.
        resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_DOWNLOAD_BYTES, MAX_DOWNLOAD_BYTES))
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        mode, source, output, hosts = sys.argv[1:]
        if mode == "fetch":
            _download_https(source, Path(output), json.loads(hosts))
        elif mode == "parse":
            pages = _extract_pages(Path(source))
            with Path(output).open("x", encoding="utf-8") as result:
                json.dump([{"number": page.number, "text": page.text} for page in pages], result)
        elif mode == "match":
            if Path(source).stat().st_size > MAX_RESULT_BYTES:
                return 1
            payload = json.loads(Path(source).read_text(encoding="utf-8"))
            slides = [SlideChange.model_validate(slide) for slide in payload["slides"]]
            decks = [(ReportMaterial.model_validate(deck["material"]),
                      [DeckPage(**page) for page in deck["pages"]]) for deck in payload["decks"]]
            matched = _match_materials(slides, decks)
            with Path(output).open("x", encoding="utf-8") as result:
                json.dump([[slide.material_title, slide.page] for slide in matched], result)
        else:
            return 1
        return 0
    except BaseException:
        return 1


if __name__ == "__main__":
    raise SystemExit(_worker_main())
