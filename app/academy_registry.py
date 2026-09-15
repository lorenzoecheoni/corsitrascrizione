"""Canonical Academy people and conservative speaker-name parsing."""

from dataclasses import dataclass
import re
import unicodedata
from typing import Sequence


_HONORIFIC = re.compile(
    r"^\s*(?P<title>avvocato|avvocata|avv\.?|dottoressa|dottore|dottor|dott\.?|"
    r"professoressa|professore|prof\.?)\s+",
    re.IGNORECASE,
)


def person_key(value: str) -> str:
    """Return the accent-, punctuation- and spacing-insensitive identity key."""
    folded = "".join(
        character for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[a-z]+", folded))


@dataclass(frozen=True)
class RegistryPerson:
    nome: str
    slug: str

    @property
    def surname_key(self) -> str:
        return " ".join(person_key(self.nome).split()[1:])

    @property
    def identity_keys(self) -> frozenset[str]:
        aliases = _ALIASES_BY_SLUG.get(self.slug, ())
        return frozenset(person_key(name) for name in (self.nome, *aliases))


@dataclass(frozen=True)
class ParsedSpeakerIdentity:
    name: str
    honorific: str | None


REGISTRY_PEOPLE = (
    RegistryPerson("Vincenzo Manfredi", "vincenzo-manfredi"),
    RegistryPerson("Gaetano De Vito", "gaetano-de-vito"),
    RegistryPerson("Furio D’Andrea", "furio-dandrea"),
    RegistryPerson("Antonio Sibilia", "antonio-sibilia"),
    RegistryPerson("Luigi Morra", "luigi-morra"),
)

_ALIASES_BY_SLUG = {
    "furio-dandrea": ("Fulvio D’Andrea",),
}


def parse_speaker_identity(value: str) -> ParsedSpeakerIdentity:
    """Separate one explicitly written honorific from the identity."""
    normalized = " ".join(value.split())
    match = _HONORIFIC.match(normalized)
    if match is None:
        return ParsedSpeakerIdentity(normalized, None)
    return ParsedSpeakerIdentity(
        normalized[match.end():].strip(),
        match.group("title").strip(),
    )


def find_registry_person(
    value: str,
    people: Sequence[RegistryPerson] | None = None,
) -> RegistryPerson | None:
    """Resolve only a full canonical name or an explicit Registry alias."""
    people = REGISTRY_PEOPLE if people is None else people
    key = person_key(value)
    return next((person for person in people if key in person.identity_keys), None)


def registered_slug(value: str) -> str | None:
    """Return the fixed slug for a full Registry identity or explicit alias."""
    parsed = parse_speaker_identity(value)
    person = find_registry_person(parsed.name)
    return person.slug if person is not None else None


__all__ = [
    "ParsedSpeakerIdentity", "REGISTRY_PEOPLE", "RegistryPerson",
    "find_registry_person", "parse_speaker_identity", "person_key", "registered_slug",
]
