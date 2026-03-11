from __future__ import annotations

from urllib.parse import parse_qs, urlparse, urlunparse

from jobscraper.src.airtable_client import normalize_url as normalize_link

from .models import JobDetail


def to_airtable_record(detail: JobDetail) -> dict[str, str]:
    return {
        "Source": _value(detail.source),
        "Link": _value(detail.final_url or detail.discovered_url),
        "Company": _value(detail.company),
        "Position": _value(detail.position),
        "Salary": _value(detail.salary),
        "Location": _value(detail.location),
        "Notes": _value(detail.notes),
        "Requirements": _value("\n".join(detail.requirements)),
        "Company description": _value(detail.company_description),
    }


def dedupe_airtable_records(records: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for record in records:
        link = record.get("Link", "")
        normalized = _dedupe_link(link)
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(record)
    return deduped


def _value(value: str) -> str:
    cleaned = str(value or "").strip()
    return cleaned if cleaned else "N/A"


def _dedupe_link(link: str) -> str:
    normalized = normalize_link(link)
    if not link:
        return normalized

    parsed = urlparse(link)
    domain = parsed.netloc.lower().removeprefix("www.")
    if domain.endswith("indeed.com") and parsed.path.rstrip("/") == "/viewjob":
        job_keys = parse_qs(parsed.query).get("jk")
        if job_keys and job_keys[0]:
            return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", f"jk={job_keys[0]}", ""))
    return normalized
