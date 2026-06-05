from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlparse, urlunparse

from jobscraper.src.airtable_client import normalize_url as normalize_link

from .models import JobDetail

SUSPICIOUS_COMPANY_VALUES = {
    "n/a",
    "company",
    "employer",
    "o firmie",
    "apply",
    "aplikuj",
    "polityka prywatności",
    "privacy policy",
}
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
    r"is the employer listed for this position.*",
)
CHROME_MARKERS = (
    "utwórz konto indeed",
    "aplikacja w witrynie firmy",
    "pełny opis stanowiska",
    "na podstawie pełnego opisu stanowiska",
    "przejdź od razu do głównej zawartości",
)
NOTES_NOISE_PATTERNS = (
    r"(?i)^aplikacja w witrynie firmy$",
    r"(?i)^utwórz konto indeed.*",
    r"(?i)^application consent and privacy notices present$",
    r"(?i)^privacy notices present$",
    r"(?i)^employer collects applications via external (?:system|form)$",
    r"(?i)^employer application via external (?:system|form)$",
    r"(?i)^application via employer'?s external (?:system|form)$",
    r"(?i)^apply via (?:external|company) (?:system|form|site|website)$",
    r"(?i)^recruitment via (?:external|employer'?s external) (?:system|form)$",
)
LOCATION_LABEL_PATTERNS = (
    r"(?i)\bmiejsce pracy\b\s*:\s*",
    r"(?i)\bworkplace\b\s*:\s*",
    r"(?i)\blokalizacja\b\s*:\s*",
)
NON_CITY_LOCATION_MARKERS = {
    "hybrydowo",
    "hybrid",
    "remote",
    "praca zdalna",
    "remote work",
    "stacjonarnie",
    "stacjonarna",
    "on-site",
    "on site",
    "office",
    "poland",
    "polska",
    "cała polska",
    "cala polska",
}
WORK_MODE_PATTERNS = (
    ("Remote", ("remote", "praca zdalna", "zdalna", "zdalnie", "remote work", "remotely")),
    ("Hybrid", ("hybrid", "hybryd", "partially remote")),
    ("On-site", ("on-site", "on site", "stacjon", "office")),
)
SALARY_LABEL_PATTERNS = (
    r"(?i)^wynagrodzenie\s*[-:]\s*",
    r"(?i)^salary\s*[-:]\s*",
    r"(?i)^compensation\s*[-:]\s*",
)
REQUIREMENT_DUTY_PREFIXES = (
    "developing ",
    "building ",
    "creating ",
    "designing ",
    "supporting ",
    "maintaining ",
    "implementing ",
)
SKILL_PREFIX_PATTERNS = (
    r"(?i)^(basic|good|strong|practical)?\s*knowledge of\s+",
    r"(?i)^familiarity with\s+",
    r"(?i)^experience with\s+",
    r"(?i)^experience in\s+",
    r"(?i)^proficiency in\s+",
    r"(?i)^understanding of\s+",
    r"(?i)^programming skills in\s+",
    r"(?i)^podstawowa znajomość\s+",
    r"(?i)^dobra znajomość\s+",
    r"(?i)^znajomość\s+",
    r"(?i)^strong foundations in\s+",
)
LIST_PREFIX_MARKERS = (
    "libraries",
    "library",
    "biblioteki",
    "frameworks",
    "framework",
    "technologies",
    "technology",
    "technologie",
    "tools",
    "tooling",
    "narzędzia",
    "skills",
    "skill",
    "stack",
    "tech stack",
    "languages",
    "języki",
    "databases",
    "bazy danych",
    "following",
    "takie jak",
)
SHORT_REQUIREMENT_CONNECTOR_PATTERNS = (
    r"\s+and/or\s+",
    r"\s+i/lub\s+",
    r"\s+oraz\s+",
)
ADDRESS_HINT_PATTERNS = (
    r"(?i)\bul\.?\b",
    r"(?i)\balej[aei]\b",
    r"(?i)\bal\.?\b",
    r"(?i)\bplac\b",
    r"(?i)\bpl\.?\b",
    r"(?i)\bstreet\b",
    r"(?i)\bst\.?\b",
    r"(?i)\broad\b",
    r"(?i)\brd\.?\b",
    r"(?i)\bavenue\b",
    r"(?i)\bave\.?\b",
)


def to_airtable_record(detail: JobDetail) -> dict[str, str]:
    company = _normalize_company(detail.company)
    position = _normalize_position(detail.position)
    salary = _normalize_salary(detail.salary)
    work_mode = _normalize_work_mode(
        detail.location,
        detail.notes,
        detail.company_description,
    )
    location = _normalize_location(
        detail.location,
        fallback_text="; ".join(part for part in (detail.notes, detail.company_description) if _normalize_scalar(part)),
    )
    requirements = _normalize_requirements(detail.requirements, notes=detail.notes)
    notes = _normalize_notes(
        detail.notes,
        company=company,
        position=position,
        salary=salary,
        location=location,
        work_mode=work_mode,
        requirements=requirements,
    )
    company_description = _normalize_company_description(
        detail.company_description,
        company=company,
        position=position,
    )
    return {
        "Source": _value(detail.source),
        "Link": _value(detail.final_url or detail.discovered_url),
        "Company": _value(company),
        "Position": _value(position),
        "Salary": _value(salary),
        "Location": _value(location),
        "Local/Remote/Hybrid": _value(work_mode),
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


def validate_airtable_record(record: dict[str, str]) -> list[str]:
    issues: list[str] = []
    link = _normalize_scalar(record.get("Link", ""))
    company = _normalize_scalar(record.get("Company", ""))
    position = _normalize_scalar(record.get("Position", ""))
    requirements = _normalize_scalar(record.get("Requirements", ""))
    notes = _normalize_scalar(record.get("Notes", ""))
    description = _normalize_scalar(record.get("Company description", ""))

    if not _looks_like_valid_job_link(link):
        issues.append("blocking: invalid job link")
    if not company or company.lower() in SUSPICIOUS_COMPANY_VALUES:
        issues.append("blocking: missing or suspicious company")
    if not position or position.lower() in {"praca", "job", "offer"}:
        issues.append("blocking: missing or suspicious position")
    if description and any(marker in description.lower() for marker in CHROME_MARKERS):
        issues.append("warning: company description contains platform chrome")
    if requirements and _looks_like_bundled_requirements(requirements):
        issues.append("warning: requirements contain bundled items")
    if notes and len(notes) > 420:
        issues.append("warning: notes are too long")
    return issues


def should_skip_airtable_record(record: dict[str, str]) -> bool:
    return any(issue.startswith("blocking:") for issue in validate_airtable_record(record))


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


def _looks_like_valid_job_link(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc:
        return False
    path = parsed.path.rstrip("/")
    return path not in {"", "/", "/pl", "/en"}


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
    for pattern in SALARY_LABEL_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned)
    cleaned = re.sub(r"\s*;\s*", "; ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -;")
    return cleaned


def _normalize_location(value: str, fallback_text: str = "") -> str:
    cleaned = _normalize_scalar(value)
    fallback_cleaned = _normalize_scalar(fallback_text)
    if not cleaned and not fallback_cleaned:
        return ""
    for pattern in LOCATION_LABEL_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned)
        fallback_cleaned = re.sub(pattern, "", fallback_cleaned)

    candidates: list[str] = []
    for segment in re.split(r"[;/|]", cleaned):
        for candidate in _extract_city_candidates(segment):
            if candidate and candidate not in candidates:
                candidates.append(_abbreviate_major_city(candidate))
    if not candidates:
        mode = _extract_work_mode(cleaned) or _extract_work_mode(fallback_cleaned)
        return mode
    return ", ".join(candidates)


def _normalize_work_mode(*values: str) -> str:
    mode = _choose_work_mode(*values)
    if mode == "On-site":
        return "Local"
    return mode


def _normalize_requirements(values: list[str], notes: str = "") -> list[str]:
    normalized: list[str] = []
    for value in values:
        for item in _expand_requirement_item(value):
            normalized_item = _normalize_requirement_item(item)
            if not normalized_item:
                continue
            normalized.append(normalized_item)
    for value in _recover_requirements_from_notes(notes):
        for item in _expand_requirement_item(value):
            normalized_item = _normalize_requirement_item(item)
            if not normalized_item:
                continue
            normalized.append(normalized_item)
    normalized = _compact_repeated_prefix_requirements(normalized)
    return _drop_redundant_requirements(normalized)


def _recover_requirements_from_notes(notes: str) -> list[str]:
    cleaned = _normalize_scalar(notes)
    if not cleaned:
        return []

    parts = [_normalize_scalar(part) for part in re.split(r"[;\n\r]+", cleaned) if _normalize_scalar(part)]
    recovered: list[str] = []
    collecting_stack = False

    for part in parts:
        stack_payload = _strip_stack_label(part)
        if stack_payload is not None:
            collecting_stack = True
            recovered.extend(_split_stack_segment(stack_payload))
            continue

        if collecting_stack and _looks_like_stack_segment(part):
            recovered.extend(_split_stack_segment(part))
            continue

        collecting_stack = False

    return recovered


def _strip_stack_label(value: str) -> str | None:
    match = re.match(
        r"(?i)^(?:tech stack(?:s| highlights?)?|technologies expected|technologies used|technologies|stack)\s*:\s*(.+)$",
        value,
    )
    if not match:
        return None
    return match.group(1).strip()


def _looks_like_stack_segment(value: str) -> bool:
    cleaned = _normalize_scalar(value)
    if not cleaned or ":" in cleaned:
        return False
    if "/" not in cleaned and "," not in cleaned:
        return False
    parts = _split_stack_segment(cleaned)
    return 1 < len(parts) <= 12


def _split_stack_segment(value: str) -> list[str]:
    normalized = _normalize_scalar(value)
    if not normalized:
        return []
    normalized = re.sub(r"(?i)\btechnologies?\b", "", normalized)
    normalized = re.sub(r"(?i)\bci\s*/\s*cd\b", "CI_CD", normalized)
    normalized = normalized.replace("/", ",")
    parts = [
        part.strip(" .")
        for part in re.split(r"[;,]", normalized)
        if part.strip(" .")
    ]

    cleaned_parts: list[str] = []
    for part in parts:
        part = part.replace("CI_CD", "CI/CD")
        if len(part.split()) > 4:
            continue
        if part.lower() in {"and", "or", "oraz", "i"}:
            continue
        if part not in cleaned_parts:
            cleaned_parts.append(part)
    return cleaned_parts


def _expand_requirement_item(value: str) -> list[str]:
    cleaned = _normalize_scalar(value)
    if not cleaned:
        return []
    match = re.match(r"(?i)^(located|based)\s+in\s+(.+)$", cleaned)
    if match:
        prefix = match.group(1).capitalize() + " in"
        locations_part = match.group(2).strip()
        if any(separator in locations_part for separator in ("/", ";", ",")):
            pieces = [
                piece.strip(" .")
                for piece in re.split(r"[/;,]", locations_part)
                if piece.strip(" .")
            ]
            if len(pieces) > 1:
                return [f"{prefix} {piece}" for piece in pieces]
        return [cleaned]

    colon_expanded = _expand_colon_requirement_list(cleaned)
    if colon_expanded:
        return colon_expanded

    short_list_expanded = _expand_short_requirement_list(cleaned)
    if short_list_expanded:
        return short_list_expanded

    return [cleaned]


def _expand_colon_requirement_list(value: str) -> list[str]:
    if ":" not in value:
        return []
    prefix, tail = value.split(":", 1)
    if not prefix.strip() or not tail.strip():
        return []
    lowered_prefix = prefix.lower().strip()
    if not any(marker in lowered_prefix for marker in LIST_PREFIX_MARKERS):
        if not (
            len(lowered_prefix.split()) <= 3
            and re.search(r"\b(?:basic|basics|podstawy|znasz)\b", lowered_prefix, flags=re.IGNORECASE)
        ):
            return []
    if re.search(r"\b(?:two or more|following|poniższych|powyższych)\b", value, flags=re.IGNORECASE):
        return []
    parts = _split_short_requirement_parts(tail)
    return parts if len(parts) > 1 else []


def _expand_short_requirement_list(value: str) -> list[str]:
    if "(" in value or ")" in value:
        return []
    if len(value) > 90:
        return []
    if re.search(r"\b(?:two or more|following|poniższych|powyższych)\b", value, flags=re.IGNORECASE):
        return []
    if not any(token in value for token in (" / ", ",", ";")) and not re.search(
        r"\b(?:and/or|i/lub|oraz)\b",
        value,
        flags=re.IGNORECASE,
    ):
        return []
    parts = _split_short_requirement_parts(value)
    if len(parts) <= 1:
        return []
    if len(parts[0].split()) > 2:
        return []
    if sum(len(part.split()) for part in parts) > 10:
        return []
    return parts


def _split_short_requirement_parts(value: str) -> list[str]:
    normalized = value
    for pattern in SHORT_REQUIREMENT_CONNECTOR_PATTERNS:
        normalized = re.sub(pattern, ",", normalized, flags=re.IGNORECASE)
    normalized = normalized.replace(" / ", ",")
    parts = [
        part.strip(" .")
        for part in re.split(r"[;,]", normalized)
        if part.strip(" .")
    ]
    if len(parts) <= 1 or len(parts) > 8:
        return []
    if not all(_is_short_requirement_part(part) for part in parts):
        return []
    return parts


def _is_short_requirement_part(value: str) -> bool:
    cleaned = _normalize_scalar(value)
    if not cleaned:
        return False
    cleaned = re.sub(r"^(?:or|and|oraz)\s+", "", cleaned, flags=re.IGNORECASE)
    word_count = len(cleaned.split())
    if word_count == 0 or word_count > 4:
        return False
    if len(cleaned) > 36:
        return False
    if re.search(r"[.!?]$", cleaned):
        return False
    return True


def _normalize_requirement_item(value: str) -> str:
    cleaned = _normalize_scalar(value)
    if not cleaned:
        return ""
    cleaned = re.sub(r"^[\-\*\u2022]+\s*", "", cleaned).strip()
    cleaned = re.sub(r"^(optional|must have|nice to have|technical skills|soft skills)\s*:\s*", "", cleaned, flags=re.IGNORECASE)
    if cleaned.lower() in REQUIREMENT_SECTION_LABELS:
        return ""
    if cleaned.lower().startswith(REQUIREMENT_DUTY_PREFIXES):
        return ""
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -;:")
    return cleaned


def _canonical_requirement(value: str) -> str:
    cleaned = value.lower()
    cleaned = re.sub(r"^(optional|must have|nice to have)\s*:\s*", "", cleaned)
    cleaned = re.sub(r"[^a-z0-9ąćęłńóśźż]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _normalize_notes(
    value: str,
    company: str = "",
    position: str = "",
    salary: str = "",
    location: str = "",
    work_mode: str = "",
    requirements: list[str] | None = None,
) -> str:
    cleaned = _normalize_scalar(value)
    requirements = requirements or []
    raw_parts = [_normalize_scalar(part) for part in re.split(r"[;\n\r]+", cleaned)]
    parts: list[str] = []
    for part in raw_parts:
        normalized_part = _normalize_note_part(part)
        if not normalized_part:
            continue
        if _is_company_only_note(normalized_part, company):
            continue
        parts.append(normalized_part)
    compact = "; ".join(parts) if len(parts) > 1 else (parts[0] if parts else "")
    if not compact:
        return _synthesize_notes(
            position=position,
            salary=salary,
            location=location,
            work_mode=work_mode,
            requirements=requirements,
        )
    compact = re.sub(r"\s*;\s*", "; ", compact)
    compact = re.sub(r"\s{2,}", " ", compact).strip(" ;")
    compact = _truncate_text(compact, max_chars=420, max_sentences=3)
    if compact:
        return compact
    return _synthesize_notes(
        position=position,
        salary=salary,
        location=location,
        work_mode=work_mode,
        requirements=requirements,
    )


def _synthesize_notes(
    *,
    position: str,
    salary: str,
    location: str,
    work_mode: str,
    requirements: list[str],
) -> str:
    parts: list[str] = []
    if work_mode:
        parts.append(f"Work mode: {work_mode}")
    if location and location not in {work_mode, "Remote", "Hybrid", "Local"}:
        parts.append(f"Location: {location}")
    if salary:
        parts.append(f"Salary: {salary}")
    requirement_hint = _pick_note_requirement(requirements)
    if requirement_hint:
        parts.append(f"Key requirement: {requirement_hint}")
    elif position:
        parts.append(f"Role: {position}")
    return "; ".join(parts[:4]).strip(" ;")


def _pick_note_requirement(requirements: list[str]) -> str:
    candidates = [_normalize_scalar(item) for item in requirements if _normalize_scalar(item)]
    if not candidates:
        return ""

    def score(value: str) -> tuple[int, int]:
        lowered = value.lower()
        technical = bool(re.search(
            r"\b(?:python|java(?:script)?|typescript|sql|html|css|aws|azure|gcp|docker|kubernetes|git|react|angular|django|flask|fastapi|node(?:\\.js)?|api|llm|rag|genai|tensorflow|pytorch|terraform|databricks|pyspark|go|golang|c\\+\\+|c#|php|bash|linux|excel|power bi)\b",
            lowered,
        ))
        concise = 20 <= len(value) <= 120
        return (2 if technical else 0) + (1 if concise else 0), -len(value)

    return max(candidates, key=score)


def _normalize_note_part(value: str) -> str:
    cleaned = _normalize_scalar(value)
    if not cleaned:
        return ""
    cleaned = re.sub(r"(?i)^(?:recruitment|rekrutacja)\s*:\s*", "Recruitment: ", cleaned)
    cleaned = re.sub(r"(?i)^(?:benefits|benefity)\s*:\s*", "Benefits: ", cleaned)
    cleaned = re.sub(r"(?i)^(?:contract|forma współpracy|formy współpracy)\s*:\s*", "", cleaned)
    cleaned = re.sub(r"(?i)^(?:work mode|tryb pracy)\s*:\s*", "", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -;:")
    if not cleaned:
        return ""
    for pattern in NOTES_NOISE_PATTERNS:
        if re.search(pattern, cleaned):
            return ""
    return cleaned


def _normalize_company_description(value: str, company: str = "", position: str = "") -> str:
    cleaned = _normalize_scalar(value)
    if not cleaned:
        return ""
    cleaned = re.sub(r"(?<=[a-ząćęłńóśźż])(?=[A-ZĄĆĘŁŃÓŚŹŻ])", " ", cleaned)
    cleaned = re.sub(r"(?<=[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż])(?=\d)", " ", cleaned)
    cleaned = re.sub(r"(?<=\d)(?=[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż])", " ", cleaned)
    for pattern in COMPANY_DESCRIPTION_NOISE_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"\bO firmie\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(?i)(?:^|[;,.]\s*)n/?a(?:$|[;,.]\s*)", " ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -;")
    if not cleaned:
        return ""
    if not re.search(r"[.!?]", cleaned) and re.search(r"\d", cleaned):
        return ""
    if _is_name_only_company_description(cleaned, company, position):
        return ""
    if len(cleaned.split()) < 5:
        return ""
    truncated = _truncate_text(cleaned, max_chars=380, max_sentences=2)
    return "" if _is_name_only_company_description(truncated, company, position) else truncated


def _drop_redundant_requirements(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = _canonical_requirement(value)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(value)

    filtered: list[str] = []
    signatures = [_requirement_skill_signature(value) for value in deduped]
    for index, value in enumerate(deduped):
        signature = signatures[index]
        if signature and any(
            other_index != index
            and signatures[other_index] == signature
            and len(deduped[other_index]) > len(value)
            for other_index in range(len(deduped))
        ):
            continue
        if signature and any(
            other_index != index
            and signatures[other_index] != signature
            and _requirement_contains_signature(deduped[other_index], signature)
            for other_index in range(len(deduped))
        ):
            continue
        filtered.append(value)
    return filtered


def _compact_repeated_prefix_requirements(values: list[str]) -> list[str]:
    grouped_tails: dict[str, list[str]] = {}
    ordered_prefixes: list[str] = []
    passthrough: list[str] = []

    for value in values:
        prefix_payload = _repeated_requirement_prefix(value)
        if not prefix_payload:
            passthrough.append(value)
            continue
        prefix, tail = prefix_payload
        if prefix not in grouped_tails:
            grouped_tails[prefix] = []
            ordered_prefixes.append(prefix)
        if tail not in grouped_tails[prefix]:
            grouped_tails[prefix].append(tail)

    if not ordered_prefixes:
        return values

    compacted: list[str] = []
    emitted_prefixes: set[str] = set()
    passthrough_index = 0
    for value in values:
        prefix_payload = _repeated_requirement_prefix(value)
        if not prefix_payload:
            compacted.append(passthrough[passthrough_index])
            passthrough_index += 1
            continue
        prefix, _tail = prefix_payload
        if prefix in emitted_prefixes:
            continue
        emitted_prefixes.add(prefix)
        tails = grouped_tails.get(prefix) or []
        if len(tails) == 1:
            compacted.append(f"{prefix}: {tails[0]}")
            continue
        compacted.append(f"{prefix}: {', '.join(tails)}")
    return compacted


def _requirement_skill_signature(value: str) -> str:
    cleaned = _normalize_scalar(value)
    if not cleaned:
        return ""
    lowered = cleaned.lower().strip(" .")
    for pattern in SKILL_PREFIX_PATTERNS:
        lowered = re.sub(pattern, "", lowered)
    lowered = re.sub(r"\([^)]*\)", "", lowered)
    lowered = lowered.strip(" .")
    if ":" in lowered and len(lowered.split()) <= 4:
        lowered = lowered.split(":", 1)[0].strip()
    if len(lowered.split()) > 3 or len(lowered) > 30:
        return ""
    return _canonical_requirement(lowered)


def _requirement_contains_signature(value: str, signature: str) -> bool:
    if not signature:
        return False
    normalized = _canonical_requirement(value)
    return normalized != signature and f" {signature} " in f" {normalized} "


def _repeated_requirement_prefix(value: str) -> tuple[str, str] | None:
    cleaned = _normalize_scalar(value)
    if not cleaned or ":" not in cleaned:
        return None
    prefix, tail = cleaned.split(":", 1)
    prefix = prefix.strip(" -;:")
    tail = tail.strip(" -;:")
    if not prefix or not tail:
        return None
    lowered_prefix = prefix.lower()
    if not any(marker in lowered_prefix for marker in ("following", "poniższych", "powyższych")):
        return None
    return prefix, tail


def _extract_city_candidates(value: str) -> list[str]:
    cleaned = _normalize_scalar(value)
    if not cleaned:
        return []
    cleaned = re.sub(r"\([^)]*\)", lambda match: match.group(0).replace(",", ";"), cleaned)
    cleaned = re.sub(r"[()]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;")
    if not cleaned:
        return []

    address_like = bool(re.search(r"\d", cleaned)) or any(
        re.search(pattern, cleaned) for pattern in ADDRESS_HINT_PATTERNS
    )
    normalized = re.sub(r"\b(?:and|oraz| i )\b", ",", cleaned, flags=re.IGNORECASE)
    parts = [part.strip(" ,;") for part in re.split(r"[;,]", normalized) if part.strip(" ,;")]
    if not parts:
        return []

    candidates: list[str] = []
    for part in parts:
        city = _clean_location_part(part)
        if city and city not in candidates:
            candidates.append(city)
    if not candidates:
        return []
    return [candidates[-1]] if address_like else candidates


def _extract_work_mode(value: str) -> str:
    return _choose_work_mode(value)


def _choose_work_mode(*values: str) -> str:
    saw_remote = False
    saw_hybrid = False
    saw_on_site = False

    for value in values:
        lowered = _normalize_scalar(value).lower()
        if not lowered:
            continue
        for label, markers in WORK_MODE_PATTERNS:
            if not any(marker in lowered for marker in markers):
                continue
            if label == "Hybrid":
                saw_hybrid = True
            elif label == "Remote":
                saw_remote = True
            elif label == "On-site":
                saw_on_site = True

    if saw_hybrid or (saw_remote and saw_on_site):
        return "Hybrid"
    if saw_remote:
        return "Remote"
    if saw_on_site:
        return "On-site"
    return ""


def _clean_location_part(value: str) -> str:
    cleaned = _normalize_scalar(value)
    if not cleaned:
        return ""
    cleaned = re.sub(r"\b\d{2}-\d{3}\b", "", cleaned)
    cleaned = re.sub(r"\b\d+[A-Za-z]?\b", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;-")
    if not cleaned:
        return ""
    lowered = cleaned.lower()
    if lowered in NON_CITY_LOCATION_MARKERS:
        return ""
    if any(marker in lowered for marker in ("remote", "hybrid", "zdal", "stacjon", "office")):
        return ""
    if lowered.endswith(("skie", "ckie", "dzkie")) and cleaned == cleaned.lower():
        return ""
    return cleaned


def _abbreviate_major_city(value: str) -> str:
    cleaned = _normalize_scalar(value)
    if not cleaned:
        return ""

    canonical = (
        cleaned.lower()
        .replace("ą", "a")
        .replace("ć", "c")
        .replace("ę", "e")
        .replace("ł", "l")
        .replace("ń", "n")
        .replace("ó", "o")
        .replace("ś", "s")
        .replace("ź", "z")
        .replace("ż", "z")
    )
    canonical = re.sub(r"[^a-z0-9]+", " ", canonical)
    canonical = re.sub(r"\s+", " ", canonical).strip()

    # Use project-specific shorthand for major Polish cities:
    # first 3 letters uppercased, except Warsaw which stays WAW.
    major_city_codes = {
        "warszawa": "WAW",
        "krakow": "KRA",
        "gdansk": "GDA",
        "wroclaw": "WRO",
        "poznan": "POZ",
        "lodz": "LOD",
        "szczecin": "SZC",
        "bydgoszcz": "BYD",
        "lublin": "LUB",
        "katowice": "KAT",
        "rzeszow": "RZE",
        "radom": "RAD",
        "olsztyn": "OLS",
        "zielona gora": "ZIE",
        "bialystok": "BIA",
        "torun": "TOR",
        "opole": "OPO",
        "kielce": "KIE",
        "gliwice": "GLI",
        "sopot": "SOP",
        "gdynia": "GDY",
        "czestochowa": "CZE",
        "bielsko biala": "BIE",
        "gorzow wielkopolski": "GOR",
    }
    return major_city_codes.get(canonical, cleaned)


def _canonical_company_text(value: str) -> str:
    cleaned = _normalize_scalar(value).lower()
    cleaned = re.sub(r"[^a-z0-9ąćęłńóśźż]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _is_company_only_note(value: str, company: str) -> bool:
    note_key = _canonical_company_text(value)
    company_key = _canonical_company_text(company)
    if not note_key:
        return True
    if not company_key:
        return False
    return note_key == company_key


def _is_name_only_company_description(value: str, company: str, position: str = "") -> bool:
    cleaned = _normalize_scalar(value)
    if not cleaned:
        return True
    description_key = _canonical_company_text(cleaned)
    company_key = _canonical_company_text(company)
    position_key = _canonical_company_text(position)
    if company_key and description_key == company_key:
        return True
    if company_key and description_key.startswith(company_key):
        remainder = description_key.removeprefix(company_key).strip()
        if not remainder or remainder in {"n a", "na"}:
            return True
    stripped = description_key
    for marker in (company_key, position_key):
        if marker:
            stripped = stripped.replace(marker, " ")
    stripped = stripped.replace("employer listed for this position", " ")
    stripped = re.sub(r"\b(?:remote|hybrid|on site|on-site|zdalna|zdalnie|hybrydowo)\b", " ", stripped)
    stripped = re.sub(r"\b(?:sp z o o|s a|sa)\b", " ", stripped)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if len(stripped.split()) < 3:
        return True
    return False


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


def _looks_like_bundled_requirements(value: str) -> bool:
    bullets = [line.strip()[2:] for line in value.splitlines() if line.strip().startswith("- ")]
    for bullet in bullets:
        if len(bullet) < 40:
            continue
        if ";" in bullet:
            return True
        if " / " in bullet:
            slash_parts = [part.strip() for part in bullet.split(" / ") if part.strip()]
            if len(slash_parts) >= 2 and all(len(part.split()) <= 2 for part in slash_parts):
                return True
        comma_parts = [part.strip() for part in bullet.split(",") if part.strip()]
        if len(comma_parts) < 3:
            continue
        first_part_words = len(comma_parts[0].split())
        short_tail_parts = sum(1 for part in comma_parts[1:] if len(part.split()) <= 2)
        if first_part_words <= 2 and short_tail_parts == len(comma_parts) - 1:
            return True
    return False
