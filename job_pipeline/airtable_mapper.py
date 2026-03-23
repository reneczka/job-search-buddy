from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlparse, urlunparse

from jobscraper.src.airtable_client import normalize_url as normalize_link

from .models import JobDetail

MISSING_VALUE_MARKERS = {
    "",
    "n/a",
    "na",
    "none",
    "null",
    "unknown",
    "not available",
    "not provided",
    "salary not specified",
    "brak widelek",
    "brak widełek",
    "no salary info",
    "not specified",
}
REQUIREMENT_SECTION_LABELS = {
    "soft skills",
    "technical skills",
    "optional",
    "must have",
    "nice to have",
    "wymagania",
    "wymagania obowiązkowe",
    "mile widziane",
}
COMPANY_DESCRIPTION_NOISE_PATTERNS = (
    r"utwórz konto indeed.*",
    r"aplikacja w witrynie firmy.*",
    r"opis stanowiska.*",
    r"pełny opis stanowiska.*",
    r"świadczenia na podstawie pełnego opisu stanowiska.*",
)


def to_airtable_record(detail: JobDetail) -> dict[str, str]:
    salary = _normalize_salary(detail.salary)
    requirements = _normalize_requirements(detail.requirements)
    notes = _normalize_notes(detail.notes)
    company_description = _normalize_company_description(detail.company_description)
    return {
        "Source": _value(detail.source),
        "Link": _value(detail.final_url or detail.discovered_url),
        "Company": _value(_normalize_company(detail.company)),
        "Position": _value(_normalize_position(detail.position)),
        "Salary": _value(salary),
        "Location": _value(_normalize_location(detail.location)),
        "Notes": _value(notes),
        "Requirements": _value(_requirements_value(requirements)),
        "Company description": _value(company_description),
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
    cleaned = _normalize_scalar(value)
    return cleaned if cleaned else "N/A"


def _requirements_value(values: list[str]) -> str:
    cleaned = [_normalize_scalar(value) for value in values if _normalize_scalar(value)]
    if not cleaned:
        return ""
    return "\n".join(f"- {value}" for value in cleaned)


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


def _normalize_scalar(value: str) -> str:
    cleaned = html.unescape(str(value or ""))
    cleaned = cleaned.replace("\xa0", " ")
    cleaned = re.sub(r"[ \t\r\f\v]+", " ", cleaned)
    cleaned = re.sub(r"\s*\n\s*", "\n", cleaned)
    cleaned = cleaned.strip(" ;\n\t")
    return "" if cleaned.lower() in MISSING_VALUE_MARKERS else cleaned


def _normalize_company(value: str) -> str:
    return _normalize_scalar(value)


def _normalize_position(value: str) -> str:
    return _normalize_scalar(value)


def _normalize_salary(value: str) -> str:
    cleaned = _normalize_scalar(value)
    if not cleaned:
        return ""
    if re.search(r"\bX+\b", cleaned, re.IGNORECASE):
        return ""
    if cleaned.lower() in MISSING_VALUE_MARKERS:
        return ""
    return cleaned


def _normalize_location(value: str) -> str:
    cleaned = _normalize_scalar(value)
    if not cleaned:
        return ""
    cleaned = re.sub(r"\s*;\s*", "; ", cleaned)
    cleaned = re.sub(r"\s*,\s*", ", ", cleaned)
    return cleaned.strip(" ;,")


def _normalize_requirements(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _normalize_requirement_item(value)
        if not item:
            continue
        key = _canonical_requirement(item)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(item)
    return normalized


def _normalize_requirement_item(value: str) -> str:
    cleaned = _normalize_scalar(value)
    if not cleaned:
        return ""
    cleaned = re.sub(r"^[\-\*\u2022]+\s*", "", cleaned).strip()
    cleaned = re.sub(r"^(optional|must have|nice to have|technical skills|soft skills)\s*:\s*", "", cleaned, flags=re.IGNORECASE)
    if cleaned.lower() in REQUIREMENT_SECTION_LABELS:
        return ""
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -;:")
    return cleaned


def _canonical_requirement(value: str) -> str:
    cleaned = value.lower()
    cleaned = re.sub(r"^(optional|must have|nice to have)\s*:\s*", "", cleaned)
    cleaned = re.sub(r"[^a-z0-9ąćęłńóśźż]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _normalize_notes(value: str) -> str:
    cleaned = _normalize_scalar(value)
    if not cleaned:
        return ""
    lines = [_normalize_scalar(part) for part in re.split(r"[\n\r]+", cleaned)]
    parts = [part for part in lines if part]
    compact = "; ".join(parts) if len(parts) > 1 else cleaned
    compact = re.sub(r"\s*;\s*", "; ", compact)
    compact = re.sub(r"\s{2,}", " ", compact).strip(" ;")
    return _truncate_text(compact, max_chars=420, max_sentences=3)


def _normalize_company_description(value: str) -> str:
    cleaned = _normalize_scalar(value)
    if not cleaned:
        return ""
    for pattern in COMPANY_DESCRIPTION_NOISE_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"\bO firmie\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -;")
    if not cleaned:
        return ""
    if len(cleaned.split()) < 5:
        return ""
    return _truncate_text(cleaned, max_chars=380, max_sentences=2)


def _truncate_text(value: str, max_chars: int, max_sentences: int) -> str:
    cleaned = _normalize_scalar(value)
    if not cleaned:
        return ""
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", cleaned) if part.strip()]
    if sentences:
        limited = " ".join(sentences[:max_sentences]).strip()
        if limited and len(limited) <= max_chars:
            return limited
    if len(cleaned) <= max_chars:
        return cleaned
    shortened = cleaned[:max_chars].rstrip(" ,;:-")
    last_separator = max(shortened.rfind(". "), shortened.rfind("; "), shortened.rfind(", "))
    if last_separator >= max_chars // 2:
        shortened = shortened[:last_separator].rstrip(" ,;:-")
    return shortened
