import asyncio
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit, urlunsplit

from dotenv import load_dotenv
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from rich.console import Console

from jobscraper.src.pydantic_sandbox_multi import (
    LoggedStagehandSession,
    OfferPattern,
    accept_cookies,
    cleanup_browser_processes,
    configure_local_stagehand_server_logging,
    debug_enabled,
    env_flag,
    env_str,
    fetch_stagehand_metrics,
    infer_offer_pattern_from_url,
    infer_offer_pattern_with_llm,
    model_options,
    normalize_domain,
    print_metrics_summary,
    print_replay_summary,
    sleep_ms,
)

console = Console()

INDEED_HOME_URL = "https://pl.indeed.com"
INDEED_SEARCH_TERM = "python junior"
EXPECTED_VISIBLE_OFFERS = 15


def normalize_indeed_offer_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def is_indeed_offer_url(url: str, domain: str) -> bool:
    try:
        parsed = urlsplit(url)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if normalize_domain(parsed.netloc) != normalize_domain(domain):
        return False
    if parsed.path.rstrip("/") != "/rc/clk":
        return False
    params = parse_qs(parsed.query)
    return bool(params.get("jk"))


async def search_indeed(
    session: Any,
    pw_page: Page,
    model_name: str,
    cache_enabled: bool,
) -> None:
    await session.execute(
        execute_options={
            "instruction": (
                f'On the Indeed homepage, enter "{INDEED_SEARCH_TERM}" into the job title or keywords search field, '
                "leave location empty, and submit the search. Stay on the first results page."
            ),
            "max_steps": 3,
        },
        agent_config={"model": model_name},
        should_cache=cache_enabled,
        page=pw_page,
    )
    await sleep_ms(1800)
    if "/jobs" in pw_page.url:
        return

    inputs = pw_page.get_by_role("combobox")
    if await inputs.count() >= 1:
        await inputs.nth(0).fill(INDEED_SEARCH_TERM)
    if await inputs.count() >= 2:
        await inputs.nth(1).fill("")

    search_button = pw_page.get_by_role("button", name="Szukaj pracy")
    if await search_button.count():
        await search_button.first.click()
    elif await inputs.count():
        await inputs.nth(0).press("Enter")
    await sleep_ms(1800)


async def infer_selector_from_first_offer(
    pw_page: Page,
    first_url: str,
) -> Optional[str]:
    del pw_page
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


async def extract_urls_with_selector(
    pw_page: Page,
    selector: str,
    domain: str,
) -> list[str]:
    try:
        raw = await pw_page.evaluate(
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
        if not normalized or not is_indeed_offer_url(normalized, domain):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


async def extract_indeed_dom_offer_urls(
    pw_page: Page,
    domain: str,
) -> list[str]:
    try:
        raw = await pw_page.evaluate(
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
        if not normalized or not is_indeed_offer_url(normalized, domain):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


async def extract_indeed_visible_offer_urls(
    session: Any,
    pw_page: Page,
    model_opts: dict[str, Any],
    domain: str,
) -> tuple[list[str], Optional[OfferPattern], Optional[str]]:
    raw_candidates = await extract_indeed_dom_offer_urls(pw_page, domain)
    filtered_candidates = list(raw_candidates)

    first_url = (filtered_candidates or raw_candidates or [""])[0]
    inferred_pattern = await infer_offer_pattern_with_llm(
        session=session,
        pw_page=pw_page,
        model_opts=model_opts,
        domain=domain,
        candidate_urls=filtered_candidates or raw_candidates,
    )
    if inferred_pattern is None and first_url:
        inferred_pattern = infer_offer_pattern_from_url(first_url)

    selector = await infer_selector_from_first_offer(
        pw_page=pw_page,
        first_url=inferred_pattern.first_url if inferred_pattern else first_url,
    )

    selector_urls = await extract_urls_with_selector(pw_page, selector, domain) if selector else []

    merged: list[str] = []
    seen: set[str] = set()
    for candidate in selector_urls + filtered_candidates + raw_candidates:
        normalized = normalize_indeed_offer_url(candidate)
        if not normalized or not is_indeed_offer_url(normalized, domain):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        merged.append(normalized)

    return merged, inferred_pattern, selector


async def run_stagehand_in() -> None:
    started = time.perf_counter()
    load_dotenv()

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
        domain = normalize_domain(urlsplit(INDEED_HOME_URL).netloc)

        console.print("CACHE_PROBE=disabled")
        console.print(f"TARGET=indeed DOMAIN_DETECTED={domain}")
        console.print(f"SEARCH_TERM={INDEED_SEARCH_TERM}")

        await session.navigate(url=INDEED_HOME_URL, page=pw_page)
        await sleep_ms(1500)
        await accept_cookies(session, pw_page, model_opts)
        await search_indeed(session, pw_page, model_name, cache_enabled)
        await accept_cookies(session, pw_page, model_opts)

        offers: list[str] = []
        inferred_pattern: Optional[OfferPattern] = None
        selector: Optional[str] = None
        for attempt in range(1, 4):
            offers, inferred_pattern, selector = await extract_indeed_visible_offer_urls(
                session=session,
                pw_page=pw_page,
                model_opts=model_opts,
                domain=domain,
            )
            console.print(f"ATTEMPT={attempt} FOUND_URLS_COUNT={len(offers)} PAGE={pw_page.url}")
            if len(offers) >= EXPECTED_VISIBLE_OFFERS:
                break
            await sleep_ms(1200)

        if inferred_pattern:
            console.print(f"FIRST_GOOD_OFFER_URL={inferred_pattern.first_url}")
            console.print(f"OFFER_URL_PATTERN_INCLUDE_TOKENS={','.join(inferred_pattern.include_tokens) or '-'}")
            console.print(f"OFFER_URL_PATTERN_EXCLUDE_TOKENS={','.join(inferred_pattern.exclude_tokens) or '-'}")
        if selector:
            console.print(f"JOB_LINK_SELECTOR={selector}")

        for offer in offers:
            console.print(offer)
        console.print(f"FOUND_URLS_COUNT={len(offers)}")
        console.print(f"OFFERS_COUNT={len(offers)}")

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
        cleanup_browser_processes()


def main() -> None:
    asyncio.run(run_stagehand_in())


if __name__ == "__main__":
    main()
