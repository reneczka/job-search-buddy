import asyncio
import os
import re
import subprocess
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from dotenv import load_dotenv
import httpx
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from pydantic import BaseModel, Field
from rich.console import Console

# Keep SDK warnings quiet unless explicitly overridden by environment.
os.environ.setdefault("AI_SDK_LOG_WARNINGS", "false")
os.environ.setdefault("LOG_LEVEL", "error")
os.environ.setdefault("PINO_LOG_LEVEL", "error")

console = Console()

BULLDOG_POC_URL = (
    "https://bulldogjob.pl/companies/jobs/s/skills,Python/experienceLevel,intern,junior/order,published,desc"
)
BD_FIRST_VISIBLE_OFFER_URL = "https://bulldogjob.pl/companies/jobs/230066-project-manager-ai-and-innovation-warsaw-teamquest"
TRUTHY = {"1", "true", "yes"}
SAFETY_STEP_LIMIT = 250


def env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def env_flag(name: str, default: str = "false") -> bool:
    return env_str(name, default).lower() in TRUTHY


def debug_enabled() -> bool:
    return env_flag("STAGEHAND_DEBUG", "false")


def normalize_domain(domain: str) -> str:
    return domain.lower().removeprefix("www.")


def normalize_offer_url(url: str) -> str:
    parts = urlsplit(url)
    path = (parts.path or "").rstrip("/")
    if not path:
        path = "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def normalize_page_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def is_tracking_or_reco_url(url: str) -> bool:
    try:
        params = parse_qs(urlsplit(url).query)
    except Exception:
        return False
    return any(key in params for key in ("sug", "utm_source", "utm_campaign"))


def path_family(path: str) -> str:
    parts = [p for p in path.split("/") if p]
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


def path_family_cluster(family: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "", family.lower())
    return (cleaned.split("-")[0] if cleaned else "") or cleaned


def url_path_signature(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if not parts:
        return "/"
    out: list[str] = []
    for seg in parts:
        low = seg.lower()
        low = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "{uuid}", low)
        low = re.sub(r"\d+", "{n}", low)
        out.append(low)
    return "/" + "/".join(out)


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
    if not path:
        return False
    if path in {"/", ""}:
        return False
    parts = [p for p in path.split("/") if p]
    blocked_segments = {"login", "signin", "account", "saved", "register", "privacy", "cookies"}
    if parts and parts[0] in blocked_segments:
        return False
    # Keep this generic so the same flow works on different boards.
    return True


def model_options(
    model_name: str,
    model_api_key: Optional[str],
    model_base_url: Optional[str],
) -> dict[str, Any]:
    cfg: dict[str, Any] = {"modelName": model_name}
    if model_api_key:
        cfg["apiKey"] = model_api_key
    if model_base_url:
        cfg["baseURL"] = model_base_url
    return {"model": cfg}


def scoped_model_options(model_opts: dict[str, Any], selector: Optional[str]) -> dict[str, Any]:
    if not selector:
        return model_opts
    scoped = dict(model_opts)
    scoped["selector"] = selector
    return scoped


def cleanup_browser_processes() -> None:
    # User explicitly requested aggressive cleanup of stale browser processes.
    patterns = [
        "stagehand",
        "playwright.*chrom",
        "chromium",
        "chrome.*remote-debugging-port",
    ]
    for pattern in patterns:
        try:
            subprocess.run(["pkill", "-f", pattern], check=False, capture_output=True)
        except Exception:
            pass


def configure_local_stagehand_server_logging() -> None:
    if debug_enabled():
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
        is_stagehand_binary = bool(cmd0 and "stagehand" in cmd0 and ("_sea" in cmd0 or "darwin" in cmd0))
        if not is_stagehand_binary:
            return original_popen(*args, **kwargs)
        env = kwargs.get("env")
        if isinstance(env, dict):
            env.setdefault("LOG_LEVEL", "error")
            env.setdefault("PINO_LOG_LEVEL", "error")
            env.setdefault("AI_SDK_LOG_WARNINGS", "false")
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
        return original_popen(*args, **kwargs)

    stagehand_sea_server.subprocess.Popen = _quiet_popen
    stagehand_sea_server._jsb_quiet_patch_applied = True


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


async def fetch_stagehand_metrics(
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
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{base_url.rstrip('/')}/metrics", headers=headers)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def print_metrics_summary(metrics: Optional[dict[str, Any]]) -> None:
    if not metrics:
        console.print("STAGEHAND_METRICS=unavailable")
        return
    prompt = int(metrics.get("totalPromptTokens") or 0)
    completion = int(metrics.get("totalCompletionTokens") or 0)
    cached = int(metrics.get("totalCachedInputTokens") or 0)
    reasoning = int(metrics.get("totalReasoningTokens") or 0)
    inference_ms = int(metrics.get("totalInferenceTimeMs") or 0)
    llm_input_total = prompt + cached
    cache_ratio = (100.0 * cached / llm_input_total) if llm_input_total > 0 else 0.0

    console.print(f"METRICS_PROMPT_TOKENS={prompt}")
    console.print(f"METRICS_COMPLETION_TOKENS={completion}")
    console.print(f"METRICS_REASONING_TOKENS={reasoning}")
    console.print(f"METRICS_CACHED_INPUT_TOKENS={cached}")
    console.print(f"METRICS_CACHE_RATIO_INPUT_PCT={cache_ratio:.1f}")
    console.print(f"METRICS_INFERENCE_MS={inference_ms}")


def print_replay_summary(replay: Any) -> None:
    try:
        pages = getattr(getattr(replay, "data", None), "pages", None) or []
        cached = 0
        input_tokens = 0
        output_tokens = 0
        actions = 0
        for page in pages:
            for action in getattr(page, "actions", None) or []:
                usage = getattr(action, "token_usage", None)
                if usage is None:
                    continue
                actions += 1
                cached += int(getattr(usage, "cached_input_tokens", 0) or 0)
                input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
                output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        total_input = cached + input_tokens
        ratio = (100.0 * cached / total_input) if total_input > 0 else 0.0
        console.print(f"STAGEHAND_REPLAY_ACTIONS={actions}")
        console.print(f"STAGEHAND_REPLAY_CACHED_INPUT_TOKENS={cached}")
        console.print(f"STAGEHAND_REPLAY_INPUT_TOKENS={input_tokens}")
        console.print(f"STAGEHAND_REPLAY_OUTPUT_TOKENS={output_tokens}")
        console.print(f"STAGEHAND_REPLAY_CACHE_RATIO_INPUT_PCT={ratio:.1f}")
    except Exception:
        console.print("STAGEHAND_REPLAY_METRICS=unavailable")


class OfferUrls(BaseModel):
    urls: list[str] = Field(default_factory=list)


class NextPageCandidate(BaseModel):
    url: str = ""


class LoggedStagehandSession:
    def __init__(self, session: Any, enable_logs: bool = True) -> None:
        self._session = session
        self._enable_logs = enable_logs
        self._counts: dict[str, int] = {}
        self._dur_ms: dict[str, int] = {}
        self._fingerprints: dict[str, int] = {}
        self._failures: dict[str, int] = {}

    def _page_url(self, kwargs: dict[str, Any]) -> str:
        page = kwargs.get("page")
        return normalize_page_url(getattr(page, "url", "")) if page is not None else "-"

    def _instruction(self, kwargs: dict[str, Any]) -> str:
        for key in ("instruction", "input"):
            value = kwargs.get(key)
            if isinstance(value, str):
                return value.strip().replace("\n", " ")[:180]
        execute_opts = kwargs.get("execute_options")
        if isinstance(execute_opts, dict):
            val = execute_opts.get("instruction")
            if isinstance(val, str):
                return val.strip().replace("\n", " ")[:180]
        return "-"

    def _log(self, action: str, ok: bool, duration_ms: int, kwargs: dict[str, Any], extra: str = "") -> None:
        self._counts[action] = self._counts.get(action, 0) + 1
        self._dur_ms[action] = self._dur_ms.get(action, 0) + duration_ms
        page_url = self._page_url(kwargs)
        instruction = self._instruction(kwargs)
        fp = f"{action}|{page_url}|{instruction}"
        self._fingerprints[fp] = self._fingerprints.get(fp, 0) + 1
        redundant = self._fingerprints[fp] > 1
        if ok:
            self._failures[action] = 0
        else:
            self._failures[action] = self._failures.get(action, 0) + 1
        if self._enable_logs:
            console.print(
                f"ACTION_{action.upper()} ok={str(ok).lower()} ms={duration_ms} page={page_url} "
                f"instruction={instruction}"
            )
            if redundant:
                console.print(f"ACTION_REDUNDANT={action} count={self._fingerprints[fp]} page={page_url}")
            if extra:
                console.print(extra)

    def should_skip(self, action: str, max_failures: int = 3) -> bool:
        return self._failures.get(action, 0) >= max_failures

    async def _call(self, action: str, fn: Any, kwargs: dict[str, Any]) -> Any:
        started = time.perf_counter()
        try:
            result = await fn(**kwargs)
            ms = int((time.perf_counter() - started) * 1000)
            self._log(action, True, ms, kwargs)
            return result
        except Exception:
            ms = int((time.perf_counter() - started) * 1000)
            self._log(action, False, ms, kwargs)
            raise

    async def observe(self, **kwargs: Any) -> Any:
        return await self._call("observe", self._session.observe, kwargs)

    async def extract(self, **kwargs: Any) -> Any:
        return await self._call("extract", self._session.extract, kwargs)

    async def act(self, **kwargs: Any) -> Any:
        return await self._call("act", self._session.act, kwargs)

    async def execute(self, **kwargs: Any) -> Any:
        return await self._call("execute", self._session.execute, kwargs)

    async def navigate(self, **kwargs: Any) -> Any:
        return await self._call("navigate", self._session.navigate, kwargs)

    async def end(self, **kwargs: Any) -> Any:
        return await self._call("end", self._session.end, kwargs)

    def print_summary(self) -> None:
        for action in sorted(self._counts):
            console.print(f"ACTION_SUMMARY_{action.upper()} count={self._counts[action]} total_ms={self._dur_ms.get(action, 0)}")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


class OfferPattern(BaseModel):
    first_url: str
    include_tokens: list[str] = Field(default_factory=list)
    exclude_tokens: list[str] = Field(default_factory=list)
    min_path_segments: int = 0
    detail_segment_index: int = -1
    detail_requires_digit: bool = False
    detail_requires_dash: bool = False


class LlmInferredPattern(BaseModel):
    first_offer_url: str = ""
    include_path_tokens: list[str] = Field(default_factory=list)
    exclude_path_tokens: list[str] = Field(default_factory=list)


def infer_offer_pattern_from_url(first_url: str) -> Optional[OfferPattern]:
    if not first_url:
        return None
    path_parts = [p for p in urlsplit(first_url).path.split("/") if p]
    include: list[str] = []
    if path_parts:
        include.append(path_parts[0].lower())
    if len(path_parts) >= 2:
        second = path_parts[1].lower()
        # Keep second segment only when it is structural, not a one-off slug.
        is_slug_like = len(second) >= 18 and ("-" in second or re.search(r"\d", second) is not None)
        if not is_slug_like:
            include.append(second)
    detail_idx = len(path_parts) - 1 if path_parts else -1
    detail_seg = path_parts[detail_idx].lower() if detail_idx >= 0 else ""
    return OfferPattern(
        first_url=first_url,
        include_tokens=include,
        exclude_tokens=[],
        min_path_segments=len(path_parts),
        detail_segment_index=detail_idx,
        detail_requires_digit=bool(re.search(r"\d", detail_seg)),
        detail_requires_dash=("-" in detail_seg),
    )


def infer_offer_pattern_from_candidates(candidates: list[str]) -> Optional[OfferPattern]:
    counts: dict[tuple[str, str], int] = {}
    grouped: dict[tuple[str, str], list[str]] = {}
    for url in candidates:
        path_parts = [p.lower() for p in urlsplit(url).path.split("/") if p]
        if len(path_parts) < 2:
            continue
        key = (path_parts[0], path_parts[1])
        counts[key] = counts.get(key, 0) + 1
        grouped.setdefault(key, []).append(url)
    if not counts:
        return None
    dominant = max(counts, key=counts.get)
    sample_url = grouped[dominant][0]
    sample_parts = [p for p in urlsplit(sample_url).path.split("/") if p]
    detail_idx = len(sample_parts) - 1 if sample_parts else -1
    detail_seg = sample_parts[detail_idx].lower() if detail_idx >= 0 else ""
    return OfferPattern(
        first_url=sample_url,
        include_tokens=[dominant[0], dominant[1]],
        exclude_tokens=[],
        min_path_segments=len(sample_parts),
        detail_segment_index=detail_idx,
        detail_requires_digit=bool(re.search(r"\d", detail_seg)),
        detail_requires_dash=("-" in detail_seg),
    )


def matches_offer_pattern(url: str, pattern: Optional[OfferPattern]) -> bool:
    if pattern is None:
        return True
    path = (urlsplit(url).path or "").lower()
    parts = [p for p in path.split("/") if p]

    def has_token(token: str) -> bool:
        tok = token.strip().lower()
        if not tok:
            return False
        if "/" in tok:
            return tok in path
        return tok in parts

    if pattern.include_tokens and not all(has_token(token) for token in pattern.include_tokens):
        return False
    if pattern.exclude_tokens and any(has_token(token) for token in pattern.exclude_tokens):
        return False
    if pattern.min_path_segments and len(parts) < pattern.min_path_segments:
        return False
    if pattern.detail_segment_index >= 0:
        if pattern.detail_segment_index >= len(parts):
            return False
        detail_seg = parts[pattern.detail_segment_index]
        if pattern.detail_requires_digit and re.search(r"\d", detail_seg) is None:
            return False
        if pattern.detail_requires_dash and "-" not in detail_seg:
            return False
    return True


async def sleep_ms(ms: int) -> None:
    await asyncio.sleep(ms / 1000)


async def listing_signature(pw_page: Page) -> str:
    try:
        payload = await pw_page.evaluate(
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
        return normalize_page_url(pw_page.url)
    if not isinstance(payload, dict):
        return normalize_page_url(pw_page.url)
    return (
        f"{normalize_page_url(str(payload.get('url') or pw_page.url))}|"
        f"{int(payload.get('hrefCount') or 0)}|"
        f"{int(payload.get('scrollHeight') or 0)}|"
        f"{str(payload.get('hrefSample') or '')}"
    )


async def read_results_header_count(pw_page: Page) -> Optional[int]:
    try:
        value = await pw_page.evaluate(
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
    session: Any,
    pw_page: Page,
    model_opts: dict[str, Any],
    domain: str,
    candidate_urls: list[str],
) -> Optional[OfferPattern]:
    if hasattr(session, "should_skip") and session.should_skip("extract"):
        return None
    allowed = [u for u in candidate_urls if is_offer_candidate_url(u, domain)]
    if not allowed:
        return None
    candidates_text = "\n".join(f"- {u}" for u in allowed[:40])
    try:
        extracted = await session.extract(
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
            options=model_opts,
            page=pw_page,
        )
        payload = getattr(extracted.data, "result", None)
        if not isinstance(payload, dict):
            return None
        first_url = str(payload.get("first_offer_url") or "").strip()
        if first_url.startswith("/"):
            first_url = urljoin(pw_page.url, first_url)
        if first_url not in allowed:
            first_url = allowed[0]
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
        if not include:
            fallback = infer_offer_pattern_from_url(first_url)
            return fallback
        return OfferPattern(first_url=first_url, include_tokens=include[:4], exclude_tokens=exclude[:8])
    except Exception:
        return None


async def extract_offer_seed_urls_with_llm(
    session: Any,
    pw_page: Page,
    model_opts: dict[str, Any],
    domain: str,
    listing_selector: Optional[str] = None,
) -> list[str]:
    if hasattr(session, "should_skip") and session.should_skip("extract"):
        return []
    try:
        extracted = await session.extract(
            instruction=(
                "Extract visible URLs for final real job-offer detail pages from listing results only. "
                "Exclude categories, filters, login, account, blog, salary pages, sponsored/promoted/recommended blocks."
            ),
            schema=OfferUrls.model_json_schema(),
            options=scoped_model_options(model_opts, listing_selector),
            page=pw_page,
        )
        payload = getattr(extracted.data, "result", None)
    except Exception:
        payload = None
    raw = payload.get("urls") if isinstance(payload, dict) else []
    out: list[str] = []
    seen: set[str] = set()
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, str):
            continue
        candidate = entry.strip()
        if candidate.startswith("/"):
            candidate = urljoin(pw_page.url, candidate)
        normalized = normalize_offer_url(candidate)
        if not normalized or not is_offer_candidate_url(normalized, domain):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


async def filter_offer_candidates_with_llm(
    session: Any,
    pw_page: Page,
    model_opts: dict[str, Any],
    domain: str,
    candidate_urls: list[str],
) -> list[str]:
    if hasattr(session, "should_skip") and session.should_skip("extract"):
        return []
    allowed = [u for u in candidate_urls if is_offer_candidate_url(u, domain)]
    if not allowed:
        return []
    candidates_text = "\n".join(f"- {u}" for u in allowed[:120])
    try:
        extracted = await session.extract(
            instruction=(
                "From the candidate URL list below, return only real final job-offer detail URLs. "
                "Exclude category, filter, search, account, blog, legal, login, and marketing pages.\n\n"
                f"CANDIDATE URLs:\n{candidates_text}"
            ),
            schema=OfferUrls.model_json_schema(),
            options=model_opts,
            page=pw_page,
        )
        payload = getattr(extracted.data, "result", None)
    except Exception:
        payload = None
    raw = payload.get("urls") if isinstance(payload, dict) else []
    out: list[str] = []
    seen: set[str] = set()
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, str):
            continue
        candidate = entry.strip()
        if candidate.startswith("/"):
            candidate = urljoin(pw_page.url, candidate)
        normalized = normalize_offer_url(candidate)
        if not normalized or normalized not in allowed:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


async def discover_next_listing_page_with_llm(
    session: Any,
    pw_page: Page,
    model_opts: dict[str, Any],
    domain: str,
    visited_pages: set[str],
    inferred_pattern: Optional[OfferPattern] = None,
) -> Optional[str]:
    if hasattr(session, "should_skip") and session.should_skip("extract"):
        return None
    try:
        extracted = await session.extract(
            instruction=(
                "Find URL of the next listing results page that should reveal more real offers. "
                "Return empty url if there is no next page. "
                "Do not return offer-detail URLs, login/account pages, or filter URLs."
            ),
            schema=NextPageCandidate.model_json_schema(),
            options=model_opts,
            page=pw_page,
        )
        payload = getattr(extracted.data, "result", None)
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        return None
    raw = str(payload.get("url") or "").strip()
    if not raw:
        return None
    candidate = urljoin(pw_page.url, raw) if raw.startswith("/") else raw
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
        # Next page must be listing/search URL, not an offer detail URL.
        return None
    return normalized


async def recover_missing_offers_with_llm(
    session: Any,
    pw_page: Page,
    model_opts: dict[str, Any],
    domain: str,
    seen_urls: set[str],
) -> list[str]:
    if hasattr(session, "should_skip") and session.should_skip("extract"):
        return []
    seen_preview = "\n".join(f"- {u}" for u in sorted(seen_urls)[:120])
    try:
        extracted = await session.extract(
            instruction=(
                "Find real final job-offer detail URLs visible on this page that are NOT in the KNOWN URL list. "
                "Exclude non-offer links.\n\n"
                f"KNOWN URLS:\n{seen_preview}"
            ),
            schema=OfferUrls.model_json_schema(),
            options=model_opts,
            page=pw_page,
        )
        payload = getattr(extracted.data, "result", None)
    except Exception:
        payload = None
    raw = payload.get("urls") if isinstance(payload, dict) else []
    out: list[str] = []
    seen_local: set[str] = set()
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, str):
            continue
        candidate = entry.strip()
        if candidate.startswith("/"):
            candidate = urljoin(pw_page.url, candidate)
        normalized = normalize_offer_url(candidate)
        if not normalized or not is_offer_candidate_url(normalized, domain):
            continue
        if normalized in seen_urls or normalized in seen_local:
            continue
        seen_local.add(normalized)
        out.append(normalized)
    return out


def choose_reveal_action(actions: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not actions:
        return None
    best: Optional[dict[str, Any]] = None
    best_score = -9999
    positive = ("load more", "show more", "next", "nast", "dalej", "więcej", "pagin")
    negative = ("sponsor", "promo", "recommended", "filter", "sort", "login", "share", "prev", "back")
    for action in actions:
        text = " ".join(str(v) for v in action.values()).lower()
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


async def discover_listing_selector(
    session: Any,
    pw_page: Page,
    model_opts: dict[str, Any],
) -> Optional[str]:
    if hasattr(session, "should_skip") and session.should_skip("observe"):
        return None
    try:
        observed = await session.observe(
            instruction=(
                "Find one safe click action inside the main job-results listing area (not header/footer/sidebar). "
                "Return one action."
            ),
            options=model_opts,
            page=pw_page,
        )
        actions = as_action_dicts(getattr(observed.data, "result", []) or [])
        for action in actions:
            selector = str(action.get("selector") or "").strip()
            if selector:
                return selector
    except Exception:
        return None
    return None


async def accept_cookies(
    session: Any,
    pw_page: Page,
    model_opts: dict[str, Any],
) -> bool:
    if hasattr(session, "should_skip") and session.should_skip("observe"):
        return False
    try:
        observed = await session.observe(
            instruction=(
                "Find cookie consent accept action (Accept/Accept all/Zgadzam/Akceptuj). "
                "Return only one safe click action if present."
            ),
            options=model_opts,
            page=pw_page,
        )
        actions = as_action_dicts(getattr(observed.data, "result", []) or [])
        if actions:
            await session.act(input=actions[0], page=pw_page)
            await sleep_ms(500)
            return True
    except Exception:
        pass

    try:
        clicked = await pw_page.evaluate(
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


async def extract_offer_urls(
    session: Any,
    pw_page: Page,
    model_opts: dict[str, Any],
    domain: str,
    inferred_pattern: Optional[OfferPattern] = None,
    listing_selector: Optional[str] = None,
    use_llm_extract: bool = True,
) -> list[str]:
    urls: list[str] = []
    card_urls: list[str] = []
    if use_llm_extract:
        try:
            extracted = await session.extract(
                instruction=(
                    "Extract visible URLs for real job offer detail pages from current listing results. "
                    "Exclude sponsored/promoted/recommended blocks and nav/filter/login/share links."
                ),
                schema=OfferUrls.model_json_schema(),
                options=scoped_model_options(model_opts, listing_selector),
                page=pw_page,
            )
            payload = getattr(extracted.data, "result", None)
            if isinstance(payload, dict):
                raw = payload.get("urls")
                if isinstance(raw, list):
                    urls.extend([u for u in raw if isinstance(u, str)])
        except Exception:
            pass

    try:
        dom_urls = await pw_page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href]')).map((a) => {
                try { return new URL(a.getAttribute('href') || a.href, window.location.origin).toString(); }
                catch { return null; }
            }).filter(Boolean)"""
        )
        if isinstance(dom_urls, list):
            urls.extend([u for u in dom_urls if isinstance(u, str)])
    except Exception:
        pass

    # Generic card-link extractor: anchors representing listing cards (usually wrap offer title headings).
    try:
        raw_card_urls = await pw_page.evaluate(
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
            card_urls.extend([u for u in raw_card_urls if isinstance(u, str)])
    except Exception:
        pass

    # Extract URLs from embedded JSON/script state (some offers may be present there before visible anchors).
    try:
        embedded_urls = await pw_page.evaluate(
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
                  const escapedMatches = t.match(/\\\\\\/[a-zA-Z0-9_\\-]+(?:\\\\\\/[a-zA-Z0-9_\\-.,%]+){1,8}/g) || [];
                  for (const m of escapedMatches) {
                    pushUrl(m.replace(/\\\\\\//g, "/"));
                  }
                }
                return out.slice(0, 5000);
            }"""
        )
        if isinstance(embedded_urls, list):
            urls.extend([u for u in embedded_urls if isinstance(u, str)])
    except Exception:
        pass

    out: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        candidate = raw.strip()
        if candidate.startswith("/"):
            candidate = urljoin(pw_page.url, candidate)
        normalized = normalize_offer_url(candidate)
        if not normalized or not is_offer_candidate_url(normalized, domain):
            continue
        if not matches_offer_pattern(normalized, inferred_pattern):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)

    # Keep high-signal listing card URLs, but still enforce inferred pattern when available.
    for raw in card_urls:
        candidate = raw.strip()
        if candidate.startswith("/"):
            candidate = urljoin(pw_page.url, candidate)
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
    pw_page: Page,
    domain: str,
    inferred_pattern: Optional[OfferPattern] = None,
) -> list[str]:
    try:
        raw = await pw_page.evaluate(
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


async def reveal_more(
    session: Any,
    pw_page: Page,
    model_opts: dict[str, Any],
    model_name: str,
    cache_enabled: bool,
) -> bool:
    before = await listing_signature(pw_page)

    if hasattr(session, "should_skip") and session.should_skip("observe"):
        pass
    else:
        try:
            observed = await session.observe(
                instruction=(
                    "Find action that reveals more job results (load more or next page). "
                    "Ignore sponsored/recommended areas and non-result UI."
                ),
                options=model_opts,
                page=pw_page,
            )
            actions = as_action_dicts(getattr(observed.data, "result", []) or [])
            action = choose_reveal_action(actions)
            if action:
                await session.act(input=action, page=pw_page)
                await sleep_ms(1200)
                after = await listing_signature(pw_page)
                return after != before
        except Exception:
            pass

    # Fast DOM fallback before using execute(): click likely "next/load more" controls.
    try:
        clicked = await pw_page.evaluate(
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
            after = await listing_signature(pw_page)
            return after != before
    except Exception:
        pass

    # Native Stagehand cache path: execute() + should_cache.
    try:
        await session.execute(
            execute_options={
                "instruction": (
                    "Reveal more job results by clicking Load more / Next page if available. "
                    "Do not click sponsored, filter, sort, login, or share UI."
                ),
                "max_steps": 1,
            },
            agent_config={"model": model_name},
            should_cache=cache_enabled,
            page=pw_page,
        )
        await sleep_ms(1200)
        after = await listing_signature(pw_page)
        return after != before
    except Exception:
        return False


async def run_cache_probe(session: Any, pw_page: Page, model_name: str, cache_enabled: bool) -> None:
    instruction = "Read current page title and URL in one short sentence."
    kwargs: dict[str, Any] = {
        "execute_options": {"instruction": instruction, "max_steps": 1},
        "agent_config": {"model": model_name},
        "should_cache": cache_enabled,
        "page": pw_page,
    }
    try:
        run1 = await session.execute(**kwargs)
        run2 = await session.execute(**kwargs)
        usage1 = getattr(getattr(run1.data, "result", None), "usage", None)
        usage2 = getattr(getattr(run2.data, "result", None), "usage", None)
        cached1 = int((getattr(usage1, "cached_input_tokens", 0) or 0))
        cached2 = int((getattr(usage2, "cached_input_tokens", 0) or 0))
        mode = "enabled" if cache_enabled else "disabled"
        console.print(f"STAGEHAND_CACHE_MODE={mode}")
        console.print(f"CACHE_PROBE_CACHED_INPUT_RUN1={cached1}")
        console.print(f"CACHE_PROBE_CACHED_INPUT_RUN2={cached2}")
    except Exception:
        console.print("CACHE_PROBE=unavailable")


async def collect_offers(
    session: Any,
    pw_page: Page,
    model_opts: dict[str, Any],
    model_name: str,
    domain: str,
    cache_enabled: bool,
    target_url: str,
    board_name: str,
) -> list[str]:
    seen: set[str] = set()
    inferred_pattern: Optional[OfferPattern] = None
    listing_selector: Optional[str] = None
    expected_from_header: Optional[int] = None
    visited_pages: set[str] = set()
    queued_pages: list[str] = []
    queued_lookup: set[str] = set()
    stagnation = 0
    step = 0
    last_listing_url = normalize_page_url(target_url)
    listing_family_name = path_family(urlsplit(target_url).path)
    cookie_checked = False
    max_stagnation = 10
    inferred_pattern = infer_offer_pattern_from_url(env_str("STAGEHAND_FIRST_OFFER_URL", BD_FIRST_VISIBLE_OFFER_URL))
    if inferred_pattern:
        console.print(f"FIRST_GOOD_OFFER_URL={inferred_pattern.first_url}")
        console.print(f"OFFER_URL_PATTERN_INCLUDE_TOKENS={','.join(inferred_pattern.include_tokens) or '-'}")
        console.print(f"OFFER_URL_PATTERN_EXCLUDE_TOKENS={','.join(inferred_pattern.exclude_tokens) or '-'}")

    while True:
        step_started = time.perf_counter()
        step += 1
        if step > SAFETY_STEP_LIMIT:
            console.print(f"STOP_REASON=safety_step_limit_{SAFETY_STEP_LIMIT}")
            break

        if not cookie_checked:
            await accept_cookies(session, pw_page, model_opts)
            cookie_checked = True
        header_on_current = await read_results_header_count(pw_page)
        if header_on_current is not None:
            last_listing_url = normalize_page_url(pw_page.url)
            if step == 1:
                expected_from_header = header_on_current
                console.print(f"HEADER_OFFERS_COUNT={expected_from_header}")
        else:
            current_norm = normalize_page_url(pw_page.url)
            if current_norm != last_listing_url:
                await session.navigate(url=last_listing_url, page=pw_page)
                await sleep_ms(1200)
                if not cookie_checked:
                    await accept_cookies(session, pw_page, model_opts)
                    cookie_checked = True
                console.print(f"RECOVER_TO_LISTING={last_listing_url}")
                continue

        current_page = normalize_page_url(pw_page.url)
        visited_pages.add(current_page)

        before = len(seen)
        if listing_selector is None:
            listing_selector = await discover_listing_selector(session, pw_page, model_opts)

        seed_urls: list[str] = []
        if inferred_pattern is None:
            seed_urls = await extract_offer_seed_urls_with_llm(
                session=session,
                pw_page=pw_page,
                model_opts=model_opts,
                domain=domain,
                listing_selector=listing_selector,
            )
        raw_extracted = await extract_offer_urls(
            session=session,
            pw_page=pw_page,
            model_opts=model_opts,
            domain=domain,
            inferred_pattern=None,
            listing_selector=listing_selector,
            use_llm_extract=(inferred_pattern is None),
        )
        if not seed_urls and raw_extracted:
            seed_urls = await filter_offer_candidates_with_llm(
                session=session,
                pw_page=pw_page,
                model_opts=model_opts,
                domain=domain,
                candidate_urls=raw_extracted,
            )

        if inferred_pattern is None:
            inferred_pattern = await infer_offer_pattern_with_llm(
                session=session,
                pw_page=pw_page,
                model_opts=model_opts,
                domain=domain,
                candidate_urls=seed_urls,
            )
            if inferred_pattern is None and seed_urls:
                inferred_pattern = infer_offer_pattern_from_url(seed_urls[0])
            if inferred_pattern is None and raw_extracted:
                inferred_pattern = infer_offer_pattern_from_candidates(raw_extracted)
            if inferred_pattern:
                console.print(f"FIRST_GOOD_OFFER_URL={inferred_pattern.first_url}")
                console.print(f"OFFER_URL_PATTERN_INCLUDE_TOKENS={','.join(inferred_pattern.include_tokens) or '-'}")
                console.print(f"OFFER_URL_PATTERN_EXCLUDE_TOKENS={','.join(inferred_pattern.exclude_tokens) or '-'}")

        if inferred_pattern is None:
            extracted = list(seed_urls)
        else:
            extracted = [
                url for url in raw_extracted
                if matches_offer_pattern(url, inferred_pattern)
            ]
        added_this_step = 0
        for url in extracted:
            if url not in seen:
                seen.add(url)
                added_this_step += 1
        if expected_from_header is not None and len(seen) < expected_from_header and added_this_step == 0:
            recovered = await recover_missing_offers_with_llm(
                session=session,
                pw_page=pw_page,
                model_opts=model_opts,
                domain=domain,
                seen_urls=seen,
            )
            for url in recovered:
                if url not in seen:
                    seen.add(url)
                    added_this_step += 1

        console.print(f"STEP={step} OFFERS={len(seen)} RAW={len(raw_extracted)} ADDED={added_this_step} PAGE={normalize_page_url(pw_page.url)}")
        if expected_from_header is not None and len(seen) >= expected_from_header:
            console.print("STOP_REASON=header_reached")
            break

        for purl in await discover_pagination_urls(pw_page, domain, inferred_pattern=inferred_pattern):
            if not is_same_listing_family(purl, domain, listing_family_name):
                continue
            if purl not in visited_pages and purl not in queued_lookup:
                queued_pages.append(purl)
                queued_lookup.add(purl)

        moved = await reveal_more(session, pw_page, model_opts, model_name, cache_enabled)
        if moved and not is_same_listing_family(pw_page.url, domain, listing_family_name):
            await session.navigate(url=last_listing_url, page=pw_page)
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
                await session.navigate(url=next_url, page=pw_page)
                await sleep_ms(1400)
                if not cookie_checked:
                    await accept_cookies(session, pw_page, model_opts)
                    cookie_checked = True
                if await read_results_header_count(pw_page) is None:
                    await session.navigate(url=last_listing_url, page=pw_page)
                    await sleep_ms(900)
                    if not cookie_checked:
                        await accept_cookies(session, pw_page, model_opts)
                        cookie_checked = True
                    console.print(f"RECOVER_TO_LISTING={last_listing_url}")
                    moved = False
                else:
                    moved = True
                    console.print(f"PAGINATION_GOTO={next_url}")

        if not moved:
            llm_next = await discover_next_listing_page_with_llm(
                session=session,
                pw_page=pw_page,
                model_opts=model_opts,
                domain=domain,
                visited_pages=visited_pages,
                inferred_pattern=inferred_pattern,
            )
            if llm_next:
                if not is_same_listing_family(llm_next, domain, listing_family_name):
                    llm_next = None
            if llm_next:
                await session.navigate(url=llm_next, page=pw_page)
                await sleep_ms(1200)
                if not cookie_checked:
                    await accept_cookies(session, pw_page, model_opts)
                    cookie_checked = True
                moved = True
                console.print(f"PAGINATION_GOTO_LLM={llm_next}")

        if not moved:
            try:
                await pw_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await sleep_ms(900)
            except Exception:
                pass

        stagnation = 0 if len(seen) > before else stagnation + 1
        step_ms = int((time.perf_counter() - step_started) * 1000)
        console.print(f"STEP_MS={step_ms}")
        if stagnation >= max_stagnation:
            console.print("STOP_REASON=stagnation")
            break
        if not moved and not queued_pages and stagnation >= 4:
            console.print("STOP_REASON=no_more_actions")
            break

    return sorted(seen)


async def run_stagehand_bd() -> None:
    started = time.perf_counter()
    load_dotenv()
    target_name = "bulldogjob"
    target_url = env_str("STAGEHAND_POC_URL", BULLDOG_POC_URL)

    model_name = env_str("MODEL")
    if not model_name:
        raise RuntimeError("MODEL env var is required (example: openai/gpt-5-mini).")

    llm_api_key = env_str("LLM_API_KEY") or None
    llm_api_base = env_str("LLM_API_BASE") or None
    cache_enabled = env_flag("STAGEHAND_EXECUTE_CACHE", "true")

    stagehand_env = env_str("STAGEHAND_ENV", "LOCAL").lower()
    stagehand_server = "remote" if stagehand_env == "remote" else "local"

    try:
        from stagehand import AsyncStagehand  # type: ignore
    except Exception as exc:
        raise RuntimeError("Stagehand is not installed. Run poetry install.") from exc

    configure_local_stagehand_server_logging()
    cleanup_browser_processes()

    client_kwargs: dict[str, Any] = {"server": stagehand_server, "model_api_key": llm_api_key}
    if stagehand_server == "local":
        client_kwargs["local_headless"] = env_flag("STAGEHAND_HEADLESS", "false")
        if llm_api_key:
            client_kwargs["local_openai_api_key"] = llm_api_key
    if env_str("BROWSERBASE_API_KEY"):
        client_kwargs["browserbase_api_key"] = env_str("BROWSERBASE_API_KEY")
    if env_str("BROWSERBASE_PROJECT_ID"):
        client_kwargs["browserbase_project_id"] = env_str("BROWSERBASE_PROJECT_ID")

    client = AsyncStagehand(**client_kwargs)
    session: Any = None
    raw_session: Any = None
    playwright: Optional[Playwright] = None
    browser: Optional[Browser] = None

    try:
        raw_session = await client.sessions.start(
            model_name=model_name,
            self_heal=True,
            verbose=1 if debug_enabled() else 0,
            browser={
                "type": "local" if stagehand_server == "local" else "browserbase",
                "launchOptions": {"headless": env_flag("STAGEHAND_HEADLESS", "false")},
            },
        )
        if not raw_session.data.cdp_url:
            raise RuntimeError("Missing cdpUrl from Stagehand session.")
        session = LoggedStagehandSession(raw_session, enable_logs=True)

        playwright = await async_playwright().start()
        browser = await playwright.chromium.connect_over_cdp(raw_session.data.cdp_url)
        context: BrowserContext = browser.contexts[0] if browser.contexts else await browser.new_context()
        pw_page: Page = context.pages[0] if context.pages else await context.new_page()

        model_opts = model_options(model_name, llm_api_key, llm_api_base)

        if env_flag("STAGEHAND_CACHE_PROBE", "false"):
            cache_probe_started = time.perf_counter()
            await run_cache_probe(session, pw_page, model_name, cache_enabled)
            console.print(f"CACHE_PROBE_MS={int((time.perf_counter() - cache_probe_started) * 1000)}")
        else:
            console.print("CACHE_PROBE=disabled")

        collect_started = time.perf_counter()
        domain = normalize_domain(urlsplit(target_url).netloc)
        console.print(f"TARGET={target_name} DOMAIN_DETECTED={domain}")

        await session.navigate(url=target_url, page=pw_page)
        await sleep_ms(1500)
        await accept_cookies(session, pw_page, model_opts)

        offers = await collect_offers(
            session=session,
            pw_page=pw_page,
            model_opts=model_opts,
            model_name=model_name,
            domain=domain,
            cache_enabled=cache_enabled,
            target_url=target_url,
            board_name=target_name,
        )

        for offer in offers:
            console.print(offer)
        console.print(f"OFFERS_COUNT={len(offers)}")
        console.print(f"COLLECT_MS={int((time.perf_counter() - collect_started) * 1000)}")
        metrics = await fetch_stagehand_metrics(
            base_url=str(client.base_url),
            model_api_key=client.model_api_key,
            browserbase_api_key=client.browserbase_api_key,
            browserbase_project_id=client.browserbase_project_id,
        )
        print_metrics_summary(metrics)
        try:
            replay = await client.sessions.replay(raw_session.id)
            print_replay_summary(replay)
        except Exception:
            console.print("STAGEHAND_REPLAY_METRICS=unavailable")
        console.print(f"RUN_TOTAL_MS={int((time.perf_counter() - started) * 1000)}")
    finally:
        if isinstance(session, LoggedStagehandSession):
            session.print_summary()
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
        cleanup_browser_processes()


def main() -> None:
    asyncio.run(run_stagehand_bd())


if __name__ == "__main__":
    main()
