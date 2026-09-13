from datetime import datetime, timezone
import json
from uuid import UUID

import httpx
import pytest

from app.bunny import BunnyCatalogVideo
from app.inventory import (
    DEFAULT_INVENTORY_TABS,
    InventoryClient,
    InventoryCourse,
    InventoryWriteUnavailable,
    normalize_title,
    propose_matches,
)


SHEET_ID = "1yw2K-1cH5goQER1tP3KItvluXMv7N_H5RoratcWZoZo"


@pytest.fixture
def csv_by_gid():
    return {
        "0": (
            'webinar,Relatore,Slide,Link\n'
            'Webinar: Governance delle holding,"Mario Rossi\nAnna Bianchi",Dispensa,bunny\n'
            ',,,\n'
            'Fiscalità dei gruppi,Luca Verdi,Slide 2024,'
            'https://iframe.mediadelivery.net/embed/748068/cbf23d46-d210-4716-809e-e2c1dbb3f4f1\n'
        ),
        "996207322": (
            'Modulo Master,Docente 1,Docente 2,Materiali,Link\n'
            'Passaggio generazionale,Giulia Blu,Paolo Neri,Workbook,\n'
        ),
        "1719623483": (
            'Corso,Relatori,materiale didattico,video\n'
            'Trust e tutela patrimoniale,"Anna Bianchi; Luca Verdi",Slide,bunny\n'
        ),
    }


def make_client(csv_by_gid):
    def handle(request: httpx.Request):
        gid = request.url.params["gid"]
        return httpx.Response(200, text=csv_by_gid[gid], request=request)

    return InventoryClient(
        SHEET_ID,
        DEFAULT_INVENTORY_TABS,
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    )


def test_fetch_reads_real_header_variants_and_preserves_sheet_row_order(csv_by_gid):
    client = make_client(csv_by_gid)
    try:
        courses = client.fetch()
    finally:
        client.close()

    assert [(item.foglio, item.riga, item.titolo) for item in courses] == [
        ("Formazione", 2, "Webinar: Governance delle holding"),
        ("Formazione", 4, "Fiscalità dei gruppi"),
        ("Corsi premium - Master", 2, "Passaggio generazionale"),
        ("Corsi Premium", 2, "Trust e tutela patrimoniale"),
    ]
    assert courses[0].relatori_attesi == ["Mario Rossi", "Anna Bianchi"]
    assert courses[0].link == "bunny"
    assert courses[0].guid_esplicito is None
    assert courses[1].guid_esplicito == UUID("cbf23d46-d210-4716-809e-e2c1dbb3f4f1")
    assert courses[2].relatori_attesi == ["Giulia Blu", "Paolo Neri"]
    assert courses[3].relatori_attesi == ["Anna Bianchi", "Luca Verdi"]
    assert courses[0].colonna_link == "D"
    assert courses[2].colonna_link == "E"


def test_fetch_failure_is_safe_and_does_not_expose_upstream_body():
    def handle(request: httpx.Request):
        return httpx.Response(503, text="PRIVATE GOOGLE DIAGNOSTIC", request=request)

    client = InventoryClient(
        SHEET_ID,
        DEFAULT_INVENTORY_TABS,
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    try:
        with pytest.raises(Exception) as caught:
            client.fetch()
    finally:
        client.close()

    assert "PRIVATE GOOGLE DIAGNOSTIC" not in str(caught.value)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (" Webinar:  La fiscalità delle holding ", "fiscalita delle holding"),
        ("MODULO 03 - Trust & Patrimonio", "trust patrimonio"),
        ("Corso — Operazioni straordinarie", "operazioni straordinarie"),
    ],
)
def test_title_normalization_folds_prefixes_accents_and_punctuation(source, expected):
    assert normalize_title(source) == expected


def inventory_course(title, *, row=2, guid=None):
    return InventoryCourse(
        id=f"0:{row}", foglio="Formazione", gid="0", posizione_foglio=0,
        riga=row, titolo=title, relatori_attesi=[], materiali=[], link=None,
        colonna_link="D", guid_esplicito=guid,
    )


def bunny_video(video_id, title):
    return BunnyCatalogVideo(
        video_id=UUID(video_id), title=title, duration_seconds=3600,
        uploaded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_matching_prefers_explicit_guid_then_unique_high_confidence_title():
    explicit_id = UUID("00000000-0000-0000-0000-000000000001")
    videos = [
        bunny_video(str(explicit_id), "Titolo molto diverso"),
        bunny_video("00000000-0000-0000-0000-000000000002", "Governance delle holding"),
    ]
    courses = [
        inventory_course("Altro titolo", guid=explicit_id),
        inventory_course("Webinar: Governance delle holding", row=3),
    ]

    matches = propose_matches(courses, videos, threshold=.88, margin=.08)

    assert [(match.course_id, match.video_id, match.reason) for match in matches] == [
        ("0:2", explicit_id, "guid_esplicito"),
        ("0:3", UUID("00000000-0000-0000-0000-000000000002"), "titolo_univoco"),
    ]


def test_matching_rejects_ambiguous_or_low_confidence_titles():
    videos = [
        bunny_video("00000000-0000-0000-0000-000000000001", "Holding di famiglia parte 1"),
        bunny_video("00000000-0000-0000-0000-000000000002", "Holding di famiglia parte 2"),
        bunny_video("00000000-0000-0000-0000-000000000003", "Tema non correlato"),
    ]

    matches = propose_matches(
        [inventory_course("Holding di famiglia"), inventory_course("Fiscalità", row=3)],
        videos,
        threshold=.70,
        margin=.10,
    )

    assert matches == []


def test_sheet_write_requires_optional_credentials(csv_by_gid):
    client = make_client(csv_by_gid)
    course = client.fetch()[0]
    try:
        with pytest.raises(InventoryWriteUnavailable):
            client.update_link(
                course,
                "https://iframe.mediadelivery.net/embed/748068/00000000-0000-0000-0000-000000000001",
            )
    finally:
        client.close()


def test_confirmed_sheet_write_targets_only_the_exact_link_cell(csv_by_gid):
    writes = []

    def handle(request: httpx.Request):
        if request.method == "GET":
            return httpx.Response(200, text=csv_by_gid[request.url.params["gid"]], request=request)
        writes.append(request)
        return httpx.Response(200, json={"updatedCells": 1}, request=request)

    client = InventoryClient(
        SHEET_ID,
        DEFAULT_INVENTORY_TABS,
        token_provider=lambda: "test-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    url = "https://iframe.mediadelivery.net/embed/748068/00000000-0000-0000-0000-000000000001"
    try:
        course = client.fetch()[0]
        client.update_link(course, url)
    finally:
        client.close()

    assert len(writes) == 1
    assert "%27Formazione%27%21D2" in str(writes[0].url)
    assert writes[0].headers["authorization"] == "Bearer test-token"
    assert json.loads(writes[0].content)["values"] == [[url]]
