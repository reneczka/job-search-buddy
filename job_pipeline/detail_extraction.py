from __future__ import annotations

import asyncio
from html import unescape
import json
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, Field

from .models import JobDetail
from .stagehand_session import StagehandRuntime, sleep_ms
from .url_discovery import accept_cookies, normalize_page_url


class JobDetailPayload(BaseModel):
    company: str = ""
    position: str = ""
    salary: str = ""
    location: str = ""
    notes: str = ""
    requirements: list[str] = Field(default_factory=list)
    company_description: str = ""


async def extract_job_detail(runtime: StagehandRuntime, source_name: str, url: str) -> JobDetail:
    page = await runtime.ensure_page()
    page = await _navigate_with_page_recovery(runtime, page, url)
    await sleep_ms(1200)
    await accept_cookies(runtime, page)

    page_error = await _recover_error_page(runtime, url)
    if page_error:
        final_url = normalize_page_url(page.url) or url
        return JobDetail(
            source=source_name,
            discovered_url=url,
            final_url=final_url,
            notes=page_error,
            raw={"page_error": page_error},
        )

    payload = await _extract_payload(runtime)
    fallback = await _fallback_page_data(page)

    company = _clean(payload.get("company")) or _clean(fallback.get("company"))
    position = _clean(payload.get("position")) or _clean(fallback.get("position"))
    salary = _clean(payload.get("salary")) or _clean(fallback.get("salary"))
    location = _clean(payload.get("location")) or _clean(fallback.get("location"))
    notes = _clean(payload.get("notes")) or _clean(fallback.get("notes"))
    company_description = _clean(payload.get("company_description")) or _clean(fallback.get("company_description"))
    body_text = _clean(fallback.get("body_text"))
    content_blocks = [
        _clean(item)
        for item in fallback.get("content_blocks", [])
        if isinstance(item, str) and _clean(item) and not _looks_like_listing_content(_clean(item))
    ]

    requirements: list[str] = []
    raw_requirements = payload.get("requirements")
    if isinstance(raw_requirements, list):
        requirements = [item.strip() for item in raw_requirements if isinstance(item, str) and item.strip()]
    if not requirements:
        requirements = [
            _clean(item)
            for item in fallback.get("requirements", [])
            if isinstance(item, str) and _clean(item)
        ]
    if _looks_like_listing_content(notes):
        notes = ""
    if _looks_like_listing_content(company_description):
        company_description = ""

    final_url = normalize_page_url(page.url) or url
    if source_name == "nofluffjobs":
        fallback_http = await _nofluff_http_fallback(url)
        fallback_company = _clean(fallback_http.get("company", ""))
        fallback_position = _clean(fallback_http.get("position", ""))
        fallback_salary = _clean(fallback_http.get("salary", ""))
        fallback_location = _clean(fallback_http.get("location", ""))
        fallback_notes = _clean(fallback_http.get("notes", ""))
        fallback_description = _clean(fallback_http.get("company_description", ""))
        fallback_requirements = [
            _clean(item)
            for item in fallback_http.get("requirements", [])
            if _clean(item)
        ]

        if (_is_suspicious_company(company) or not company) and fallback_company:
            company = fallback_company
        if (not position or _looks_like_listing_title(position) or _same_meaning(position, "Praca")) and fallback_position:
            position = fallback_position
        if (not salary or _is_suspicious_salary(salary)) and fallback_salary:
            salary = fallback_salary
        if (not location or _is_suspicious_location(location)) and fallback_location:
            location = fallback_location
        if _notes_need_nofluff_override(notes, position) and fallback_notes:
            notes = fallback_notes
        if (
            not company_description
            or _same_meaning(company_description, position)
            or _looks_like_weak_description(company_description)
            or _looks_like_titleish_nofluff_description(company_description, position, company)
            or _looks_like_nofluff_meta_text(company_description)
        ) and fallback_description:
            company_description = fallback_description
        if _notes_need_nofluff_override(notes, position) and fallback_description:
            fallback_summary = _first_sentence(_clean_intro_text(fallback_description, position, company))
            if fallback_summary:
                notes = fallback_summary
        if fallback_requirements and _requirements_need_override(requirements):
            requirements = fallback_requirements
        final_url = fallback_http.get("final_url") or url

    if (
        not salary
        or not notes
        or _looks_like_listing_content(notes)
        or not company_description
        or _same_meaning(company_description, position)
        or not requirements
    ):
        generic_http = await _generic_http_fallback(url)
        fallback_salary = _clean(generic_http.get("salary", ""))
        fallback_location = _clean(generic_http.get("location", ""))
        fallback_company = _clean(generic_http.get("company", ""))
        fallback_position = _clean(generic_http.get("position", ""))
        fallback_notes = _clean(generic_http.get("notes", ""))
        fallback_description = _clean(generic_http.get("company_description", ""))
        fallback_requirements = [
            _clean(item)
            for item in generic_http.get("requirements", [])
            if _clean(item)
        ]
        if fallback_salary:
            salary = fallback_salary
        if not location and fallback_location:
            location = fallback_location
        if (not company or _is_suspicious_company(company)) and fallback_company:
            company = fallback_company
        if (not position or _looks_like_listing_title(position)) and fallback_position:
            position = fallback_position
        if (not notes or _looks_like_listing_content(notes) or _same_meaning(notes, position)) and fallback_notes:
            notes = fallback_notes
        if (not company_description or _same_meaning(company_description, position)) and fallback_description:
            company_description = fallback_description
        if not requirements and fallback_requirements:
            requirements = fallback_requirements

    if not notes or _same_meaning(notes, position):
        fallback_notes = _extract_summary_from_content(content_blocks, body_text, position, company)
        if fallback_notes:
            notes = fallback_notes
    if not company_description or _same_meaning(company_description, position):
        fallback_description = _extract_description_from_content(content_blocks, body_text, position, company)
        if fallback_description:
            company_description = fallback_description

    return JobDetail(
        source=source_name,
        discovered_url=url,
        final_url=final_url,
        company=company,
        position=position,
        salary=salary,
        location=location,
        notes=notes,
        requirements=requirements,
        company_description=company_description,
        raw=dict(payload),
    )


async def _extract_payload(runtime: StagehandRuntime) -> dict[str, Any]:
    try:
        extracted = await runtime.session.extract(
            instruction=(
                "Extract job details from this job offer page. "
                "Return concise plain text. "
                "If a field is missing, return an empty string. "
                "Requirements should be short text items describing concrete skills, experience, technologies, tools, languages, or availability, not responsibilities. "
                "Notes can contain a richer summary of the job, team, or hiring info."
            ),
            schema=JobDetailPayload.model_json_schema(),
            options=runtime.model_opts,
            page=runtime.page,
        )
        payload = getattr(extracted.data, "result", None)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


async def _fallback_page_data(page: Any) -> dict[str, str]:
    try:
        payload = await page.evaluate(
            """() => {
                const text = (el) => (el?.textContent || "").trim();
                const meta = (name) => document.querySelector(`meta[name="${name}"], meta[property="${name}"]`)?.content || "";
                const root = document.querySelector("main, article, [role='main']") || document.body;
                const bodyText = (document.body?.innerText || "").trim();
                const uniqueTexts = (selector, minLen, maxLen = 4000) => {
                  const seen = new Set();
                  const values = [];
                  for (const node of root.querySelectorAll(selector)) {
                    const value = text(node).replace(/\\s+/g, " ").trim();
                    if (!value || value.length < minLen || value.length > maxLen) continue;
                    const key = value.toLowerCase();
                    if (seen.has(key)) continue;
                    seen.add(key);
                    values.push(value);
                  }
                  return values;
                };
                const salaryPattern = /\\b\\d[\\d\\s.,]{2,}(?:\\s*[-–]\\s*\\d[\\d\\s.,]{2,})?\\s*(?:PLN|zł|EUR|USD)\\b[^\\n|]{0,40}/i;
                const findSalary = (...values) => {
                  for (const value of values) {
                    const source = String(value || "");
                    const match = source.match(salaryPattern);
                    if (match) return match[0].trim();
                  }
                  return "";
                };
                const cleanSalary = (value, currency, unit) => {
                  const amount = String(value || "").trim();
                  const curr = String(currency || "").trim();
                  const period = String(unit || "").trim();
                  if (!amount) return "";
                  const parts = [amount, curr].filter(Boolean);
                  let out = parts.join(" ").trim();
                  if (period) out = `${out} / ${period}`;
                  return out.trim();
                };
                const cleanLocation = (value) => {
                  const parts = String(value || "").split(",").map((part) => part.trim()).filter(Boolean);
                  const deduped = [];
                  const seen = new Set();
                  for (const part of parts) {
                    const key = part.toLowerCase();
                    if (seen.has(key)) continue;
                    seen.add(key);
                    deduped.push(part);
                  }
                  return deduped.join(", ");
                };
                const unwrapJobPosting = (value) => {
                  if (!value) return null;
                  if (Array.isArray(value)) {
                    for (const item of value) {
                      const found = unwrapJobPosting(item);
                      if (found) return found;
                    }
                    return null;
                  }
                  if (typeof value !== "object") return null;
                  if (String(value["@type"] || "").toLowerCase() === "jobposting") return value;
                  if (Array.isArray(value["@graph"])) {
                    return unwrapJobPosting(value["@graph"]);
                  }
                  return null;
                };
                let jobPosting = null;
                for (const node of document.querySelectorAll('script[type="application/ld+json"]')) {
                  try {
                    const parsed = JSON.parse(node.textContent || "");
                    const found = unwrapJobPosting(parsed);
                    if (found) {
                      jobPosting = found;
                      break;
                    }
                  } catch {}
                }
                const h1 = text(document.querySelector("h1"));
                const title = document.title || "";
                const subtitle = text(document.querySelector("h2, [class*='company'], [data-testid*='company']"));
                const paragraphs = uniqueTexts("p", 40, 1200);
                const listItems = uniqueTexts("li", 12, 280);
                const descriptiveBlocks = uniqueTexts(
                  "article, section, [data-testid*='description'], [class*='description'], [class*='content'], [class*='details'], [class*='about'], [class*='job'], [class*='offer']",
                  100,
                  4000,
                );
                const contentBlocks = [...descriptiveBlocks, ...paragraphs]
                  .filter((value, index, array) => array.indexOf(value) === index)
                  .slice(0, 12);
                const mainText = (root?.innerText || bodyText || "").replace(/\\s+/g, " ").trim();
                let ldSalary = "";
                let ldLocation = "";
                let ldCompany = "";
                let ldPosition = "";
                let ldDescription = "";
                if (jobPosting) {
                  const baseSalary = jobPosting.baseSalary;
                  if (baseSalary && typeof baseSalary === "object") {
                    const value = baseSalary.value;
                    if (value && typeof value === "object") {
                      ldSalary = cleanSalary(value.value, baseSalary.currency, value.unitText);
                    }
                  }
                  const jobLocation = jobPosting.jobLocation;
                  if (jobLocation && typeof jobLocation === "object") {
                    const address = jobLocation.address;
                    if (address && typeof address === "object") {
                      ldLocation = cleanLocation([address.addressLocality, address.streetAddress].filter(Boolean).join(", "));
                    }
                  }
                  const org = jobPosting.hiringOrganization;
                  if (org && typeof org === "object") {
                    ldCompany = String(org.name || "").trim();
                  }
                  ldPosition = String(jobPosting.title || "").trim();
                  ldDescription = String(jobPosting.description || "").replace(/<br\\s*\\/?>/gi, "\\n").replace(/<[^>]+>/g, " ").replace(/\\s+/g, " ").trim();
                }
                const metaDescription = meta("description") || meta("og:description");
                return {
                  position: h1 || ldPosition || title.split("|")[0] || "",
                  company: subtitle || ldCompany,
                  salary: ldSalary || findSalary(metaDescription, title, bodyText),
                  location: ldLocation,
                  notes: meta("description") || paragraphs[0] || descriptiveBlocks[0] || "",
                  company_description: ldDescription || descriptiveBlocks[0] || paragraphs.slice(0, 3).join(" "),
                  requirements: listItems.slice(0, 16),
                  body_text: mainText,
                  content_blocks: contentBlocks,
                };
            }"""
        )
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _clean(value: Any) -> str:
    return str(value or "").strip()


async def _nofluff_http_fallback(url: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            response = await client.get(url)
        response.raise_for_status()
    except Exception:
        return {"final_url": url}

    return await asyncio.to_thread(_parse_nofluff_html, response.text, str(response.url))


async def _generic_http_fallback(url: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            response = await client.get(url)
        response.raise_for_status()
    except Exception:
        return {"final_url": url}

    return await asyncio.to_thread(_parse_generic_job_html, response.text, str(response.url))


def _parse_nofluff_html(html: str, final_url: str) -> dict[str, Any]:
    job_posting = _find_job_posting_json_ld(html)
    og_description = _extract_meta_content(html, "og:description") or _extract_meta_content(html, "description")
    if not isinstance(job_posting, dict):
        return {
            "final_url": final_url,
            "notes": og_description,
        }

    hiring_org = job_posting.get("hiringOrganization")
    company = ""
    if isinstance(hiring_org, dict):
        company = _clean(hiring_org.get("name"))

    title = _clean(job_posting.get("title"))
    description = _html_to_text(str(job_posting.get("description") or ""))
    employment_type = _clean(job_posting.get("employmentType"))

    salary = ""
    base_salary = job_posting.get("baseSalary")
    if isinstance(base_salary, dict):
        currency = _clean(base_salary.get("currency"))
        value = base_salary.get("value")
        if isinstance(value, dict):
            amount = value.get("value")
            unit = _clean(value.get("unitText"))
            if amount not in (None, ""):
                salary = f"{amount} {currency}".strip()
                if unit:
                    salary = f"{salary} / {unit}"

    location = ""
    job_location = job_posting.get("jobLocation")
    if isinstance(job_location, dict):
        address = job_location.get("address")
        if isinstance(address, dict):
            parts = [
                _clean(address.get("addressLocality")),
                _clean(address.get("streetAddress")),
            ]
            location = ", ".join(part for part in parts if part)

    requirements: list[str] = []
    skills = job_posting.get("skills")
    if isinstance(skills, list):
        requirements = [
            _clean(item.get("value"))
            for item in skills
            if isinstance(item, dict) and _clean(item.get("value"))
        ]

    notes = _clean(og_description) or employment_type
    if _same_meaning(notes, title):
        notes = employment_type
    if _same_meaning(description, title):
        description = ""
    return {
        "final_url": final_url,
        "company": company,
        "position": title,
        "salary": salary,
        "location": location,
        "notes": notes,
        "requirements": requirements,
        "company_description": description,
    }


def _parse_generic_job_html(html: str, final_url: str) -> dict[str, Any]:
    job_posting = _find_job_posting_json_ld(html)
    og_title = _extract_meta_content(html, "og:title")
    og_description = _extract_meta_content(html, "og:description") or _extract_meta_content(html, "description")

    company = ""
    position = og_title
    salary = _extract_salary_from_text(og_description, og_title)
    location = _extract_location_from_text(og_description)
    notes = _clean(og_description)
    description = ""
    requirements: list[str] = []

    if isinstance(job_posting, dict):
        hiring_org = job_posting.get("hiringOrganization")
        if isinstance(hiring_org, dict):
            company = _clean(hiring_org.get("name"))
        position = _clean(job_posting.get("title")) or position
        description = _html_to_text(str(job_posting.get("description") or ""))

        base_salary = job_posting.get("baseSalary")
        structured_salary = _salary_from_job_posting(base_salary)
        if structured_salary:
            salary = structured_salary

        structured_location = _location_from_job_posting(job_posting.get("jobLocation"))
        if structured_location:
            location = structured_location

        skills = job_posting.get("skills")
        if isinstance(skills, list):
            requirements = [
                _clean(item.get("value"))
                for item in skills
                if isinstance(item, dict) and _clean(item.get("value"))
            ]

    if _looks_like_listing_content(notes) or _looks_like_compact_meta_blurb(notes):
        notes = ""
    if _same_meaning(notes, position):
        notes = ""
    if not notes:
        notes = _first_sentence(_clean_intro_text(description or og_description, position, company))

    if _same_meaning(description, position):
        description = ""
    if not description:
        description = _clean_intro_text(og_description, position, company)

    if not requirements:
        requirements = _extract_requirements_from_text(description)

    return {
        "final_url": final_url,
        "company": company,
        "position": position,
        "salary": salary,
        "location": location,
        "notes": notes,
        "requirements": requirements,
        "company_description": description,
    }


def _find_job_posting_json_ld(html: str) -> dict[str, Any] | None:
    matches = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    for raw in matches:
        try:
            payload = json.loads(raw)
        except Exception:
            continue

        found = _extract_job_posting_object(payload)
        if found:
            return found
    return None


def _extract_job_posting_object(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        payload_type = str(payload.get("@type") or "").lower()
        if payload_type == "jobposting":
            return payload
        graph = payload.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                found = _extract_job_posting_object(item)
                if found:
                    return found
    elif isinstance(payload, list):
        for item in payload:
            found = _extract_job_posting_object(item)
            if found:
                return found
    return None


def _html_to_text(value: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_meta_content(html: str, name: str) -> str:
    pattern = rf'<meta[^>]+(?:property|name)="{re.escape(name)}"[^>]+content="([^"]*)"'
    match = re.search(pattern, html, re.I)
    return _clean(match.group(1)) if match else ""


def _same_meaning(left: str, right: str) -> bool:
    normalize = lambda value: re.sub(r"[^a-z0-9]+", " ", _clean(value).lower()).strip()
    return bool(left and right and normalize(left) == normalize(right))


def _looks_like_nofluff_root(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.lower().removeprefix("www.") == "nofluffjobs.com" and parsed.path.rstrip("/") == "/pl"


def _is_suspicious_company(value: str) -> bool:
    lowered = _clean(value).lower()
    return lowered in {"", "n/a", "obowiązkowe", "requirements", "must have", "nice to have", "lokalizacje"}


def _looks_like_listing_title(value: str) -> bool:
    return _clean(value).lower().startswith("praca ")


def _looks_like_weak_description(value: str) -> bool:
    cleaned = _clean(value)
    if not cleaned:
        return True
    if len(cleaned) < 80:
        return True
    if "..." in cleaned or "…" in cleaned:
        return True
    return False


def _notes_need_nofluff_override(value: str, position: str) -> bool:
    cleaned = _clean(value)
    if not cleaned:
        return True
    lowered = cleaned.lower()
    if "..." in cleaned or "…" in cleaned:
        return True
    if lowered.startswith(("masz plany", "spędź", "spedz", "marzysz", "chcesz ")):
        return True
    if _looks_like_nofluff_meta_text(cleaned):
        return True
    return _same_meaning(cleaned, position)


def _looks_like_titleish_nofluff_description(value: str, position: str, company: str) -> bool:
    cleaned = _clean(value)
    if not cleaned:
        return False
    lowered = cleaned.lower()
    title = _clean(position).lower()
    employer = _clean(company).lower()
    if len(cleaned.split()) > 14:
        return False
    return bool(
        title
        and employer
        and title in lowered
        and employer in lowered
    )


def _looks_like_nofluff_meta_text(value: str) -> bool:
    lowered = _clean(value).lower()
    if not lowered:
        return False
    meta_markers = (
        "kategoria:",
        "lokalizacje:",
        "hybrydowo",
        "stacjonarnie",
        "zdalnie",
    )
    return any(marker in lowered for marker in meta_markers)


def _requirements_need_override(items: list[str]) -> bool:
    cleaned_items = [_clean(item) for item in items if _clean(item)]
    if not cleaned_items:
        return True
    if len(cleaned_items) <= 1:
        return True

    lowered = " ".join(cleaned_items).lower()
    meta_markers = (
        "powrót do wyszukiwania",
        "kategoria:",
        "lokalizacje:",
        "oferta ważna do",
        "rekrutacja online",
        "język rekrutacji",
        "kontrakt tymczasowy",
        "praca zdalna:",
        "komputer:",
        "agile management",
    )
    return any(marker in lowered for marker in meta_markers)


def _salary_from_job_posting(base_salary: Any) -> str:
    if not isinstance(base_salary, dict):
        return ""
    currency = _clean(base_salary.get("currency"))
    value = base_salary.get("value")
    if not isinstance(value, dict):
        return ""
    amount = value.get("value")
    unit = _clean(value.get("unitText"))
    if amount in (None, ""):
        return ""
    out = f"{amount} {currency}".strip()
    if unit:
        out = f"{out} / {unit}"
    return out.strip()


def _location_from_job_posting(job_location: Any) -> str:
    if not isinstance(job_location, dict):
        return ""
    address = job_location.get("address")
    if not isinstance(address, dict):
        return ""
    parts = [_clean(address.get("streetAddress")), _clean(address.get("addressLocality"))]
    seen: set[str] = set()
    ordered: list[str] = []
    for part in parts:
        if not part:
            continue
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(part)
    return ", ".join(ordered)


def _extract_salary_from_text(*values: str) -> str:
    pattern = re.compile(
        r"(?<!\d)\d[\d\s.,]{2,}(?:\s*[-–]\s*\d[\d\s.,]{2,})?\s*(?:PLN|zł|EUR|USD)\b[^\n|<]{0,40}",
        re.I,
    )
    for value in values:
        if _looks_like_listing_content(str(value or "")):
            continue
        match = pattern.search(str(value or ""))
        if match:
            cleaned = _clean(match.group(0))
            cleaned = re.sub(r"^20\d{2}\s+(?=\d)", "", cleaned)
            return cleaned
    return ""


def _extract_location_from_text(value: str) -> str:
    text = _clean(value)
    if "|" not in text:
        return ""
    parts = [part.strip() for part in text.split("|") if part.strip()]
    for part in parts:
        lowered = part.lower()
        if any(token in lowered for token in ("pln", "eur", "usd", "gross", "net", "b2b")):
            continue
        if len(part) > 2:
            return part
    return ""


def _extract_requirements_from_text(value: str) -> list[str]:
    text = _clean_intro_text(value, "", "")
    if not text:
        return []

    markers = (
        "requirements",
        "wymagania",
        "must have",
        "what we look for",
        "kogo szukamy",
        "qualifications",
    )
    lowered = text.lower()
    start_index = -1
    start_marker = ""
    for marker in markers:
        index = lowered.find(marker)
        if index != -1 and (start_index == -1 or index < start_index):
            start_index = index
            start_marker = marker
    if start_index == -1:
        return []

    section = text[start_index + len(start_marker) :].strip(" :-")
    section = _trim_after_content_markers(section)
    if not section:
        return []

    section = re.sub(r"\s*[•✓·]\s*", "\n", section)
    parts = [part.strip(" -:") for part in re.split(r"\n+|(?<=[.!?])\s+", section) if part.strip()]
    if len(parts) <= 1 and "," in section:
        parts = [part.strip(" -:") for part in section.split(",") if part.strip()]

    items: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if len(part) < 4:
            continue
        lowered_part = part.lower()
        if any(
            marker in lowered_part
            for marker in (
                "what we offer",
                "benefits",
                "salary",
                "wynagrodzenie",
                "jak wygląda proces rekrutacji",
                "additional information",
            )
        ):
            continue
        key = re.sub(r"\s+", " ", lowered_part)
        if key in seen:
            continue
        seen.add(key)
        items.append(part)
    return items[:8]


def _extract_summary_from_content(content_blocks: list[str], body_text: str, position: str, company: str) -> str:
    for block in content_blocks:
        summary = _first_sentence(_clean_intro_text(block, position, company))
        if summary and not _looks_like_compact_meta_blurb(summary):
            return summary
    summary = _first_sentence(_clean_intro_text(body_text, position, company))
    return "" if _looks_like_compact_meta_blurb(summary) else summary


def _extract_description_from_content(content_blocks: list[str], body_text: str, position: str, company: str) -> str:
    for block in content_blocks:
        description = _clean_intro_text(block, position, company)
        if len(description) >= 80:
            return description[:2000]
    description = _clean_intro_text(body_text, position, company)
    return description[:2000] if len(description) >= 80 else ""


def _clean_intro_text(value: str, position: str, company: str) -> str:
    text = unescape(_clean(value))
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    if _looks_like_listing_content(text):
        return ""
    text = _trim_before_intro_markers(text)
    text = _trim_after_content_markers(text)
    text = re.sub(r"^(about the company|about the role|job description|opis stanowiska|o firmie)\s*[:\-]?\s*", "", text, flags=re.I)

    normalized_position = _normalize_text_identity(position)
    normalized_company = _normalize_text_identity(company)
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    filtered: list[str] = []
    for sentence in sentences:
        normalized = _normalize_text_identity(sentence)
        if not normalized:
            continue
        if normalized == normalized_position or normalized == normalized_company:
            continue
        filtered.append(sentence)
    return " ".join(filtered).strip()


def _trim_before_intro_markers(text: str) -> str:
    markers = (
        "about the team:",
        "about us:",
        "about the role:",
        "about the company:",
        "opis stanowiska:",
        "o firmie:",
        "kim jesteśmy?",
    )
    lowered = text.lower()
    for marker in markers:
        index = lowered.find(marker)
        if index != -1:
            return text[index + len(marker) :].strip()
    return text


def _trim_after_content_markers(text: str) -> str:
    markers = (
        "requirements",
        "wymagania",
        "must have",
        "nice to have",
        "what we look for",
        "what we offer",
        "benefits",
        "jak wygląda proces rekrutacji",
        "how does the recruitment process look like",
        "salary",
        "wynagrodzenie",
        "compensation",
    )
    lowered = text.lower()
    end_index = len(text)
    for marker in markers:
        index = lowered.find(marker)
        if index != -1:
            end_index = min(end_index, index)
    return text[:end_index].strip()


def _first_sentence(text: str) -> str:
    cleaned = _clean(text)
    if not cleaned:
        return ""
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", cleaned) if part.strip()]
    for sentence in sentences:
        if 40 <= len(sentence) <= 320:
            return sentence
    return cleaned[:320].strip() if cleaned else ""


def _looks_like_compact_meta_blurb(value: str) -> bool:
    cleaned = _clean(value)
    if not cleaned:
        return False
    lowered = cleaned.lower()
    if "|" not in cleaned:
        return False
    if not re.search(r"\b(PLN|zł|EUR|USD)\b", cleaned, re.I):
        return False
    return any(token in lowered for token in ("warsz", "krak", "gda", "katow", "wroc", "łód", "lodz", "remote", "hybrid"))


def _normalize_text_identity(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _clean(value).lower()).strip()


def _looks_like_listing_content(value: str) -> bool:
    lowered = _clean(value).lower()
    if not lowered:
        return False

    obvious_markers = (
        "superoferta",
        "zaloguj się, aby lepiej dopasować oferty",
        "dodaj ogłoszenie",
        "praca it",
        "praca w miastach",
        "lista ofert",
        "you can start asap",
        "stanowiska:",
        "technologie i narzędzia:",
        "specjalizacje:",
        "kreator cv",
        "kalkulator wynagrodzeń",
        "kalkulator godzinowy",
        "kalkulator vat",
        "kalkulator urlopu",
        "kalkulator podróży służbowej",
        "kalkulator ekwiwalentu",
        "przejdź od razu do głównej zawartości",
        "opinie o pracodawcach",
        "przeglądaj wynagrodzenia",
        "początek treści głównej",
        "czego szukasz?",
        "wybraliśmy dla ciebie",
        "więcej ofert",
        "quick apply",
        "wygenerowane przez ai",
        "na bazie pełnej treści ogłoszenia",
    )
    if any(marker in lowered for marker in obvious_markers):
        return True
    return lowered.count("superoferta") >= 2 or lowered.count("praca it") >= 2


def _is_suspicious_salary(value: str) -> bool:
    cleaned = _clean(value)
    if not cleaned:
        return True
    return bool(re.search(r"\b\d\b\s+\d{1,3}\s+\d{3}\s*(PLN|zł|EUR|USD)\b", cleaned, re.I))


def _is_suspicious_location(value: str) -> bool:
    cleaned = _clean(value)
    if not cleaned:
        return True
    return cleaned.lower().startswith("poland")


async def _navigate_with_page_recovery(runtime: StagehandRuntime, page: Any, url: str) -> Any:
    recovery_exc: Exception | None = None
    try:
        await runtime.session.navigate(url=url, page=page)
        return page
    except Exception as exc:
        if not _is_retryable_navigation_error(exc):
            raise
        recovery_exc = exc

    await runtime.restart_browser_session(reason=_navigation_error_reason(recovery_exc))
    recovered_page = await runtime.ensure_page(force_new=True)
    try:
        await runtime.session.navigate(url=url, page=recovered_page)
        return recovered_page
    except Exception as retry_exc:
        if not _is_retryable_navigation_error(retry_exc):
            raise
        recovery_exc = retry_exc

    await runtime.restart_browser_session(reason=_navigation_error_reason(recovery_exc))
    recovered_page = await runtime.ensure_page(force_new=True)
    await runtime.session.navigate(url=url, page=recovered_page)
    return recovered_page


async def _recover_error_page(runtime: StagehandRuntime, url: str) -> str:
    first_error = await _page_error_message(runtime.page)
    if not first_error:
        return ""

    await sleep_ms(1500)
    await runtime.session.navigate(url=url, page=runtime.page)
    await sleep_ms(1200)
    await accept_cookies(runtime, runtime.page)
    return await _page_error_message(runtime.page)


async def _page_error_message(page: Any) -> str:
    try:
        snapshot = await page.evaluate(
            """() => ({
                title: (document.title || "").trim(),
                body: (document.body?.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 1200),
            })"""
        )
    except Exception:
        return ""

    if not isinstance(snapshot, dict):
        return ""

    title = str(snapshot.get("title") or "").strip()
    body = str(snapshot.get("body") or "").strip()
    haystack = f"{title}\n{body}".lower()

    if "bad gateway" in haystack or ("error code 502" in haystack) or ("what happened?" in haystack and "502" in haystack):
        return "Error page detected after retry: 502 Bad Gateway"
    if re.search(r"\b404\b", haystack) and "not found" in haystack:
        return "Error page detected after retry: 404 Not Found"
    if "access denied" in haystack or "forbidden" in haystack:
        return "Error page detected after retry: Access denied"
    return ""


def _is_closed_target_error(exc: PlaywrightError) -> bool:
    return "Target page, context or browser has been closed" in str(exc)


def _is_retryable_navigation_error(exc: Exception) -> bool:
    message = str(exc)
    return (
        "Target page, context or browser has been closed" in message
        or "navigate failed" in message.lower()
    )


def _navigation_error_reason(exc: Exception | None) -> str:
    message = str(exc or "").lower()
    if "navigate failed" in message:
        return "stagehand_navigate_failed"
    return "browser_stack_closed"
