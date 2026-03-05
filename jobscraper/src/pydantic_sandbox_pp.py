import asyncio
import time
from collections import Counter
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

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

PRACUJ_POC_URL = "https://it.pracuj.pl/praca?et=1%2C3%2C17&sc=0&itth=37"


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
    if normalize_domain(parsed.netloc) != normalize_domain(domain):
        return False
    path = parsed.path.rstrip("/")
    return path.startswith("/praca/") and ",oferta," in path


async def dismiss_pracuj_popups(pw_page: Page) -> None:
    for label in ("Zamknij", "Akceptuj wszystkie"):
        try:
            button = pw_page.get_by_role("button", name=label)
            if await button.count():
                await button.first.click(timeout=1500)
                await sleep_ms(400)
        except Exception:
            pass


async def collect_visible_offer_urls(
    pw_page: Page,
    domain: str,
    selector: str = "#offers-list a[href]",
) -> list[str]:
    try:
        raw = await pw_page.evaluate(
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


async def collect_visible_offer_cards(
    pw_page: Page,
    domain: str,
) -> list[dict[str, str]]:
    try:
        raw = await pw_page.evaluate(
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
        out.append(
            {
                "offer_id": offer_id,
                "title": title,
                "url": url,
            }
        )
    return out


async def extract_next_data_offer_urls(
    pw_page: Page,
    domain: str,
) -> dict[str, str]:
    try:
        raw = await pw_page.evaluate(
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


async def collect_page_offer_urls(
    pw_page: Page,
    domain: str,
    selector: Optional[str],
) -> tuple[list[str], int]:
    cards = await collect_visible_offer_cards(pw_page, domain)
    next_data_urls = await extract_next_data_offer_urls(pw_page, domain)
    selector_urls = await collect_visible_offer_urls(pw_page, domain, selector) if selector else []
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


async def has_next_page(pw_page: Page) -> bool:
    try:
        return await pw_page.evaluate(
            """() => {
                const btn = document.querySelector('[data-test="top-pagination-next-button"], [data-test="bottom-pagination-button-next"]');
                if (!btn) return false;
                const disabled = btn.hasAttribute('disabled') || btn.getAttribute('aria-disabled') === 'true';
                return !disabled;
            }"""
        )
    except Exception:
        return False


async def goto_next_page(pw_page: Page) -> bool:
    try:
        button = pw_page.locator('[data-test="top-pagination-next-button"]').first
        if await button.count() == 0:
            button = pw_page.locator('[data-test="bottom-pagination-button-next"]').first
        if await button.count() == 0:
            return False
        before = pw_page.url
        await button.click()
        await sleep_ms(2200)
        return pw_page.url != before
    except Exception:
        return False


async def infer_selector_from_first_offer(
    pw_page: Page,
    first_url: str,
) -> Optional[str]:
    try:
        payload = await pw_page.evaluate(
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
                  className: typeof el.className === "string" ? el.className : "",
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
        item for item in matches
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
    session: Any,
    pw_page: Page,
    model_opts: dict[str, Any],
    domain: str,
) -> tuple[list[str], Optional[OfferPattern], Optional[str], int]:
    raw_candidates = await collect_visible_offer_urls(pw_page, domain)
    first_url = raw_candidates[0] if raw_candidates else ""

    inferred_pattern = await infer_offer_pattern_with_llm(
        session=session,
        pw_page=pw_page,
        model_opts=model_opts,
        domain=domain,
        candidate_urls=raw_candidates,
    )
    if inferred_pattern is None and first_url:
        inferred_pattern = infer_offer_pattern_from_url(first_url)

    selector = await infer_selector_from_first_offer(
        pw_page=pw_page,
        first_url=inferred_pattern.first_url if inferred_pattern else first_url,
    )
    page_urls, visible_cards_count = await collect_page_offer_urls(
        pw_page=pw_page,
        domain=domain,
        selector=selector,
    )
    return page_urls, inferred_pattern, selector, visible_cards_count


async def run_stagehand_pp() -> None:
    started = time.perf_counter()
    load_dotenv()

    model_name = env_str("MODEL")
    if not model_name:
        raise RuntimeError("MODEL env var is required (example: openai/gpt-5-mini).")

    llm_api_key = env_str("LLM_API_KEY") or None
    llm_api_base = env_str("LLM_API_BASE") or None

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
        domain = normalize_domain(urlsplit("https://www.pracuj.pl").netloc)

        console.print("CACHE_PROBE=disabled")
        console.print(f"TARGET=pracuj DOMAIN_DETECTED={domain}")
        console.print(f"START_URL={PRACUJ_POC_URL}")

        await session.navigate(url=PRACUJ_POC_URL, page=pw_page)
        await sleep_ms(2000)
        await accept_cookies(session, pw_page, model_opts)
        await dismiss_pracuj_popups(pw_page)
        await accept_cookies(session, pw_page, model_opts)

        offers: list[str] = []
        inferred_pattern: Optional[OfferPattern] = None
        selector: Optional[str] = None
        raw_count = 0
        for attempt in range(1, 5):
            offers, inferred_pattern, selector, raw_count = await extract_pracuj_visible_offer_urls(
                session=session,
                pw_page=pw_page,
                model_opts=model_opts,
                domain=domain,
            )
            console.print(f"ATTEMPT={attempt} RAW_CANDIDATE_COUNT={raw_count} FOUND_URLS_COUNT={len(offers)} PAGE={pw_page.url}")
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
                f"PAGE_FOUND_URLS_COUNT={len(current_page_offers)} TOTAL_FOUND_URLS_COUNT={len(all_offers)} PAGE={pw_page.url}"
            )
            if not await has_next_page(pw_page):
                break
            moved = await goto_next_page(pw_page)
            if not moved:
                break
            current_page_offers, _, _, current_page_cards = await extract_pracuj_visible_offer_urls(
                session=session,
                pw_page=pw_page,
                model_opts=model_opts,
                domain=domain,
            )
            page_index += 1

        if inferred_pattern:
            console.print(f"FIRST_GOOD_OFFER_URL={inferred_pattern.first_url}")
            console.print(f"OFFER_URL_PATTERN_INCLUDE_TOKENS={','.join(inferred_pattern.include_tokens) or '-'}")
            console.print(f"OFFER_URL_PATTERN_EXCLUDE_TOKENS={','.join(inferred_pattern.exclude_tokens) or '-'}")
        if selector:
            console.print(f"JOB_LINK_SELECTOR={selector}", markup=False)

        for offer in all_offers:
            console.print(offer)
        console.print(f"FOUND_URLS_COUNT={len(all_offers)}")
        console.print(f"OFFERS_COUNT={len(all_offers)}")

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
    asyncio.run(run_stagehand_pp())


if __name__ == "__main__":
    main()
