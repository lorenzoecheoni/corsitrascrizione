"""Small persisted profile-2 fixtures; no provider/network dependencies."""

from app.models import AcademyReport, BoundaryEvidence, Intervention, SpeechBlock


def governance_report():
    chapters = []
    intervals = [(0, 600), (600, 1500), (1500, 3000), (3000, 3900), (3900, 4800), (4800, 5789)]
    for index, (start, end) in enumerate(intervals):
        chapters.append(Intervention(
            id=f"saved-{6-index}", start_seconds=start, end_seconds=end,
            tipo="intervento", relatori=["Furio D ’ Andrea" if index < 3 else "Dottor Morra"],
            titolo=f"Tema {index + 1}", sintesi="Poteri, deleghe e controlli.",
            punti_chiave=["Poteri", "Deleghe", "Controlli"], confidenza=.95,
            block_id="stored-z" if index < 3 else "stored-a",
            chapter_number=index % 3 + 1, chapters_in_block=3,
            boundary_origin={
                "motivo_editoriale": "slide_e_tema" if index == 1 else "inizio_blocco" if index % 3 == 0 else "cambio_tema",
                "regola_audio": "short_pause",
                "slide_indizio_seconds": 599.2 if index == 1 else None,
            },
        ))
    return AcademyReport(
        title="Governance", bunny_title="Governance originale", duration_seconds=5789.87,
        detected_language="it", synopsis="Assetti della holding.", speakers=[],
        analysis_profile=2, audio_boundary_version=1, uncertainties=[],
        interventions=chapters,
        speech_blocks=[
            SpeechBlock(id="stored-z", start_seconds=0, end_seconds=3000, tipo="intervento",
                        relatori=["Furio D’Andrea"], titolo="Governance", sinossi="Poteri e deleghe."),
            SpeechBlock(id="stored-a", start_seconds=3000, end_seconds=5789, tipo="intervento",
                        relatori=["Luigi Morra"], titolo="Controlli", sinossi="Assetti e controlli."),
        ],
        boundaries=[BoundaryEvidence(
            previous_intervention_id=previous.id, next_intervention_id=following.id,
            boundary_seconds=int(previous.end_seconds), words_before=["si", "conclude", "qui", "il", "tema"],
            words_after=["passiamo", "ora", "al", "tema", "successivo"],
            pause_before=False, pause_after=False, rule="short_pause",
        ) for previous, following in zip(chapters, chapters[1:])],
        slides=[
            {"timestamp_seconds": 599.2, "title": "Deleghe", "visible_content": ["Poteri e deleghe"],
             "confidence": "alta", "material_title": "Slide · Furio D’Andrea", "page": 2},
            {"timestamp_seconds": 5788.9, "title": "Chiusura", "confidence": "alta"},
        ],
        materials=[{"titolo": "Slide · Furio D’Andrea", "relatore": "Avv. Furio D'Andrea",
                    "url": "https://www.assoholding.it/governance.pptx", "pagine": 18}],
        material_failures=["MATERIALE_NON_RAGGIUNGIBILE"],
        cost={"estimated_low_usd": .123456, "estimated_high_usd": .789012,
              "bunny_bandwidth_usd": .012345, "transcription_usd": .234567,
              "analysis_usd": .345678, "basis": "Costi persistiti per richiesta."},
    )


def conflicting_material_report():
    """Reviewer A/B/C case: a repeated source must not rebind a slide title."""
    report = governance_report()
    first = report.materials[0]
    report.materials = [
        first.model_copy(update={"url": "https://www.assoholding.it/X.pptx"}),
        first.model_copy(update={"titolo": "Deck B", "url": "https://www.assoholding.it/Y.pptx"}),
        first.model_copy(update={"url": "https://www.assoholding.it/Y.pptx"}),
    ]
    return report


# Syntax-only export checks must reject these without resolving any source.
UNSAFE_MATERIAL_SOURCES = [
    ("url", ""),
    ("file", ""),
    ("url", "PRIVATE_SOURCE_SENTINEL"),
    ("url", "javascript:PRIVATE_SOURCE_SENTINEL"),
    ("url", "http://www.assoholding.it/PRIVATE_SOURCE_SENTINEL.pptx"),
    ("url", "ftp://www.assoholding.it/PRIVATE_SOURCE_SENTINEL.pptx"),
    ("url", "https://user:PRIVATE_SOURCE_SENTINEL@www.assoholding.it/deck.pptx"),
    ("url", "https://www.assoholding.it/deck.pptx?token=PRIVATE_SOURCE_SENTINEL"),
    ("url", "https://www.assoholding.it/deck.pptx#PRIVATE_SOURCE_SENTINEL"),
    ("url", "https://www.assoholding.it/deck.pptx?"),
    ("url", "https://www.assoholding.it/deck.pptx#"),
    ("url", "https://[broken/PRIVATE_SOURCE_SENTINEL.pptx"),
    ("url", "https://localhost/PRIVATE_SOURCE_SENTINEL.pptx"),
    ("url", "https://127.0.0.1/PRIVATE_SOURCE_SENTINEL.pptx"),
    ("url", "https://[::1]/PRIVATE_SOURCE_SENTINEL.pptx"),
    ("url", "https://cdn.internal/PRIVATE_SOURCE_SENTINEL.pptx"),
    ("url", "https://bad_host.example/PRIVATE_SOURCE_SENTINEL.pptx"),
    ("url", "https://-bad.example/PRIVATE_SOURCE_SENTINEL.pptx"),
    ("url", "https://www..assoholding.it/PRIVATE_SOURCE_SENTINEL.pptx"),
    ("url", "https://www.assoholding.it:bad/PRIVATE_SOURCE_SENTINEL.pptx"),
    ("url", "https://www.assoholding.it:444/PRIVATE_SOURCE_SENTINEL.pptx"),
    ("url", "https://www.assoholding.it:/PRIVATE_SOURCE_SENTINEL.pptx"),
    ("url", "https://www.assoholding.it/../PRIVATE_SOURCE_SENTINEL.pptx"),
    ("url", "https://www.assoholding.it/%2e%2e/PRIVATE_SOURCE_SENTINEL.pptx"),
    ("url", "https://www.assoholding.it/%252e%252e/PRIVATE_SOURCE_SENTINEL.pptx"),
    ("url", "https://www.assoholding.it/a%2fb/PRIVATE_SOURCE_SENTINEL.pptx"),
    ("url", "https://www.assoholding.it/%5cPRIVATE_SOURCE_SENTINEL.pptx"),
    ("url", "https://www.assoholding.it/%00PRIVATE_SOURCE_SENTINEL.pptx"),
    ("url", "https://www.assoholding.it/%ZZPRIVATE_SOURCE_SENTINEL.pptx"),
    ("url", "https://www.assoholding.it//PRIVATE_SOURCE_SENTINEL.pptx"),
    ("url", "https://www.assoholding.it/PRIVATE_SOURCE_SENTINEL\ndeck.pptx"),
    ("file", "PRIVATE_SOURCE_SENTINEL"),
    ("file", "PRIVATE_SOURCE_SENTINEL.txt"),
    ("file", "../PRIVATE_SOURCE_SENTINEL.pptx"),
    ("file", "decks/../PRIVATE_SOURCE_SENTINEL.pdf"),
    ("file", "decks\\PRIVATE_SOURCE_SENTINEL.pptx"),
    ("file", "decks/%2e%2e/PRIVATE_SOURCE_SENTINEL.pptx"),
    ("file", "file:///PRIVATE_SOURCE_SENTINEL.pptx"),
    ("file", "decks/PRIVATE_SOURCE_SENTINEL.pptx?token=secret"),
    ("file", "/tmp/PRIVATE_SOURCE_SENTINEL.pptx"),
    ("file", "/private/var/folders/random/PRIVATE_SOURCE_SENTINEL.pdf"),
    ("file", "/data/bunny-video-random/PRIVATE_SOURCE_SENTINEL.pdf"),
    ("file", "workspace/PRIVATE_SOURCE_SENTINEL.pdf"),
    ("file", "material-random/PRIVATE_SOURCE_SENTINEL.pdf"),
]
