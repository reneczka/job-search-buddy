from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, Field
from rich.console import Console

from .detail_cache import get_detail_cache
from .boards import board_alias
from .models import JobDetail
from .stagehand_session import StagehandRuntime, scoped_model_options, sleep_ms
from .url_discovery import accept_cookies, normalize_page_url


console = Console()
MIN_PRIMARY_CONTENT_CHARS = 450
MAX_FALLBACK_TEXT_CHARS = 12000
MAX_MAIN_TEXT_CHARS = 10000
DETAIL_CACHE_VERSION = "v6"
SIGNATURE_TEXT_CHARS = 2500
SIGNATURE_META_CHARS = 300
SIGNATURE_TITLE_CHARS = 200
VOLATILE_SIGNATURE_PATTERNS = (
    r"\b\d+\s+(?:min(?:ute)?s?|godz(?:iny)?|hours?|days?)\b",
    r"\b\d{1,2}:\d{2}\b",
)
MISSING_MARKERS = {"", "n/a", "na", "none", "null", "unknown", "not available", "not provided"}


class JobDetailPayload(BaseModel):
    company: str = ""
    position: str = ""
    salary: str = ""
    location: str = ""
    notes: str = ""
    requirements: list[str] = Field(default_factory=list)
    company_description: str = ""


@dataclass(frozen=True)
class PageContentSnapshot:
    selector: str = ""
    content_text: str = ""
    fallback_text: str = ""
    page_title: str = ""
    meta_description: str = ""
    content_chars: int = 0
    fallback_chars: int = 0
    used_selector_fallback: bool = False


async def extract_job_detail(runtime: StagehandRuntime, source_name: str, url: str) -> JobDetail:
    first_pass = await _extract_job_detail_once(runtime, source_name, url)
    if not _should_retry_detail(first_pass):
        _store_detail_cache(first_pass)
        return first_pass

    console.print(f"DETAIL_RETRY site={board_alias(source_name)} reason=broken_first_pass url={url}")
    try:
        retry_pass = await _extract_job_detail_once(runtime, source_name, url, force_new_page=True)
    except Exception as exc:
        first_pass.raw["retry_attempted"] = True
        first_pass.raw["retry_error"] = str(exc)
        return first_pass

    chosen = retry_pass if _detail_score(retry_pass) > _detail_score(first_pass) else first_pass
    chosen.raw["retry_attempted"] = True
    _store_detail_cache(chosen)
    return chosen


async def _extract_job_detail_once(
    runtime: StagehandRuntime,
    source_name: str,
    url: str,
    force_new_page: bool = False,
) -> JobDetail:
    page = await runtime.ensure_page(force_new=force_new_page)
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
            raw={"page_error": page_error, "retry_used": force_new_page},
        )

    content = await _capture_page_content(page)
    console.print(
        f"DETAIL_CONTENT selector={content.selector or '-'} "
        f"content_chars={content.content_chars} fallback_chars={content.fallback_chars}"
    )
    content_signature = _content_signature(content)
    cache_key = normalize_page_url(page.url) or url
    cached_payload = get_detail_cache().get(cache_key, content_signature)
    if cached_payload:
        console.print(f"DETAIL_CACHE_HIT=true url={cache_key}")
        return JobDetail(
            source=source_name,
            discovered_url=url,
            final_url=cache_key,
            company=_normalize_missing_marker(cached_payload.get("company")),
            position=_normalize_missing_marker(cached_payload.get("position")),
            salary=_normalize_missing_marker(cached_payload.get("salary")),
            location=_normalize_missing_marker(cached_payload.get("location")),
            notes=_normalize_missing_marker(cached_payload.get("notes")),
            requirements=_normalize_requirements(cached_payload.get("requirements") or []),
            company_description=_normalize_missing_marker(cached_payload.get("company_description")),
            raw={
                "cache_hit": True,
                "content_signature": content_signature,
                "content_selector": content.selector,
                "content_chars": content.content_chars,
                "fallback_chars": content.fallback_chars,
                "selector_fallback_used": False,
                "retry_used": force_new_page,
                "retry_attempted": False,
            },
        )

    payload, used_broad_fallback = await _extract_payload(runtime, content)
    fallback = await _fallback_page_data(page, content)

    company = _coalesce_value(payload.get("company"), fallback.get("company"))
    position = _coalesce_value(payload.get("position"), fallback.get("position"))
    salary = _coalesce_value(payload.get("salary"), fallback.get("salary"))
    location = _coalesce_value(payload.get("location"), fallback.get("location"))
    notes = _coalesce_value(payload.get("notes"), fallback.get("notes"))
    company_description = _coalesce_value(payload.get("company_description"), fallback.get("company_description"))

    requirements: list[str] = []
    raw_requirements = payload.get("requirements")
    if isinstance(raw_requirements, list):
        requirements = _normalize_requirements(raw_requirements)
    if not requirements:
        fallback_requirements = fallback.get("requirements")
        if isinstance(fallback_requirements, list):
            requirements = _normalize_requirements(fallback_requirements)

    return JobDetail(
        source=source_name,
        discovered_url=url,
        final_url=normalize_page_url(page.url) or url,
        company=company,
        position=position,
        salary=salary,
        location=location,
        notes=notes,
        requirements=requirements,
        company_description=company_description,
        raw={
            "llm_payload": dict(payload),
            "content_selector": content.selector,
            "content_chars": content.content_chars,
            "fallback_chars": content.fallback_chars,
            "selector_fallback_used": used_broad_fallback,
            "content_signature": content_signature,
            "cache_hit": False,
            "retry_used": force_new_page,
            "retry_attempted": False,
        },
    )


async def _extract_payload(runtime: StagehandRuntime, content: PageContentSnapshot) -> tuple[dict[str, Any], bool]:
    used_broad_fallback = False
    selector = content.selector if content.content_chars >= MIN_PRIMARY_CONTENT_CHARS else ""
    instruction = (
        "Extract job details from this job offer page. "
        "Focus on the main offer content only. Ignore navigation, headers, footers, cookie banners, "
        "related jobs, sidebars, and marketing content. "
        "Return concise factual plain text only. Do not guess. "
        "Return 'N/A' for missing scalar fields. "
        "Return an empty list for missing requirements. "
        "Use these field rules strictly: "
        "company: employer name only. "
        "position: core job title only; remove decorative suffixes or labels that are not part of the role. "
        "salary: return visible salary text only, stripped of labels like 'Salary' or 'Wynagrodzenie'; "
        "if multiple contract-specific salary variants are explicitly shown, keep them in one concise field; "
        "if hidden, placeholder, or unspecified, return 'N/A'. "
        "location: city name only when a city is explicitly shown; remove street names, building numbers, districts, postal codes, "
        "country-only labels, and UI labels like 'Miejsce pracy'. "
        "If multiple cities are explicitly listed, keep all city names only in one concise comma-separated field. "
        "If no city is shown but the work mode is explicit, return only 'Remote', 'Hybrid', or 'On-site'. "
        "requirements: only explicit requirements as short bullet-ready items; no section labels like 'Soft skills', "
        "'Technical skills', 'Optional', 'Nice to have', or duplicated items. "
        "Each list item must contain exactly one requirement only. "
        "Do not combine multiple requirements into one item with commas, semicolons, slashes, or 'and'. "
        "Split grouped skills or expectations into separate items whenever they are independently understandable. "
        "Split grouped location-eligibility requirements like 'Located in CityA/CityB/CityC' into separate items. "
        "Do not split shared-prefix requirements when splitting would repeat the same lead-in or lose meaning, "
        "for example phrases like 'two or more of the following'. Keep those as one coherent item. "
        "Never drop explicit technologies, frameworks, tools, or languages from the source text. "
        "When a requirement line lists named basics after a shared prefix like 'basic knowledge of' or "
        "'Znasz podstawy:', keep every named technology as its own item, for example Python, Django, SQL, Git. "
        "Do not include responsibilities, tasks, benefits, or generic filler if they are not real requirements. "
        "If the same skill appears both as a short standalone item and as part of a richer requirement, keep only the richer item. "
        "Keep each item short and concrete. "
        "notes: concise leftover job details only, such as contract type, work mode, schedule, benefits, recruitment steps; "
        "do not dump long page text. "
        "company_description: 1-2 short factual sentences about the employer only; do not include job-description text, "
        "application instructions, platform chrome, or marketing boilerplate. "
        "If no reliable company description is present, or the only text is the company name, return 'N/A'."
    )

    try:
        extracted = await runtime.session.extract(
            instruction=instruction,
            schema=JobDetailPayload.model_json_schema(),
            options=scoped_model_options(runtime.model_opts, selector),
            page=runtime.page,
        )
        payload = getattr(extracted.data, "result", None)
        result = payload if isinstance(payload, dict) else {}
    except Exception:
        result = {}

    if selector and _should_broaden_extraction(content, result):
        try:
            extracted = await runtime.session.extract(
                instruction=instruction,
                schema=JobDetailPayload.model_json_schema(),
                options=runtime.model_opts,
                page=runtime.page,
            )
            payload = getattr(extracted.data, "result", None)
            result = payload if isinstance(payload, dict) else result
            used_broad_fallback = True
            console.print("DETAIL_SELECTOR_FALLBACK=broad_page")
        except Exception:
            pass

    return result, used_broad_fallback


async def _fallback_page_data(page: Any, content: PageContentSnapshot) -> dict[str, Any]:
    try:
        payload = await page.evaluate(
            """() => {
                const text = (el) => (el?.textContent || "").trim();
                const meta = (name) => document.querySelector(`meta[name="${name}"], meta[property="${name}"]`)?.content || "";
                const normalize = (value) => (value || "").replace(/\u00a0/g, " ").replace(/\s+/g, " ").trim();
                const root = document.querySelector("main, article, [role='main']") || document.body;
                const requirementPatterns = [
                  /requirements?/i,
                  /qualifications?/i,
                  /what you(?:'|’)ll need/i,
                  /what we(?:'|’)re looking for/i,
                  /must have/i,
                  /nice to have/i,
                  /od kandydatów/i,
                  /oczekujemy/i,
                  /poszukiwane kompetencje/i,
                  /wymagania/i,
                  /mile widziane/i,
                  /atutami będą/i,
                ];
                const stopPatterns = [
                  /responsibilit/i,
                  /duties/i,
                  /what you(?:'|’)ll do/i,
                  /zakres obowiązków/i,
                  /oferujemy/i,
                  /benefits?/i,
                  /świadczenia/i,
                ];
                const collectPreviousLabel = (node) => {
                  let current = node;
                  while (current && current !== root) {
                    let sibling = current.previousSibling;
                    while (sibling) {
                      if (sibling.nodeType === Node.TEXT_NODE) {
                        const raw = normalize(sibling.textContent || "");
                        if (raw && raw.length <= 120) {
                          return raw;
                        }
                      } else if (sibling.nodeType === Node.ELEMENT_NODE) {
                        const raw = normalize(sibling.innerText || sibling.textContent || "");
                        if (raw && raw.length <= 120) {
                          return raw;
                        }
                      }
                      sibling = sibling.previousSibling;
                    }
                    current = current.parentElement;
                  }
                  return "";
                };
                const requirementItems = [];
                const seen = new Set();
                for (const list of root.querySelectorAll("ul, ol")) {
                  const label = collectPreviousLabel(list);
                  if (!label) continue;
                  if (stopPatterns.some((pattern) => pattern.test(label))) continue;
                  if (!requirementPatterns.some((pattern) => pattern.test(label))) continue;
                  for (const item of list.querySelectorAll(":scope > li")) {
                    const raw = normalize(item.innerText || item.textContent || "");
                    if (!raw || raw.length < 2 || raw.length > 280) continue;
                    const key = raw.toLowerCase();
                    if (seen.has(key)) continue;
                    seen.add(key);
                    requirementItems.push(raw);
                  }
                }
                const h1 = text(document.querySelector("h1"));
                const title = document.title || "";
                const subtitle = text(document.querySelector("h2, [class*='company'], [data-testid*='company']"));
                const paragraphs = Array.from(document.querySelectorAll("main p, article p"))
                  .map((node) => text(node))
                  .filter(Boolean);
                return {
                  position: h1 || title.split("|")[0] || "",
                  company: subtitle,
                  salary: "",
                  location: "",
                  notes: meta("description") || paragraphs[0] || "",
                  company_description: paragraphs.slice(0, 2).join(" "),
                  requirements: requirementItems,
                };
            }"""
        )
        parsed = payload if isinstance(payload, dict) else {}
    except Exception:
        parsed = {}

    notes = _coalesce_value(
        parsed.get("notes"),
        content.meta_description,
        _first_meaningful_sentence(content.content_text),
        _first_meaningful_sentence(content.fallback_text),
    )
    company_description = _coalesce_value(
        parsed.get("company_description"),
        _first_paragraphs(content.content_text, limit=2),
    )

    return {
        "company": _clean(parsed.get("company")),
        "position": _clean(parsed.get("position")),
        "salary": _clean(parsed.get("salary")),
        "location": _clean(parsed.get("location")),
        "notes": notes,
        "company_description": company_description,
        "requirements": parsed.get("requirements") if isinstance(parsed.get("requirements"), list) else [],
    }


async def _capture_page_content(page: Any) -> PageContentSnapshot:
    try:
        payload = await page.evaluate(
            f"""() => {{
                const maxMainChars = {MAX_MAIN_TEXT_CHARS};
                const maxFallbackChars = {MAX_FALLBACK_TEXT_CHARS};
                const excludedSelector = [
                  "nav", "header", "footer", "aside", "form", "script", "style", "noscript", "svg", "iframe",
                  "[role='navigation']",
                  "[class*='cookie' i]", "[id*='cookie' i]",
                  "[class*='consent' i]", "[id*='consent' i]",
                  "[class*='newsletter' i]", "[id*='newsletter' i]",
                  "[class*='recommend' i]", "[id*='recommend' i]",
                  "[class*='related' i]", "[id*='related' i]",
                  "[class*='share' i]", "[id*='share' i]",
                  "[class*='social' i]", "[id*='social' i]",
                  "[class*='banner' i]", "[id*='banner' i]",
                  "[class*='promo' i]", "[id*='promo' i]",
                  "[class*='advert' i]", "[id*='advert' i]"
                ].join(", ");
                const normalizeText = (value) => (value || "")
                  .replace(/\\u00a0/g, " ")
                  .replace(/\\s+/g, " ")
                  .trim();
                const isVisible = (el) => {{
                  if (!el) return false;
                  const style = window.getComputedStyle(el);
                  if (!style || style.display === "none" || style.visibility === "hidden") return false;
                  const rect = el.getBoundingClientRect();
                  return rect.width > 0 && rect.height > 0;
                }};
                const cloneAndClean = (node) => {{
                  const clone = node.cloneNode(true);
                  clone.querySelectorAll(excludedSelector).forEach((child) => child.remove());
                  return clone;
                }};
                const cleanedText = (node, limit) => normalizeText(cloneAndClean(node).innerText || "").slice(0, limit);
                const buildSelector = (el) => {{
                  if (!el || el === document.body) return "body";
                  if (el.id) return `#${{CSS.escape(el.id)}}`;
                  const parts = [];
                  let current = el;
                  while (current && current.nodeType === Node.ELEMENT_NODE && current !== document.body && parts.length < 6) {{
                    let part = current.tagName.toLowerCase();
                    const parent = current.parentElement;
                    if (parent) {{
                      const siblings = Array.from(parent.children).filter((child) => child.tagName === current.tagName);
                      if (siblings.length > 1) {{
                        part += `:nth-of-type(${{siblings.indexOf(current) + 1}})`;
                      }}
                    }}
                    parts.unshift(part);
                    current = current.parentElement;
                  }}
                  return parts.length ? parts.join(" > ") : "body";
                }};
                const scoreCandidate = (el) => {{
                  const textValue = cleanedText(el, maxMainChars);
                  if (textValue.length < 180) return null;
                  const headings = el.querySelectorAll("h1, h2, h3").length;
                  const blocks = el.querySelectorAll("p, li").length;
                  const links = normalizeText(
                    Array.from(el.querySelectorAll("a"))
                      .map((node) => node.textContent || "")
                      .join(" ")
                  ).length;
                  const linkPenalty = Math.min(Math.floor(links * 0.35), textValue.length);
                  const score = textValue.length + (headings * 120) + (blocks * 35) - linkPenalty;
                  return {{
                    selector: buildSelector(el),
                    text: textValue,
                    chars: textValue.length,
                    score,
                  }};
                }};

                const preferred = ["main", "article", "[role='main']", "main article"];
                const candidates = [];
                const seen = new Set();

                for (const selector of preferred) {{
                  const el = document.querySelector(selector);
                  if (!el || !isVisible(el)) continue;
                  const scored = scoreCandidate(el);
                  if (scored && !seen.has(scored.selector)) {{
                    candidates.push(scored);
                    seen.add(scored.selector);
                  }}
                }}

                const blocks = Array.from(document.querySelectorAll("section, div"));
                for (const el of blocks.slice(0, 400)) {{
                  if (!isVisible(el)) continue;
                  const scored = scoreCandidate(el);
                  if (scored && !seen.has(scored.selector)) {{
                    candidates.push(scored);
                    seen.add(scored.selector);
                  }}
                }}

                candidates.sort((a, b) => b.score - a.score);
                const best = candidates[0] || null;
                const bodyText = cleanedText(document.body, maxFallbackChars);
                return {{
                  selector: best?.selector || "",
                  contentText: best?.text || "",
                  fallbackText: bodyText,
                  pageTitle: normalizeText(document.title || ""),
                  metaDescription: normalizeText(
                    document.querySelector("meta[name='description'], meta[property='description'], meta[property='og:description']")?.content || ""
                  ),
                  contentChars: best?.chars || 0,
                  fallbackChars: bodyText.length,
                  usedSelectorFallback: !best,
                }};
            }}"""
        )
    except Exception:
        payload = None

    if not isinstance(payload, dict):
        return PageContentSnapshot()

    return PageContentSnapshot(
        selector=_clean(payload.get("selector")),
        content_text=_clean(payload.get("contentText")),
        fallback_text=_clean(payload.get("fallbackText")),
        page_title=_clean(payload.get("pageTitle")),
        meta_description=_clean(payload.get("metaDescription")),
        content_chars=int(payload.get("contentChars") or 0),
        fallback_chars=int(payload.get("fallbackChars") or 0),
        used_selector_fallback=bool(payload.get("usedSelectorFallback")),
    )


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _coalesce_value(*values: Any) -> str:
    for value in values:
        cleaned = _normalize_missing_marker(value)
        if cleaned:
            return cleaned
    return ""


def _normalize_missing_marker(value: Any) -> str:
    cleaned = _clean(value)
    return "" if cleaned.lower() in MISSING_MARKERS else cleaned


def _normalize_requirements(values: list[Any]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _normalize_missing_marker(value)
        if not cleaned:
            continue
        cleaned = re.sub(r"^[\-\*\u2022]+\s*", "", cleaned).strip()
        key = cleaned.lower()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        normalized.append(cleaned)
    return normalized


def _should_retry_detail(detail: JobDetail) -> bool:
    final_url = (detail.final_url or "").lower()
    content_chars = int(detail.raw.get("content_chars") or 0)
    has_company = bool(_normalize_missing_marker(detail.company))
    has_position = bool(_normalize_missing_marker(detail.position))
    has_requirements = bool(detail.requirements)
    suspicious_path = any(
        marker in final_url
        for marker in ("/login", "signin", "authorize", "callback", "account", "privacy", "policy", "legal", "consent")
    )
    if suspicious_path:
        return True
    if (not has_company or not has_position) and not has_requirements:
        return True
    if content_chars and content_chars < MIN_PRIMARY_CONTENT_CHARS and not has_requirements:
        return True
    return False


def _detail_score(detail: JobDetail) -> int:
    score = 0
    for value in (detail.company, detail.position, detail.salary, detail.location, detail.notes, detail.company_description):
        if _normalize_missing_marker(value):
            score += 1
    score += len(detail.requirements)
    final_url = (detail.final_url or "").lower()
    if any(marker in final_url for marker in ("/login", "signin", "authorize", "callback", "account", "privacy", "policy", "legal", "consent")):
        score -= 5
    if detail.raw.get("page_error"):
        score -= 5
    return score


def _content_signature(content: PageContentSnapshot) -> str:
    primary_text = _signature_text(content.content_text or content.fallback_text, limit=SIGNATURE_TEXT_CHARS)
    title_text = _signature_text(content.page_title, limit=SIGNATURE_TITLE_CHARS)
    meta_text = _signature_text(content.meta_description, limit=SIGNATURE_META_CHARS)
    payload = "\n".join((DETAIL_CACHE_VERSION, primary_text, title_text, meta_text))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _signature_text(value: str, *, limit: int) -> str:
    cleaned = _clean(value)
    if not cleaned:
        return ""
    cleaned = re.sub(r"\s+", " ", cleaned)
    for pattern in VOLATILE_SIGNATURE_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()[:limit]


def _store_detail_cache(detail: JobDetail) -> None:
    signature = str(detail.raw.get("content_signature") or "").strip()
    cache_key = normalize_page_url(detail.final_url or detail.discovered_url)
    if not signature or not cache_key or detail.raw.get("page_error") or _should_retry_detail(detail):
        return
    get_detail_cache().set(
        cache_key,
        signature,
        {
            "company": detail.company,
            "position": detail.position,
            "salary": detail.salary,
            "location": detail.location,
            "notes": detail.notes,
            "requirements": list(detail.requirements),
            "company_description": detail.company_description,
        },
    )


def _should_broaden_extraction(content: PageContentSnapshot, payload: dict[str, Any]) -> bool:
    if content.content_chars >= MIN_PRIMARY_CONTENT_CHARS:
        return False
    if content.fallback_chars <= content.content_chars:
        return False
    useful_values = 0
    for key in ("company", "position", "salary", "location", "notes", "company_description"):
        if _normalize_missing_marker(payload.get(key)):
            useful_values += 1
    requirements = payload.get("requirements")
    if isinstance(requirements, list) and _normalize_requirements(requirements):
        useful_values += 1
    return useful_values < 2


def _first_meaningful_sentence(text: str) -> str:
    cleaned = _normalize_missing_marker(text)
    if not cleaned:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    for sentence in sentences:
        candidate = sentence.strip()
        if len(candidate) >= 40:
            return candidate[:280]
    return cleaned[:280]


def _first_paragraphs(text: str, limit: int) -> str:
    cleaned = _normalize_missing_marker(text)
    if not cleaned:
        return ""
    parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", cleaned) if part.strip()]
    return " ".join(parts[:limit])[:400]


async def _navigate_with_page_recovery(runtime: StagehandRuntime, page: Any, url: str) -> Any:
    try:
        await runtime.session.navigate(url=url, page=page)
        return page
    except PlaywrightError as exc:
        if not _is_closed_target_error(exc):
            raise

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
