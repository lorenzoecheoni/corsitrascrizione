import json
from pathlib import Path
import socket
import threading
import time
from uuid import UUID

import httpx
import pytest

from app import intermediate_report
from app.intermediate_models import (
    IntermediateSlideV11,
    IntermediateSpeakerV11,
    IntermediateVideoV11,
)
from app.intermediate_report import (
    build_intermediate_report,
    parse_material_source,
    registered_slug,
    split_role_organization,
)
from app.models import AcademyReport, BoundaryEvidence, Intervention, SpeakerProfile
from app.reporting import correct_speaker_name_mentions, render_markdown, render_text


TARGET_GUID = UUID("7f254c4d-fe34-4fd3-a4cf-cda4f447e438")


def report_with_boundary_evidence() -> AcademyReport:
    interventions = [
        make_intervention(0, 11, titolo="Uno"),
        make_intervention(11, 22, titolo="Due"),
        make_intervention(22, 33, titolo="Tre"),
        make_intervention(33, 44, titolo="Quattro"),
    ]
    for intervention, stored_id in zip(
        interventions, ["stored-z", "stored-a", "stored-y", "stored-b"], strict=True,
    ):
        intervention.id = stored_id
    report = make_report(interventions, duration=44)
    report.boundaries = [
        BoundaryEvidence(
            previous_intervention_id="stored-z", next_intervention_id="stored-a",
            boundary_seconds=11,
            words_before=["la", "governance", "si", "chiude", "qui"],
            words_after=["passiamo", "ora", "al", "tema", "fiscale"],
            pause_before=False, pause_after=False, rule="no_pause",
        ),
        BoundaryEvidence(
            previous_intervention_id="stored-a", next_intervention_id="stored-y",
            boundary_seconds=22, words_before=["solo", "quattro", "parole", "qui"],
            words_after=[], pause_before=True, pause_after=True, rule="short_pause",
        ),
        BoundaryEvidence(
            previous_intervention_id="stored-y", next_intervention_id="stored-b",
            boundary_seconds=33, words_before=[], words_after=[],
            pause_before=True, pause_after=True, rule="long_pause",
        ),
    ]
    return report


def test_confine_warnings_use_chronological_export_ids_and_exact_audio_evidence():
    report = report_with_boundary_evidence()

    payload = build_intermediate_report(report, TARGET_GUID).model_dump(
        mode="json", by_alias=True, exclude_none=True,
    )
    checks = [
        item for item in payload["verifiche_richieste"] if item["codice"] == "CONFINE"
    ]

    assert [item["intervento"] for item in checks] == ["v1-i002", "v1-i003", "v1-i004"]
    assert [item["campo"] for item in checks] == ["inizio", "inizio", "inizio"]
    assert "Confine 0:00:11; termina v1-i001." in checks[0]["messaggio"]
    assert "Prima: «la governance si chiude qui»." in checks[0]["messaggio"]
    assert "Dopo: «passiamo ora al tema fiscale»." in checks[0]["messaggio"]
    assert "Prima: «solo quattro parole qui» (pausa)." in checks[1]["messaggio"]
    assert "Dopo: (pausa)." in checks[1]["messaggio"]
    assert checks[2]["messaggio"].count("(pausa)") == 2
    assert payload["stato"] == "verificato"


def test_boundary_builder_rejects_incomplete_evidence_and_keeps_distinct_pairs():
    report = report_with_boundary_evidence()
    mapping = {
        intervention.id: f"v1-i{index:03d}"
        for index, intervention in enumerate(report.interventions, start=1)
    }

    checks = intermediate_report.build_boundary_verifications(report, mapping)

    assert len(checks) == 3
    assert all(item.livello == "avviso" for item in checks)
    report.boundaries.pop()
    with pytest.raises(ValueError, match="confini"):
        intermediate_report.build_boundary_verifications(report, mapping)


@pytest.mark.parametrize(("words_before", "pause_before", "words_after", "pause_after", "expected"), [
    (["meno", "di", "cinque", "parole"], True,
     ["dopo", "restano", "cinque", "parole", "esatte"], False,
     "Prima: «meno di cinque parole» (pausa). Dopo: «dopo restano cinque parole esatte»."),
    (["prima", "restano", "cinque", "parole", "esatte"], False,
     [], True,
     "Prima: «prima restano cinque parole esatte». Dopo: (pausa)."),
    (["cinque", "parole", "ma", "pausa", "prima"], True,
     ["dopo", "restano", "cinque", "parole", "esatte"], False,
     "Prima: «cinque parole ma pausa prima» (pausa). Dopo: «dopo restano cinque parole esatte»."),
    (["prima", "restano", "cinque", "parole", "esatte"], False,
     ["cinque", "parole", "ma", "pausa", "dopo"], True,
     "Prima: «prima restano cinque parole esatte». Dopo: «cinque parole ma pausa dopo» (pausa)."),
    (["cinque", "parole", "ma", "pausa", "prima"], True,
     ["cinque", "parole", "ma", "pausa", "dopo"], True,
     "Prima: «cinque parole ma pausa prima» (pausa). Dopo: «cinque parole ma pausa dopo» (pausa)."),
])
def test_confine_renders_pause_flags_instead_of_partial_or_zero_word_excerpts(
    words_before, pause_before, words_after, pause_after, expected,
):
    report = make_report([
        make_intervention(0, 10), make_intervention(10, 20),
    ], duration=20)
    report.interventions[0].id = "first"
    report.interventions[1].id = "second"
    report.boundaries = [BoundaryEvidence(
        previous_intervention_id="first", next_intervention_id="second",
        boundary_seconds=10, words_before=words_before, words_after=words_after,
        pause_before=pause_before, pause_after=pause_after, rule="short_pause",
    )]

    check = intermediate_report.build_boundary_verifications(
        report, {"first": "v1-i001", "second": "v1-i002"},
    )[0]

    assert expected in check.messaggio


@pytest.mark.parametrize("formal_names", [
    ["Marco Rossi", "Mario Rossi"], ["Marco Rossi"], [],
])
def test_explicit_similar_speaker_identities_remain_distinct_in_all_exports(formal_names):
    report = make_report([
        make_intervention(0, 120, relatori=["Marco Rossi"], sintesi="Marco Rossi tratta la governance."),
        make_intervention(120, 240, relatori=["Mario Rossi"], sintesi="Mario Rossi tratta i controlli."),
    ])
    report.speakers = [SpeakerProfile(
        id=name, display_name=name, confidence="alta",
        evidence=[{"kind": "introduzione", "note": f"Presentazione di {name}."}],
    ) for name in formal_names]
    saved = report.model_dump()

    result = build_intermediate_report(report, TARGET_GUID)

    assert [speaker.nome for speaker in result.relatori] == ["Marco Rossi", "Mario Rossi"]
    assert [item.relatori for item in result.video[0].interventi] == [["Marco Rossi"], ["Mario Rossi"]]
    for render in (render_markdown, render_text):
        text = render(report)
        assert "Mario Rossi (confidenza:" in text
        assert "Marco Rossi (confidenza:" in text
        assert "Relatori: Marco Rossi;" in text
        assert "Relatori: Mario Rossi;" in text
        assert "Marco Rossi tratta la governance." in text
        assert "Mario Rossi tratta i controlli." in text
    assert report.model_dump() == saved


def test_accent_insensitive_intervention_references_reuse_first_canonical_profile():
    report = make_report([
        make_intervention(0, 120, relatori=["Jose Nunez"]),
        make_intervention(120, 240, relatori=["JOSÉ NÚÑEZ"]),
    ])
    report.speakers = [SpeakerProfile(
        id="jose", display_name="José Núñez", confidence="alta",
        evidence=[{"kind": "slide", "note": "Nome in slide: José Núñez."}],
    )]
    saved = report.model_dump()

    result = build_intermediate_report(report, TARGET_GUID)

    assert [speaker.nome for speaker in result.relatori] == ["José Núñez"]
    assert [item.relatori for item in result.video[0].interventi] == [["José Núñez"], ["José Núñez"]]
    assert report.model_dump() == saved


@pytest.mark.parametrize("summary", [
    "Relatore Gaetano De Vito approfondisce il realizzo controllato.",
    "Relatore Marco Rossi: illustra gli assetti.",
    "Relatore José Núñez presenta i controlli.",
])
def test_provider_summary_normalization_preserves_explicit_personal_names(summary):
    report = make_report([make_intervention(0, 120, sintesi=summary)])

    assert intermediate_report.normalize_interventions(report)[0].sintesi == summary


def test_malformed_optional_material_url_is_retained_with_nonblocking_warning():
    source = "https://[::1/dispensa.pdf"
    report = make_report([make_intervention(0, 120, relatori=["Vincenzo Manfredi"])])

    result = build_intermediate_report(report, TARGET_GUID, material_sources=[source])

    assert result.video[0].materiali[0].url == source
    assert result.video[0].materiali[0].titolo == source
    assert result.stato == "verificato"
    assert [check.codice for check in result.verifiche_richieste if check.campo == f"materiali:{source}"] == [
        "MATERIALE_NON_RAGGIUNGIBILE",
    ]


@pytest.mark.parametrize("phase", ["headers", "redirects", "tls_handshake"])
def test_material_probe_cancels_slow_headers_within_five_seconds_and_closes_socket(monkeypatch, phase):
    # Route this public literal to a local deterministic server at the socket
    # boundary; the production URL validator and real HTTP transport still run.
    stopped = threading.Event()
    peer_closed = threading.Event()
    existing_threads = set(threading.enumerate())
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(.1)
    original_connect = socket.socket.connect
    original_getaddrinfo = socket.getaddrinfo

    def literal_address(host, port, *args, **kwargs):
        if host == "93.184.216.34":
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (host, port))]
        return original_getaddrinfo(host, port, *args, **kwargs)

    def local_connect(sock, address):
        if address[0] == "93.184.216.34":
            address = listener.getsockname()
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_connect)
    monkeypatch.setattr(socket, "getaddrinfo", literal_address)

    def accept_connection():
        while not stopped.is_set():
            try:
                connection, _ = listener.accept()
                connection.settimeout(.1)
                return connection
            except TimeoutError:
                continue
        return None

    def read_request(connection):
        request = b""
        while b"\r\n\r\n" not in request and not stopped.is_set():
            try:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                request += chunk
            except TimeoutError:
                continue

    def serve_slow_headers():
        if phase == "redirects":
            first = accept_connection()
            if first is None:
                return
            with first:
                read_request(first)
                if stopped.wait(2.5):
                    return
                first.sendall(b"HTTP/1.1 302 Found\r\nLocation: /next\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        connection = accept_connection()
        if connection is None:
            return
        with connection:
            if phase != "tls_handshake":
                read_request(connection)
                connection.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
            finish = time.monotonic() + 6
            while not stopped.is_set():
                try:
                    if connection.recv(1) == b"":
                        peer_closed.set()
                        return
                except TimeoutError:
                    if phase == "tls_handshake":
                        continue
                    try:
                        connection.sendall(b"x" if time.monotonic() < finish else b"\r\nContent-Length: 0\r\n\r\n")
                    except OSError:
                        peer_closed.set()
                        return
                except OSError:
                    peer_closed.set()
                    return

    server = threading.Thread(target=serve_slow_headers, daemon=True)
    server.start()
    try:
        started = time.monotonic()
        scheme = "https" if phase == "tls_handshake" else "http"
        reachable = intermediate_report._material_url_is_reachable(f"{scheme}://93.184.216.34/slow.pdf")
        elapsed = time.monotonic() - started
        assert elapsed <= 5.0, f"caller blocked for {elapsed:.3f}s"
        assert reachable is False
        assert peer_closed.wait(.5), "cancelled probe left its socket connected"
    finally:
        stopped.set()
        server.join(1)
        listener.close()
    assert not server.is_alive()
    assert set(threading.enumerate()) <= existing_threads


@pytest.mark.parametrize("label", ["Relatore A", "Relatore 12", "Speaker_01", "provider-F", "voce C2"])
def test_supported_provider_label_formats_still_normalize_to_content(label):
    report = make_report([make_intervention(0, 120, sintesi=f"{label}: approfondisce i controlli.")])

    assert intermediate_report.normalize_interventions(report)[0].sintesi == "Tema trattato: i controlli."


def test_timeline_only_normalized_names_preserve_the_first_spelling_and_known_alias():
    report = make_report([
        make_intervention(0, 120, relatori=["Jose Nunez"]),
        make_intervention(120, 240, relatori=["José Núñez", "Fulvio D’Andrea"]),
        make_intervention(240, 360, relatori=["Furio d'Andrea"]),
    ])
    report.speakers = []

    result = build_intermediate_report(report, TARGET_GUID)

    assert [speaker.nome for speaker in result.relatori] == ["Jose Nunez", "Furio d'Andrea"]
    assert [item.relatori for item in result.video[0].interventi] == [
        ["Jose Nunez"], ["Jose Nunez", "Furio d'Andrea"], ["Furio d'Andrea"],
    ]
    assert "Fulvio" not in render_markdown(report)
    assert "Fulvio" not in render_text(report)


def make_intervention(
    start: float,
    end: float,
    *,
    tipo: str = "intervento",
    titolo: str = "Approfondimento",
    sintesi: str = "Approfondimento sul tema.",
    relatori: list[str] | None = None,
    punti_chiave: list[str] | None = None,
    confidenza: float = .9,
) -> Intervention:
    return Intervention(
        id=f"source-{start}-{end}",
        start_seconds=start,
        end_seconds=end,
        tipo=tipo,
        relatori=[] if relatori is None else relatori,
        titolo=titolo,
        sintesi=sintesi,
        punti_chiave=(
            ["Primo punto", "Secondo punto", "Terzo punto"]
            if punti_chiave is None and tipo == "intervento"
            else punti_chiave or []
        ),
        confidenza=confidenza,
    )


def make_report(items: list[Intervention], *, duration: int | None = None) -> AcademyReport:
    report = report_with_reconciled_speakers()
    report.interventions = items
    report.duration_seconds = duration if duration is not None else int(items[-1].end_seconds)
    return report


def make_video(
    interventions: list[Intervention],
    *,
    duration: int | None = None,
    slide_confidences: list[float] = (),
) -> IntermediateVideoV11:
    normalized = intermediate_report.normalize_interventions(
        make_report(interventions, duration=duration)
    )
    return IntermediateVideoV11(
        chiave="v1",
        guid=str(TARGET_GUID),
        titolo_bunny="Video di prova",
        durata_secondi=duration if duration is not None else int(interventions[-1].end_seconds),
        ordine=1,
        lingua="italiano",
        sinossi="Sinossi",
        interventi=normalized,
        slide=[
            IntermediateSlideV11(
                inizio=index * 10,
                titolo="Slide",
                testo_principale="Contenuto",
                confidenza=confidence,
            )
            for index, confidence in enumerate(slide_confidences)
        ],
        materiali=[],
    )


def report_with_reconciled_speakers() -> AcademyReport:
    data = json.loads(Path("tests/fixtures/report.json").read_text())
    report = AcademyReport.model_validate(data)
    report.speakers = [
        SpeakerProfile(
            id="vincenzo",
            display_name="Vincenzo Manfredi",
            role="Presidente",
            confidence="alta",
            evidence=[{
                "kind": "introduzione",
                "timestamp_seconds": 10,
                "note": "Vincenzo Manfredi apre i lavori.",
            }],
        )
    ]
    report.interventions = [
        Intervention(
            id=f"i{index}",
            start_seconds=(index - 1) * 180,
            end_seconds=index * 180,
            tipo="intervento",
            relatori=[name],
            titolo=f"Intervento di {name}",
            sintesi=f"{name} tratta gli aspetti rilevanti.",
            punti_chiave=["Primo punto", "Secondo punto", "Terzo punto"],
            confidenza=.9,
        )
        for index, name in enumerate([
            "Vincenzo Manfredi", "Gaetano De Vito", "Furio d'Andrea",
            "Antonio Sibilia", "Luigi Morra",
        ], start=1)
    ]
    report.interventions[1].titolo = "Intervento di Fulvio D'Andrea con Gaetano De Vito"
    report.interventions[1].sintesi = "Fulvio D'Andrea e Gaetano De Vito discutono il tema."
    report.interventions[1].punti_chiave[0] = "Fulvio D'Andrea presenta il primo punto"
    report.slides[0].title = "Fulvio D'Andrea in apertura"
    report.slides[0].visible_content = ["Fulvio D'Andrea", "Agenda"]
    report.title = "Webinar con Fulvio D'Andrea"
    report.bunny_title = "Bunny: Fulvio D'Andrea"
    report.synopsis = "Fulvio D'Andrea presenta la sinossi."
    return report


def test_builder_wraps_one_video_and_reconciles_registry_speakers() -> None:
    report = report_with_reconciled_speakers()

    result = build_intermediate_report(report, TARGET_GUID)

    data = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert data["versione"] == 1
    assert data["corso"] == {
        "titolo": "Webinar con Furio d'Andrea",
        "sinossi_corso": "Furio d'Andrea presenta la sinossi.",
    }
    assert [(item["chiave"], item["ordine"]) for item in data["video"]] == [("v1", 1)]
    assert [item["nome"] for item in data["relatori"]] == [
        "Vincenzo Manfredi", "Gaetano De Vito", "Furio d'Andrea",
        "Antonio Sibilia", "Luigi Morra",
    ]
    assert {item.get("slug") for item in data["relatori"]} == {
        "vincenzo-manfredi", "gaetano-de-vito", "antonio-sibilia",
        "luigi-morra", None,
    }
    assert [item["id"] for item in data["video"][0]["interventi"]] == [
        "v1-i001", "v1-i002", "v1-i003", "v1-i004", "v1-i005",
    ]
    assert [
        (item["inizio"], item["fine"]) for item in data["video"][0]["interventi"]
    ] == [
        ("0:00:00", "0:03:00"), ("0:03:00", "0:06:00"),
        ("0:06:00", "0:09:00"), ("0:09:00", "0:12:00"),
        ("0:12:00", "0:15:00"),
    ]
    assert "Fulvio" not in json.dumps(data, ensure_ascii=False)


def test_registered_speakers_receive_fixed_slugs_and_timeline_names_are_audio_only() -> None:
    report = report_with_reconciled_speakers()

    result = build_intermediate_report(report, TARGET_GUID)
    speakers = result.model_dump(mode="json", exclude_none=True)["relatori"]

    assert registered_slug("GAETANO de Vito") == "gaetano-de-vito"
    assert registered_slug("Furio d'Andrea") is None
    assert speakers[1]["origine_nome"] == ["audio"]
    assert speakers[1]["confidenza"] == .65


def test_role_split_and_furio_registry_warning_request_missing_qualification() -> None:
    report = report_with_reconciled_speakers()
    report.interventions[1].relatori = ["Gaetano De Vito"]
    report.speakers.append(SpeakerProfile(
        id="gaetano",
        display_name="Gaetano De Vito",
        role="Public Policy and Advocacy Director di Ass Holding",
        confidence="alta",
        evidence=[{
            "kind": "slide", "timestamp_seconds": 90,
            "note": "Gaetano De Vito è presentato in slide.",
        }],
    ))

    result = build_intermediate_report(report, TARGET_GUID)
    data = result.model_dump(mode="json", exclude_none=True)
    gaetano = next(item for item in data["relatori"] if item["nome"] == "Gaetano De Vito")
    furio_warnings = [
        item for item in data["verifiche_richieste"]
        if item["codice"] == "RELATORE_NON_NEL_REGISTRO"
    ]

    assert split_role_organization("Public Policy and Advocacy Director di Ass Holding") == (
        "Public Policy and Advocacy Director", "Assoholding",
    )
    assert gaetano["ruolo"] == "Public Policy and Advocacy Director"
    assert gaetano["organizzazione"] == "Assoholding"
    assert len(furio_warnings) == 1
    assert "Furio d'Andrea" in furio_warnings[0]["messaggio"]
    assert "qualifica" in furio_warnings[0]["messaggio"].casefold()


@pytest.mark.parametrize("organization", ["Ass Holding", "Asso Holding", "Assoholding"])
def test_role_split_accepts_every_explicit_assoholding_spelling(organization: str) -> None:
    assert split_role_organization(f"Direttore di {organization}") == (
        "Direttore", "Assoholding",
    )


def test_role_split_rejects_assholding_without_the_required_space() -> None:
    assert split_role_organization("Direttore di Assholding") == (
        "Direttore di Assholding", None,
    )


def test_furio_is_canonical_when_furio_and_fulvio_are_both_candidate_names() -> None:
    report = report_with_reconciled_speakers()
    report.speakers.append(SpeakerProfile(
        id="fulvio-duplicate",
        display_name="Fulvio D'Andrea",
        confidence="alta",
        evidence=[{
            "kind": "slide", "timestamp_seconds": 90,
            "note": "Fulvio D'Andrea compare nella slide.",
        }],
    ))

    result = build_intermediate_report(report, TARGET_GUID)
    names = [item.nome for item in result.relatori]

    assert correct_speaker_name_mentions(
        "Furio d'Andrea e Fulvio D'Andrea", ["Furio d'Andrea", "Fulvio D'Andrea"]
    ) == "Furio d'Andrea e Furio d'Andrea"
    assert names.count("Furio d'Andrea") == 1
    assert "Fulvio D'Andrea" not in names


def test_builder_filters_generic_formal_speaker_profiles() -> None:
    report = report_with_reconciled_speakers()
    report.speakers.append(SpeakerProfile(
        id="generic",
        display_name="Relatore 3",
        confidence="bassa",
        evidence=[{
            "kind": "inferenza",
            "timestamp_seconds": 90,
            "note": "Voce distinta senza identità verificata.",
        }],
    ))

    result = build_intermediate_report(report, TARGET_GUID)

    assert "Relatore 3" not in [item.nome for item in result.relatori]


def test_builder_renumbers_interventions_in_chronological_stable_order() -> None:
    report = report_with_reconciled_speakers()
    first, second, third, fourth, fifth = report.interventions
    second.start_seconds = 0
    report.interventions = [fourth, second, first, third, fifth]

    result = build_intermediate_report(report, TARGET_GUID)
    interventions = result.video[0].interventi

    assert [item.id for item in interventions] == [
        "v1-i001", "v1-i002", "v1-i003", "v1-i004", "v1-i005",
    ]
    assert [item.relatori for item in interventions] == [
        ["Gaetano De Vito"], ["Vincenzo Manfredi"], ["Furio d'Andrea"],
        ["Antonio Sibilia"], ["Luigi Morra"],
    ]


@pytest.mark.parametrize(("title", "summary", "speakers", "expected"), [
    ("Ringraziamenti", "Grazie a tutti.", ["Vincenzo Manfredi"], "saluti"),
    ("Passaggio", "Passaggio della parola ad Antonio.", ["Vincenzo Manfredi"], "cambio_relatore"),
    ("Chiarimento", "Risposta sul regime fiscale.", ["Luigi Morra"], "domande"),
    ("Micro-turno", "Precisazione sul valore fiscale.", ["Luigi Morra"], "domande"),
    ("Silenzio", "Nessun parlato.", [], "pausa"),
])
def test_under_twenty_seconds_is_never_an_intervention(
    title: str, summary: str, speakers: list[str], expected: str
) -> None:
    item = make_intervention(0, 9, titolo=title, sintesi=summary, relatori=speakers)

    result = intermediate_report.normalize_interventions(make_report([item]))[0]

    assert result.tipo == expected
    assert result.punti_chiave == []


def test_non_interventions_lose_key_points_without_mutating_the_saved_report() -> None:
    item = make_intervention(
        0, 30, tipo="saluti", punti_chiave=["Dati editoriali residui"],
    )
    report = make_report([item])

    result = intermediate_report.normalize_interventions(report)[0]

    assert result.punti_chiave == []
    assert report.interventions[0].punti_chiave == ["Dati editoriali residui"]


def test_provider_initial_summary_is_rewritten_to_content_first_form() -> None:
    item = make_intervention(
        0, 30, sintesi="F approfondisce il realizzo controllato.",
    )

    result = intermediate_report.normalize_interventions(make_report([item]))[0]

    assert result.sintesi == "Tema trattato: il realizzo controllato."


@pytest.mark.parametrize("label", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
@pytest.mark.parametrize("delimiter", [": ", ". ", " - "])
def test_explicit_single_letter_provider_labels_are_rewritten_without_delimiters(
    label: str, delimiter: str,
) -> None:
    item = make_intervention(
        0, 30, sintesi=f"{label}{delimiter}approfondisce il realizzo controllato.",
    )

    result = intermediate_report.normalize_interventions(make_report([item]))[0]

    assert result.sintesi == "Tema trattato: il realizzo controllato."


@pytest.mark.parametrize("summary", [
    "A questo punto affronta il realizzo controllato.",
    "E approfondisce il realizzo controllato.",
    "A seguito della premessa tratta il realizzo controllato.",
    "E quindi approfondisce il realizzo controllato.",
])
def test_legitimate_italian_a_and_e_openings_are_not_provider_labels(summary: str) -> None:
    item = make_intervention(0, 30, sintesi=summary)

    result = intermediate_report.normalize_interventions(make_report([item]))[0]

    assert result.sintesi == summary


@pytest.mark.parametrize("seconds", [20, 119])
def test_twenty_to_one_hundred_nineteen_seconds_stays_intervention_and_warns(
    seconds: int,
) -> None:
    item = make_intervention(0, seconds, relatori=["Vincenzo Manfredi"])
    report = make_report([item])

    result = build_intermediate_report(report, TARGET_GUID)

    assert result.video[0].interventi[0].tipo == "intervento"
    assert "INTERVENTO_BREVE" in [check.codice for check in result.verifiche_richieste]


def test_one_hundred_twenty_seconds_does_not_warn_as_short_intervention() -> None:
    item = make_intervention(0, 120, relatori=["Vincenzo Manfredi"])

    result = build_intermediate_report(make_report([item]), TARGET_GUID)

    assert all(check.codice != "INTERVENTO_BREVE" for check in result.verifiche_richieste)


def test_access_selects_only_first_intervention_between_eight_and_fifteen_minutes() -> None:
    report = make_report([
        make_intervention(0, 360, relatori=["Vincenzo Manfredi"]),
        make_intervention(360, 900, relatori=["Vincenzo Manfredi"]),
        make_intervention(900, 2100, relatori=["Vincenzo Manfredi"]),
    ])

    result = build_intermediate_report(report, TARGET_GUID).video[0].interventi

    assert [item.accesso for item in result] == ["iscritti", "pubblico", "iscritti"]


def test_access_falls_back_to_first_eight_minute_intervention() -> None:
    report = make_report([
        make_intervention(0, 900, relatori=["Vincenzo Manfredi"]),
        make_intervention(900, 2100, relatori=["Vincenzo Manfredi"]),
        make_intervention(2100, 3000, relatori=["Vincenzo Manfredi"]),
    ])

    result = build_intermediate_report(report, TARGET_GUID).video[0].interventi

    assert [item.accesso for item in result] == ["pubblico", "iscritti", "iscritti"]


def test_access_falls_back_to_longest_substantive_intervention() -> None:
    report = make_report([
        make_intervention(0, 300, relatori=["Vincenzo Manfredi"]),
        make_intervention(300, 720, relatori=["Vincenzo Manfredi"]),
        make_intervention(720, 960, relatori=["Vincenzo Manfredi"]),
    ])

    result = build_intermediate_report(report, TARGET_GUID).video[0].interventi

    assert [item.accesso for item in result] == ["iscritti", "pubblico", "iscritti"]


def test_choose_public_intervention_returns_none_without_substantive_segments() -> None:
    items = intermediate_report.normalize_interventions(make_report([
        make_intervention(0, 10, tipo="saluti", punti_chiave=[]),
    ]))

    assert intermediate_report.choose_public_intervention(items) is None


def test_missing_speaker_on_spoken_segment_is_critical_verification() -> None:
    video = make_video([make_intervention(0, 30, relatori=[])])

    checks = intermediate_report.build_verifications(video, [], [])

    assert [(check.livello, check.codice, check.intervento) for check in checks] == [
        ("critico", "RELATORE_NON_IDENTIFICATO", "v1-i001"),
        ("avviso", "INTERVENTO_BREVE", "v1-i001"),
    ]


@pytest.mark.parametrize("second_start, second_end, duration", [
    (11, 20, 20),
    (9, 20, 20),
    (10, 21, 20),
])
def test_gap_overlap_or_end_beyond_duration_is_critical_timeline_verification(
    second_start: int, second_end: int, duration: int,
) -> None:
    video = make_video([
        make_intervention(0, 10, relatori=["Vincenzo Manfredi"]),
        make_intervention(second_start, second_end, relatori=["Vincenzo Manfredi"]),
    ], duration=duration)

    checks = intermediate_report.build_verifications(video, [], [])

    assert any(
        check.livello == "critico" and check.codice == "TEMPI_INCOERENTI"
        for check in checks
    )


def test_low_intervention_and_slide_confidence_each_emit_a_warning() -> None:
    video = make_video(
        [make_intervention(0, 120, relatori=["Vincenzo Manfredi"], confidenza=.79)],
        slide_confidences=[.69],
    )

    checks = intermediate_report.build_verifications(video, [], [])

    confidence_checks = [check for check in checks if check.codice == "CONFIDENZA_BASSA"]
    assert [(check.intervento, check.campo) for check in confidence_checks] == [
        ("v1-i001", "confidenza"), (None, "slide[0].confidenza"),
    ]


def test_exact_confidence_thresholds_do_not_emit_warnings() -> None:
    video = make_video(
        [make_intervention(0, 120, relatori=["Vincenzo Manfredi"], confidenza=.8)],
        slide_confidences=[.7],
    )

    checks = intermediate_report.build_verifications(video, [], [])

    assert all(check.codice != "CONFIDENZA_BASSA" for check in checks)


def test_repeated_material_detection_is_deduplicated_by_code_and_context() -> None:
    video = make_video([make_intervention(0, 120, relatori=["Vincenzo Manfredi"])])

    checks = intermediate_report.build_verifications(
        video, [], ["dispensa.pdf", "dispensa.pdf"],
    )

    material_checks = [check for check in checks if check.codice == "MATERIALE_NON_RAGGIUNGIBILE"]
    assert len(material_checks) == 1
    assert material_checks[0].campo == "materiali:dispensa.pdf"


@pytest.mark.parametrize("roles", [
    (None, None),
    ("Avvocata", "Commercialista"),
])
def test_unregistered_speakers_keep_distinct_warnings_and_deduplicate_exact_repeats(
    roles: tuple[str | None, str | None],
) -> None:
    video = make_video([make_intervention(0, 120, relatori=["Vincenzo Manfredi"])])
    furio = IntermediateSpeakerV11(
        nome="Furio d'Andrea", ruolo=roles[0], confidenza=.65, origine_nome=["audio"],
    )
    marta = IntermediateSpeakerV11(
        nome="Marta Verdi", ruolo=roles[1], confidenza=.65, origine_nome=["audio"],
    )

    checks = intermediate_report.build_verifications(video, [furio, marta, furio], [])

    registry_checks = [check for check in checks if check.codice == "RELATORE_NON_NEL_REGISTRO"]
    assert len(registry_checks) == 2
    assert {check.campo for check in registry_checks} == {
        "relatori:furio d andrea:ruolo" if roles[0] is None else "relatori:furio d andrea:nome",
        "relatori:marta verdi:ruolo" if roles[1] is None else "relatori:marta verdi:nome",
    }


def test_critical_checks_set_report_status_after_verifications_are_deduplicated() -> None:
    report = make_report([make_intervention(0, 30, relatori=[])])

    result = build_intermediate_report(report, TARGET_GUID)

    assert result.stato == "da_verificare"


def test_warnings_without_critical_checks_leave_report_verified() -> None:
    report = make_report([
        make_intervention(0, 30, relatori=["Vincenzo Manfredi"], confidenza=.79),
    ])

    result = build_intermediate_report(report, TARGET_GUID)

    assert result.stato == "verificato"


def test_parse_material_source_extracts_only_a_declared_url_and_title() -> None:
    material = parse_material_source(
        "Slide governance | https://example.test/governance.pdf"
    )

    assert material is not None
    assert material.model_dump(exclude_none=True) == {
        "titolo": "Slide governance",
        "url": "https://example.test/governance.pdf",
        "accesso": "iscritti",
    }


def test_file_material_is_retained_and_warned_without_network_access() -> None:
    report = make_report([make_intervention(0, 120, relatori=["Vincenzo Manfredi"])])

    result = build_intermediate_report(report, TARGET_GUID, material_sources=["dispensa.pdf"])

    assert result.video[0].materiali[0].model_dump(exclude_none=True) == {
        "titolo": "dispensa",
        "file": "dispensa.pdf",
        "accesso": "iscritti",
    }
    assert any(
        check.codice == "MATERIALE_NON_RAGGIUNGIBILE"
        for check in result.verifiche_richieste
    )


def test_unreachable_url_checker_warns_but_does_not_block_report_creation() -> None:
    report = make_report([make_intervention(0, 120, relatori=["Vincenzo Manfredi"])])

    result = build_intermediate_report(
        report,
        TARGET_GUID,
        material_sources=["Slide | https://example.test/unavailable.pdf"],
        material_url_checker=lambda url: False,
    )

    assert result.stato == "verificato"
    assert any(
        check.codice == "MATERIALE_NON_RAGGIUNGIBILE"
        for check in result.verifiche_richieste
    )


def test_empty_material_sources_leave_materials_and_slide_links_empty() -> None:
    report = make_report([make_intervention(0, 120, relatori=["Vincenzo Manfredi"])])

    result = build_intermediate_report(report, TARGET_GUID)
    data = result.model_dump(mode="json", exclude_none=True)

    assert data["video"][0]["materiali"] == []
    assert all("materiale" not in slide and "pagina" not in slide for slide in data["video"][0]["slide"])


def test_parse_material_source_strips_trailing_prose_punctuation_and_whitespace() -> None:
    material = parse_material_source(
        " \n\t https://example.test/materiali/dispensa.pdf).,| \n"
    )

    assert material is not None
    assert material.model_dump(exclude_none=True) == {
        "titolo": "dispensa.pdf",
        "url": "https://example.test/materiali/dispensa.pdf",
        "accesso": "iscritti",
    }


def test_parse_material_source_strips_mixed_title_url_separators() -> None:
    material = parse_material_source(
        "Slide | - \t https://example.test/materiali/dispensa.pdf"
    )

    assert material is not None
    assert material.titolo == "Slide"


def test_material_url_checker_rejects_private_direct_targets_before_any_request(monkeypatch) -> None:
    monkeypatch.setattr(
        intermediate_report.httpx,
        "AsyncClient",
        lambda **kwargs: pytest.fail("private URL must not create an HTTP client"),
    )

    assert intermediate_report._material_url_is_reachable("http://127.0.0.1/private") is False


def test_material_url_checker_rejects_private_dns_answers_before_any_request(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("10.0.0.12", 80)),
        ],
    )
    monkeypatch.setattr(
        intermediate_report.httpx,
        "AsyncClient",
        lambda **kwargs: pytest.fail("private DNS answer must not create an HTTP client"),
    )

    assert intermediate_report._material_url_is_reachable("https://materials.example.test/doc.pdf") is False


def test_material_url_checker_rejects_credentials_before_any_request(monkeypatch) -> None:
    monkeypatch.setattr(
        intermediate_report.httpx,
        "AsyncClient",
        lambda **kwargs: pytest.fail("credential URL must not create an HTTP client"),
    )

    assert intermediate_report._material_url_is_reachable(
        "https://user:password@materials.example.test/doc.pdf"
    ) is False


def test_material_url_checker_rejects_private_redirect_without_fetching_it(monkeypatch) -> None:
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "http://127.0.0.1/private"},
            request=request,
    )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(intermediate_report.httpx, "AsyncClient", lambda **kwargs: client)

    assert intermediate_report._material_url_is_reachable("http://93.184.216.34/doc.pdf") is False
    assert requests == ["http://93.184.216.34/doc.pdf"]


def test_material_url_checker_enforces_one_wall_clock_deadline_across_redirects(monkeypatch) -> None:
    now = [0.0]
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        now[0] += 3.0
        if len(requests) == 1:
            return httpx.Response(302, headers={"location": "/next"}, request=request)
        return httpx.Response(200, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(intermediate_report.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(intermediate_report.httpx, "AsyncClient", lambda **kwargs: client)

    assert intermediate_report._material_url_is_reachable("http://93.184.216.34/start") is False
    assert requests == [
        "http://93.184.216.34/start",
        "http://93.184.216.34/next",
    ]


def test_material_url_checker_fails_closed_for_rebinding_hostname_without_request(monkeypatch) -> None:
    resolutions: list[str] = []

    def rebinding_resolver(host: str, *args, **kwargs):
        resolutions.append(host)
        address = "93.184.216.34" if len(resolutions) == 1 else "10.0.0.12"
        return [(2, 1, 6, "", (address, 443))]

    monkeypatch.setattr(socket, "getaddrinfo", rebinding_resolver)
    monkeypatch.setattr(
        intermediate_report.httpx,
        "AsyncClient",
        lambda **kwargs: pytest.fail("hostname must not be resolved or requested"),
    )

    assert intermediate_report._material_url_is_reachable(
        "https://materials.example.test/document.pdf"
    ) is False
    assert resolutions == []


def test_material_url_checker_does_not_wait_for_a_blocking_hostname_resolver(monkeypatch) -> None:
    now = [0.0]
    resolutions: list[str] = []

    def blocking_resolver(host: str, *args, **kwargs):
        resolutions.append(host)
        now[0] += 10.0
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", blocking_resolver)
    monkeypatch.setattr(intermediate_report.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        intermediate_report.httpx,
        "AsyncClient",
        lambda **kwargs: pytest.fail("hostname must not start an HTTP request"),
    )

    assert intermediate_report._material_url_is_reachable(
        "https://materials.example.test/document.pdf"
    ) is False
    assert resolutions == []
    assert now[0] < 5.0


def test_material_url_checker_uses_the_validated_public_literal_ip(monkeypatch) -> None:
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(intermediate_report.httpx, "AsyncClient", lambda **kwargs: client)

    assert intermediate_report._material_url_is_reachable(
        "http://93.184.216.34/document.pdf"
    ) is True
    assert requests == ["http://93.184.216.34/document.pdf"]
