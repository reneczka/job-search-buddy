from __future__ import annotations

from jobscraper.src.airtable_client import AirtableClient, AirtableConfig


def build_airtable_client() -> AirtableClient:
    return AirtableClient(AirtableConfig.from_env())


def write_airtable_records(records: list[dict[str, str]]) -> None:
    del records
    raise RuntimeError(
        "Airtable writes are intentionally disabled until the new dry-run pipeline is validated."
    )

