from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel

from jobscraper.src.airtable_client import AirtableClient, AirtableConfig

from .boards import board_alias
from .airtable_mapper import dedupe_airtable_records


console = Console()


def build_airtable_client() -> AirtableClient:
    return AirtableClient(AirtableConfig.from_env())


def write_airtable_records(records: list[dict[str, str]]) -> dict[str, object]:
    return _write_full_records(records, source_label="all sources")


def write_indeed_full_records(records: list[dict[str, str]]) -> dict[str, object]:
    normalized_records = _normalize_records(records, source_name="indeed")
    return _write_full_records(normalized_records, source_label=board_alias("indeed"), records_are_normalized=True)


def write_indeed_url_test_records(records: list[dict[str, str]]) -> dict[str, object]:
    minimal_records = [
        {"Source": "indeed", "Link": record["Link"]}
        for record in _normalize_records(records, source_name="indeed")
    ]
    if not minimal_records:
        raise RuntimeError("No Indeed URL records available for the Airtable write test.")

    client = build_airtable_client()
    table = client._connect()
    console.print(f"[dim]Fetching existing Airtable links for the {board_alias('indeed')} URL-only test...[/]")
    existing_records = table.all(fields=["Link"])
    existing_links = {
        _dedupe_link(str(record.get("fields", {}).get("Link") or "").strip())
        for record in existing_records
        if str(record.get("fields", {}).get("Link") or "").strip()
    }

    new_records: list[dict[str, str]] = []
    skipped = 0
    for record in minimal_records:
        link = record["Link"]
        dedupe_key = _dedupe_link(link)
        if dedupe_key in existing_links:
            skipped += 1
            console.print(f"[dim yellow]Skipping duplicate {board_alias('indeed')} URL: {link}[/]")
            continue
        existing_links.add(dedupe_key)
        new_records.append(record)

    created = table.batch_create(new_records) if new_records else []
    style = "green" if created else "yellow"
    console.print(
        Panel(
            f"{board_alias('indeed')} URL-only Airtable test created {len(created)} records and skipped {skipped} duplicates.",
            title="Airtable",
            style=style,
        )
    )
    return {
        "created": len(created),
        "skipped": skipped,
        "records": created,
    }

def _write_full_records(
    records: list[dict[str, str]],
    *,
    source_label: str,
    records_are_normalized: bool = False,
) -> dict[str, object]:
    normalized_records = records if records_are_normalized else _normalize_records(records)
    if not normalized_records:
        raise RuntimeError(f"No records available for the {source_label} Airtable write.")

    client = build_airtable_client()
    table = client._connect()
    console.print(f"[dim]Fetching existing Airtable links for the {source_label} Airtable write...[/]")
    existing_records = table.all()
    existing_by_link = {
        _dedupe_link(str(record.get("fields", {}).get("Link") or "").strip()): record
        for record in existing_records
        if str(record.get("fields", {}).get("Link") or "").strip()
    }

    creates: list[dict[str, str]] = []
    updates: list[dict[str, object]] = []
    skipped = 0

    for record in normalized_records:
        link = record["Link"]
        dedupe_key = _dedupe_link(link)
        existing = existing_by_link.get(dedupe_key)
        if existing is None:
            creates.append(record)
            continue

        changed_fields = _changed_fields(record, existing.get("fields", {}))
        if not changed_fields:
            skipped += 1
            console.print(f"[dim yellow]Skipping unchanged row: {link}[/]")
            continue

        updates.append({"id": existing["id"], "fields": changed_fields})

    created_records = table.batch_create(creates) if creates else []
    updated_records = client.batch_update_records(updates) if updates else []
    style = "green" if created_records or updated_records else "yellow"
    console.print(
        Panel(
            (
                f"{source_label} Airtable write created {len(created_records)} records, "
                f"updated {len(updated_records)} records, and skipped {skipped} unchanged rows."
            ),
            title="Airtable",
            style=style,
        )
    )
    return {
        "created": len(created_records),
        "updated": len(updated_records),
        "skipped": skipped,
        "records": created_records,
    }


def _normalize_records(records: list[dict[str, str]], source_name: str | None = None) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for record in dedupe_airtable_records(records):
        source = str(record.get("Source") or "").strip().lower()
        if source_name and source != source_name:
            continue
        link = str(record.get("Link") or "").strip()
        if not link:
            continue
        normalized_record = AirtableClient._normalize_record(dict(record))
        if source:
            normalized_record["Source"] = source
        normalized_record["Link"] = link
        normalized.append(normalized_record)
    return normalized


def _changed_fields(record: dict[str, str], existing_fields: dict[str, Any]) -> dict[str, str]:
    changed: dict[str, str] = {}
    for field, value in record.items():
        current = str(existing_fields.get(field) or "").strip()
        if current != value:
            changed[field] = value
    return changed


def _dedupe_link(link: str) -> str:
    if not link:
        return ""

    if "indeed.com/viewjob" in link and "jk=" in link:
        prefix, _, suffix = link.partition("jk=")
        job_key = suffix.split("&", 1)[0].strip()
        if job_key:
            return prefix + "jk=" + job_key
    return link.split("?", 1)[0]
