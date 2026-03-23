from __future__ import annotations

import re
import time
from collections import Counter
from typing import Any, Optional
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from playwright.async_api import Page
from pydantic import BaseModel, Field
from rich.console import Console

from .models import BoardConfig, DiscoveryMetadata, DiscoveryResult
from .stagehand_session import (
    StagehandRuntime,
    console as shared_console,
    env_str,
    scoped_model_options,
    sleep_ms,
)


console: Console = shared_console

INDEED_SEARCH_TERM = "python junior"
INDEED_EXPECTED_VISIBLE_OFFERS = 15
SAFETY_STEP_LIMIT = 250
JJ_FIRST_VISIBLE_OFFER_URL = "https://justjoin.it/job-offer/epam-systems-python-engineering-trainee-poland-remote--python"
NF_FIRST_VISIBLE_OFFER_URL = (
    "https://nofluffjobs.com/pl/job/software-engineer-early-careers-programme-tesco-technology-krakow"
)
BD_FIRST_VISIBLE_OFFER_URL = "https://bulldogjob.pl/companies/jobs/230066-project-manager-ai-and-innovation-warsaw-teamquest"


class OfferUrls(BaseModel):
    urls: list[str] = Field(default_factory=list)


class NextPageCandidate(BaseModel):
    url: str = ""


class OfferPattern(BaseModel):
    first_url: str
    include_tokens: list[str] = Field(default_factory=list)
    exclude_tokens: list[str] = Field(default_factory=list)


class LlmInferredPattern(BaseModel):
    first_offer_url: str = ""
    include_path_tokens: list[str] = Field(default_factory=list)
    exclude_path_tokens: list[str] = Field(default_factory=list)


def log_offer_pattern(pattern: Optional["OfferPattern"]) -> None:
    if not pattern:
        return
    console.print(f"FIRST_GOOD_OFFER_URL={pattern.first_url}")
    console.print(f"OFFER_URL_PATTERN_INCLUDE_TOKENS={','.join(pattern.include_tokens) or '-'}")
    console.print(f"OFFER_URL_PATTERN_EXCLUDE_TOKENS={','.join(pattern.exclude_tokens) or '-'}")


def log_job_link_selector(selector: Optional[str]) -> None:
    if selector:
        console.print(f"JOB_LINK_SELECTOR={selector}", markup=False)


def as_action_dicts(items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not items:
        return out
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                out.append(item)
            elif hasattr(item, "model_dump"):
                out.append(item.model_dump(by_alias=True, exclude_none=True))
    return out


def normalize_domain(domain: str) -> str:
    return domain.lower().removeprefix("www.")


def is_pracuj_domain(domain: str) -> bool:
    normalized = normalize_domain(domain)
    return normalized == "pracuj.pl" or normalized.endswith(".pracuj.pl")


def is_indeed_domain(domain: str) -> bool:
    normalized = normalize_domain(domain)
    return normalized == "indeed.com" or normalized.endswith(".indeed.com")


def normalize_offer_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def normalize_indeed_offer_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def normalize_page_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def path_family(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    return parts[0].lower() if parts else ""


def is_same_listing_family(url: str, domain: str, listing_family_name: str) -> bool:
    try:
        parsed = urlsplit(url)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if normalize_domain(parsed.netloc) != normalize_domain(domain):
        return False
    if not listing_family_name:
        return True
    return path_family(parsed.path) == listing_family_name


def infer_offer_pattern_from_url(first_url: str) -> Optional[OfferPattern]:
    if not first_url:
        return None
    path_parts = [part for part in urlsplit(first_url).path.split("/") if part]
    include_tokens: list[str] = []
    if path_parts:
        include_tokens.append(path_parts[0].lower())
    if len(path_parts) >= 2:
        second = path_parts[1].lower()
        is_slug_like = len(second) >= 18 and ("-" in second or re.search(r"\d", second) is not None)
        if not is_slug_like:
            include_tokens.append(second)
    return OfferPattern(first_url=first_url, include_tokens=include_tokens, exclude_tokens=[])


def infer_offer_pattern_from_candidates(candidates: list[str]) -> Optional[OfferPattern]:
    counts: dict[tuple[str, str], int] = {}
    grouped: dict[tuple[str, str], list[str]] = {}
    for url in candidates:
        path_parts = [part.lower() for part in urlsplit(url).path.split("/") if part]
        if len(path_parts) < 2:
            continue
        key = (path_parts[0], path_parts[1])
        counts[key] = counts.get(key, 0) + 1
        grouped.setdefault(key, []).append(url)
    if not counts:
        return None
    dominant = max(counts, key=counts.get)
    sample_url = grouped[dominant][0]
    return OfferPattern(first_url=sample_url, include_tokens=[dominant[0], dominant[1]], exclude_tokens=[])


def _path_text(url: str) -> str:
    return (urlsplit(url).path or "").lower()


def _token_support(token: str, candidates: list[str]) -> int:
    normalized = token.strip().lower()
    if not normalized:
        return 0
    return sum(1 for url in candidates if normalized in _path_text(url))


def _pattern_match_count(pattern: Optional[OfferPattern], candidates: list[str]) -> int:
    if pattern is None:
        return 0
    return sum(1 for url in candidates if matches_offer_pattern(url, pattern))


def _best_structural_offer_pattern(first_url: str, candidates: list[str]) -> Optional[OfferPattern]:
    options = [
        infer_offer_pattern_from_url(first_url) if first_url else None,
        infer_offer_pattern_from_candidates(candidates),
    ]
    best: Optional[OfferPattern] = None
    best_count = -1
    for option in options:
        count = _pattern_match_count(option, candidates)
        if count > best_count:
            best = option
            best_count = count
    return best


def validate_offer_pattern(pattern: Optional[OfferPattern], candidates: list[str]) -> Optional[OfferPattern]:
    if pattern is None or not candidates:
        return pattern

    min_reasonable_matches = 2 if len(candidates) >= 5 else 1
    current_count = _pattern_match_count(pattern, candidates)
    if current_count >= min_reasonable_matches:
        return pattern

    fallback = _best_structural_offer_pattern(pattern.first_url, candidates)
    if _pattern_match_count(fallback, candidates) > current_count:
        return fallback
    return pattern if current_count else fallback


def matches_offer_pattern(url: str, pattern: Optional[OfferPattern]) -> bool:
    if pattern is None:
        return True
    path = (urlsplit(url).path or "").lower()

    def has_token(token: str) -> bool:
        normalized = token.strip().lower()
        return bool(normalized and normalized in path)

    if pattern.include_tokens and not all(has_token(token) for token in pattern.include_tokens):
        return False
    if pattern.exclude_tokens and any(has_token(token) for token in pattern.exclude_tokens):
        return False
    return True


def is_offer_candidate_url(url: str, domain: str) -> bool:
    try:
        parsed = urlsplit(url)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if normalize_domain(parsed.netloc) != normalize_domain(domain):
        return False
    path = (parsed.path or "").strip().lower()
    if not path or path == "/":
        return False
    parts = [part for part in path.split("/") if part]
    blocked_segments = {"login", "signin", "account", "saved", "register", "privacy", "cookies"}
    if parts and parts[0] in blocked_segments:
        return False

    domain_norm = normalize_domain(domain)
    if domain_norm == "justjoin.it":
        return path.startswith("/job-offer/")
    if domain_norm == "theprotocol.it":
        return path.startswith("/szczegoly/praca/")
    if domain_norm == "nofluffjobs.com":
        return path.startswith("/pl/job/")
    if domain_norm == "bulldogjob.pl":
        if not path.startswith("/companies/jobs/"):
            return False
        tail = parts[2] if len(parts) >= 3 else ""
        if not tail or tail == "s":
            return False
        return bool(re.search(r"\d", tail) and "-" in tail)
    if is_pracuj_domain(domain_norm):
        return path.startswith("/praca/") and ",oferta," in path
    if is_indeed_domain(domain_norm):
        return path.rstrip("/") == "/rc/clk" and bool(parse_qs(parsed.query).get("jk"))
    return True


async def listing_signature(page: Page) -> str:
    try:
        payload = await page.evaluate(
            """() => {
                const anchors = Array.from(document.querySelectorAll("a[href]"));
                const hrefs = anchors
                  .map((a) => (a.getAttribute("href") || a.href || "").trim())
                  .filter(Boolean)
                  .slice(0, 120);
                const scrollHeight = document.body ? document.body.scrollHeight : 0;
                return {
                  url: window.location.href,
                  hrefCount: anchors.length,
                  hrefSample: hrefs.join("|"),
                  scrollHeight,
                };
            }"""
        )
    except Exception:
        return normalize_page_url(page.url)
    if not isinstance(payload, dict):
        return normalize_page_url(page.url)
    return (
        f"{normalize_page_url(str(payload.get('url') or page.url))}|"
        f"{int(payload.get('hrefCount') or 0)}|"
        f"{int(payload.get('scrollHeight') or 0)}|"
        f"{str(payload.get('hrefSample') or '')}"
    )


async def read_results_header_count(page: Page) -> Optional[int]:
    try:
        value = await page.evaluate(
            """() => {
                const text = (document.body && document.body.innerText) || "";
                const patterns = [
                  /wyniki\\s*\\((\\d+)\\s+ofert\\)/i,
                  /results\\s*\\((\\d+)\\s+offers\\)/i,
                  /(\\d+)\\s+ofert\\b/i,
                  /(\\d+)\\s+offers\\b/i
                ];
                for (const pattern of patterns) {
                  const match = text.match(pattern);
                  if (match && match[1]) return Number(match[1]);
                }
                return null;
            }"""
        )
    except Exception:
        return None
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


async def infer_offer_pattern_with_llm(
    runtime: StagehandRuntime,
    page: Page,
    domain: str,
    candidate_urls: list[str],
) -> Optional[OfferPattern]:
    if runtime.session.should_skip("extract"):
        return None
    allowed = [url for url in candidate_urls if is_offer_candidate_url(url, domain)]
    if not allowed:
        return None
    candidates_text = "\n".join(f"- {url}" for url in allowed[:40])
    try:
        extracted = await runtime.session.extract(
            instruction=(
                "Select ONE real final job-offer detail URL only from the CANDIDATE URLs list below.\n"
                "Do not invent URL. Use exact URL from the list.\n\n"
                f"CANDIDATE URLs:\n{candidates_text}\n\n"
                "Then infer dynamic URL pattern rules for this board only:\n"
                "- include_path_tokens: short path substrings that real offer detail URLs usually contain\n"
                "- exclude_path_tokens: short path substrings that non-offer pages contain\n"
                "Return absolute URL in first_offer_url."
            ),
            schema=LlmInferredPattern.model_json_schema(),
            options=runtime.model_opts,
            page=page,
        )
        payload = getattr(extracted.data, "result", None)
        if not isinstance(payload, dict):
            return None
        first_url = str(payload.get("first_offer_url") or "").strip()
        if first_url.startswith("/"):
            first_url = urljoin(page.url, first_url)
        if first_url not in allowed:
            first_url = allowed[0]
        first_path = _path_text(first_url)
        include = [
            str(token).strip().lower()
            for token in (payload.get("include_path_tokens") or [])
            if isinstance(token, str) and str(token).strip()
        ]
        exclude = [
            str(token).strip().lower()
            for token in (payload.get("exclude_path_tokens") or [])
            if isinstance(token, str) and str(token).strip()
        ]
        min_include_support = 2 if len(allowed) >= 5 else 1
        include = [
            token
            for token in include[:4]
            if _token_support(token, allowed) >= min_include_support
        ]
        exclude = [
            token
            for token in exclude[:8]
            if token not in first_path and _token_support(token, allowed) == 0
        ]
        if not include:
            return _best_structural_offer_pattern(first_url, allowed)
        pattern = OfferPattern(first_url=first_url, include_tokens=include, exclude_tokens=exclude)
        return validate_offer_pattern(pattern, allowed)
    except Exception:
        return None


async def extract_offer_seed_urls_with_llm(
    runtime: StagehandRuntime,
    page: Page,
    domain: str,
    listing_selector: Optional[str] = None,
) -> list[str]:
    if runtime.session.should_skip("extract"):
        return []
    try:
        extracted = await runtime.session.extract(
            instruction=(
                "Extract visible URLs for final real job-offer detail pages from listing results only. "
                "Exclude categories, filters, login, account, blog, salary pages, sponsored/promoted/recommended blocks."
            ),
            schema=OfferUrls.model_json_schema(),
            options=scoped_model_options(runtime.model_opts, listing_selector),
            page=page,
        )
        payload = getattr(extracted.data, "result", None)
    except Exception:
        payload = None

    raw = payload.get("urls") if isinstance(payload, dict) else []
    return _normalize_offer_list(raw, page, domain)


async def filter_offer_candidates_with_llm(
    runtime: StagehandRuntime,
    page: Page,
    domain: str,
    candidate_urls: list[str],
) -> list[str]:
    if runtime.session.should_skip("extract"):
        return []
    allowed = [url for url in candidate_urls if is_offer_candidate_url(url, domain)]
    if not allowed:
        return []
    candidates_text = "\n".join(f"- {url}" for url in allowed[:120])
    try:
        extracted = await runtime.session.extract(
            instruction=(
                "From the candidate URL list below, return only real final job-offer detail URLs. "
                "Exclude category, filter, search, account, blog, legal, login, and marketing pages.\n\n"
                f"CANDIDATE URLs:\n{candidates_text}"
            ),
            schema=OfferUrls.model_json_schema(),
            options=runtime.model_opts,
            page=page,
        )
        payload = getattr(extracted.data, "result", None)
    except Exception:
        payload = None

    raw = payload.get("urls") if isinstance(payload, dict) else []
    normalized = _normalize_offer_list(raw, page, domain)
    return [url for url in normalized if url in allowed]


async def discover_next_listing_page_with_llm(
    runtime: StagehandRuntime,
    page: Page,
    domain: str,
    visited_pages: set[str],
    inferred_pattern: Optional[OfferPattern] = None,
) -> Optional[str]:
    if runtime.session.should_skip("extract"):
        return None
    try:
        extracted = await runtime.session.extract(
            instruction=(
                "Find URL of the next listing results page that should reveal more real offers. "
                "Return empty url if there is no next page. "
                "Do not return offer-detail URLs, login/account pages, or filter URLs."
            ),
            schema=NextPageCandidate.model_json_schema(),
            options=runtime.model_opts,
            page=page,
        )
        payload = getattr(extracted.data, "result", None)
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        return None
    raw = str(payload.get("url") or "").strip()
    if not raw:
        return None
    candidate = urljoin(page.url, raw) if raw.startswith("/") else raw
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"}:
        return None
    if normalize_domain(parsed.netloc) != normalize_domain(domain):
        return None
    normalized = normalize_page_url(candidate)
    if normalized in visited_pages:
        return None
    path = (parsed.path or "").lower()
    if any(token in path for token in ("login", "signin", "account", "saved", "register")):
        return None
    if inferred_pattern is not None and matches_offer_pattern(normalized, inferred_pattern):
        return None
    return normalized


async def recover_missing_offers_with_llm(
    runtime: StagehandRuntime,
    page: Page,
    domain: str,
    seen_urls: set[str],
) -> list[str]:
    if runtime.session.should_skip("extract"):
        return []
    seen_preview = "\n".join(f"- {url}" for url in sorted(seen_urls)[:120])
    try:
        extracted = await runtime.session.extract(
            instruction=(
                "Find real final job-offer detail URLs visible on this page that are NOT in the KNOWN URL list. "
                "Exclude non-offer links.\n\n"
                f"KNOWN URLS:\n{seen_preview}"
            ),
            schema=OfferUrls.model_json_schema(),
            options=runtime.model_opts,
            page=page,
        )
        payload = getattr(extracted.data, "result", None)
    except Exception:
        payload = None

    raw = payload.get("urls") if isinstance(payload, dict) else []
    normalized = _normalize_offer_list(raw, page, domain)
    return [url for url in normalized if url not in seen_urls]


def choose_reveal_action(actions: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not actions:
        return None
    best: Optional[dict[str, Any]] = None
    best_score = -9999
    positive = ("load more", "show more", "next", "nast", "dalej", "więcej", "pagin")
    negative = ("sponsor", "promo", "recommended", "filter", "sort", "login", "share", "prev", "back")
    for action in actions:
        text = " ".join(str(value) for value in action.values()).lower()
        score = 0
        if "click" in text:
            score += 1
        if any(token in text for token in positive):
            score += 6
        if any(token in text for token in negative):
            score -= 8
        if score > best_score:
            best_score = score
            best = action
    return best if best_score >= 2 else None


def merge_unique_urls(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for url in group:
            if url in seen:
                continue
            seen.add(url)
            merged.append(url)
    return merged


async def discover_listing_selector(runtime: StagehandRuntime, page: Page) -> Optional[str]:
    if runtime.session.should_skip("observe"):
        return None
    try:
        observed = await runtime.session.observe(
            instruction=(
                "Find one safe click action inside the main job-results listing area (not header/footer/sidebar). "
                "Return one action."
            ),
            options=runtime.model_opts,
            page=page,
        )
        actions = as_action_dicts(getattr(observed.data, "result", []) or [])
        for action in actions:
            selector = str(action.get("selector") or "").strip()
            if selector:
                return selector
    except Exception:
        return None
    return None


async def accept_cookies(runtime: StagehandRuntime, page: Page) -> bool:
    if not runtime.session.should_skip("observe"):
        try:
            observed = await runtime.session.observe(
                instruction=(
                    "Find cookie consent accept action (Accept/Accept all/Zgadzam/Akceptuj). "
                    "Return only one safe click action if present."
                ),
                options=runtime.model_opts,
                page=page,
            )
            actions = as_action_dicts(getattr(observed.data, "result", []) or [])
            if actions:
                await runtime.session.act(input=actions[0], page=page)
                await sleep_ms(500)
                return True
        except Exception:
            pass

    try:
        clicked = await page.evaluate(
            """() => {
                const labels = [
                  "accept", "accept all", "agree", "allow all", "got it",
                  "zgadzam", "akcept", "zaakceptuj", "akceptuj", "akceptuję", "rozumiem"
                ];
                const nodes = Array.from(document.querySelectorAll("button, [role='button'], a"));
                for (const el of nodes) {
                  const txt = (el.textContent || "").trim().toLowerCase().replace(/\\s+/g, " ");
                  if (!txt) continue;
                  if (!labels.some((label) => txt.includes(label))) continue;
                  const disabled = el.hasAttribute("disabled") || el.getAttribute("aria-disabled") === "true";
                  if (disabled) continue;
                  el.click();
                  return true;
                }
                return false;
            }"""
        )
        if clicked:
            await sleep_ms(500)
        return bool(clicked)
    except Exception:
        return False


async def dismiss_pracuj_popups(page: Page) -> None:
    for label in ("Zamknij", "Akceptuj wszystkie"):
        try:
            button = page.get_by_role("button", name=label)
            if await button.count():
                await button.first.click(timeout=1500)
                await sleep_ms(400)
        except Exception:
            pass


async def search_indeed(runtime: StagehandRuntime, page: Page) -> None:
    await runtime.session.execute(
        execute_options={
            "instruction": (
                f'On the Indeed homepage, enter "{INDEED_SEARCH_TERM}" into the job title or keywords search field, '
                "leave location empty, and submit the search. Stay on the first results page."
            ),
            "max_steps": 3,
        },
        agent_config={"model": runtime.model_name},
        should_cache=runtime.cache_enabled,
        page=page,
    )
    await sleep_ms(1800)
    if "/jobs" in page.url:
        return

    inputs = page.get_by_role("combobox")
    if await inputs.count() >= 1:
        await inputs.nth(0).fill(INDEED_SEARCH_TERM)
    if await inputs.count() >= 2:
        await inputs.nth(1).fill("")

    search_button = page.get_by_role("button", name="Szukaj pracy")
    if await search_button.count():
        await search_button.first.click()
    elif await inputs.count():
        await inputs.nth(0).press("Enter")
    await sleep_ms(1800)


async def infer_selector_from_first_offer(first_url: str) -> Optional[str]:
    try:
        parsed = urlsplit(first_url)
    except Exception:
        return None
    if not parsed.path:
        return None
    query_keys = [key for key in parse_qs(parsed.query).keys() if key]
    if query_keys:
        return f'main a[href*="{parsed.path}"][href*="{query_keys[0]}="]'
    return f'main a[href*="{parsed.path}"]'


async def extract_urls_with_selector(page: Page, selector: str, domain: str) -> list[str]:
    try:
        raw = await page.evaluate(
            """(selector) => {
                const normalize = (value) => {
                  try {
                    return new URL(value, window.location.href).toString();
                  } catch {
                    return "";
                  }
                };
                const visible = (el) => {
                  const rect = el.getBoundingClientRect();
                  return rect.width > 0 && rect.height > 0;
                };
                return Array.from(document.querySelectorAll(selector))
                  .filter((el) => visible(el))
                  .map((el) => normalize(el.getAttribute("href") || el.href || ""))
                  .filter(Boolean);
            }""",
            selector,
        )
    except Exception:
        raw = []

    out: list[str] = []
    seen: set[str] = set()
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, str):
            continue
        normalized = normalize_indeed_offer_url(entry)
        if not normalized or not is_offer_candidate_url(normalized, domain):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


async def extract_indeed_dom_offer_urls(page: Page, domain: str) -> list[str]:
    try:
        raw = await page.evaluate(
            """() => {
                const toAbs = (value) => {
                  try {
                    return new URL(value, window.location.href).toString();
                  } catch {
                    return "";
                  }
                };
                const visible = (el) => {
                  const rect = el.getBoundingClientRect();
                  return rect.width > 0 && rect.height > 0;
                };
                return Array.from(document.querySelectorAll("main a[href]"))
                  .filter((el) => visible(el))
                  .map((el) => toAbs(el.getAttribute("href") || el.href || ""))
                  .filter(Boolean);
            }"""
        )
    except Exception:
        raw = []

    out: list[str] = []
    seen: set[str] = set()
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, str):
            continue
        normalized = normalize_indeed_offer_url(entry)
        if not normalized or not is_offer_candidate_url(normalized, domain):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


async def extract_indeed_visible_offer_urls(
    runtime: StagehandRuntime,
    page: Page,
    domain: str,
) -> tuple[list[str], Optional[OfferPattern], Optional[str]]:
    raw_candidates = await extract_indeed_dom_offer_urls(page, domain)
    filtered_candidates = list(raw_candidates)
    first_url = (filtered_candidates or raw_candidates or [""])[0]

    inferred_pattern = await infer_offer_pattern_with_llm(
        runtime=runtime,
        page=page,
        domain=domain,
        candidate_urls=filtered_candidates or raw_candidates,
    )
    if inferred_pattern is None and first_url:
        inferred_pattern = infer_offer_pattern_from_url(first_url)

    selector = await infer_selector_from_first_offer(
        inferred_pattern.first_url if inferred_pattern else first_url
    )
    selector_urls = await extract_urls_with_selector(page, selector, domain) if selector else []

    merged: list[str] = []
    seen: set[str] = set()
    for candidate in selector_urls + filtered_candidates + raw_candidates:
        normalized = normalize_indeed_offer_url(candidate)
        if not normalized or not is_offer_candidate_url(normalized, domain):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        merged.append(normalized)

    return merged, inferred_pattern, selector


def normalize_pracuj_offer_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def is_pracuj_offer_url(url: str, domain: str) -> bool:
    try:
        parsed = urlsplit(url)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if not is_pracuj_domain(parsed.netloc):
        return False
    path = parsed.path.rstrip("/")
    return path.startswith("/praca/") and ",oferta," in path


async def collect_pracuj_visible_offer_urls(page: Page, domain: str, selector: str = "#offers-list a[href]") -> list[str]:
    try:
        raw = await page.evaluate(
            """(selector) => {
                const visible = (el) => {
                  const style = window.getComputedStyle(el);
                  const rect = el.getBoundingClientRect();
                  return style.display !== "none"
                    && style.visibility !== "hidden"
                    && rect.width > 0
                    && rect.height > 0;
                };
                const toAbs = (value) => {
                  try {
                    return new URL(value, window.location.href).toString();
                  } catch {
                    return "";
                  }
                };
                return Array.from(document.querySelectorAll(selector))
                  .filter((el) => visible(el))
                  .map((el) => toAbs(el.getAttribute("href") || el.href || ""))
                  .filter(Boolean);
            }""",
            selector,
        )
    except Exception:
        raw = []

    out: list[str] = []
    seen: set[str] = set()
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, str):
            continue
        normalized = normalize_pracuj_offer_url(entry)
        if not normalized or not is_pracuj_offer_url(normalized, domain):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


async def collect_pracuj_visible_offer_cards(page: Page, domain: str) -> list[dict[str, str]]:
    try:
        raw = await page.evaluate(
            """() => {
                const visible = (el) => {
                  const style = window.getComputedStyle(el);
                  const rect = el.getBoundingClientRect();
                  return style.display !== "none"
                    && style.visibility !== "hidden"
                    && rect.width > 0
                    && rect.height > 0;
                };
                const toAbs = (value) => {
                  try {
                    return new URL(value, window.location.href).toString();
                  } catch {
                    return "";
                  }
                };
                return Array.from(document.querySelectorAll('#offers-list [data-test="default-offer"]'))
                  .filter((card) => visible(card))
                  .map((card) => {
                    const titleLink = card.querySelector('a[data-test="link-offer-title"]');
                    const offerLink = card.querySelector('a[data-test="link-offer"]');
                    const titleNode = card.querySelector('[data-test="offer-title"]');
                    return {
                      offerId: card.getAttribute('data-test-offerid') || "",
                      title: (titleNode?.textContent || "").trim(),
                      titleLink: toAbs(titleLink?.getAttribute("href") || titleLink?.href || ""),
                      offerLink: toAbs(offerLink?.getAttribute("href") || offerLink?.href || ""),
                    };
                  });
            }"""
        )
    except Exception:
        raw = []

    out: list[dict[str, str]] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        offer_id = str(entry.get("offerId") or "").strip()
        title = str(entry.get("title") or "").strip()
        title_link = normalize_pracuj_offer_url(str(entry.get("titleLink") or "").strip())
        offer_link = normalize_pracuj_offer_url(str(entry.get("offerLink") or "").strip())
        url = title_link or offer_link
        if url and not is_pracuj_offer_url(url, domain):
            url = ""
        out.append({"offer_id": offer_id, "title": title, "url": url})
    return out


async def extract_pracuj_next_data_offer_urls(page: Page, domain: str) -> dict[str, str]:
    try:
        raw = await page.evaluate(
            """() => {
                const script = document.getElementById("__NEXT_DATA__");
                if (!script?.textContent) return [];
                const data = JSON.parse(script.textContent);
                const grouped = data?.props?.pageProps?.dehydratedState?.queries?.[0]?.state?.data?.groupedOffers || [];
                const out = [];
                for (const group of grouped) {
                  for (const offer of group?.offers || []) {
                    if (!offer?.partitionId || !offer?.offerAbsoluteUri) continue;
                    out.push({
                      offerId: String(offer.partitionId),
                      url: String(offer.offerAbsoluteUri),
                    });
                  }
                }
                return out;
            }"""
        )
    except Exception:
        raw = []

    out: dict[str, str] = {}
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        offer_id = str(entry.get("offerId") or "").strip()
        url = normalize_pracuj_offer_url(str(entry.get("url") or "").strip())
        if not offer_id or not url or not is_pracuj_offer_url(url, domain):
            continue
        out.setdefault(offer_id, url)
    return out


async def collect_pracuj_page_offer_urls(
    page: Page,
    domain: str,
    selector: Optional[str],
) -> tuple[list[str], int]:
    cards = await collect_pracuj_visible_offer_cards(page, domain)
    next_data_urls = await extract_pracuj_next_data_offer_urls(page, domain)
    selector_urls = await collect_pracuj_visible_offer_urls(page, domain, selector) if selector else []

    selector_by_id: dict[str, str] = {}
    for url in selector_urls:
        path = urlsplit(url).path
        offer_id = path.split(",oferta,")[-1] if ",oferta," in path else ""
        offer_id = offer_id.split("/")[0]
        if offer_id:
            selector_by_id.setdefault(offer_id, url)

    urls: list[str] = []
    seen: set[str] = set()
    for card in cards:
        offer_id = card["offer_id"]
        candidate = card["url"] or selector_by_id.get(offer_id, "") or next_data_urls.get(offer_id, "")
        if not candidate:
            continue
        normalized = normalize_pracuj_offer_url(candidate)
        if not normalized or not is_pracuj_offer_url(normalized, domain):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        urls.append(normalized)

    return urls, len(cards)


async def has_pracuj_next_page(page: Page) -> bool:
    try:
        return await page.evaluate(
            """() => {
                const btn = document.querySelector('[data-test="top-pagination-next-button"], [data-test="bottom-pagination-button-next"]');
                if (!btn) return false;
                const disabled = btn.hasAttribute('disabled') || btn.getAttribute("aria-disabled") === "true";
                return !disabled;
            }"""
        )
    except Exception:
        return False


async def goto_pracuj_next_page(page: Page) -> bool:
    try:
        button = page.locator('[data-test="top-pagination-next-button"]').first
        if await button.count() == 0:
            button = page.locator('[data-test="bottom-pagination-button-next"]').first
        if await button.count() == 0:
            return False
        before = page.url
        await button.click()
        await sleep_ms(2200)
        return page.url != before
    except Exception:
        return False


async def infer_pracuj_selector_from_first_offer(page: Page, first_url: str) -> Optional[str]:
    try:
        payload = await page.evaluate(
            """(firstUrl) => {
                const visible = (el) => {
                  const style = window.getComputedStyle(el);
                  const rect = el.getBoundingClientRect();
                  return style.display !== "none"
                    && style.visibility !== "hidden"
                    && rect.width > 0
                    && rect.height > 0;
                };
                const normalize = (value) => {
                  try {
                    const url = new URL(value, window.location.href);
                    url.hash = "";
                    return url.toString();
                  } catch {
                    return "";
                  }
                };
                const anchors = Array.from(document.querySelectorAll("#offers-list a[href]"))
                  .filter((el) => visible(el))
                  .filter((el) => normalize(el.getAttribute("href") || el.href || "") === firstUrl);
                return anchors.map((el) => ({
                  dataTest: el.getAttribute("data-test") || "",
                  dataTestId: el.getAttribute("data-testid") || "",
                  textLength: (el.textContent || "").trim().length,
                }));
            }""",
            first_url,
        )
    except Exception:
        payload = []

    matches = payload if isinstance(payload, list) else []
    if not matches:
        return None

    text_first_matches = [
        item
        for item in matches
        if isinstance(item, dict) and int(item.get("textLength") or 0) > 0 and item.get("dataTest")
    ]
    if text_first_matches:
        best = max(text_first_matches, key=lambda item: int(item.get("textLength") or 0))
        return f'#offers-list a[data-test="{str(best.get("dataTest")).strip()}"]'

    data_test = Counter(
        item.get("dataTest", "").strip()
        for item in matches
        if isinstance(item, dict) and item.get("dataTest")
    )
    if data_test:
        return f'#offers-list a[data-test="{data_test.most_common(1)[0][0]}"]'

    data_test_id = Counter(
        item.get("dataTestId", "").strip()
        for item in matches
        if isinstance(item, dict) and item.get("dataTestId")
    )
    if data_test_id:
        return f'#offers-list a[data-testid="{data_test_id.most_common(1)[0][0]}"]'

    path = urlsplit(first_url).path
    if ",oferta," in path:
        return '#offers-list a[href*=",oferta,"]'
    if path:
        return f'#offers-list a[href*="{path}"]'
    return None


async def extract_pracuj_visible_offer_urls(
    runtime: StagehandRuntime,
    page: Page,
    domain: str,
) -> tuple[list[str], Optional[OfferPattern], Optional[str], int]:
    raw_candidates = await collect_pracuj_visible_offer_urls(page, domain)
    first_url = raw_candidates[0] if raw_candidates else ""

    inferred_pattern = await infer_offer_pattern_with_llm(
        runtime=runtime,
        page=page,
        domain=domain,
        candidate_urls=raw_candidates,
    )
    if inferred_pattern is None and first_url:
        inferred_pattern = infer_offer_pattern_from_url(first_url)

    selector = await infer_pracuj_selector_from_first_offer(
        page,
        inferred_pattern.first_url if inferred_pattern else first_url,
    )
    page_urls, visible_cards_count = await collect_pracuj_page_offer_urls(page, domain, selector)
    return page_urls, inferred_pattern, selector, visible_cards_count


async def extract_offer_urls(
    runtime: StagehandRuntime,
    page: Page,
    domain: str,
    inferred_pattern: Optional[OfferPattern] = None,
    listing_selector: Optional[str] = None,
    use_llm_extract: bool = True,
) -> list[str]:
    urls: list[str] = []
    card_urls: list[str] = []

    if use_llm_extract:
        try:
            extracted = await runtime.session.extract(
                instruction=(
                    "Extract visible URLs for real job offer detail pages from current listing results. "
                    "Exclude sponsored/promoted/recommended blocks and nav/filter/login/share links."
                ),
                schema=OfferUrls.model_json_schema(),
                options=scoped_model_options(runtime.model_opts, listing_selector),
                page=page,
            )
            payload = getattr(extracted.data, "result", None)
            if isinstance(payload, dict):
                raw = payload.get("urls")
                if isinstance(raw, list):
                    urls.extend([url for url in raw if isinstance(url, str)])
        except Exception:
            pass

    try:
        dom_urls = await page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href]')).map((a) => {
                try { return new URL(a.getAttribute('href') || a.href, window.location.origin).toString(); }
                catch { return null; }
            }).filter(Boolean)"""
        )
        if isinstance(dom_urls, list):
            urls.extend([url for url in dom_urls if isinstance(url, str)])
    except Exception:
        pass

    try:
        raw_card_urls = await page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                const anchors = Array.from(document.querySelectorAll("a[href]"));
                for (const a of anchors) {
                  const hrefRaw = a.getAttribute("href") || a.href || "";
                  if (!hrefRaw) continue;
                  const hasTitle = !!a.querySelector("h1, h2, h3, [data-testid*='title'], [class*='title']");
                  if (!hasTitle) continue;
                  const inFooter = !!a.closest("footer");
                  const inHeader = !!a.closest("header");
                  if (inFooter || inHeader) continue;
                  let abs = "";
                  try { abs = new URL(hrefRaw, window.location.href).toString(); } catch { continue; }
                  if (!abs || seen.has(abs)) continue;
                  seen.add(abs);
                  out.push(abs);
                }
                return out;
            }"""
        )
        if isinstance(raw_card_urls, list):
            card_urls.extend([url for url in raw_card_urls if isinstance(url, str)])
    except Exception:
        pass

    try:
        embedded_urls = await page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                const pushUrl = (value) => {
                  if (typeof value !== "string") return;
                  const v = value.trim();
                  if (!v) return;
                  if (!(v.startsWith("http://") || v.startsWith("https://") || v.startsWith("/"))) return;
                  if (seen.has(v)) return;
                  seen.add(v);
                  out.push(v);
                };
                const scripts = Array.from(document.querySelectorAll("script"));
                for (const s of scripts) {
                  const t = (s.textContent || "");
                  if (!t) continue;
                  const matches = t.match(/https?:\\/\\/[^"'\\s<>]+|\\/[a-zA-Z0-9_\\-\\/.,%]+/g) || [];
                  for (const m of matches) pushUrl(m);
                  const escapedMatches = t.match(/\\\\\\/pl\\\\\\/job\\\\\\/[a-zA-Z0-9\\-]+/g) || [];
                  for (const m of escapedMatches) {
                    pushUrl(m.replace(/\\\\\\//g, "/"));
                  }
                }
                return out.slice(0, 5000);
            }"""
        )
        if isinstance(embedded_urls, list):
            urls.extend([url for url in embedded_urls if isinstance(url, str)])
    except Exception:
        pass

    out: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        candidate = raw.strip()
        if candidate.startswith("/"):
            candidate = urljoin(page.url, candidate)
        normalized = normalize_offer_url(candidate)
        if not normalized or not is_offer_candidate_url(normalized, domain):
            continue
        if not matches_offer_pattern(normalized, inferred_pattern):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)

    for raw in card_urls:
        candidate = raw.strip()
        if candidate.startswith("/"):
            candidate = urljoin(page.url, candidate)
        normalized = normalize_offer_url(candidate)
        if not normalized or not is_offer_candidate_url(normalized, domain):
            continue
        if inferred_pattern is not None and not matches_offer_pattern(normalized, inferred_pattern):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)

    return out


async def discover_pagination_urls(
    page: Page,
    domain: str,
    inferred_pattern: Optional[OfferPattern] = None,
) -> list[str]:
    try:
        raw = await page.evaluate(
            """() => {
                const toAbs = (href) => {
                  try { return new URL(href, window.location.href).toString(); } catch { return ""; }
                };
                const out = [];
                const seen = new Set();
                const nextTerms = /(next|nast[eę]pn|dalej|kolejn|›|»|→)/i;
                const nodes = Array.from(document.querySelectorAll("a[href], button[data-href], [role='button'][data-href]"));
                for (const node of nodes) {
                  const hrefRaw = node.tagName === "A" ? (node.getAttribute("href") || node.href || "") : (node.getAttribute("data-href") || "");
                  const href = toAbs(hrefRaw);
                  if (!href) continue;
                  const text = ((node.textContent || "") + " " + (node.getAttribute("aria-label") || "")).trim().toLowerCase();
                  const rel = (node.getAttribute("rel") || "").toLowerCase();
                  const pagePattern = /(?:[?&](page|strona|pagenumber)=\\d+)|\\/(?:page|strona)\\/\\d+|\\/p\\/(\\d+)/i.test(href);
                  const numeric = /^\\d+$/.test((node.textContent || "").trim());
                  const looksNext = rel.includes("next") || nextTerms.test(text);
                  if (!(looksNext || pagePattern || numeric)) continue;
                  if (seen.has(href)) continue;
                  seen.add(href);
                  out.push(href);
                }
                return out;
            }"""
        )
    except Exception:
        return []

    if not isinstance(raw, list):
        return []

    out: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str):
            continue
        parsed = urlsplit(entry)
        if parsed.scheme not in {"http", "https"}:
            continue
        if normalize_domain(parsed.netloc) != normalize_domain(domain):
            continue
        normalized = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
        if inferred_pattern is not None and matches_offer_pattern(normalized, inferred_pattern):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


async def reveal_more(runtime: StagehandRuntime, page: Page) -> bool:
    before = await listing_signature(page)

    if not runtime.session.should_skip("observe"):
        try:
            observed = await runtime.session.observe(
                instruction=(
                    "Find action that reveals more job results (load more or next page). "
                    "Ignore sponsored/recommended areas and non-result UI."
                ),
                options=runtime.model_opts,
                page=page,
            )
            actions = as_action_dicts(getattr(observed.data, "result", []) or [])
            action = choose_reveal_action(actions)
            if action:
                await runtime.session.act(input=action, page=page)
                await sleep_ms(1200)
                return await listing_signature(page) != before
        except Exception:
            pass

    try:
        clicked = await page.evaluate(
            """() => {
                const labels = ["load more", "show more", "more", "next", "więcej", "pokaż", "dalej"];
                const nodes = Array.from(document.querySelectorAll("button, a[role='button'], [role='button']"));
                for (const el of nodes) {
                  const txt = ((el.textContent || "") + " " + (el.getAttribute("aria-label") || ""))
                    .trim()
                    .toLowerCase()
                    .replace(/\\s+/g, " ");
                  if (!txt) continue;
                  if (!labels.some((label) => txt.includes(label))) continue;
                  const disabled = el.hasAttribute("disabled") || el.getAttribute("aria-disabled") === "true";
                  if (disabled) continue;
                  el.click();
                  return true;
                }
                return false;
            }"""
        )
        if clicked:
            await sleep_ms(1200)
            return await listing_signature(page) != before
    except Exception:
        pass

    try:
        await runtime.session.execute(
            execute_options={
                "instruction": (
                    "Reveal more job results by clicking Load more / Next page if available. "
                    "Do not click sponsored, filter, sort, login, or share UI."
                ),
                "max_steps": 1,
            },
            agent_config={"model": runtime.model_name},
            should_cache=runtime.cache_enabled,
            page=page,
        )
        await sleep_ms(1200)
        return await listing_signature(page) != before
    except Exception:
        return False


async def collect_pracuj_offers(runtime: StagehandRuntime, board: BoardConfig, page: Page) -> DiscoveryResult:
    await accept_cookies(runtime, page)
    await dismiss_pracuj_popups(page)
    await accept_cookies(runtime, page)

    offers: list[str] = []
    inferred_pattern: Optional[OfferPattern] = None
    selector: Optional[str] = None
    raw_count = 0
    for attempt in range(1, 5):
        offers, inferred_pattern, selector, raw_count = await extract_pracuj_visible_offer_urls(
            runtime=runtime,
            page=page,
            domain=normalize_domain(urlsplit(board.url).netloc),
        )
        console.print(
            f"ATTEMPT={attempt} RAW_CANDIDATE_COUNT={raw_count} FOUND_URLS_COUNT={len(offers)} PAGE={page.url}"
        )
        if offers:
            break
        await sleep_ms(1200)

    all_offers: list[str] = []
    seen_offers: set[str] = set()
    page_index = 1
    current_page_offers = offers
    current_page_cards = raw_count
    while True:
        for offer in current_page_offers:
            if offer in seen_offers:
                continue
            seen_offers.add(offer)
            all_offers.append(offer)
        console.print(
            f"PAGE_INDEX={page_index} VISIBLE_CARDS_COUNT={current_page_cards} "
            f"PAGE_FOUND_URLS_COUNT={len(current_page_offers)} TOTAL_FOUND_URLS_COUNT={len(all_offers)} PAGE={page.url}"
        )
        if not await has_pracuj_next_page(page):
            break
        moved = await goto_pracuj_next_page(page)
        if not moved:
            break
        current_page_offers, _, _, current_page_cards = await extract_pracuj_visible_offer_urls(
            runtime=runtime,
            page=page,
            domain=normalize_domain(urlsplit(board.url).netloc),
        )
        page_index += 1

    log_offer_pattern(inferred_pattern)
    log_job_link_selector(selector)

    metadata = DiscoveryMetadata(
        first_offer_url=inferred_pattern.first_url if inferred_pattern else "",
        selector=selector or "",
        include_tokens=list(inferred_pattern.include_tokens) if inferred_pattern else [],
        exclude_tokens=list(inferred_pattern.exclude_tokens) if inferred_pattern else [],
        total_steps=page_index,
    )
    return DiscoveryResult(board=board, urls=all_offers, metadata=metadata)


async def collect_indeed_offers(runtime: StagehandRuntime, board: BoardConfig, page: Page) -> DiscoveryResult:
    domain = normalize_domain(urlsplit(board.url).netloc)
    console.print(f"SEARCH_TERM={INDEED_SEARCH_TERM}")
    await accept_cookies(runtime, page)
    await search_indeed(runtime, page)
    await accept_cookies(runtime, page)

    offers: list[str] = []
    inferred_pattern: Optional[OfferPattern] = None
    selector: Optional[str] = None
    for attempt in range(1, 4):
        offers, inferred_pattern, selector = await extract_indeed_visible_offer_urls(runtime, page, domain)
        console.print(f"ATTEMPT={attempt} FOUND_URLS_COUNT={len(offers)} PAGE={page.url}")
        if len(offers) >= INDEED_EXPECTED_VISIBLE_OFFERS:
            break
        await sleep_ms(1200)

    log_offer_pattern(inferred_pattern)
    log_job_link_selector(selector)

    metadata = DiscoveryMetadata(
        first_offer_url=inferred_pattern.first_url if inferred_pattern else "",
        selector=selector or "",
        include_tokens=list(inferred_pattern.include_tokens) if inferred_pattern else [],
        exclude_tokens=list(inferred_pattern.exclude_tokens) if inferred_pattern else [],
        expected_count=INDEED_EXPECTED_VISIBLE_OFFERS,
        total_steps=1,
    )
    return DiscoveryResult(board=board, urls=offers, metadata=metadata)


async def collect_generic_offers(runtime: StagehandRuntime, board: BoardConfig, page: Page) -> DiscoveryResult:
    domain = normalize_domain(urlsplit(board.url).netloc)
    seen: set[str] = set()
    inferred_pattern: Optional[OfferPattern] = None
    listing_selector: Optional[str] = None
    expected_from_header: Optional[int] = None
    visited_pages: set[str] = set()
    queued_pages: list[str] = []
    queued_lookup: set[str] = set()
    stagnation = 0
    step = 0
    last_listing_url = normalize_page_url(board.url)
    listing_family_name = path_family(urlsplit(board.url).path)
    cookie_checked = False
    max_stagnation = 8 if board.name == "justjoin" else 10
    pattern_logged = False
    selector_logged = False
    initial_offer_url = ""
    if board.name == "justjoin":
        initial_offer_url = JJ_FIRST_VISIBLE_OFFER_URL
    elif board.name == "nofluffjobs":
        initial_offer_url = NF_FIRST_VISIBLE_OFFER_URL
    elif board.name == "bulldogjob":
        initial_offer_url = BD_FIRST_VISIBLE_OFFER_URL

    if initial_offer_url:
        inferred_pattern = infer_offer_pattern_from_url(env_str("STAGEHAND_FIRST_OFFER_URL", initial_offer_url))
        if inferred_pattern:
            log_offer_pattern(inferred_pattern)
            pattern_logged = True

    while True:
        step_started = time.perf_counter()
        step += 1
        if step > SAFETY_STEP_LIMIT:
            console.print(f"STOP_REASON=safety_step_limit_{SAFETY_STEP_LIMIT}")
            break

        if not cookie_checked:
            await accept_cookies(runtime, page)
            cookie_checked = True

        header_on_current = await read_results_header_count(page)
        if header_on_current is not None:
            last_listing_url = normalize_page_url(page.url)
            if step == 1 and board.name != "justjoin":
                expected_from_header = header_on_current
                console.print(f"HEADER_OFFERS_COUNT={expected_from_header}")
        else:
            current_norm = normalize_page_url(page.url)
            if is_same_listing_family(current_norm, domain, listing_family_name):
                last_listing_url = current_norm
            elif current_norm != last_listing_url:
                await runtime.session.navigate(url=last_listing_url, page=page)
                await sleep_ms(1200)
                console.print(f"RECOVER_TO_LISTING={last_listing_url}")
                continue

        current_page = normalize_page_url(page.url)
        visited_pages.add(current_page)
        before = len(seen)

        if listing_selector is None:
            listing_selector = await discover_listing_selector(runtime, page)
            if listing_selector and not selector_logged:
                log_job_link_selector(listing_selector)
                selector_logged = True

        seed_urls: list[str] = []
        if inferred_pattern is None:
            seed_urls = await extract_offer_seed_urls_with_llm(runtime, page, domain, listing_selector)

        raw_extracted = await extract_offer_urls(
            runtime=runtime,
            page=page,
            domain=domain,
            inferred_pattern=None,
            listing_selector=listing_selector,
            use_llm_extract=(inferred_pattern is None and board.name != "justjoin"),
        )

        if board.name == "nofluffjobs":
            await sleep_ms(1200)
            late_dom_extracted = await extract_offer_urls(
                runtime=runtime,
                page=page,
                domain=domain,
                inferred_pattern=None,
                listing_selector=listing_selector,
                use_llm_extract=False,
            )
            raw_extracted = merge_unique_urls(raw_extracted, late_dom_extracted)

        if not seed_urls and raw_extracted:
            seed_urls = await filter_offer_candidates_with_llm(runtime, page, domain, raw_extracted)

        if inferred_pattern is None:
            inferred_pattern = await infer_offer_pattern_with_llm(runtime, page, domain, seed_urls)
            if inferred_pattern is None and seed_urls:
                inferred_pattern = infer_offer_pattern_from_url(seed_urls[0])
            if inferred_pattern is None and raw_extracted:
                inferred_pattern = infer_offer_pattern_from_candidates(raw_extracted)
            if inferred_pattern and not pattern_logged:
                log_offer_pattern(inferred_pattern)
                pattern_logged = True

        extracted = list(seed_urls) if inferred_pattern is None else [
            url for url in raw_extracted if matches_offer_pattern(url, inferred_pattern)
        ]

        added_this_step = 0
        for url in extracted:
            if url not in seen:
                seen.add(url)
                added_this_step += 1

        if expected_from_header is not None and len(seen) < expected_from_header and added_this_step == 0:
            recovered = await recover_missing_offers_with_llm(runtime, page, domain, seen)
            for url in recovered:
                if url not in seen:
                    seen.add(url)
                    added_this_step += 1

        console.print(
            f"STEP={step} OFFERS={len(seen)} RAW={len(raw_extracted)} ADDED={added_this_step} PAGE={current_page}"
        )
        if expected_from_header is not None and len(seen) >= expected_from_header:
            console.print("STOP_REASON=header_reached")
            break

        for page_url in await discover_pagination_urls(page, domain, inferred_pattern):
            if not is_same_listing_family(page_url, domain, listing_family_name):
                continue
            if page_url not in visited_pages and page_url not in queued_lookup:
                queued_pages.append(page_url)
                queued_lookup.add(page_url)

        moved = False if board.name == "justjoin" else await reveal_more(runtime, page)
        if moved and not is_same_listing_family(page.url, domain, listing_family_name):
            await runtime.session.navigate(url=last_listing_url, page=page)
            await sleep_ms(900)
            console.print(f"RECOVER_TO_LISTING={last_listing_url}")
            moved = False

        if not moved:
            next_url: Optional[str] = None
            while queued_pages:
                candidate = queued_pages.pop(0)
                queued_lookup.discard(candidate)
                if candidate in visited_pages:
                    continue
                next_url = candidate
                break
            if next_url:
                await runtime.session.navigate(url=next_url, page=page)
                await sleep_ms(1400)
                if await read_results_header_count(page) is None:
                    await runtime.session.navigate(url=last_listing_url, page=page)
                    await sleep_ms(900)
                    console.print(f"RECOVER_TO_LISTING={last_listing_url}")
                    moved = False
                else:
                    moved = True
                    console.print(f"PAGINATION_GOTO={next_url}")

        if not moved:
            llm_next = await discover_next_listing_page_with_llm(
                runtime=runtime,
                page=page,
                domain=domain,
                visited_pages=visited_pages,
                inferred_pattern=inferred_pattern,
            )
            if llm_next and not is_same_listing_family(llm_next, domain, listing_family_name):
                llm_next = None
            if llm_next:
                await runtime.session.navigate(url=llm_next, page=page)
                await sleep_ms(1200)
                moved = True
                console.print(f"PAGINATION_GOTO_LLM={llm_next}")

        if not moved:
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await sleep_ms(900)
            except Exception:
                pass

        stagnation = 0 if len(seen) > before else stagnation + 1
        console.print(f"STEP_MS={int((time.perf_counter() - step_started) * 1000)}")
        if stagnation >= max_stagnation:
            console.print("STOP_REASON=stagnation")
            break

        no_more_actions_threshold = 4 if board.name == "nofluffjobs" else 2
        if not moved and not queued_pages and stagnation >= no_more_actions_threshold:
            console.print("STOP_REASON=no_more_actions")
            break

    metadata = DiscoveryMetadata(
        first_offer_url=inferred_pattern.first_url if inferred_pattern else "",
        selector=listing_selector or "",
        include_tokens=list(inferred_pattern.include_tokens) if inferred_pattern else [],
        exclude_tokens=list(inferred_pattern.exclude_tokens) if inferred_pattern else [],
        expected_count=expected_from_header,
        total_steps=step,
    )
    return DiscoveryResult(board=board, urls=sorted(seen), metadata=metadata)


async def discover_job_urls(runtime: StagehandRuntime, board: BoardConfig) -> DiscoveryResult:
    page = runtime.page
    console.print(f"TARGET={board.name} URL={board.url}")
    await runtime.session.navigate(url=board.url, page=page)
    await sleep_ms(1500)
    await accept_cookies(runtime, page)

    if board.discovery_mode == "pracuj":
        return await collect_pracuj_offers(runtime, board, page)
    if board.discovery_mode == "indeed":
        return await collect_indeed_offers(runtime, board, page)
    return await collect_generic_offers(runtime, board, page)


def _normalize_offer_list(raw: Any, page: Page, domain: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, str):
            continue
        candidate = entry.strip()
        if candidate.startswith("/"):
            candidate = urljoin(page.url, candidate)
        normalized = normalize_offer_url(candidate)
        if not normalized or not is_offer_candidate_url(normalized, domain):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out
