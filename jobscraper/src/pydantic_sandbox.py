import asyncio
import hashlib
import json
import os
import re
import subprocess
from collections import Counter
from typing import Any, Optional
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import httpx
from dotenv import load_dotenv
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from pydantic import BaseModel, Field
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Reduce SDK noise unless explicitly overridden by user environment.
os.environ.setdefault("AI_SDK_LOG_WARNINGS", "false")

console = Console()

PROTOCOL_POC_URL = "https://theprotocol.it/filtry/python;t/trainee,assistant,junior;p?sort=date"
_TRUTHY_ENV_VALUES = {"1", "true", "yes"}


def _normalize_domain(domain: str) -> str:
    return domain.lower().removeprefix("www.")


def _env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_flag(name: str, default: str = "false") -> bool:
    return _env_str(name, default).lower() in _TRUTHY_ENV_VALUES


def _debug_enabled() -> bool:
    return _env_flag("STAGEHAND_DEBUG")


def _debug(message: str) -> None:
    if _debug_enabled():
        console.print(f"[dim cyan][debug][/dim cyan] {message}")


def _trace_enabled() -> bool:
    return _env_flag("STAGEHAND_TRACE")


def _trace(message: str) -> None:
    if _trace_enabled():
        console.print(f"[dim]{message}[/dim]")


def _cache_audit_enabled() -> bool:
    return _env_flag("STAGEHAND_CACHE_AUDIT")


def _show_offer_urls_enabled() -> bool:
    return _env_flag("STAGEHAND_SHOW_OFFER_URLS", "true")


def _local_server_logs_enabled() -> bool:
    return _env_flag("STAGEHAND_LOCAL_SERVER_LOGS")


def _configure_local_stagehand_server_logging() -> None:
    """Silence noisy local Stagehand server logs unless explicitly enabled."""
    if _local_server_logs_enabled() or _debug_enabled():
        return
    try:
        from stagehand.lib import sea_server as stagehand_sea_server  # type: ignore
    except Exception:
        return
    if getattr(stagehand_sea_server, "_jsb_quiet_patch_applied", False):
        return

    original_popen = stagehand_sea_server.subprocess.Popen

    def _quiet_popen(*args: Any, **kwargs: Any) -> Any:
        cmd0 = None
        if args and isinstance(args[0], (list, tuple)) and args[0]:
            cmd0 = str(args[0][0])
        elif args and isinstance(args[0], str):
            cmd0 = args[0]
        elif isinstance(kwargs.get("args"), (list, tuple)) and kwargs.get("args"):
            cmd0 = str(kwargs["args"][0])
        elif isinstance(kwargs.get("args"), str):
            cmd0 = kwargs["args"]

        # Only silence the Stagehand local SEA server binary itself.
        is_stagehand_binary = bool(cmd0 and "stagehand" in cmd0 and ("_sea" in cmd0 or "darwin" in cmd0))
        if not is_stagehand_binary:
            return original_popen(*args, **kwargs)

        env = kwargs.get("env")
        if isinstance(env, dict):
            env.setdefault("LOG_LEVEL", "error")
            env.setdefault("PINO_LOG_LEVEL", "error")
            env.setdefault("AI_SDK_LOG_WARNINGS", "false")
        # Force-silence local Stagehand server process output.
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
        return original_popen(*args, **kwargs)

    stagehand_sea_server.subprocess.Popen = _quiet_popen
    stagehand_sea_server._jsb_quiet_patch_applied = True


async def _fetch_stagehand_metrics(
    base_url: str,
    model_api_key: Optional[str],
    browserbase_api_key: Optional[str],
    browserbase_project_id: Optional[str],
) -> Optional[dict[str, Any]]:
    headers: dict[str, str] = {}
    if model_api_key:
        headers["x-model-api-key"] = model_api_key
    if browserbase_api_key:
        headers["x-bb-api-key"] = browserbase_api_key
    if browserbase_project_id:
        headers["x-bb-project-id"] = browserbase_project_id
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            response = await client.get(f"{base_url.rstrip('/')}/metrics", headers=headers)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _print_metrics_summary(metrics: Optional[dict[str, Any]]) -> None:
    if not metrics:
        console.print("STAGEHAND_METRICS=unavailable")
        return
    total_prompt = int(metrics.get("totalPromptTokens") or 0)
    total_completion = int(metrics.get("totalCompletionTokens") or 0)
    total_reasoning = int(metrics.get("totalReasoningTokens") or 0)
    total_cached = int(metrics.get("totalCachedInputTokens") or 0)
    total_inference_ms = int(metrics.get("totalInferenceTimeMs") or 0)
    total_effective_input = total_prompt + total_cached
    cache_share = (100.0 * total_cached / total_effective_input) if total_effective_input > 0 else 0.0
    cache_status = "cache hits detected" if total_cached > 0 else "no cache hits yet"

    lines = [
        "[bold]Token Summary[/bold]",
        f"Prompt tokens: {total_prompt}",
        f"Completion tokens: {total_completion}",
        f"Reasoning tokens: {total_reasoning}",
        f"Cached input tokens: {total_cached}",
        f"Cache share (input): {cache_share:.1f}%",
        f"Total inference time: {total_inference_ms} ms",
        f"Status: {cache_status}",
    ]
    console.print(Panel("\n".join(lines), title="Stagehand Metrics", style="green"))


def _metric_int(metrics: Optional[dict[str, Any]], key: str) -> int:
    if not metrics:
        return 0
    return int(metrics.get(key) or 0)


async def _fetch_client_metrics(client: Any) -> Optional[dict[str, Any]]:
    return await _fetch_stagehand_metrics(
        str(client.base_url),
        client.model_api_key,
        client.browserbase_api_key,
        client.browserbase_project_id,
    )


def _print_offer_urls(urls: list[str]) -> None:
    table = Table(title="Collected Offer URLs", header_style="bold cyan")
    table.add_column("#", style="cyan", justify="right", width=4)
    table.add_column("URL", style="white", overflow="fold")
    for index, offer_url in enumerate(urls, start=1):
        table.add_row(str(index), offer_url)
    console.print(table)


def _generic_fallback_enabled() -> bool:
    return _env_flag("STAGEHAND_ENABLE_GENERIC_FALLBACK")


def _inferred_pattern_enabled() -> bool:
    return _env_flag("STAGEHAND_ENABLE_INFERRED_PATTERN", "true")


class OfferUrls(BaseModel):
    urls: list[str] = Field(default_factory=list, description="Absolute URLs to real non-promotional job offer detail pages currently visible.")


class StagehandPage:
    """Thin adapter that keeps old page-shaped callsites while using Stagehand v3 sessions."""

    def __init__(
        self,
        session: Any,
        pw_page: Page,
        model_name: str,
        model_api_key: Optional[str],
        model_base_url: Optional[str],
    ) -> None:
        self._session = session
        self._pw_page = pw_page
        self._model_name = model_name
        self._model_api_key = model_api_key
        self._model_base_url = model_base_url

    @property
    def url(self) -> str:
        return self._pw_page.url

    def _model_options(self) -> dict[str, Any]:
        model_cfg: dict[str, Any] = {"modelName": self._model_name}
        if self._model_api_key:
            model_cfg["apiKey"] = self._model_api_key
        if self._model_base_url:
            model_cfg["baseURL"] = self._model_base_url
        return {"model": model_cfg}

    async def goto(self, url: str) -> None:
        await self._session.navigate(url=url, page=self._pw_page)

    async def wait_for_timeout(self, timeout_ms: int) -> None:
        await asyncio.sleep(timeout_ms / 1000)

    async def evaluate(self, script: str) -> Any:
        return await self._pw_page.evaluate(script)

    async def observe(self, instruction: str) -> list[dict[str, Any]]:
        response = await self._session.observe(
            instruction=instruction,
            options=self._model_options(),
            page=self._pw_page,
        )
        return [
            item.model_dump(by_alias=True, exclude_none=True)
            for item in getattr(response.data, "result", []) or []
        ]

    async def act(self, payload: Any) -> Any:
        kwargs: dict[str, Any] = {"input": payload, "page": self._pw_page}
        # Only include model options for NL instructions; action objects should replay without extra inference.
        if isinstance(payload, str):
            kwargs["options"] = self._model_options()
        response = await self._session.act(**kwargs)
        return response.data.result

    async def extract(self, instruction: str, schema: Any) -> Any:
        if hasattr(schema, "model_json_schema"):
            schema_payload = schema.model_json_schema()
        else:
            schema_payload = schema
        response = await self._session.extract(
            instruction=instruction,
            schema=schema_payload,
            options=self._model_options(),
            page=self._pw_page,
        )
        return response.data.result


def _normalize_offer_url(url: str) -> str:
    """Normalize offer URL by removing query params and fragments."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _normalize_page_url(url: str) -> str:
    """Normalize page URL for visited-page tracking."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _is_recommendation_tracking_url(url: str) -> bool:
    """Exclude URLs that are clearly recommendation/add-on tracked entries."""
    try:
        params = parse_qs(urlsplit(url).query)
    except Exception:
        return False
    return "sug" in params


def _is_probable_offer_detail_url(url: str, domain: str) -> bool:
    """Strong URL-level guard against non-offer links."""
    try:
        parts = urlsplit(url)
    except Exception:
        return False
    if parts.scheme not in {"http", "https"}:
        return False
    if _normalize_domain(parts.netloc) != _normalize_domain(domain):
        return False

    path = (parts.path or "").strip()
    if path in {"", "/"}:
        return False
    lower_path = path.lower()

    # Reject obvious route patterns / regex-like placeholders accidentally scraped from embedded state.
    if any(token in lower_path for token in ("(", ")", "{", "}", "[", "]", "|", ":%2f", "%2f):", ".+")):
        return False

    # Domain-specific strict guard for theprotocol.it listing/detail pages.
    if _normalize_domain(domain).endswith("theprotocol.it"):
        if not lower_path.startswith("/szczegoly/praca/"):
            return False
        # Keep strict offer-detail format observed on listing pages.
        tail = lower_path.removeprefix("/szczegoly/praca/")
        if len(tail) < 16:
            return False
        if ",oferta," not in tail:
            return False
        if not re.fullmatch(r"[a-z0-9-]+,oferta,[0-9a-f-]{20,}", tail, flags=re.I):
            return False
        if any(token in tail for token in ("filtry", "polityka", "cookies", "prywatnosci", "informacje-dla-aktu")):
            return False
        return True

    # Generic fallback for other domains.
    job_terms = ("job", "jobs", "career", "careers", "position", "positions", "vacancy", "vacancies", "praca", "oferta")
    return any(term in lower_path for term in job_terms)


def _resolve_stagehand_model_name() -> Optional[str]:
    """Use MODEL env and keep provider/model format expected by Stagehand v3."""
    raw_model = _env_str("MODEL")
    if not raw_model:
        return None
    return raw_model


def _resolve_llm_api_key() -> Optional[str]:
    """Resolve API key from LLM_API_KEY."""
    return _env_str("LLM_API_KEY") or None


def _resolve_llm_api_base() -> Optional[str]:
    """Resolve API base from LLM_API_BASE."""
    return _env_str("LLM_API_BASE") or None


def _extract_urls_from_payload(payload: Any) -> list[str]:
    """Recursively collect URL strings from arbitrary payload shapes."""
    found: list[str] = []
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, list):
        for item in payload:
            found.extend(_extract_urls_from_payload(item))
        return found
    if isinstance(payload, dict):
        for value in payload.values():
            found.extend(_extract_urls_from_payload(value))
        return found
    return found


async def _close_cookie_overlay(page: Any) -> None:
    """Best-effort generic cookie acceptance without LLM calls."""
    try:
        await page.evaluate(
            """() => {
              const labels = [
                "accept", "accept all", "agree", "allow all", "got it",
                "zgadzam", "akcept", "zaakceptuj"
              ];
              const candidates = Array.from(document.querySelectorAll("button, [role='button'], a"));
              for (const el of candidates) {
                const text = (el.textContent || "").trim().toLowerCase();
                if (!text) continue;
                if (!labels.some((label) => text.includes(label))) continue;
                const disabled = el.hasAttribute("disabled") || el.getAttribute("aria-disabled") === "true";
                if (disabled) continue;
                el.click();
                return true;
              }
              return false;
            }"""
        )
    except Exception:
        pass


async def _is_security_challenge(page: Any) -> bool:
    try:
        title = (await page.evaluate("document.title") or "").strip().lower()
        body_preview = (
            (await page.evaluate("(document.body && document.body.innerText || '').slice(0, 300)") or "")
            .strip()
            .lower()
        )
    except Exception:
        return False
    return (
        "just a moment" in title
        or "security verification" in body_preview
        or "cloudflare" in body_preview
    )


async def _wait_for_security_challenge(page: Any) -> None:
    """Wait out bot-check interstitials (e.g., Cloudflare) when they auto-resolve."""
    max_wait_s = int(os.getenv("STAGEHAND_POC_SECURITY_WAIT_SECONDS", "35"))
    for second in range(max_wait_s):
        try:
            href_count = int(await page.evaluate("document.querySelectorAll('a[href]').length"))
        except Exception:
            await page.wait_for_timeout(1000)
            continue

        if not await _is_security_challenge(page) and href_count > 8:
            if second > 0:
                _debug(f"security challenge cleared after {second}s")
            return

        await page.wait_for_timeout(1000)

    _debug("security challenge wait window elapsed; continuing with current page state")


async def _extract_expected_offer_count(page: Any) -> Optional[int]:
    """Best-effort extraction of expected result count from visible page text."""
    try:
        count = await page.evaluate(
            """() => {
              const bodyText = (document.body?.innerText || "").slice(0, 12000);
              const patterns = [
                /wyniki\\s*\\((\\d+)\\s+ofert\\)/i,
                /results\\s*\\((\\d+)\\s+(jobs|offers|results)\\)/i,
                /(\\d+)\\s+ofert/i,
                /(\\d+)\\s+jobs/i,
                /(\\d+)\\s+results/i
              ];
              for (const pattern of patterns) {
                const match = bodyText.match(pattern);
                if (match && match[1]) {
                  const num = Number(match[1]);
                  if (Number.isFinite(num) && num > 0 && num < 20000) return num;
                }
              }
              const nodes = Array.from(document.querySelectorAll("h1,h2,h3,div,span,p"));
              for (const node of nodes) {
                const text = (node.textContent || "").trim();
                if (!text || text.length > 120) continue;
                for (const pattern of patterns) {
                  const match = text.match(pattern);
                  if (match && match[1]) {
                    const num = Number(match[1]);
                    if (Number.isFinite(num) && num > 0 && num < 20000) return num;
                  }
                }
              }

              // Generic embedded-data fallback (e.g., Next.js __NEXT_DATA__).
              const nextDataNode = document.getElementById("__NEXT_DATA__");
              if (nextDataNode && nextDataNode.textContent) {
                try {
                  const payload = JSON.parse(nextDataNode.textContent);
                  const queue = [payload];
                  const candidateKeys = new Set(["offersCount", "jobsCount", "resultsCount", "totalCount"]);
                  while (queue.length) {
                    const current = queue.shift();
                    if (!current || typeof current !== "object") continue;
                    for (const [k, v] of Object.entries(current)) {
                      if (candidateKeys.has(k) && typeof v === "number" && v > 0 && v < 20000) {
                        return v;
                      }
                      if (v && typeof v === "object") queue.push(v);
                    }
                  }
                } catch {}
              }
              return null;
            }"""
        )
    except Exception:
        return None
    if isinstance(count, (int, float)):
        parsed = int(count)
        if parsed > 0:
            return parsed
    return None


async def _extract_visible_offer_urls_with_llm(
    page: Any,
    domain: str,
    allow_structural_fallback: bool = True,
) -> list[str]:
    """Use Stagehand LLM extraction with dynamic promotional filtering."""
    try:
        extracted = await page.extract(
            """
            Extract currently visible URLs for real job-offer detail pages from the main search/listing results.
            Exclude sponsored/promoted/advertisement/banner content dynamically.
            Exclude recommendation panels (e.g. "recommended for you"/"we picked for you"/similar jobs),
            plus pagination, filters, sorting, navigation, login, and share links.
            Return absolute URLs only.
            """,
            schema=OfferUrls,
        )
    except Exception:
        return []

    raw_urls: list[str] = []
    if isinstance(extracted, OfferUrls):
        raw_urls = extracted.urls
    else:
        candidate_payloads: list[Any] = [extracted, getattr(extracted, "extraction", None)]
        if hasattr(extracted, "model_dump"):
            try:
                candidate_payloads.append(extracted.model_dump())
            except Exception:
                pass

        for payload in candidate_payloads:
            if payload is None:
                continue
            raw_urls.extend(_extract_urls_from_payload(payload))

        if not raw_urls:
            text = ""
            try:
                text = json.dumps(getattr(extracted, "model_dump", lambda: extracted)(), ensure_ascii=False)
            except Exception:
                text = str(extracted)
            raw_urls.extend(re.findall(r"https?://[^\\s\"'<>]+", text))

    page_base = page.url
    visible_same_domain = set(await _extract_visible_offer_urls_generic(page, domain))

    def _clean_urls(candidates: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in candidates:
            if not isinstance(raw, str):
                continue
            candidate = raw.strip()
            if candidate.startswith("/"):
                candidate = urljoin(page_base, candidate)
            if _is_recommendation_tracking_url(candidate):
                continue
            normalized = _normalize_offer_url(candidate)
            if not normalized:
                continue
            parts = urlsplit(normalized)
            if parts.scheme not in {"http", "https"}:
                continue
            if not parts.netloc:
                normalized = _normalize_offer_url(urljoin(page_base, normalized))
                parts = urlsplit(normalized)
            if _normalize_domain(parts.netloc) != _normalize_domain(domain):
                continue
            if not _is_probable_offer_detail_url(normalized, domain):
                continue
            if visible_same_domain and normalized not in visible_same_domain:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            out.append(normalized)
        return _filter_by_dominant_path_family(out)

    out = _clean_urls(raw_urls)
    if out:
        return out

    try:
        relaxed = await page.extract(
            """
            Extract visible URLs for real job-offer detail pages from the current results view.
            Include offers revealed by pagination or load-more interactions.
            Exclude sponsored/promoted content, recommendation widgets, similar-jobs blocks, and navigation/filter/sort/account links.
            Return absolute URLs only.
            """,
            schema=OfferUrls,
        )
    except Exception:
        relaxed = None

    relaxed_raw: list[str] = []
    if isinstance(relaxed, OfferUrls):
        relaxed_raw = relaxed.urls
    elif relaxed is not None:
        relaxed_raw = _extract_urls_from_payload(relaxed)

    out = _clean_urls(relaxed_raw)
    if out:
        _trace(f"extract: relaxed llm fallback used ({len(out)})")
        return out

    if allow_structural_fallback:
        structural = await _extract_offer_like_urls_by_structure(page, domain)
        if structural:
            _trace(f"extract: structural fallback used ({len(structural)})")
        return structural
    return []


async def _extract_visible_offer_urls_generic(page: Any, domain: str) -> list[str]:
    """Generic deterministic same-domain link sweep (site-agnostic fallback only)."""
    try:
        raw = await page.evaluate(
            """() => {
              return Array.from(document.querySelectorAll("a[href]")).map((a) => {
                try {
                  return new URL(a.getAttribute("href") || a.href, window.location.origin).toString();
                } catch {
                  return null;
                }
              }).filter(Boolean);
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
        if _is_recommendation_tracking_url(entry):
            continue
        normalized = _normalize_offer_url(entry)
        if not normalized:
            continue
        parts = urlsplit(normalized)
        if parts.scheme not in {"http", "https"}:
            continue
        if _normalize_domain(parts.netloc) != _normalize_domain(domain):
            continue
        if not _is_probable_offer_detail_url(normalized, domain):
            continue
        # Keep URL-only heuristic generic and minimal: require non-root path.
        if parts.path in {"", "/"}:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


async def _extract_offer_like_urls_by_structure(page: Any, domain: str) -> list[str]:
    """Generic structural fallback based on card-like anchors and dominant URL family."""
    try:
        raw = await page.evaluate(
            """() => {
              const links = Array.from(document.querySelectorAll("a[href]"));
              const out = [];
              for (const a of links) {
                let url = "";
                try {
                  url = new URL(a.getAttribute("href") || a.href, window.location.origin).toString();
                } catch {
                  continue;
                }
                const text = (a.textContent || "").trim().replace(/\\s+/g, " ");
                const hasHeading = Boolean(a.querySelector("h1,h2,h3,h4"));
                const inChrome = Boolean(a.closest("header,footer,nav,aside,dialog"));
                out.push({ url, text, hasHeading, inChrome });
              }
              return out;
            }"""
        )
    except Exception:
        return []

    if not isinstance(raw, list):
        return []

    candidates: list[str] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        url = row.get("url")
        if not isinstance(url, str):
            continue
        if not row.get("hasHeading"):
            continue
        if row.get("inChrome"):
            continue
        text = str(row.get("text") or "")
        if len(text) < 12:
            continue
        normalized = _normalize_offer_url(url)
        if not normalized:
            continue
        parsed = urlsplit(normalized)
        if _normalize_domain(parsed.netloc) != _normalize_domain(domain):
            continue
        if not _is_probable_offer_detail_url(normalized, domain):
            continue
        if _is_recommendation_tracking_url(url):
            continue
        candidates.append(normalized)

    if not candidates:
        return []

    # Keep dominant path family among card-like links.
    family_counts = Counter(_path_family(urlsplit(url).path) for url in candidates if _path_family(urlsplit(url).path))
    if not family_counts:
        return []
    dominant, dominant_count = family_counts.most_common(1)[0]
    if dominant_count < 3:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for url in candidates:
        if _path_family(urlsplit(url).path) != dominant:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def _path_prefix(path: str, depth: int = 2) -> str:
    parts = [part for part in path.split("/") if part]
    if not parts:
        return "/"
    return "/" + "/".join(parts[:depth]).lower()


def _infer_offer_prefixes(seed_urls: list[str], depth: int = 2) -> set[str]:
    counts = Counter(_path_prefix(urlsplit(url).path, depth=depth) for url in seed_urls)
    return {prefix for prefix, count in counts.items() if prefix != "/" and count >= 2}


def _signature_hash(payload: Any) -> str:
    try:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except Exception:
        serialized = repr(payload)
    return hashlib.sha1(serialized.encode("utf-8", errors="ignore")).hexdigest()


def _protocol_offer_slug_to_path(slug: str) -> Optional[str]:
    cleaned = slug.strip().strip("/")
    if not cleaned:
        return None
    lower = cleaned.lower()
    if lower.startswith("szczegoly/praca/"):
        return "/" + cleaned.lstrip("/")
    if ",oferta," in lower:
        if re.fullmatch(r"[a-z0-9-]+,oferta,[0-9a-f-]{20,}", cleaned, flags=re.I):
            return f"/szczegoly/praca/{cleaned}"
        return None
    return None


async def _capture_listing_state_signature(page: Any, domain: str) -> dict[str, Any]:
    """Capture lightweight listing state and derive a stable signature for transition detection."""
    try:
        snapshot = await page.evaluate(
            """() => {
              const normalizeOffer = (href) => {
                try {
                  const u = new URL(href, window.location.href);
                  u.hash = "";
                  u.search = "";
                  return u.toString();
                } catch {
                  return "";
                }
              };
              const normalizePage = (href) => {
                try {
                  const u = new URL(href, window.location.href);
                  u.hash = "";
                  return u.toString();
                } catch {
                  return "";
                }
              };
              const offerLinks = [];
              const offerSeen = new Set();
              for (const a of Array.from(document.querySelectorAll("a[href]"))) {
                const href = normalizeOffer(a.getAttribute("href") || a.href || "");
                if (!href || !href.startsWith(window.location.origin)) continue;
                if (!href.includes("/szczegoly/praca/")) continue;
                if (offerSeen.has(href)) continue;
                offerSeen.add(href);
                offerLinks.push(href);
              }

              const currentNode = document.querySelector("[aria-current='page'], .pagination [aria-current], nav [aria-current]");
              const currentPageLabel = (currentNode && currentNode.textContent ? currentNode.textContent : "").trim();

              const nextDataNode = document.getElementById("__NEXT_DATA__");
              const nextDataText = nextDataNode && nextDataNode.textContent ? nextDataNode.textContent : "";
              let nextDataChecksum = 0;
              const cap = Math.min(nextDataText.length, 8000);
              for (let idx = 0; idx < cap; idx++) {
                nextDataChecksum = (nextDataChecksum * 33 + nextDataText.charCodeAt(idx)) >>> 0;
              }

              const nextHints = [];
              const nextSeen = new Set();
              const nextTerms = /(next|nast[eę]pn|dalej|kolejn|›|»|→)/i;
              const prevTerms = /(prev|poprzed|wstecz|back|‹|«|←)/i;
              for (const el of Array.from(document.querySelectorAll("a[href],button,[role='button']"))) {
                const text = (el.textContent || "").trim().toLowerCase();
                const aria = (el.getAttribute("aria-label") || "").trim().toLowerCase();
                const rel = (el.getAttribute("rel") || "").toLowerCase();
                const disabled = Boolean(
                  el.hasAttribute("disabled")
                  || el.getAttribute("aria-disabled") === "true"
                  || (el.tagName === "BUTTON" && el.disabled)
                );
                if (disabled) continue;
                const inPagination = Boolean(
                  el.closest("nav,[aria-label*='pagin' i],[class*='pagin' i],[data-testid*='pagin' i],ul,ol")
                );
                const looksPrev = rel.includes("prev") || prevTerms.test(text) || prevTerms.test(aria);
                if (looksPrev) continue;
                const looksNext = rel.includes("next") || nextTerms.test(text) || nextTerms.test(aria);
                const href = el.tagName === "A" ? normalizePage(el.getAttribute("href") || el.href || "") : "";
                const hasPageMarker = Boolean(
                  href && /(?:[?&](page|strona|pagenumber)=\d+)|\/(?:page|strona)\/\d+/i.test(href)
                );
                const looksNumeric = /^\d+$/.test(text);
                if (!(looksNext || (inPagination && (hasPageMarker || looksNumeric)))) continue;
                if (!href) continue;
                if (nextSeen.has(href)) continue;
                nextSeen.add(href);
                nextHints.push(href);
              }

              return {
                url: window.location.href,
                offerLinks: offerLinks.slice(0, 240),
                nextHints: nextHints.slice(0, 80),
                currentPageLabel,
                nextDataDigest: `${nextDataChecksum}:${nextDataText.length}`,
                hrefCount: document.querySelectorAll("a[href]").length
              };
            }"""
        )
    except Exception:
        snapshot = {}

    if not isinstance(snapshot, dict):
        snapshot = {}

    normalized_url = _normalize_page_url(str(snapshot.get("url") or page.url or ""))
    offer_links: list[str] = []
    for value in snapshot.get("offerLinks", []) if isinstance(snapshot.get("offerLinks"), list) else []:
        if not isinstance(value, str):
            continue
        normalized = _normalize_offer_url(value)
        if not normalized:
            continue
        parsed = urlsplit(normalized)
        if _normalize_domain(parsed.netloc) != _normalize_domain(domain):
            continue
        if not _is_probable_offer_detail_url(normalized, domain):
            continue
        if normalized not in offer_links:
            offer_links.append(normalized)

    next_hints: list[str] = []
    for value in snapshot.get("nextHints", []) if isinstance(snapshot.get("nextHints"), list) else []:
        if not isinstance(value, str):
            continue
        normalized = _normalize_page_url(value)
        if not normalized:
            continue
        if normalized not in next_hints:
            next_hints.append(normalized)

    payload = {
        "url": normalized_url,
        "offer_links": sorted(offer_links),
        "next_hints": sorted(next_hints)[:20],
        "current_page": str(snapshot.get("currentPageLabel") or "").strip().lower(),
        "next_data_digest": str(snapshot.get("nextDataDigest") or ""),
        "href_count": int(snapshot.get("hrefCount") or 0),
    }
    return {
        "signature": _signature_hash(payload),
        "url": normalized_url,
        "offer_links": offer_links,
        "next_hints": next_hints,
        "current_page": str(snapshot.get("currentPageLabel") or "").strip(),
    }


async def _wait_for_listing_state_change(
    page: Any,
    domain: str,
    before_signature: str,
    timeout_ms: int = 9000,
) -> Optional[dict[str, Any]]:
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
    while asyncio.get_running_loop().time() < deadline:
        await page.wait_for_timeout(250)
        snapshot = await _capture_listing_state_signature(page, domain)
        if snapshot.get("signature") != before_signature:
            return snapshot
    return None


async def _discover_pagination_urls(page: Any, target_url: str, domain: str) -> list[str]:
    """Collect pagination/listing URLs from current DOM state without hardcoded page numbers."""
    snapshot = await _capture_listing_state_signature(page, domain)
    base_path = urlsplit(target_url).path.rstrip("/")
    out: list[str] = []
    seen: set[str] = set()
    for candidate in snapshot.get("next_hints", []):
        if not isinstance(candidate, str):
            continue
        parsed = urlsplit(candidate)
        if _normalize_domain(parsed.netloc) != _normalize_domain(domain):
            continue
        path = parsed.path.rstrip("/")
        same_listing_path = path == base_path or bool(base_path and path.startswith(base_path + "/"))
        has_page_indicator = bool(
            re.search(r"(?:^|[?&])(page|strona|pagenumber)=\d+", parsed.query, flags=re.I)
            or re.search(r"/(?:page|strona)/\d+(?:/|$)", parsed.path, flags=re.I)
        )
        if not (same_listing_path or has_page_indicator):
            continue
        normalized = _normalize_page_url(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


async def _click_next_pagination_control(page: Any) -> bool:
    """Click best-effort "next page" control when href discovery is not enough."""
    try:
        clicked = await page.evaluate(
            """() => {
              const resolve = (href) => {
                try { return new URL(href, window.location.href).toString(); } catch { return ""; }
              };
              const nextTerms = /(next|nast[eę]pn|dalej|kolejn|›|»|→)/i;
              const prevTerms = /(prev|poprzed|wstecz|back|‹|«|←)/i;
              let bestEl = null;
              let bestScore = -100000;
              let bestLabel = "";
              let bestHref = "";

              for (const el of Array.from(document.querySelectorAll("a[href],button,[role='button']"))) {
                const text = (el.textContent || "").trim().toLowerCase();
                const aria = (el.getAttribute("aria-label") || "").trim().toLowerCase();
                const rel = (el.getAttribute("rel") || "").toLowerCase();
                const disabled = Boolean(
                  el.hasAttribute("disabled")
                  || el.getAttribute("aria-disabled") === "true"
                  || (el.tagName === "BUTTON" && el.disabled)
                );
                if (disabled) continue;
                const inPagination = Boolean(
                  el.closest("nav,[aria-label*='pagin' i],[class*='pagin' i],[data-testid*='pagin' i],ul,ol")
                );
                const looksPrev = rel.includes("prev") || prevTerms.test(text) || prevTerms.test(aria);
                if (looksPrev) continue;
                const looksNext = rel.includes("next") || nextTerms.test(text) || nextTerms.test(aria);
                const href = el.tagName === "A" ? resolve(el.getAttribute("href") || el.href || "") : "";
                const hasPageMarker = Boolean(
                  href && /(?:[?&](page|strona|pagenumber)=\d+)|\/(?:page|strona)\/\d+/i.test(href)
                );
                if (!(inPagination || looksNext || rel.includes("next") || hasPageMarker)) continue;

                let score = 0;
                if (rel.includes("next")) score += 120;
                if (looksNext) score += 80;
                if (inPagination) score += 30;
                if (hasPageMarker) score += 20;
                if (/^\d+$/.test(text)) score -= 12;
                if (href && href === window.location.href) score -= 30;
                if (score > bestScore) {
                  bestScore = score;
                  bestEl = el;
                  bestLabel = text || aria || href || "";
                  bestHref = href || "";
                }
              }

              if (!bestEl || bestScore < 60) {
                return { clicked: false, reason: "no-control" };
              }
              bestEl.click();
              return { clicked: true, score: bestScore, label: bestLabel, href: bestHref };
            }"""
        )
    except Exception:
        return False

    if isinstance(clicked, dict) and clicked.get("clicked"):
        _trace(f"pagination-click: label={clicked.get('label', '')[:100]} score={clicked.get('score')}")
        return True
    return False


async def _extract_offer_urls_from_embedded_state(page: Any, domain: str, seed_urls: Optional[list[str]] = None) -> list[str]:
    """Generic structured-data URL sweep from embedded JSON state."""
    try:
        raw = await page.evaluate(
            """() => {
              const nodes = [];
              const nextData = document.getElementById("__NEXT_DATA__");
              if (nextData && nextData.textContent) nodes.push(nextData.textContent);
              for (const script of Array.from(document.querySelectorAll("script[type='application/json'], script[type='application/ld+json']"))) {
                if (script.textContent) nodes.push(script.textContent);
              }
              for (const script of Array.from(document.querySelectorAll("script:not([src])"))) {
                if (!script.textContent) continue;
                if (script.type && !["", "text/javascript", "application/javascript", "module"].includes(script.type)) continue;
                if (script.textContent.length > 1500000) continue;
                nodes.push(script.textContent);
              }

              const urls = [];
              const slugs = [];
              const seenUrls = new Set();
              const seenSlugs = new Set();
              const pushMaybe = (value, fromOfferKey = false) => {
                if (typeof value !== "string") return;
                const text = value.trim();
                if (!text || text.length > 800) return;
                const looksLikeOfferSlug = /^[a-z0-9-]+,oferta,[0-9a-f-]{20,}$/i.test(text);
                const looksLikePlainSlug = /^[a-z0-9-]{8,}$/i.test(text);
                const plainSlugAllowed = fromOfferKey && looksLikePlainSlug;
                if (!text.includes("/") && !looksLikeOfferSlug && !plainSlugAllowed) return;
                if (/^https?:\\/\\//i.test(text) || text.startsWith("/") || looksLikeOfferSlug) {
                  if (!seenUrls.has(text)) {
                    seenUrls.add(text);
                    urls.push(text);
                  }
                }
                if (looksLikeOfferSlug || plainSlugAllowed) {
                  if (!seenSlugs.has(text)) {
                    seenSlugs.add(text);
                    slugs.push(text);
                  }
                }
              };
              const visit = (node) => {
                const queue = [{ value: node, hint: false }];
                let visited = 0;
                while (queue.length) {
                  visited += 1;
                  if (visited > 250000) break;
                  const current = queue.shift();
                  if (!current) continue;
                  const cur = current.value;
                  const hint = Boolean(current.hint);
                  if (!cur) continue;
                  if (typeof cur === "string") {
                    if (hint) pushMaybe(cur);
                    continue;
                  }
                  if (Array.isArray(cur)) {
                    for (const item of cur) queue.push({ value: item, hint });
                    continue;
                  }
                  if (typeof cur === "object") {
                    for (const [key, value] of Object.entries(cur)) {
                      const lowerKey = String(key || "").toLowerCase();
                      const isOfferName = /offerurlname/.test(lowerKey);
                      const keyLooksOffer = /offerurlname|offer|job|url|href|link|path|slug/.test(lowerKey);
                      if (typeof value === "string" && keyLooksOffer) {
                        pushMaybe(value, isOfferName);
                        if (!seenSlugs.has(value)) {
                          seenSlugs.add(value);
                          slugs.push(value);
                        }
                      }
                      queue.push({ value, hint: keyLooksOffer });
                    }
                  }
                }
              };
              for (const text of nodes) {
                if (typeof text !== "string" || !text) continue;
                const offerNameMatches = text.matchAll(/["']offerUrlName["']\\s*[:=]\\s*["']([^"']+)["']/gi);
                for (const match of offerNameMatches) {
                  if (match && match[1]) pushMaybe(match[1], true);
                }
                const directDetailMatches = text.matchAll(/(?:https?:\\/\\/[^"'<\\s]+)?\\/szczegoly\\/praca\\/[a-z0-9-]+(?:,oferta,[0-9a-f-]{20,})?/gi);
                for (const match of directDetailMatches) {
                  if (match && match[0]) pushMaybe(match[0].replace(/\\\\\\//g, "/"));
                }
                if (/offerUrlName/i.test(text)) {
                  const slugMatches = text.matchAll(/[a-z0-9-]+,oferta,[0-9a-f-]{20,}/gi);
                  for (const match of slugMatches) {
                    if (match && match[0]) pushMaybe(match[0]);
                  }
                }
                try {
                  const parsed = JSON.parse(text);
                  visit(parsed);
                } catch {}
              }
              return { urls, slugs };
            }"""
        )
    except Exception:
        return []

    if not isinstance(raw, dict):
        return []

    prefixes = _infer_offer_prefixes(seed_urls or [])
    raw_urls: list[str] = []
    for entry in raw.get("urls", []) if isinstance(raw.get("urls"), list) else []:
        if isinstance(entry, str):
            raw_urls.append(entry)
    for entry in raw.get("slugs", []) if isinstance(raw.get("slugs"), list) else []:
        if not isinstance(entry, str):
            continue
        slug_path = _protocol_offer_slug_to_path(entry)
        if slug_path:
            raw_urls.append(slug_path)

    out: list[str] = []
    seen: set[str] = set()
    for item in raw_urls:
        candidate = item.strip()
        if _is_recommendation_tracking_url(candidate):
            continue
        if (
            _normalize_domain(domain).endswith("theprotocol.it")
            and not candidate.startswith(("http://", "https://", "/"))
        ):
            slug_path = _protocol_offer_slug_to_path(candidate)
            if not slug_path:
                continue
            candidate = slug_path
        if (
            _normalize_domain(domain).endswith("theprotocol.it")
            and ",oferta," in candidate.lower()
            and not candidate.startswith(("http://", "https://", "/"))
        ):
            candidate = f"/szczegoly/praca/{candidate}"
        normalized = _normalize_offer_url(urljoin(page.url, candidate))
        if not normalized:
            continue
        parsed = urlsplit(normalized)
        if _normalize_domain(parsed.netloc) != _normalize_domain(domain):
            continue
        if not _is_probable_offer_detail_url(normalized, domain):
            continue
        if prefixes and _path_prefix(parsed.path) not in prefixes:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return _filter_by_dominant_path_family(out)


def _url_path_signature(path: str) -> str:
    """Convert path into a generic signature inferred from real offers."""
    parts = [p for p in path.split("/") if p]
    if not parts:
        return "/"

    transformed: list[str] = []
    for seg in parts:
        lower = seg.lower()
        # Collapse UUID-like tokens first.
        lower = re.sub(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            "{uuid}",
            lower,
        )
        # Collapse long numeric runs.
        lower = re.sub(r"\d+", "{n}", lower)
        transformed.append(lower)
    return "/" + "/".join(transformed)


def _infer_offer_signatures(seed_urls: list[str]) -> set[str]:
    """Infer dominant URL path signatures from LLM-confirmed offer URLs."""
    counts: dict[str, int] = {}
    for url in seed_urls:
        sig = _url_path_signature(urlsplit(url).path)
        counts[sig] = counts.get(sig, 0) + 1

    if not counts:
        return set()

    threshold = max(2, int(len(seed_urls) * 0.2))
    # Keep only signatures that repeat and are not too generic.
    return {
        sig for sig, cnt in counts.items()
        if cnt >= threshold and sig not in {"/", "/{n}"}
    }


def _summarize_url_signatures(urls: list[str], limit: int = 4) -> str:
    if not urls:
        return "-"
    counts = Counter(_url_path_signature(urlsplit(url).path) for url in urls)
    top = counts.most_common(limit)
    return ", ".join(f"{sig}:{count}" for sig, count in top)


def _path_family(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    return parts[0].lower() if parts else ""


def _path_family_cluster(family: str) -> str:
    """Cluster close path families (e.g. oferta / oferta-pracy) without site-specific selectors."""
    if not family:
        return ""
    cleaned = re.sub(r"[^a-z0-9-]+", "", family.lower())
    first = cleaned.split("-")[0]
    return first or cleaned


def _filter_by_dominant_path_family(urls: list[str]) -> list[str]:
    if len(urls) < 8:
        return urls
    families = [_path_family(urlsplit(url).path) for url in urls]
    families = [name for name in families if name]
    if not families:
        return urls
    cluster_counts = Counter(_path_family_cluster(name) for name in families)
    ranked = cluster_counts.most_common()
    dominant_cluster, dominant_count = ranked[0]
    second_count = ranked[1][1] if len(ranked) > 1 else 0
    dominant_ratio = dominant_count / max(1, len(urls))
    # Keep strong secondary clusters to avoid pruning valid paginated families.
    if second_count >= max(8, int(len(urls) * 0.12)):
        return urls
    if dominant_ratio < 0.7:
        return urls
    return [
        url for url in urls
        if _path_family_cluster(_path_family(urlsplit(url).path)) == dominant_cluster
    ]


def _action_to_text(action: dict[str, Any]) -> str:
    keys = ("description", "instruction", "label", "selector", "method", "action")
    parts: list[str] = []
    for key in keys:
        value = action.get(key)
        if not value:
            continue
        text = str(value).strip().replace("\n", " ")
        if text:
            parts.append(text)
    if parts:
        return " | ".join(parts)
    return json.dumps(action, ensure_ascii=False)


def _choose_reveal_action(
    actions: list[dict[str, Any]],
    prefer_pagination: bool = False,
) -> tuple[Optional[dict[str, Any]], int]:
    if not actions:
        return None
    positive_terms = (
        "load more",
        "show more",
        "more offers",
        "more jobs",
        "next",
        "nast",
        "kolejn",
        "pagination",
        "page ",
        "strona",
    )
    strong_next_terms = ("next", "nast", "dalej", "kolejna")
    load_more_terms = ("load more", "show more", "more offers", "więcej ofert", "pokaż więcej")
    negative_terms = (
        "sponsor",
        "promo",
        "advert",
        "banner",
        "recommended",
        "wybraliśmy",
        "dla ciebie",
        "podobne",
        "similar jobs",
        "you may also",
        "newsletter",
        "share",
        "sort",
        "filter",
        "login",
        "sign in",
        "apply now",
    )
    strong_previous_terms = ("previous", "prev", "poprzed", "wstecz", "back")
    first_page_terms = ("page 1", "strona 1", "strony 1", "stronę 1", "idź do strony 1")
    best: Optional[dict[str, Any]] = None
    best_score = -10_000
    for action in actions:
        text = _action_to_text(action).lower()
        score = 0
        if "click" in text:
            score += 2
        if any(term in text for term in positive_terms):
            score += 6
        if any(term in text for term in load_more_terms):
            score += 12
        if any(term in text for term in strong_next_terms):
            score += 4
        if any(term in text for term in negative_terms):
            score -= 8
        if any(term in text for term in strong_previous_terms):
            score -= 10
        if any(term in text for term in first_page_terms):
            score -= 12
        if "page=" in text or "/p/" in text:
            score += 3
        if prefer_pagination:
            if "page" in text or "stron" in text or any(term in text for term in strong_next_terms):
                score += 10
            if any(term in text for term in load_more_terms):
                score -= 6
        if score > best_score:
            best_score = score
            best = action
    return best, best_score


async def _extract_visible_offer_urls_by_inferred_pattern(page: Any, domain: str, seed_urls: list[str]) -> list[str]:
    """Use inferred offer URL signature(s) to collect additional real offers."""
    signatures = _infer_offer_signatures(seed_urls)
    if not signatures:
        return []

    candidates = await _extract_visible_offer_urls_generic(page, domain)
    out: list[str] = []
    seen: set[str] = set()
    for url in candidates:
        sig = _url_path_signature(urlsplit(url).path)
        if sig not in signatures:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


async def _reveal_more_results_with_llm(page: Any, prefer_pagination: bool = False) -> bool:
    """Use Stagehand LLM to choose next action: load-more or pagination next."""
    domain = urlsplit(page.url).netloc
    before_state = await _capture_listing_state_signature(page, domain)
    before_signature = str(before_state.get("signature") or "")
    try:
        actions = await page.observe(
            """
            Identify actions that reveal additional real job offers in the results listing.
            Prefer, in order:
            1) "load/show more offers/jobs" style controls for the listing,
            2) next page in listing pagination.
            Ignore sponsored/promo areas, ads, and non-listing UI like sort/filter/login/share.
            Return only actions that should increase visible non-promotional offers.
            """
        )
    except Exception:
        return False

    if not actions:
        _trace("observe: actions=0")
        return False

    preview = [_action_to_text(a)[:140] for a in actions[:3]]
    _trace(f"observe: actions={len(actions)} top={preview}")

    chosen, chosen_score = _choose_reveal_action(actions, prefer_pagination=prefer_pagination)
    if not chosen:
        _trace("observe: no acceptable reveal action")
        return False
    _trace(
        f"observe: chosen={_action_to_text(chosen)[:180]} score={chosen_score} mode={'pagination' if prefer_pagination else 'load-more-first'}"
    )

    try:
        result = await page.act(chosen)
    except Exception:
        _trace("act: failed")
        return False

    changed = await _wait_for_listing_state_change(
        page,
        domain,
        before_signature=before_signature,
        timeout_ms=9500,
    )
    success = bool(getattr(result, "success", True))
    if changed is None:
        _trace(f"act: success={success} state_change=no")
        return False
    _trace(
        f"act: success={success} state_change=yes page={changed.get('url', '-')}"
    )
    return success


async def _collect_offer_urls_universal(page: Any, domain: str, expected_count: Optional[int] = None) -> list[str]:
    """Universal collection: lazy scroll + optional load-more + optional pagination."""
    max_steps = int(os.getenv("STAGEHAND_POC_MAX_STEPS", "5"))

    seen_urls: set[str] = set()
    visited_pages: set[str] = set()
    stagnation = 0

    for idx in range(max_steps):
        step_num = idx + 1
        current_page = _normalize_page_url(page.url)
        _trace(f"step={step_num} page={current_page} seen={len(seen_urls)} stagnation={stagnation}")
        if current_page in visited_pages and stagnation >= 4:
            _trace(f"stop: revisited page at step={step_num} with stagnation={stagnation}")
            break
        visited_pages.add(current_page)

        before = len(seen_urls)
        extracted_urls = await _extract_visible_offer_urls_with_llm(
            page,
            domain,
            allow_structural_fallback=(len(seen_urls) == 0),
        )
        embedded_urls = await _extract_offer_urls_from_embedded_state(page, domain, seed_urls=list(seen_urls))
        _trace(
            f"extract: step={step_num} got={len(extracted_urls)} signatures={_summarize_url_signatures(extracted_urls)}"
        )
        added_from_extract = 0
        for url in extracted_urls:
            if url not in seen_urls:
                added_from_extract += 1
                seen_urls.add(url)
        _trace(f"dedupe: step={step_num} extracted={len(extracted_urls)} added={added_from_extract} seen={len(seen_urls)}")
        added_from_embedded = 0
        for url in embedded_urls:
            if url not in seen_urls:
                added_from_embedded += 1
                seen_urls.add(url)
        _trace(f"dedupe: step={step_num} embedded={len(embedded_urls)} added={added_from_embedded} seen={len(seen_urls)}")
        if expected_count and len(seen_urls) >= expected_count:
            _trace(f"stop: expected count reached in universal ({len(seen_urls)}/{expected_count})")
            break

        if _inferred_pattern_enabled():
            inferred_urls = await _extract_visible_offer_urls_by_inferred_pattern(page, domain, list(seen_urls))
            _trace(f"inferred: step={step_num} got={len(inferred_urls)}")
            added_from_inferred = 0
            for url in inferred_urls:
                if url not in seen_urls:
                    added_from_inferred += 1
                    seen_urls.add(url)
            _trace(f"dedupe: step={step_num} inferred={len(inferred_urls)} added={added_from_inferred} seen={len(seen_urls)}")

        if len(seen_urls) > before:
            stagnation = 0
        else:
            stagnation += 1

        prefer_pagination = stagnation >= 1 and len(seen_urls) >= 40
        moved = await _reveal_more_results_with_llm(page, prefer_pagination=prefer_pagination)
        if moved:
            _trace(f"move: step={step_num} via-observe seen={len(seen_urls)}")
            continue

        # Fallback lazy-load trigger when no explicit action is found.
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(800)
        except Exception:
            pass

        if _generic_fallback_enabled():
            after_scroll_before = len(seen_urls)
            fallback_urls = await _extract_visible_offer_urls_generic(page, domain)
            _trace(f"fallback: step={step_num} extracted={len(fallback_urls)}")
            for url in fallback_urls:
                seen_urls.add(url)

            if len(seen_urls) > after_scroll_before:
                stagnation = 0
            else:
                stagnation += 1

        if stagnation >= 3:
            _trace(f"stop: stagnation>=3 at step={step_num}")
            break

    filtered = list(seen_urls)
    if _generic_fallback_enabled() or len(filtered) > 120:
        filtered = _filter_by_dominant_path_family(filtered)
    if expected_count and len(filtered) > expected_count:
        filtered = filtered[:expected_count]
    _trace(f"done: total_seen={len(seen_urls)} filtered={len(filtered)} pages={len(visited_pages)}")
    return filtered


async def _collect_offer_urls_recovery(
    page: Any,
    target_url: str,
    domain: str,
    expected_count: Optional[int] = None,
    seed_urls: Optional[list[str]] = None,
) -> list[str]:
    """Dynamic pagination sweep for missed offers using runtime-discovered controls only."""
    seen_urls: set[str] = set(seed_urls or [])
    max_pages = int(os.getenv("STAGEHAND_RECOVERY_MAX_PAGES", "40"))
    max_reveals_per_page = int(os.getenv("STAGEHAND_RECOVERY_MAX_ACTIONS", "2"))

    async def _collect_current_page_urls() -> int:
        urls = await _extract_visible_offer_urls_with_llm(
            page,
            domain,
            allow_structural_fallback=(len(seen_urls) == 0),
        )
        embedded = await _extract_offer_urls_from_embedded_state(page, domain, seed_urls=list(seen_urls) + urls)
        urls = urls + [u for u in embedded if u not in urls]
        if _inferred_pattern_enabled():
            inferred = await _extract_visible_offer_urls_by_inferred_pattern(page, domain, list(seen_urls) + urls)
            urls = urls + [u for u in inferred if u not in urls]
        if not urls and _generic_fallback_enabled():
            urls = await _extract_visible_offer_urls_generic(page, domain)
        added = 0
        for u in urls:
            if u not in seen_urls:
                added += 1
                seen_urls.add(u)
        _trace(f"recovery: extract got={len(urls)} added={added} seen={len(seen_urls)}")
        return added

    def _enqueue(url: str, queue: list[str], queued: set[str], visited: set[str]) -> None:
        normalized = _normalize_page_url(url)
        if not normalized:
            return
        if normalized in visited or normalized in queued:
            return
        queue.append(normalized)
        queued.add(normalized)

    def _dequeue_next(queue: list[str], queued: set[str], visited: set[str], current_url: str) -> Optional[str]:
        current_normalized = _normalize_page_url(current_url)
        while queue:
            candidate = queue.pop(0)
            queued.discard(candidate)
            if candidate in visited:
                continue
            if candidate == current_normalized:
                continue
            return candidate
        return None

    # Recovery always restarts from the target listing URL so pagination discovery is deterministic.
    if _normalize_page_url(page.url) != _normalize_page_url(target_url):
        _trace(f"recovery: restart at listing={target_url}")
        await page.goto(target_url)
        await page.wait_for_timeout(1300)
    await _wait_for_security_challenge(page)
    await _close_cookie_overlay(page)

    queued_pages: list[str] = []
    queued_lookup: set[str] = set()
    visited_pages: set[str] = set()
    visited_state_hashes: set[str] = set()
    pages_processed = 0
    stop_reason = "pagination-exhausted"
    total_recovery_actions = 0
    while pages_processed < max_pages:
        await _wait_for_security_challenge(page)
        await _close_cookie_overlay(page)
        try:
            await page.evaluate("window.scrollTo(0, 800)")
            await page.wait_for_timeout(500)
        except Exception:
            pass

        state = await _capture_listing_state_signature(page, domain)
        state_hash = str(state.get("signature") or "")
        current_page_url = _normalize_page_url(page.url)
        if state_hash in visited_state_hashes:
            stop_reason = "state-loop-detected"
            _trace(f"recovery: repeated-state hash={state_hash[:10]} page={current_page_url}")
            break
        visited_state_hashes.add(state_hash)
        visited_pages.add(current_page_url)
        pages_processed += 1

        added = await _collect_current_page_urls()
        if expected_count and len(seen_urls) >= expected_count:
            stop_reason = "expected-count-reached"
            break
        if pages_processed > 1 and added == 0:
            stop_reason = "no-new-offers-on-next-page"
            _trace(f"recovery: stop no new offers on page={current_page_url}")
            break

        discovered = await _discover_pagination_urls(page, target_url, domain)
        for discovered_url in discovered:
            _enqueue(discovered_url, queued_pages, queued_lookup, visited_pages)

        next_page_url = _dequeue_next(queued_pages, queued_lookup, visited_pages, current_page_url)
        if next_page_url:
            _trace(
                f"recovery: goto next={next_page_url} queue={len(queued_pages)} visited={len(visited_pages)}"
            )
            before_signature = state_hash
            await page.goto(next_page_url)
            changed = await _wait_for_listing_state_change(
                page,
                domain,
                before_signature=before_signature,
                timeout_ms=11000,
            )
            if changed is None:
                _trace("recovery: goto completed but listing signature did not change")
            continue

        moved = False
        before_signature = state_hash
        if await _click_next_pagination_control(page):
            total_recovery_actions += 1
            changed = await _wait_for_listing_state_change(
                page,
                domain,
                before_signature=before_signature,
                timeout_ms=11000,
            )
            moved = changed is not None
            if not moved:
                _trace("recovery: next-control click did not change listing state")

        if not moved:
            for _ in range(max_reveals_per_page):
                acted = await _reveal_more_results_with_llm(page, prefer_pagination=True)
                if not acted:
                    break
                total_recovery_actions += 1
                moved = True
                break

        if not moved:
            stop_reason = "pagination-exhausted"
            break

    if pages_processed >= max_pages and stop_reason == "pagination-exhausted":
        stop_reason = "max-pages-guard"

    filtered = list(seen_urls)
    if _generic_fallback_enabled() or len(filtered) > 120:
        filtered = _filter_by_dominant_path_family(filtered)
    if expected_count and len(filtered) > expected_count:
        filtered = filtered[:expected_count]
    _trace(
        f"recovery: done seen={len(seen_urls)} filtered={len(filtered)} pages={len(visited_pages)} actions={total_recovery_actions} stop={stop_reason}"
    )
    return filtered


async def run_stagehand_protocol_first_offer() -> None:
    """Print all non-promotional offer URLs from theprotocol.it."""
    load_dotenv()
    if not _debug_enabled():
        # Reduce noisy AI SDK warnings in normal runs.
        os.environ.setdefault("AI_SDK_LOG_WARNINGS", "false")

    target_url = _env_str("STAGEHAND_POC_URL", PROTOCOL_POC_URL)
    domain = urlsplit(target_url).netloc

    model_name = _resolve_stagehand_model_name()
    if not model_name:
        raise RuntimeError("MODEL env var is required for Stagehand v3 (example: openai/gpt-5-mini).")

    try:
        from stagehand import AsyncStagehand  # type: ignore
    except Exception as exc:
        raise RuntimeError("Stagehand is not installed. Install deps and retry (poetry install).") from exc
    _configure_local_stagehand_server_logging()

    llm_api_key = _resolve_llm_api_key()
    llm_api_base = _resolve_llm_api_base()
    stagehand_env = _env_str("STAGEHAND_ENV", "LOCAL").lower()
    stagehand_server = "remote" if stagehand_env == "remote" else "local"
    requested_headless = _env_flag("STAGEHAND_HEADLESS")
    # Force headed mode for this scraping flow (Cloudflare challenge is unreliable in headless).
    headless = False
    if requested_headless:
        console.print("[yellow]STAGEHAND_HEADLESS=true ignored; forcing headed mode for this flow.[/yellow]")

    client_kwargs: dict[str, Any] = {
        "server": stagehand_server,
        "model_api_key": llm_api_key,
    }
    if stagehand_server == "local":
        client_kwargs["local_headless"] = headless
        if llm_api_key:
            client_kwargs["local_openai_api_key"] = llm_api_key
        chrome_path = _env_str("CHROME_PATH")
        if chrome_path:
            client_kwargs["local_chrome_path"] = chrome_path
    browserbase_api_key = _env_str("BROWSERBASE_API_KEY")
    browserbase_project_id = _env_str("BROWSERBASE_PROJECT_ID")
    if browserbase_api_key:
        client_kwargs["browserbase_api_key"] = browserbase_api_key
    if browserbase_project_id:
        client_kwargs["browserbase_project_id"] = browserbase_project_id

    client = AsyncStagehand(**client_kwargs)
    session: Any = None
    playwright: Optional[Playwright] = None
    browser: Optional[Browser] = None
    context: Optional[BrowserContext] = None
    stagehand_page: Any = None

    try:
        start_kwargs: dict[str, Any] = {
            "model_name": model_name,
            "self_heal": True,
            "verbose": 1 if _debug_enabled() else 0,
            "browser": {
                "type": "local" if stagehand_server == "local" else "browserbase",
                "launchOptions": {"headless": headless},
            },
        }
        session = await client.sessions.start(**start_kwargs)

        if not session.data.cdp_url:
            raise RuntimeError("Stagehand did not return cdpUrl; cannot run JS scroll/evaluate steps.")

        playwright = await async_playwright().start()
        browser = await playwright.chromium.connect_over_cdp(session.data.cdp_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        pw_page = context.pages[0] if context.pages else await context.new_page()
        stagehand_page = StagehandPage(
            session=session,
            pw_page=pw_page,
            model_name=model_name,
            model_api_key=llm_api_key,
            model_base_url=llm_api_base,
        )

        await stagehand_page.goto(target_url)
        await stagehand_page.wait_for_timeout(1500)
        console.print(Panel(
            f"[bold]Running Stagehand sandbox[/bold]\n"
            f"[cyan]Server:[/] {stagehand_server}\n"
            f"[cyan]Headless:[/] {headless}\n"
            f"[cyan]URL:[/] {target_url}",
            title="Stagehand Run",
            style="blue",
        ))
        await _wait_for_security_challenge(stagehand_page)
        if await _is_security_challenge(stagehand_page):
            raise RuntimeError(
                "Blocked by Cloudflare security verification in local Stagehand mode. "
                "Run with Browserbase remote session/proxy or a trusted IP to continue scraping."
            )
        try:
            title = await stagehand_page.evaluate("document.title")
            href_count = await stagehand_page.evaluate("document.querySelectorAll('a[href]').length")
            _debug(f"page_title={title}")
            _debug(f"page_initial_href_count={href_count}")
        except Exception:
            pass

        await _close_cookie_overlay(stagehand_page)
        await stagehand_page.wait_for_timeout(500)

        expected_count = await _extract_expected_offer_count(stagehand_page)
        if expected_count:
            _trace(f"expected_offers={expected_count}")
        else:
            _trace("expected_offers=unknown")

        offer_urls = await _collect_offer_urls_universal(stagehand_page, domain, expected_count=expected_count)
        recovery_trigger_min = int(os.getenv("STAGEHAND_RECOVERY_TRIGGER_MIN", "55"))
        needs_recovery = (
            (expected_count is not None and len(offer_urls) < expected_count)
            or len(offer_urls) < recovery_trigger_min
        )
        if needs_recovery:
            recovered = await _collect_offer_urls_recovery(
                stagehand_page,
                target_url,
                domain,
                expected_count=expected_count,
                seed_urls=offer_urls,
            )
            merged_urls = sorted(set(offer_urls) | set(recovered))
            if len(merged_urls) > len(offer_urls):
                _debug(f"recovery improved count from {len(offer_urls)} to {len(merged_urls)}")
                offer_urls = merged_urls
        if not offer_urls:
            raise RuntimeError("No non-promotional job offers found on page.")

        offer_urls = sorted(offer_urls)

        console.print(Panel(
            f"[cyan]URL:[/] {target_url}\n"
            f"[cyan]Domain:[/] {domain}\n"
            f"[bold green]Collected:[/] {len(offer_urls)}",
            title="Stagehand Sandbox Result",
            style="green",
        ))
        if _show_offer_urls_enabled():
            _print_offer_urls(offer_urls)
        console.print(f"OFFERS_COUNT={len(offer_urls)}")

        metrics_before_audit = await _fetch_client_metrics(client)
        cache_working = False
        cache_verdict_note = "no cache-hit evidence detected"
        cache_verdict_from_audit = False

        if _cache_audit_enabled():
            try:
                cache_probe_instruction = (
                    "Without clicking, typing, or navigating, read current page title and URL "
                    "and return one short sentence."
                )
                execute_kwargs: dict[str, Any] = {
                    "execute_options": {
                        "instruction": cache_probe_instruction,
                        "max_steps": 1,
                    },
                    "agent_config": {"model": model_name},
                    "should_cache": True,
                    "page": pw_page,
                }
                run1 = await session.execute(**execute_kwargs)
                run2 = await session.execute(**execute_kwargs)

                usage1 = run1.data.result.usage
                usage2 = run2.data.result.usage
                cached1 = int((usage1.cached_input_tokens or 0.0) if usage1 else 0.0)
                cached2 = int((usage2.cached_input_tokens or 0.0) if usage2 else 0.0)
                input1 = int((usage1.input_tokens or 0.0) if usage1 else 0.0)
                input2 = int((usage2.input_tokens or 0.0) if usage2 else 0.0)
                output1 = int((usage1.output_tokens or 0.0) if usage1 else 0.0)
                output2 = int((usage2.output_tokens or 0.0) if usage2 else 0.0)
                metrics_after_audit = await _fetch_client_metrics(client)
                delta_prompt = _metric_int(metrics_after_audit, "totalPromptTokens") - _metric_int(
                    metrics_before_audit, "totalPromptTokens"
                )
                delta_cached = _metric_int(metrics_after_audit, "totalCachedInputTokens") - _metric_int(
                    metrics_before_audit, "totalCachedInputTokens"
                )
                delta_completion = _metric_int(metrics_after_audit, "totalCompletionTokens") - _metric_int(
                    metrics_before_audit, "totalCompletionTokens"
                )
                delta_effective_input = max(delta_prompt + delta_cached, 0)
                cache_share = (100.0 * delta_cached / delta_effective_input) if delta_effective_input > 0 else 0.0
                metrics_available = metrics_before_audit is not None and metrics_after_audit is not None
                sdk_cache_hit = cached2 > 0
                sdk_input_drop = input1 > 0 and input2 > 0 and input2 < input1
                metrics_cache_hit = metrics_available and delta_cached > 0
                cache_working = sdk_cache_hit or metrics_cache_hit or sdk_input_drop
                cache_verdict_from_audit = True
                if sdk_cache_hit:
                    cache_verdict_note = "SDK usage reported cached input tokens on probe #2"
                elif metrics_cache_hit:
                    cache_verdict_note = "metrics delta reported cached input tokens"
                elif sdk_input_drop:
                    cache_verdict_note = "probe #2 used fewer input tokens than probe #1"
                else:
                    cache_verdict_note = "no cache-hit signals in probes"

                audit_lines = [
                    "[bold]Cache Audit[/bold]",
                    f"Probe 1 SDK usage: input={input1}, cached={cached1}, output={output1}",
                    f"Probe 2 SDK usage: input={input2}, cached={cached2}, output={output2}",
                    (
                        f"Metrics delta prompt={delta_prompt}, completion={delta_completion}, cached_input={delta_cached}"
                        if metrics_available else
                        "Metrics delta: unavailable"
                    ),
                    (f"Metrics cache share (input): {cache_share:.1f}%" if metrics_available else "Metrics cache share (input): n/a"),
                ]
                if cache_working:
                    audit_lines.append("[bold green]Result: cache working.[/bold green]")
                else:
                    audit_lines.append("[bold yellow]Result: no cache-hit evidence in this run.[/bold yellow]")
                audit_lines.append(f"Verdict basis: {cache_verdict_note}")
                console.print(Panel("\n".join(audit_lines), style="cyan", title="Stagehand Cache"))
            except Exception as exc:
                console.print(Panel("Cache audit unavailable for this run.", style="yellow", title="Stagehand Cache"))
                cache_verdict_note = "cache audit failed"
                _debug(f"cache audit failed: {exc!r}")

        metrics_payload = await _fetch_client_metrics(client)
        _print_metrics_summary(metrics_payload)
        if not cache_verdict_from_audit:
            # Fallback when audit is disabled/unavailable: rely on overall metrics only.
            cache_working = _metric_int(metrics_payload, "totalCachedInputTokens") > 0
            cache_verdict_note = (
                "metrics show cached input tokens"
                if cache_working else
                "no cached input tokens in metrics fallback"
            )

        replay_mode = _env_str("STAGEHAND_REPLAY_METRICS", "auto").lower()
        replay_enabled = (
            replay_mode == "on"
            or (replay_mode == "auto" and stagehand_server == "remote")
        )
        if not replay_enabled:
            console.print(
                "STAGEHAND_REPLAY_METRICS=skipped "
                "(local mode; replay endpoint is typically unavailable)"
            )
            console.print("STAGEHAND_CACHE_MODE=auto (SDK-managed)")
        else:
            try:
                replay = await client.sessions.replay(id=session.id)
                total_input = 0.0
                total_cached_input = 0.0
                total_output = 0.0
                for page_item in replay.data.pages or []:
                    for action in page_item.actions or []:
                        usage = action.token_usage
                        if not usage:
                            continue
                        total_input += usage.input_tokens or 0.0
                        total_cached_input += usage.cached_input_tokens or 0.0
                        total_output += usage.output_tokens or 0.0
                console.print(f"STAGEHAND_REPLAY_INPUT_TOKENS={int(total_input)}")
                console.print(f"STAGEHAND_REPLAY_CACHED_INPUT_TOKENS={int(total_cached_input)}")
                console.print(f"STAGEHAND_REPLAY_OUTPUT_TOKENS={int(total_output)}")
            except Exception as exc:
                status_code = (
                    getattr(exc, "status_code", None)
                    or getattr(getattr(exc, "response", None), "status_code", None)
                )
                if status_code:
                    console.print(f"STAGEHAND_REPLAY_METRICS=unavailable (status={status_code})")
                else:
                    console.print("STAGEHAND_REPLAY_METRICS=unavailable")
                console.print("STAGEHAND_CACHE_MODE=auto (SDK-managed)")
                _debug(f"replay metrics fetch failed: {exc!r}")
        console.print(f"CACHE_WORKING={'yes' if cache_working else 'no'}")
        _trace(f"cache_verdict={cache_verdict_note}")
    finally:
        if session is not None:
            try:
                await session.end()
            except Exception:
                pass
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass
        await client.close()


def main() -> None:
    asyncio.run(run_stagehand_protocol_first_offer())


if __name__ == "__main__":
    main()
