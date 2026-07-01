from __future__ import annotations

from .models import BoardConfig
from .stagehand_session import env_str


JUSTJOIN_POC_URL = (
    "https://justjoin.it/job-offers/all-locations/python?experience-level=junior&orderBy=DESC&sortBy=newest"
)
PROTOCOL_POC_URL = "https://theprotocol.it/filtry/python;t/trainee,assistant,junior;p?sort=date"
NOFLUFF_POC_URL = "https://nofluffjobs.com/pl/Python?lang=en&criteria=seniority%3Dtrainee,junior&sort=newest"
BULLDOG_POC_URL = (
    "https://bulldogjob.pl/companies/jobs/s/skills,Python/experienceLevel,intern,junior/order,published,desc"
)
PRACUJ_POC_URL = "https://it.pracuj.pl/praca?et=1%2C3%2C17&sc=0&itth=37"
INDEED_HOME_URL = "https://pl.indeed.com"

BOARD_NAME_ALIASES = {
    "justjoin": "JJ",
    "pracuj": "PR",
    "nofluffjobs": "NF",
    "bulldogjob": "BD",
    "theprotocol": "TP",
    "indeed": "IN",
}

BOARD_NAME_BY_ALIAS = {alias.lower(): canonical for canonical, alias in BOARD_NAME_ALIASES.items()}


def all_boards() -> list[BoardConfig]:
    return [
        BoardConfig(name="justjoin", url=JUSTJOIN_POC_URL),
        BoardConfig(name="theprotocol", url=PROTOCOL_POC_URL),
        BoardConfig(name="nofluffjobs", url=NOFLUFF_POC_URL),
        BoardConfig(name="bulldogjob", url=env_str("STAGEHAND_POC_URL", BULLDOG_POC_URL)),
        BoardConfig(name="pracuj", url=PRACUJ_POC_URL, discovery_mode="pracuj"),
        BoardConfig(name="indeed", url=INDEED_HOME_URL, discovery_mode="indeed"),
    ]


def supported_site_names() -> list[str]:
    return [board.name for board in all_boards()]


def supported_site_aliases() -> list[str]:
    return [BOARD_NAME_ALIASES[board.name] for board in all_boards()]


def supported_site_choices() -> list[str]:
    choices: list[str] = []
    for value in [*supported_site_names(), *supported_site_aliases()]:
        if value not in choices:
            choices.append(value)
    return choices


def normalize_site_name(site: str) -> str:
    normalized = (site or "").strip().lower()
    if not normalized:
        return ""
    if normalized in BOARD_NAME_ALIASES:
        return normalized
    return BOARD_NAME_BY_ALIAS.get(normalized, normalized)


def board_alias(site: str) -> str:
    canonical = normalize_site_name(site)
    if not canonical:
        return ""
    return BOARD_NAME_ALIASES.get(canonical, canonical.upper())


def selected_boards(site: str) -> list[BoardConfig]:
    boards = all_boards()
    normalized_site = normalize_site_name(site)
    if normalized_site == "all":
        return boards
    return [board for board in boards if board.name == normalized_site]
