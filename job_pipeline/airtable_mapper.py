from __future__ import annotations

from html import unescape
import re
from urllib.parse import parse_qs, urlparse, urlunparse

from jobscraper.src.airtable_client import normalize_url as normalize_link

from .models import JobDetail


def to_airtable_record(detail: JobDetail) -> dict[str, str]:
    return _clean_record(
        {
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
    )


def validate_airtable_record(record: dict[str, str]) -> list[str]:
    issues: list[str] = []
    link = str(record.get("Link") or "").strip()
    company = str(record.get("Company") or "").strip()
    position = str(record.get("Position") or "").strip()

    if _looks_like_root_or_locale_page(link):
        issues.append("link looks like a root or locale page")
    if company == "N/A" or _is_suspicious_company_value(company):
        issues.append("company looks like a UI label or placeholder")
    if position == "N/A":
        issues.append("position is missing")
    return issues


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


def _clean_record(record: dict[str, str]) -> dict[str, str]:
    raw_notes = record.get("Notes", "")
    raw_description = record.get("Company description", "")
    raw_requirements = record.get("Requirements", "")
    cleaned = dict(record)
    cleaned["Company"] = _clean_company(cleaned.get("Company", ""))
    cleaned["Position"] = _normalize_missing(cleaned.get("Position", ""))
    cleaned["Salary"] = _clean_salary(cleaned.get("Salary", ""))
    cleaned["Location"] = _clean_location(cleaned.get("Location", ""))
    cleaned["Notes"] = _clean_listing_summary(cleaned.get("Notes", ""))
    cleaned["Requirements"] = _clean_requirements(cleaned.get("Requirements", ""))
    cleaned["Company description"] = _clean_company_description(cleaned.get("Company description", ""))
    cleaned = _enrich_record(
        cleaned,
        raw_notes=raw_notes,
        raw_description=raw_description,
        raw_requirements=raw_requirements,
    )
    return cleaned


def _clean_company(value: str) -> str:
    cleaned = _normalize_missing(value)
    if cleaned == "N/A":
        return cleaned

    prefixes = ("Firma:", "Company:")
    suffixes = ("About the company", "O firmie")

    for prefix in prefixes:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip(" :-")
    for suffix in suffixes:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip(" -")

    if _is_suspicious_company_value(cleaned):
        return "N/A"
    return _normalize_missing(cleaned)


def _clean_listing_summary(value: str) -> str:
    cleaned = _normalize_free_text(value)
    if cleaned == "N/A":
        return cleaned

    cleaned = _strip_leading_city_prefix(cleaned)
    cleaned = _strip_formulaic_offer_lead(cleaned)
    cleaned = _strip_leading_cta_questions(cleaned)
    cleaned = _strip_trailing_listing_location(cleaned)
    cleaned = _strip_labeled_prefix_blocks(cleaned)
    cleaned = _strip_leading_section_labels(
        cleaned,
        (
            "company description",
            "job description",
            "what you will do",
            "why should you join us?",
            "co będziesz robić",
            "kim jesteśmy?",
            "nasze zasady",
            "who we are",
        ),
    )
    cleaned = _strip_inline_section_labels(
        cleaned,
        (
            "company description",
            "job description",
            "what you will do",
            "why should you join us?",
            "co będziesz robić",
            "kim jesteśmy?",
            "nasze zasady",
            "who we are",
        ),
    )
    cleaned = cleaned.strip(" -:")
    if not cleaned:
        return "N/A"

    lowered = cleaned.lower()
    boilerplate_snippets = (
        "the first map of the labor market in the it sector",
        "we want to simplify the search process to minimum",
    )
    junk_markers = (
        "zaloguj się, aby lepiej dopasować oferty",
        "dodaj ogłoszenie",
        "praca it",
        "superoferta",
        "kreator cv",
        "kalkulator wynagrodzeń",
        "kalkulator godzinowy",
        "kalkulator vat",
        "kalkulator urlopu",
        "kalkulator podróży służbowej",
        "kalkulator ekwiwalentu",
        "you can start asap",
        "na podstawie pełnego opisu stanowiska",
        "przejdź od razu do głównej zawartości",
        "opinie o pracodawcach",
        "przeglądaj wynagrodzenia",
        "początek treści głównej",
        "czego szukasz?",
        "wygenerowane przez ai",
        "na bazie pełnej treści ogłoszenia",
        "lab duration:",
        "dates:",
        "format:",
        "time commitment:",
        "compensation:",
        "do wiadomości prosimy dołączyć",
        "prosimy dołączyć rozwiązanie",
    )
    requirementish_prefixes = (
        "znajomości ",
        "praktycznej znajomości",
        "umiejętności ",
        "doświadczenia ",
        "otwartości ",
        "swobody ",
    )
    if lowered.startswith("praca:"):
        candidate = cleaned.split(":", 1)[1].strip()
        if " poszukuje " in candidate.lower() and len(candidate) >= 24:
            cleaned = candidate
            lowered = cleaned.lower()
        else:
            return "N/A"
    if (
        lowered.startswith("oferta pracy ")
        or lowered.startswith("praca ")
        or lowered.startswith("offer:")
    ):
        return "N/A"
    if _looks_like_compact_meta_blurb(cleaned):
        return "N/A"
    if _contains_job_meta_chrome(cleaned):
        return "N/A"
    if lowered.startswith(requirementish_prefixes):
        return "N/A"
    if _looks_like_cta_question(cleaned):
        return "N/A"
    if _looks_like_culture_or_perks_blurb(cleaned):
        return "N/A"
    if any(snippet in lowered for snippet in boilerplate_snippets):
        return "N/A"
    if any(marker in lowered for marker in junk_markers):
        return "N/A"
    return cleaned


def _clean_requirements(value: str) -> str:
    cleaned = _normalize_missing(value)
    if cleaned == "N/A":
        return cleaned

    cleaned = _strip_leading_requirement_meta(cleaned)
    cleaned = _normalize_requirement_labels(cleaned)
    cleaned = _strip_leading_section_labels(
        cleaned,
        (
            "requirements",
            "wymagania",
            "must have",
            "kogo szukamy?",
            "kogo szukamy",
            "what we look for",
            "here’s what you will need",
            "here's what you will need",
            "qualifications",
        ),
    )
    lowered = cleaned.lower()
    if lowered.startswith("lokalizacje:") or lowered.startswith("utworzona w:"):
        return "N/A"
    cleaned = _trim_after_markers(
        cleaned,
        (
            "compensation and benefits",
            "base pay grade",
            "what we offer",
            "what you'll do",
            "what you will do",
            "responsibilities",
            "your responsibilities",
            "co będziesz robić",
            "czym będziesz się zajmować",
            "bonus points if you have",
            "nice to have",
            "praca z nami to",
            "oferujemy",
            "benefits",
            "flexible:",
            "w czym możesz nam pomóc",
            "w zamian oferujemy",
            "spodziewaj się",
            "co możemy ci zaoferować",
            "czekamy na twoją aplikację",
            "jak wygląda proces rekrutacji",
            "parleto tworzą ludzie",
            "dodatkowymi atutami będą",
            "dodatkowe atuty",
            "you will work on",
            "the work:",
            "the work",
            "lokalizacje:",
            "locations:",
            "oferta ważna do",
            "rekrutacja online",
            "język rekrutacji",
            "kontrakt tymczasowy",
            "praca zdalna:",
            "komputer:",
            "agile management",
            "valid for",
            "hybrydowo",
        ),
    )
    cleaned = cleaned.strip(" -:")
    cleaned = re.sub(r"^[?✓•·]+\s*", "", cleaned)
    if not cleaned:
        return "N/A"

    items = _split_requirement_items(cleaned)
    if items:
        return "\n".join(items[:8])
    if _looks_like_requirement_meta_blob(cleaned):
        keyword_items = _extract_requirement_keywords(cleaned)
        if keyword_items:
            return "\n".join(keyword_items[:8])
    cleaned = _clean_requirement_item(cleaned)
    if not cleaned:
        return "N/A"
    if not _looks_like_requirement_item(cleaned):
        return "N/A"
    return cleaned


def _clean_company_description(value: str) -> str:
    cleaned = _normalize_free_text(value)
    if cleaned == "N/A":
        return cleaned

    cleaned = _strip_leading_city_prefix(cleaned)
    cleaned = _strip_formulaic_offer_lead(cleaned)
    cleaned = _strip_leading_cta_questions(cleaned)
    cleaned = _normalize_leading_description_fragment(cleaned)
    cleaned = _drop_leading_metadata_segments(cleaned)
    if _clean_listing_summary(cleaned) == "N/A":
        return "N/A"
    cleaned = _strip_labeled_prefix_blocks(cleaned)
    cleaned = _strip_leading_section_labels(
        cleaned,
        (
            "company description",
            "job description",
            "what you will do",
            "why should you join us?",
            "co będziesz robić",
            "kim jesteśmy?",
            "who we are",
            "nasze zasady",
        ),
    )
    cleaned = _strip_inline_section_labels(
        cleaned,
        (
            "company description",
            "job description",
            "what you will do",
            "why should you join us?",
            "co będziesz robić",
            "kim jesteśmy?",
            "who we are",
            "nasze zasady",
        ),
    )
    lowered = cleaned.lower()
    if lowered.startswith("lokalizacje:") or lowered.startswith("utworzona w:"):
        return "N/A"
    if lowered.startswith("nr ref.:") or lowered.startswith("od kandydatów na to stanowisko oczekujemy"):
        return "N/A"
    if _contains_job_meta_chrome(cleaned):
        return "N/A"

    cleaned = re.sub(r"^(job description|what you will do|co będziesz robić|why should you join us\?)\s*[:\-]?\s*", "", cleaned, flags=re.I)
    cleaned = _trim_after_markers(
        cleaned,
        (
            "the work:",
            "the work",
            "jak możesz nam pomóc",
            "kim jesteśmy?",
            "kim jesteśmy",
            "stack :",
            "stack:",
            "czym będziesz się zajmować",
            "kategoria:",
            "lokalizacje:",
            "oferta ważna do",
            "requirements",
            "wymagania",
            "kogo szukamy",
            "what we look for",
            "here’s what you will need",
            "here's what you will need",
            "what do you need to succeed",
            "poszukiwane cechy i umiejętności",
            "must have",
            "qualifications",
            "preferred qualifications",
            "additional information",
            "what we offer",
            "what you can expect from us",
            "oferujemy",
            "co możemy ci zaoferować",
            "benefits",
            "our culture",
            "jak wygląda proces rekrutacji",
            "czekamy na twoją aplikację",
            "parleto tworzą ludzie",
        ),
    )
    cleaned = cleaned.strip(" -:")
    cleaned = _trim_incomplete_trailing_fragment(cleaned)
    if len(cleaned) < 24:
        return "N/A"
    if len(cleaned) > 900:
        sentence_parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", cleaned) if part.strip()]
        shortened = ""
        for part in sentence_parts:
            candidate = f"{shortened} {part}".strip()
            if len(candidate) > 900:
                break
            shortened = candidate
        cleaned = shortened or cleaned[:900].strip()
    return cleaned


def _clean_location(value: str) -> str:
    cleaned = _normalize_missing(value)
    if cleaned == "N/A":
        return cleaned

    parts = [part.strip() for part in cleaned.split(",") if part.strip()]
    deduped_parts: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if part in {"-", "–"}:
            continue
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped_parts.append(part)
    return ", ".join(deduped_parts) if deduped_parts else "N/A"


def _clean_salary(value: str) -> str:
    cleaned = _normalize_missing(value)
    if cleaned == "N/A":
        return cleaned

    cleaned = unescape(cleaned).replace("\xa0", " ").replace("\n", " ")
    cleaned = cleaned.replace("zł", "PLN")
    cleaned = re.sub(r"(?<=\d)(PLN|EUR|USD)\b", r" \1", cleaned)
    cleaned = re.sub(r"\bza miesiąc\b", "/ month", cleaned, flags=re.I)
    cleaned = re.sub(r"\bza godzinę\b", "/ hour", cleaned, flags=re.I)
    cleaned = re.sub(r"/\s*mies\.?\b", "/ month", cleaned, flags=re.I)
    cleaned = re.sub(r"/\s*godz\.?\b", "/ hour", cleaned, flags=re.I)
    cleaned = re.sub(r"/\s*rok\b", "/ year", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(miesiąc|mies\.)\b", "month", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\s*/\s*", " / ", cleaned)
    cleaned = cleaned.replace(" / Month", " / month").replace(" / Hour", " / hour")
    extracted = _extract_salary_from_text(cleaned)
    if extracted:
        cleaned = extracted
    return cleaned or "N/A"


def _normalize_missing(value: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned or cleaned.upper() == "N/A":
        return "N/A"
    return cleaned


def _enrich_record(
    record: dict[str, str],
    raw_notes: str = "",
    raw_description: str = "",
    raw_requirements: str = "",
) -> dict[str, str]:
    enriched = dict(record)
    company = enriched.get("Company", "N/A")
    position = enriched.get("Position", "N/A")
    notes = enriched.get("Notes", "N/A")
    description = enriched.get("Company description", "N/A")
    salary = enriched.get("Salary", "N/A")
    location = enriched.get("Location", "N/A")
    source_notes = _normalize_free_text(raw_notes)
    source_description = _normalize_free_text(raw_description)
    source_requirements = str(raw_requirements or "").strip()

    if salary == "N/A":
        fallback_salary = _extract_salary_from_text(source_notes, source_description, notes, description)
        if fallback_salary:
            enriched["Salary"] = _clean_salary(fallback_salary)
    elif _should_reextract_salary(salary):
        fallback_salary = _extract_salary_from_text(salary, source_notes, source_description, notes, description)
        if fallback_salary:
            enriched["Salary"] = _clean_salary(fallback_salary)

    if location == "N/A":
        fallback_location = _extract_location_from_text(source_notes, source_description, notes, description)
        if fallback_location:
            enriched["Location"] = _clean_location(fallback_location)

    fallback_requirements = _extract_requirements_from_text(
        source_description,
        source_notes,
        source_requirements,
        description,
        notes,
    )
    if fallback_requirements and (
        enriched.get("Requirements", "N/A") == "N/A"
        or _requirements_look_task_heavy(enriched.get("Requirements", "N/A"))
        or _requirements_need_restructure(enriched.get("Requirements", "N/A"))
    ):
        enriched["Requirements"] = _clean_requirements(fallback_requirements)

    if notes == "N/A" or _looks_like_listing_summary(notes):
        fallback_notes = _extract_summary_from_text(
            source_description,
            source_notes,
            source_requirements,
            description,
        )
        if fallback_notes:
            enriched["Notes"] = _clean_listing_summary(fallback_notes)

    requirements_summary = _extract_summary_from_requirement_text(source_requirements)
    if requirements_summary and (
        enriched.get("Notes", "N/A") == "N/A"
        or _looks_like_title_prefixed_summary(enriched.get("Notes", "N/A"), position)
        or _looks_like_titleish_summary(enriched.get("Notes", "N/A"), position, company)
    ):
        enriched["Notes"] = _clean_listing_summary(requirements_summary)

    if enriched.get("Company description", "N/A") == "N/A":
        requirements_description = _extract_description_from_requirement_text(source_requirements)
        if requirements_description:
            enriched["Company description"] = _clean_company_description(requirements_description)

    if enriched.get("Company description", "N/A") != "N/A":
        enriched["Company description"] = _clean_company_description(enriched["Company description"])

    if _same_meaning_text(description, position):
        enriched["Company description"] = "N/A"
    if _same_meaning_text(notes, position) or _same_meaning_text(notes, enriched.get("Company description", "N/A")):
        enriched["Notes"] = "N/A"

    enriched["Notes"] = _clean_listing_summary(enriched.get("Notes", "N/A"))
    if enriched.get("Company description", "N/A") != "N/A":
        enriched["Company description"] = _clean_company_description(enriched["Company description"])
    if position != "N/A":
        enriched["Notes"] = _strip_position_echo(enriched.get("Notes", "N/A"), position)
        enriched["Company description"] = _strip_position_echo(enriched.get("Company description", "N/A"), position)
    if _should_promote_description_to_notes(enriched["Notes"]):
        description_summary = _summarize_text(enriched.get("Company description", ""))
        current_notes = _normalize_free_text(enriched.get("Notes", "N/A"))
        if (
            description_summary
            and not _same_meaning_text(description_summary, position)
            and not _looks_like_titleish_summary(description_summary, position, company)
            and (
                current_notes == "N/A"
                or len(description_summary) > len(current_notes)
                or current_notes.endswith("...")
                or current_notes.endswith("…")
            )
        ):
            enriched["Notes"] = _clean_listing_summary(description_summary)
    enriched["Requirements"] = _clean_requirements(enriched.get("Requirements", "N/A"))
    enriched["Requirements"] = _prefer_keyword_requirements(enriched["Requirements"], source_requirements)
    enriched["Requirements"] = _enrich_sparse_requirements(
        enriched["Requirements"],
        source_notes,
        source_description,
        source_requirements,
        notes,
        description,
    )
    return enriched


def _is_suspicious_company_value(value: str) -> bool:
    lowered = str(value or "").strip().lower()
    if not lowered or lowered == "n/a":
        return True

    suspicious_values = {
        "obowiązkowe",
        "requirements",
        "must have",
        "nice to have",
        "lokalizacje",
        "locations",
    }
    return lowered in suspicious_values


def _looks_like_root_or_locale_page(link: str) -> bool:
    parsed = urlparse(link)
    if not parsed.scheme or not parsed.netloc:
        return True

    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        return True
    if len(segments) == 1 and len(segments[0]) <= 3 and not parsed.query:
        return True
    return False


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


def _extract_salary_from_text(*values: str) -> str:
    pattern = re.compile(
        r"(?<!\d)\d[\d\s.,]{2,}(?:\s*[-–]\s*\d[\d\s.,]{2,})?\s*(?:PLN|zł|EUR|USD)\b(?:\s*(?:/|za)\s*(?:miesiąc|month|mies\.|hour|godzinę|godz\.|year|rok))?",
        re.I,
    )
    for value in values:
        match = pattern.search(str(value or ""))
        if match:
            return match.group(0).strip()
    return ""


def _extract_location_from_text(*values: str) -> str:
    patterns = (
        re.compile(r"-\s*(.*?)\s*,\s*technologie", re.I),
        re.compile(r"\b(?:location|lokalizacja|miejsce pracy)\s*[:\-]\s*([^|.;]+)", re.I),
    )
    for value in values:
        source = _normalize_missing(value)
        if source == "N/A":
            continue
        for pattern in patterns:
            match = pattern.search(source)
            if match:
                return match.group(1).strip()
    return ""


def _same_meaning_text(left: str, right: str) -> bool:
    normalize = lambda value: re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    return bool(left and right and normalize(left) == normalize(right))


def _strip_position_echo(value: str, position: str) -> str:
    cleaned = _normalize_free_text(value)
    title = _normalize_free_text(position)
    if cleaned == "N/A" or title == "N/A":
        return cleaned
    if _same_meaning_text(cleaned, title):
        return "N/A"

    title_pattern = re.escape(title)
    cleaned = re.sub(rf"[\s,;:!\-–|]*{title_pattern}\s*$", "", cleaned, flags=re.I).strip(" ,;:!-–|")
    return cleaned if cleaned and not _same_meaning_text(cleaned, title) else "N/A"


def _extract_requirements_from_text(*values: str) -> str:
    start_markers = (
        "requirements",
        "wymagania",
        "what we look for",
        "here’s what you will need",
        "here's what you will need",
        "what do you need to succeed",
        "poszukiwane cechy i umiejętności",
        "must have",
        "kogo szukamy",
    )
    end_markers = (
        "compensation and benefits",
        "what we offer",
        "what you'll do",
        "what you will do",
        "responsibilities",
        "your responsibilities",
        "co możemy ci zaoferować",
        "czym będziesz się zajmować",
        "jak wygląda proces rekrutacji",
        "how does the recruitment process look like",
        "what you’ll get",
        "what you'll get",
        "dodatkowe atuty",
        "bonus points if you have",
        "nice to have",
        "praca z nami to",
        "benefits",
        "flexible:",
        "w czym możesz nam pomóc",
        "w zamian oferujemy",
        "spodziewaj się",
        "czekamy na twoją aplikację",
        "parleto tworzą ludzie",
        "location",
        "lokalizacja",
    )

    for value in values:
        source = _normalize_missing(value)
        if source == "N/A":
            continue
        section = _slice_section(source, start_markers, end_markers)
        if not section:
            continue
        items = _split_requirement_items(section)
        if items:
            return "\n".join(items[:8])
    fallback_keywords = _extract_requirement_keywords(" ".join(str(value or "") for value in values))
    if fallback_keywords:
        return "\n".join(fallback_keywords[:8])
    return ""


def _strip_leading_requirement_meta(source: str) -> str:
    cleaned = _normalize_free_text(source)
    if cleaned == "N/A":
        return cleaned

    lowered = cleaned.lower()
    meta_markers = (
        "powrót do wyszukiwania",
        "kategoria:",
        "lokalizacje:",
        "oferta ważna do",
        "valid for",
        "rekrutacja online",
        "język rekrutacji",
        "start ",
        "kontrakt tymczasowy",
        "praca zdalna:",
        "komputer:",
        "agile management",
        "hybrydowo",
    )
    meaningful_markers = (
        "experience with",
        "experience in",
        "strong knowledge",
        "strong understanding",
        "knowledge of",
        "understanding of",
        "python",
        "django",
        "sql",
        "javascript",
        "node.js",
        "nlp",
        "pandas",
        "tableau",
        "powerbi",
        "quicksight",
        "looker",
        "angielski",
        "english",
        "znajomość",
        "doświadczenie",
        "umiejętność",
        "umiejętności",
        "wiedza",
        "minimum ",
        "bachelor",
    )

    earliest_meaningful = len(cleaned)
    found_meaningful = False
    for marker in meaningful_markers:
        index = lowered.find(marker)
        if index == -1:
            continue
        earliest_meaningful = min(earliest_meaningful, index)
        found_meaningful = True

    if not found_meaningful:
        return cleaned
    if any(marker in lowered[:earliest_meaningful] for marker in meta_markers):
        return cleaned[earliest_meaningful:].strip(" -:,")
    return cleaned


def _should_reextract_salary(value: str) -> bool:
    lowered = value.lower()
    return "kontr" in lowered or bool(re.search(r"\b\d\b\s+\d{1,3}\s+\d{3}\b", value))


def _extract_summary_from_text(*values: str) -> str:
    bad_prefixes = (
        "lab duration:",
        "requirements",
        "wymagania",
        "what we offer",
        "bonus points",
        "nice to have",
        "lokalizacja",
        "location",
        "dates:",
        "date:",
        "format:",
        "time commitment:",
        "compensation:",
        "wynagrodzenie:",
    )
    for value in values:
        source = _drop_leading_metadata_segments(_normalize_free_text(value))
        if source == "N/A":
            continue
        segments = _split_summary_segments(source)
        for segment in segments:
            lowered = segment.lower()
            if any(lowered.startswith(prefix) for prefix in bad_prefixes):
                continue
            if len(segment) < 30:
                continue
            return segment[:320].strip()
    return ""


def _slice_section(source: str, start_markers: tuple[str, ...], end_markers: tuple[str, ...]) -> str:
    lowered = source.lower()
    start_index = -1
    start_marker = ""
    for marker in start_markers:
        index = lowered.find(marker)
        if index == -1:
            continue
        if start_index == -1 or index < start_index:
            start_index = index
            start_marker = marker
    if start_index == -1:
        return ""

    section = source[start_index + len(start_marker) :].strip(" :-\n")
    lowered_section = section.lower()
    end_index = len(section)
    for marker in end_markers:
        index = lowered_section.find(marker)
        if index != -1:
            end_index = min(end_index, index)
    return section[:end_index].strip()


def _split_requirement_items(section: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", section).strip()
    if not cleaned:
        return []

    cleaned = _normalize_requirement_boundaries(cleaned)
    cleaned = cleaned.replace("e.g.", "eg").replace("i.e.", "ie").replace("etc.", "etc")
    cleaned = re.sub(r"\s*[✓•·]\s*", "\n", cleaned)
    parts = [part.strip(" -:") for part in re.split(r"\n+|(?<=[.!?])\s+", cleaned) if part.strip()]
    if len(parts) <= 1 and "," in cleaned:
        parts = [part.strip(" -:") for part in cleaned.split(",") if part.strip()]
    parts = _merge_requirement_fragments(parts)

    items: list[str] = []
    seen: set[str] = set()
    for part in parts:
        part = _clean_requirement_item(part)
        if not part:
            continue
        if len(part) < 4:
            continue
        lowered = part.lower()
        if lowered in {"requirements", "wymagania", "must have"}:
            continue
        if _contains_job_meta_chrome(part):
            continue
        if any(
            marker in lowered
            for marker in (
                "compensation and benefits",
                "what we offer",
                "bonus points",
                "nice to have",
                "praca z nami to",
                "benefits",
                "flexible:",
                "what you will do",
                "responsibilities",
                "w czym możesz nam pomóc",
                "ważna jeszcze",
                "valid for",
                "contract of employment",
                "umowa o pracę",
                "umowa o staż",
                "pełny etat",
                "praca stacjonarna",
                "rekrutacja zdalna",
                "atrakcyjne wynagrodzenie",
                "praca hybrydowa",
            )
        ):
            continue
        if not _looks_like_requirement_item(part):
            continue
        key = lowered
        if key in seen:
            continue
        seen.add(key)
        items.append(part)
    return items


def _normalize_free_text(value: str) -> str:
    cleaned = _normalize_missing(value)
    if cleaned == "N/A":
        return cleaned

    cleaned = unescape(cleaned).replace("\xa0", " ")
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"(?<=[.!?])(?=[A-ZŁŚŻŹĆĄĘÓŃ])", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "N/A"


def _looks_like_listing_summary(value: str) -> bool:
    lowered = _normalize_free_text(value).lower()
    if lowered == "n/a":
        return True
    return "technologie:" in lowered or lowered.startswith("praca ") or lowered.startswith("oferta pracy ")


def _split_summary_segments(source: str) -> list[str]:
    prepared = re.sub(r"[⏳📆📍💼💰🎓]", "|", source)
    prepared = re.sub(r"\s*\|\s*", "|", prepared)
    segments = [part.strip(" -:") for part in re.split(r"\||(?<=[.!?])\s+", prepared) if part.strip()]
    return segments


def _first_sentence(source: str) -> str:
    cleaned = _normalize_free_text(source)
    if cleaned == "N/A":
        return ""
    parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", cleaned) if part.strip()]
    for part in parts:
        if 30 <= len(part) <= 320:
            return part
    return cleaned[:320].strip()


def _summarize_text(source: str, *, max_sentences: int = 2, max_len: int = 320) -> str:
    cleaned = _normalize_free_text(source)
    if cleaned == "N/A":
        return ""
    parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", cleaned) if part.strip()]
    picked: list[str] = []
    for part in parts:
        candidate = " ".join([*picked, part]).strip()
        if len(candidate) > max_len:
            break
        picked.append(part)
        if len(picked) >= max_sentences:
            break
    return " ".join(picked).strip() or cleaned[:max_len].strip()


def _should_promote_description_to_notes(value: str) -> bool:
    cleaned = _normalize_free_text(value)
    if cleaned == "N/A":
        return True
    if len(cleaned) < 30:
        return True
    if cleaned.endswith("...") or cleaned.endswith("…"):
        return True
    if len(cleaned) > 140 and not re.search(r"[.!?…]$", cleaned):
        return True
    return False


def _looks_like_titleish_summary(value: str, position: str, company: str) -> bool:
    cleaned = _normalize_free_text(value)
    if cleaned == "N/A":
        return False
    word_count = len(cleaned.split())
    if word_count > 12:
        return False
    normalized = re.sub(r"[^a-z0-9]+", " ", cleaned.lower()).strip()
    position_norm = re.sub(r"[^a-z0-9]+", " ", str(position or "").lower()).strip()
    company_norm = re.sub(r"[^a-z0-9]+", " ", str(company or "").lower()).strip()
    return bool(position_norm and company_norm and position_norm in normalized and company_norm in normalized)


def _looks_like_title_prefixed_summary(value: str, position: str) -> bool:
    cleaned = _normalize_free_text(value)
    title = _normalize_free_text(position)
    if cleaned == "N/A" or title == "N/A":
        return False
    cleaned_norm = re.sub(r"[^a-z0-9]+", " ", cleaned.lower()).strip()
    title_norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    if not title_norm:
        return False
    return cleaned_norm.startswith(title_norm[: max(18, len(title_norm) // 2)])


def _normalize_requirement_boundaries(source: str) -> str:
    cleaned = source
    starters = (
        "Frontend",
        "Backend",
        "Stack",
        "Bachelor",
        "Degree",
        "1 year of experience",
        "2 years of experience",
        "3 years of experience",
        "Experience with",
        "Experience in",
        "Located in",
        "Availability",
        "Experience",
        "Interest in",
        "Knowledge of",
        "Understanding of",
        "Basic ",
        "English proficiency",
        "Willingness to",
        "Familiarity with",
        "Ability to",
        "Strong ",
        "Solid ",
        "Proficiency in",
        "Hands-on experience",
        "Exposure to",
        "Good knowledge of",
        "SQL skills",
        "You have ",
        "You are ",
        "You are comfortable ",
        "Your experience ",
        "Znasz ",
        "Stale ",
        "Wytrwale ",
        "Masz ",
        "Lubisz ",
        "Posiadasz ",
        "Wykształcenie ",
        "Znajomość ",
        "Znajomość dowolnego ",
        "Wiedza ",
        "Umiejętność ",
        "Umiejętności ",
        "Mentalność ",
        "Biegła ",
        "Ciekawość ",
        "Komunikatywność",
        "Gotowość do",
        "Dostępność ",
        "Dobra znajomość ",
        "Doświadczenie w",
        "Polski ",
        "Mile widziana",
        "Ops:",
        "Frontend współpraca:",
        "Backend:",
        "Praktycznej znajomości ",
        "Praktyczna znajomość ",
        "Znajomości ",
        "Znajomości języka ",
        "Znajomość ",
        "Umiejętności ",
        "Umiejętność ",
        "Otwartości ",
        "Swoboda ",
    )
    for starter in starters:
        cleaned = re.sub(rf"(?<!^)\s*(?={re.escape(starter)})", "\n", cleaned, flags=re.I)
        cleaned = re.sub(rf",(?={re.escape(starter)})", ",\n", cleaned, flags=re.I)
    cleaned = re.sub(r"(?<!^)\s*(?=Minimum\s+\d)", "\n", cleaned)
    cleaned = re.sub(r"(?<!^)\s+(?=(?:Polski|Angielski)\s*\()", "\n", cleaned)
    return cleaned


def _looks_like_requirement_item(value: str) -> bool:
    lowered = re.sub(r"\s+", " ", value.lower()).strip()
    if not lowered:
        return False
    if _contains_job_meta_chrome(value):
        return False
    if _looks_like_culture_or_perks_blurb(value):
        return False
    if re.match(r"^(frontend|backend|full[\s-]?stack|mobile|devops|data)\s+(write|build|develop|design|maintain|support)\b", lowered):
        return False

    responsibility_starters = (
        "a chance for ",
        "the opportunity to ",
        "and neither should",
        "to nie jest ",
        "elastyczne godziny ",
        "dołącz do ",
        "you will ",
        "you'll ",
        "contribute to ",
        "creating ",
        "researching ",
        "preparing ",
        "designing ",
        "supporting ",
        "acting as",
        "developing ",
        "implementing ",
        "analyzing ",
        "collaborating ",
        "conducting ",
        "participating ",
        "work with ",
        "working with ",
        "rozwój ",
        "praca z ",
        "zarządzanie ",
        "administracja ",
        "utrzymanie ",
        "współpraca ",
        "tworzenie ",
        "opracowywanie ",
        "budowanie ",
        "wdrażanie ",
        "implementacja ",
        "testowanie ",
    )
    if lowered.startswith(responsibility_starters):
        return False

    requirement_markers = (
        "degree",
        "bachelor",
        "student",
        "experience",
        "years of",
        "year of",
        "knowledge of",
        "knowledge ",
        "familiarity with",
        "familiar with",
        "understanding of",
        "ability to",
        "ability ",
        "proficiency in",
        "proficiency ",
        "strong ",
        "solid ",
        "basic ",
        "english",
        "polish",
        "availability",
        "located in",
        "willingness to",
        "must",
        "should have",
        "active student status",
        "education",
        "znajomość",
        "doświadczenie",
        "wykształcenie",
        "umiejętność",
        "umiejętności",
        "wiedza",
        "gotowość do",
        "komunikatywność",
        "biegła znajomość",
        "język angielski",
        "języka angielskiego",
        "angielski",
        "dyspozycyjność",
        "dostępność",
        "aktywny status studenta",
        "mile widziana",
        "minimum ",
        "min. ",
        "min ",
    )
    if any(marker in lowered for marker in requirement_markers):
        return True

    technology_pattern = re.compile(
        r"\b("
        r"python|java(script)?|typescript|kotlin|scala|sql|django|fastapi|flask|drf|react|angular|vue|node(\.js)?|"
        r"aws|azure|gcp|docker|kubernetes|terraform|ansible|git|gitlab|github|postgres(ql)?|mysql|dynamodb|"
        r"rest api|graphql|linux|uipath|oracle apex|power platform|html|css|json|xml|api|nosql|spark|airflow|"
        r"powerbi|tableau|quicksight|looker(?:\s+studio)?|pandas|matplotlib|seaborn|etl/?elt|statistics|"
        r"pytorch|tensorflow|langchain|llamaindex|hugging face|rag|llm|nlp|spacy|nltk|guidewire|gosu"
        r")\b",
        re.I,
    )
    return bool(technology_pattern.search(value))


def _clean_requirement_item(value: str) -> str:
    cleaned = value.strip(" -:")
    if not cleaned:
        return ""

    cleaned = _trim_after_markers(
        cleaned,
        (
            "dołącz do naszego zespołu",
            "join our team",
            "apply now",
            "czekamy na twoją aplikację",
            "what we offer",
            "benefits",
        ),
    ).strip(" -:")
    if not cleaned:
        return ""

    lowered = cleaned.lower()
    if "internship program" in lowered or "(evergreen)" in lowered or "(open)" in lowered:
        return ""
    if lowered.startswith(("'fullstackowa", "fullstackowa", "dodatkowymi atutami", "dodatkowe atuty")):
        return ""
    if re.match(r"^(frontend|backend|full[\s-]?stack|mobile|devops|data)\s+(write|build|develop|design|maintain|support)\b", lowered):
        return ""
    return cleaned


def _merge_requirement_fragments(parts: list[str]) -> list[str]:
    merged: list[str] = []
    for part in parts:
        cleaned = part.strip()
        if not cleaned:
            continue
        if merged:
            previous = merged[-1]
            if re.search(r"(?:\band\b|\beg\b|\(eg)$", previous, re.I):
                merged[-1] = f"{previous} {cleaned}".strip()
                continue
        if merged and re.match(r"^\d+\s*(h|hours?|lat|years?)\b", cleaned, re.I):
            previous = merged[-1].lower()
            if previous.endswith("min.") or previous.endswith("minimum") or previous.endswith("availability"):
                merged[-1] = f"{merged[-1]} {cleaned}".strip()
                continue
        merged.append(cleaned)
    return merged


def _strip_labeled_prefix_blocks(source: str) -> str:
    cleaned = source.strip()
    label_pattern = re.compile(
        r"^\s*(?:lokalizacja|location|wymiar pracy|job type|employment type|świadczenia|benefits|pełny opis stanowiska)\s*[:\-]?\s*",
        re.I,
    )
    next_label_pattern = re.compile(
        r"\b(?:lokalizacja|location|wymiar pracy|job type|employment type|świadczenia|benefits|pełny opis stanowiska)\b\s*[:\-]?",
        re.I,
    )

    while True:
        label_match = label_pattern.match(cleaned)
        if not label_match:
            return cleaned
        cleaned = cleaned[label_match.end() :].lstrip()
        next_label_match = next_label_pattern.search(cleaned)
        sentence_match = re.search(r"(?<=[.!?])\s+", cleaned)
        if sentence_match and (not next_label_match or sentence_match.start() < next_label_match.start()):
            cleaned = cleaned[sentence_match.end() :].lstrip()
            continue
        if next_label_match:
            cleaned = cleaned[next_label_match.end() :].lstrip()
            continue
        return ""


def _strip_leading_section_labels(source: str, labels: tuple[str, ...]) -> str:
    cleaned = source.strip()
    for _ in range(4):
        updated = cleaned
        for label in labels:
            updated = re.sub(rf"^\s*{re.escape(label)}\s*[:\-]?\s*", "", updated, flags=re.I)
        if updated == cleaned:
            break
        cleaned = updated.strip()
    return cleaned


def _strip_leading_city_prefix(source: str) -> str:
    cleaned = source.strip()
    if not cleaned:
        return cleaned
    city_prefixes = (
        "warszawa",
        "warsaw",
        "kraków",
        "krakow",
        "gdańsk",
        "gdansk",
        "wrocław",
        "wroclaw",
        "poznań",
        "poznan",
        "łódź",
        "lodz",
        "katowice",
        "opole",
    )
    lowered = cleaned.lower()
    for city in city_prefixes:
        prefix = f"{city} "
        if lowered.startswith(prefix):
            return cleaned[len(prefix) :].strip()
    return cleaned


def _strip_formulaic_offer_lead(source: str) -> str:
    cleaned = source.strip()
    if not cleaned:
        return cleaned

    patterns = (
        r"^Oferta dotyczy stanowiska\s+.+?\s+w firmie\s+.+?,\s*",
        r"^Oferta dotyczy stanowiska\s+.+?\.\s*",
    )
    for pattern in patterns:
        updated = re.sub(pattern, "", cleaned, flags=re.I)
        if updated != cleaned:
            return updated.strip()
    return cleaned


def _strip_leading_cta_questions(source: str) -> str:
    cleaned = source.strip()
    if not cleaned:
        return cleaned

    parts = [part.strip() for part in re.split(r"(?<=[!?])\s+", cleaned) if part.strip()]
    if len(parts) < 2:
        return cleaned

    dropped = 0
    while parts and _looks_like_cta_lead_segment(parts[0]):
        parts.pop(0)
        dropped += 1

    if dropped == 0:
        return cleaned

    candidate = " ".join(parts).strip()
    return candidate if len(candidate) >= 24 else cleaned


def _strip_trailing_listing_location(source: str) -> str:
    cleaned = source.strip()
    if not cleaned:
        return cleaned
    return re.sub(r"\s*Praca\s+[A-ZŁŚŻŹĆĄĘÓŃ][A-Za-zŁŚŻŹĆĄĘÓŃąćęłńóśźż\-\s]+\.?$", "", cleaned).strip()


def _normalize_leading_description_fragment(source: str) -> str:
    cleaned = source.strip()
    if not cleaned:
        return cleaned
    lowered = cleaned.lower()
    if lowered.startswith("odpowiedzialnego za "):
        return f"Rola odpowiedzialna za {cleaned[len('odpowiedzialnego za '):]}".strip()
    return cleaned


def _strip_inline_section_labels(source: str, labels: tuple[str, ...]) -> str:
    cleaned = source
    for label in labels:
        cleaned = re.sub(rf"\b{re.escape(label)}\s*[:\-]?\s*", " ", cleaned, flags=re.I)
    return re.sub(r"\s+", " ", cleaned).strip()


def _looks_like_compact_meta_blurb(value: str) -> bool:
    cleaned = str(value or "").strip()
    if "|" not in cleaned:
        return False
    if not re.search(r"\b(PLN|zł|EUR|USD)\b", cleaned, re.I):
        return False
    lowered = cleaned.lower()
    return any(token in lowered for token in ("warsz", "krak", "gda", "katow", "wroc", "łód", "lodz", "remote", "hybrid"))


def _trim_after_markers(source: str, markers: tuple[str, ...]) -> str:
    lowered = source.lower()
    end_index = len(source)
    for marker in markers:
        index = lowered.find(marker)
        if index != -1:
            end_index = min(end_index, index)
    return source[:end_index].strip()


def _normalize_requirement_labels(source: str) -> str:
    cleaned = str(source or "")
    replacements = (
        ("Frontend współpraca:", "Frontend:"),
        ("Frontend wspolpraca:", "Frontend:"),
        ("Ops:", "\nOps:"),
        ("Backend:", "\nBackend:"),
        ("Frontend:", "\nFrontend:"),
    )
    for old, new in replacements:
        cleaned = cleaned.replace(old, new)
    return cleaned.strip()


def _trim_incomplete_trailing_fragment(source: str) -> str:
    cleaned = source.strip()
    if not cleaned:
        return cleaned
    if re.search(r'[.!?]["”)]?\s*$', cleaned):
        return cleaned

    sentence_endings = list(re.finditer(r'[.!?]["”)]?(?=\s|$)', cleaned))
    if sentence_endings:
        last_complete = cleaned[: sentence_endings[-1].end()].strip()
        if len(last_complete) >= 24:
            return last_complete
    return cleaned


def _drop_leading_metadata_segments(source: str) -> str:
    bad_prefixes = (
        "lab duration:",
        "duration:",
        "dates:",
        "format:",
        "time commitment:",
        "compensation:",
        "location:",
        "lokalizacja:",
        "wynagrodzenie:",
    )
    segments = _split_summary_segments(source)
    if not segments:
        return source
    start = 0
    for index, segment in enumerate(segments):
        lowered = segment.lower()
        if any(lowered.startswith(prefix) for prefix in bad_prefixes):
            continue
        start = index
        break
    return " ".join(segments[start:]).strip() or source


def _contains_job_meta_chrome(value: str) -> bool:
    lowered = _normalize_free_text(value).lower()
    if lowered == "n/a":
        return False
    chrome_markers = (
        "about the company",
        "o firmie",
        "valid for",
        "ważna jeszcze",
        "zapisz",
        "asystent pracuj.pl",
        "sprawdź, jak dobrze ta oferta do ciebie pasuje",
        "podsumowanie oferty",
        "contract of employment",
        "full-time",
        "pełny etat",
        "praca stacjonarna",
        "hybrid work",
        "rekrutacja zdalna",
        "quick apply",
    )
    return sum(1 for marker in chrome_markers if marker in lowered) >= 2


def _looks_like_requirement_meta_blob(value: str) -> bool:
    lowered = _normalize_free_text(value).lower()
    if lowered == "n/a":
        return False
    meta_markers = (
        "powrót do wyszukiwania",
        "kategoria:",
        "lokalizacje:",
        "oferta ważna do",
        "valid for",
        "ważna jeszcze",
        "rekrutacja online",
        "rekrutacja zdalna",
        "język rekrutacji",
        "kontrakt tymczasowy",
        "contract of mandate",
        "assistant, junior specialist",
        "praktykant / praktykantka",
        "praca zdalna:",
        "home office work",
        "praca stacjonarna",
        "pełny etat",
        "immediate employment",
        "komputer:",
        "agile management",
        "hybrydowo",
        "specializations:",
        "specjalizacje:",
    )
    return sum(1 for marker in meta_markers if marker in lowered) >= 2


def _extract_requirement_keywords(source: str) -> list[str]:
    cleaned = _normalize_free_text(source)
    if cleaned == "N/A":
        return []

    keyword_map = (
        ("AI/ML", r"\bai/ml\b|\bmachine learning\b"),
        ("Computer Vision", r"\bcomputer vision\b"),
        ("LLM", r"\bllms?\b|\blarge language models?\b"),
        ("English", r"\bangielski\b|\benglish\b"),
        ("Polish", r"\bpolski\b|\bpolish\b"),
        ("WhatsApp API", r"\bwhatsapp api\b"),
        ("Azure", r"\bmicrosoft azure\b|\bazure\b"),
        ("Azure DevOps", r"\bazure dev\s*ops\b|\bazure devops\b"),
        ("SQL Server", r"\bmicrosoft sql server\b|\bsql server\b"),
        ("PowerBi", r"\bpower\s*bi\b|\bpowerbi\b"),
        ("Tableau", r"\btableau\b"),
        ("QuickSight", r"\bquicksight\b"),
        ("Looker", r"\blooker(?:\s+studio)?\b"),
        ("Pandas", r"\bpandas\b"),
        ("Python", r"\bpython\b"),
        ("Node.js", r"\bnode(?:\.js)?\b"),
        ("JavaScript", r"\bjavascript\b"),
        ("HTML", r"\bhtml\b"),
        ("Go", r"\bgo\b"),
        ("NLP", r"\bnlp\b"),
        ("SQL", r"\bsql\b"),
        ("Django", r"\bdjango\b"),
        ("Flask", r"\bflask\b"),
        ("GraphQL", r"\bgraphql\b"),
        ("Git", r"\bgit\b"),
        ("AWS", r"\baws\b"),
        ("Docker", r"\bdocker\b"),
        ("Kubernetes", r"\bkubernetes\b|\bk8s\b"),
        ("Linux", r"\blinux\b"),
        ("Unix", r"\bunix(?:owych|owych)?\b|\bunix\b"),
        ("BI", r"\bbi\b"),
        ("Spark", r"\bspark\b"),
        ("Airflow", r"\bairflow\b"),
        ("Matplotlib", r"\bmatplotlib\b"),
        ("Seaborn", r"\bseaborn\b"),
    )

    items: list[str] = []
    for label, pattern in keyword_map:
        if re.search(pattern, cleaned, re.I):
            items.append(label)
    return items


def _enrich_sparse_requirements(current: str, *values: str) -> str:
    cleaned = _clean_requirements(current)
    if cleaned == "N/A":
        return cleaned

    current_items = [item.strip() for item in cleaned.splitlines() if item.strip()]
    if len(current_items) > 2:
        return cleaned

    keyword_items = _extract_requirement_keywords(" ".join(str(value or "") for value in values))
    if not keyword_items:
        return cleaned

    current_blob = " ".join(current_items).lower()
    seen = {item.lower() for item in current_items}
    merged = list(current_items)
    for item in keyword_items:
        item_key = item.lower()
        if item_key in seen or item_key in current_blob:
            continue
        seen.add(item_key)
        merged.append(item)

    return "\n".join(merged[:8]) if merged else "N/A"


def _prefer_keyword_requirements(current: str, raw_requirements: str) -> str:
    cleaned = _clean_requirements(current)
    raw = str(raw_requirements or "").strip()
    if not raw:
        return cleaned

    keyword_items = _extract_requirement_keywords(raw)
    if not keyword_items:
        return cleaned

    lowered = cleaned.lower()
    if cleaned == "N/A" or _looks_like_requirement_meta_blob(raw) or any(
        marker in lowered
        for marker in (
            "opportunity overview",
            "expect hands-on exposure",
            "dołącz do innowacyjnego projektu",
            "to nie jest standardowa praktyka",
        )
    ) or _requirements_look_dense_blob(cleaned):
        return "\n".join(keyword_items[:8])
    return cleaned


def _requirements_look_task_heavy(value: str) -> bool:
    cleaned = _normalize_free_text(value)
    if cleaned == "N/A":
        return False

    lowered = cleaned.lower()
    task_markers = (
        "combine software engineering",
        "work directly with customers",
        "develop expertise",
        "collaborate closely",
        "learn and use",
        "understand systems end-to-end",
        "take ownership of production-grade integrations",
        "grow toward",
        "write project-specific code",
        "implement and integrate",
        "configure ",
        "work with routers",
        "analyze logs",
        "support your integrations",
        "what does the day-to-day work look like",
        "your work combines",
    )
    requirement_markers = (
        "experience",
        "knowledge of",
        "understanding of",
        "english proficiency",
        "willingness to",
        "degree",
        "python preferred",
        "rest apis",
        "linux administration",
    )
    task_hits = sum(1 for marker in task_markers if marker in lowered)
    requirement_hits = sum(1 for marker in requirement_markers if marker in lowered)
    return task_hits >= 2 and requirement_hits <= 2


def _requirements_need_restructure(value: str) -> bool:
    cleaned = _normalize_free_text(value)
    if cleaned == "N/A" or "\n" in cleaned:
        return False
    starters = ("you have ", "you are ", "your experience ", "experience with ", "understanding of ")
    return sum(cleaned.lower().count(starter) for starter in starters) >= 2


def _requirements_look_dense_blob(value: str) -> bool:
    cleaned = _normalize_free_text(value)
    if cleaned == "N/A":
        return False

    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if any(len(line) > 140 for line in lines):
        return True

    lowered = cleaned.lower()
    blob_markers = (
        "znajomości ",
        "praktycznej znajomości",
        "umiejętności ",
        "experience in ",
        "experience with ",
    )
    if sum(lowered.count(marker) for marker in blob_markers) >= 3:
        return True
    return len(lines) >= 5 and sum(1 for line in lines if len(line) > 60) >= 3


def _extract_summary_from_requirement_text(source: str) -> str:
    lines = _extract_requirement_narrative_lines(source)
    if not lines:
        return ""
    return _first_sentence(lines[0])


def _extract_description_from_requirement_text(source: str) -> str:
    lines = _extract_requirement_narrative_lines(source)
    if not lines:
        return ""

    picked: list[str] = []
    total_len = 0
    for line in lines[:3]:
        candidate_len = total_len + len(line) + (1 if picked else 0)
        if candidate_len > 900:
            break
        picked.append(line)
        total_len = candidate_len
    return " ".join(picked).strip()


def _extract_requirement_narrative_lines(source: str) -> list[str]:
    cleaned = str(source or "").strip()
    if not cleaned:
        return []

    lines = [line.strip(" -:") for line in re.split(r"\n+", cleaned) if line.strip()]
    narrative_lines: list[str] = []
    bad_prefixes = (
        "valid for",
        "ważna jeszcze",
        "jana ",
        "technologiczna ",
        "contract of",
        "umowa ",
        "assistant, junior specialist",
        "praktykant",
        "home office work",
        "praca stacjonarna",
        "full-time",
        "pełny etat",
        "immediate employment",
        "remote recruitment",
        "rekrutacja zdalna",
        "specializations:",
        "specjalizacje:",
        "microsoft azure",
        "microsoft sql server",
        "apache spark",
        "azure devops",
        "azure dev ops",
        "apache airflow",
        "whatsapp api",
    )
    label_prefixes = ("opportunity overview", "overview")

    for line in lines:
        lowered = line.lower()
        if len(line) < 40:
            continue
        if any(lowered.startswith(prefix) for prefix in bad_prefixes):
            continue
        if _looks_like_location_meta_line(line):
            continue
        if _contains_job_meta_chrome(line):
            continue
        for label in label_prefixes:
            if lowered.startswith(label):
                line = line[len(label) :].strip(" :-")
                lowered = line.lower()
                break
        if not line or len(line) < 40:
            continue
        if _looks_like_requirement_item(line) and len(line) < 120:
            continue
        narrative_lines.append(line)

    return narrative_lines


def _looks_like_location_meta_line(value: str) -> bool:
    cleaned = _normalize_free_text(value)
    if cleaned == "N/A":
        return False
    lowered = cleaned.lower()
    if any(token in lowered for token in ("masovian", "opolskie", "śląskie", "małopolskie", "dolnośląskie")):
        return True
    if cleaned.count(",") >= 2 and "(" in cleaned and ")" in cleaned:
        return True
    if re.search(r"\b(?:warszawa|kraków|krakow|gdańsk|gdansk|wrocław|wroclaw|łódź|lodz|opole|katowice)\b", lowered) and re.search(r"\d", cleaned):
        return True
    return False


def _looks_like_culture_or_perks_blurb(value: str) -> bool:
    lowered = _normalize_free_text(value).lower()
    if lowered == "n/a":
        return False
    culture_markers = (
        "mamy bardzo elastyczne godziny pracy",
        "mamy bardzo elastyczny czas pracy",
        "nie uznajemy hierarchii",
        "tworzymy pozytywną energię",
        "organizujemy dodatkowe aktywności",
        "kanapę i biblioteczkę",
        "poznaj inny wymiar współpracy",
        "elastyczny czas pracy",
        "20 min codziennego czytania",
        "szkolenia wewnętrzne",
        "brak korporacyjnych struktur",
        "dzielenie się wiedzą",
        "po prostu praca z sensem",
        "integracje",
        "dołącz do nas",
    )
    role_markers = (
        "python",
        "django",
        "sql",
        "aws",
        "git",
        "experience",
        "doświadczenie",
        "requirements",
        "wymagania",
        "responsibilities",
        "czym będziesz",
    )
    return sum(1 for marker in culture_markers if marker in lowered) >= 2 and not any(
        marker in lowered for marker in role_markers
    )


def _looks_like_cta_question(value: str) -> bool:
    cleaned = _normalize_free_text(value)
    if cleaned == "N/A":
        return False
    return _looks_like_cta_lead_segment(cleaned)


def _looks_like_cta_lead_segment(value: str) -> bool:
    cleaned = _normalize_free_text(value)
    if cleaned == "N/A":
        return False
    lowered = cleaned.lower()
    if not cleaned.endswith(("?", "!")):
        return False
    if len(cleaned) > 140:
        return False
    cta_starters = (
        "lubisz ",
        "chcesz ",
        "marzysz ",
        "spędź ",
        "spedz ",
        "masz plany",
        "gotowy ",
        "ready ",
        "want to ",
        "dołącz ",
        "dolacz ",
        "to świetnie",
    )
    return lowered.startswith(cta_starters)
