from __future__ import annotations

from rich.console import Console
from rich.panel import Panel

from jobscraper.src.airtable_client import AirtableClient, AirtableConfig

from .airtable_mapper import dedupe_airtable_records


console = Console()


def build_airtable_client() -> AirtableClient:
    return AirtableClient(AirtableConfig.from_env())


def write_airtable_records(records: list[dict[str, str]]) -> None:
    del records
    raise RuntimeError(
        "Airtable writes are intentionally disabled until the new dry-run pipeline is validated."
    )


def write_indeed_url_test_records(records: list[dict[str, str]]) -> dict[str, object]:
    minimal_records = dedupe_airtable_records(
        [
            {
                "Source": "indeed",
                "Link": str(record.get("Link") or "").strip(),
            }
            for record in records
            if str(record.get("Source") or "").strip().lower() == "indeed"
            and str(record.get("Link") or "").strip()
        ]
    )
    if not minimal_records:
        raise RuntimeError("No Indeed URL records available for the Airtable write test.")

    client = build_airtable_client()
    table = client._connect()
    console.print("[dim]Fetching existing Airtable links for the Indeed URL-only test...[/]")
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
            console.print(f"[dim yellow]Skipping duplicate Indeed URL: {link}[/]")
            continue
        existing_links.add(dedupe_key)
        new_records.append(record)

    created = table.batch_create(new_records) if new_records else []
    style = "green" if created else "yellow"
    console.print(
        Panel(
            f"Indeed URL-only Airtable test created {len(created)} records and skipped {skipped} duplicates.",
            title="Airtable",
            style=style,
        )
    )
    return {
        "created": len(created),
        "skipped": skipped,
        "records": created,
    }


def _dedupe_link(link: str) -> str:
    if not link:
        return ""

    if "indeed.com/viewjob" in link and "jk=" in link:
        prefix, _, suffix = link.partition("jk=")
        job_key = suffix.split("&", 1)[0].strip()
        if job_key:
            return prefix + "jk=" + job_key
    return link.split("?", 1)[0]
