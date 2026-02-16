import asyncio
import json
import os
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from rich.console import Console

console = Console(markup=False)

PROTOCOL_POC_URL = "https://theprotocol.it/filtry/python;t/trainee,assistant,junior;p?sort=date"


def _debug_enabled() -> bool:
    return os.getenv("STAGEHAND_DEBUG", "false").lower() in {"1", "true", "yes"}


def _debug(message: str) -> None:
    if _debug_enabled():
        console.print(f"[debug] {message}")


class OfferUrls(BaseModel):
    urls: list[str] = Field(default_factory=list, description="Absolute URLs to real non-promotional job offer detail pages currently visible.")


def _normalize_offer_url(url: str) -> str:
    """Normalize offer URL by removing query params and fragments."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _normalize_page_url(url: str) -> str:
    """Normalize page URL for visited-page tracking."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _resolve_stagehand_model_name() -> Optional[str]:
    """Use MODEL env (OpenRouter-compatible) and map it to Stagehand/LiteLLM model id."""
    raw_model = (os.getenv("MODEL") or "").strip()
    if not raw_model:
        return None

    base_url = (os.getenv("LLM_API_BASE") or "").lower()
    if "openrouter.ai" in base_url and not raw_model.startswith("openrouter/"):
        return f"openrouter/{raw_model}"
    return raw_model


def _resolve_llm_api_key() -> Optional[str]:
    """Resolve API key from LLM_API_KEY."""
    return (os.getenv("LLM_API_KEY") or "").strip() or None


def _resolve_llm_api_base() -> Optional[str]:
    """Resolve API base from LLM_API_BASE."""
    return (os.getenv("LLM_API_BASE") or "").strip() or None


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
        for key, value in payload.items():
            if key.lower() in {"url", "urls", "link", "links", "href", "hrefs"}:
                found.extend(_extract_urls_from_payload(value))
            else:
                found.extend(_extract_urls_from_payload(value))
        return found
    return found


async def _close_cookie_overlay(page: Any) -> None:
    """Best-effort cookie acceptance to unblock page content."""
    await page.evaluate(
        """() => {
          const labels = ["accept", "accept all", "agree", "zgadzam", "akcept", "zaakceptuj"];
          const candidates = Array.from(document.querySelectorAll('button, [role="button"], a'));
          for (const el of candidates) {
            const text = (el.textContent || "").trim().toLowerCase();
            if (!text) continue;
            if (labels.some((label) => text.includes(label))) {
              el.click();
              return true;
            }
          }
          return false;
        }"""
    )


async def _extract_visible_offer_urls_with_llm(page: Any, domain: str) -> list[str]:
    """Use Stagehand LLM extraction instead of hardcoded DOM selectors."""
    try:
        extracted = await page.extract(
            """
            Extract currently visible, real job-offer detail URLs from the main results area.
            Exclude sponsored/promoted/advertisement/banner offers and controls.
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
            # Last-resort parse from serialized output for model variations.
            text = ""
            try:
                text = json.dumps(getattr(extracted, "model_dump", lambda: extracted)(), ensure_ascii=False)
            except Exception:
                text = str(extracted)

            import re
            raw_urls.extend(re.findall(r"https?://[^\\s\"'<>]+", text))

    out: list[str] = []
    seen: set[str] = set()
    page_base = page.url
    for raw in raw_urls:
        if not isinstance(raw, str):
            continue
        candidate = raw.strip()
        if candidate.startswith("/"):
            candidate = urljoin(page_base, candidate)
        normalized = _normalize_offer_url(candidate)
        if not normalized:
            continue
        parts = urlsplit(normalized)
        if parts.scheme not in {"http", "https"}:
            continue
        if not parts.netloc:
            normalized = _normalize_offer_url(urljoin(page_base, normalized))
            parts = urlsplit(normalized)
        if parts.netloc.lower() != domain.lower():
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)

    generic = await _extract_visible_offer_urls_generic(page, domain)
    merged: list[str] = []
    seen: set[str] = set()
    for url in out + generic:
        if url in seen:
            continue
        seen.add(url)
        merged.append(url)
    return merged


async def _extract_visible_offer_urls_generic(page: Any, domain: str) -> list[str]:
    """Generic DOM fallback extraction (non-site-specific selectors)."""
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
        normalized = _normalize_offer_url(entry)
        parts = urlsplit(normalized)
        if parts.netloc.lower() != domain.lower():
            continue
        path = parts.path.lower()
        if "/szczegoly/praca/" not in path and ",oferta," not in normalized.lower():
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


async def _reveal_more_results_with_llm(page: Any) -> bool:
    """Use Stagehand LLM to choose next action: load-more or pagination next."""
    try:
        actions = await page.observe(
            """
            Find one best action that reveals more real job offers in the main listing.
            Priority:
            1) Click a real 'load more offers' control in job results.
            2) If unavailable, click next page in results pagination.
            Ignore promotional/sponsored sections and ads.
            If no such action exists, return no actions.
            """
        )
    except Exception:
        return False

    if not actions:
        return False

    try:
        result = await page.act(actions[0])
    except Exception:
        return False

    await page.wait_for_timeout(1400)
    return bool(getattr(result, "success", True))


async def _collect_offer_urls_universal(page: Any, domain: str) -> list[str]:
    """Universal collection: lazy scroll + optional load-more + optional pagination."""
    max_steps = int(os.getenv("STAGEHAND_POC_MAX_STEPS", "20"))

    seen_urls: set[str] = set()
    visited_pages: set[str] = set()
    stagnation = 0

    for _ in range(max_steps):
        step_num = _ + 1
        current_page = _normalize_page_url(page.url)
        _debug(f"step={step_num} page={current_page} seen={len(seen_urls)} stagnation={stagnation}")
        if current_page in visited_pages and stagnation >= 2:
            _debug(f"stop: revisited page with stagnation>=2 at step={step_num}")
            break
        visited_pages.add(current_page)

        before = len(seen_urls)
        extracted_urls = await _extract_visible_offer_urls_with_llm(page, domain)
        _debug(f"step={step_num} extracted={len(extracted_urls)}")
        for url in extracted_urls:
            seen_urls.add(url)

        if len(seen_urls) > before:
            stagnation = 0
        else:
            stagnation += 1

        moved = await _reveal_more_results_with_llm(page)
        if moved:
            _debug(f"step={step_num} action=move-more-results")
            continue

        # Fallback lazy-load trigger when no explicit action is found.
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(800)
        except Exception:
            pass

        after_scroll_before = len(seen_urls)
        fallback_urls = await _extract_visible_offer_urls_generic(page, domain)
        _debug(f"step={step_num} fallback_extracted={len(fallback_urls)}")
        for url in fallback_urls:
            seen_urls.add(url)

        if len(seen_urls) > after_scroll_before:
            stagnation = 0
        else:
            stagnation += 1

        if stagnation >= 3:
            _debug(f"stop: stagnation>=3 at step={step_num}")
            break

    _debug(f"done: total_seen={len(seen_urls)} pages={len(visited_pages)}")
    return list(seen_urls)


async def _collect_offer_urls_recovery(page: Any, target_url: str, domain: str) -> list[str]:
    """Deterministic recovery sweep across pagination URLs when LLM path under-collects."""
    base_path = urlsplit(target_url).path
    seen_urls: set[str] = set()
    pages_to_visit: set[str] = {_normalize_page_url(target_url), _normalize_page_url(page.url)}

    async def _collect_current_page_urls() -> None:
        urls = await _extract_visible_offer_urls_generic(page, domain)
        for u in urls:
            seen_urls.add(u)

    async def _collect_pagination_links() -> None:
        links = await page.evaluate(
            """() => {
              const out = [];
              for (const a of Array.from(document.querySelectorAll("a[href]"))) {
                const href = a.getAttribute("href") || "";
                const text = (a.textContent || "").trim().toLowerCase();
                const rel = (a.getAttribute("rel") || "").toLowerCase();
                const aria = (a.getAttribute("aria-label") || "").toLowerCase();
                const isPagy = rel === "next" || rel === "prev" || /page=\\d+|strona=\\d+|\\/page\\/\\d+|\\/strona\\/\\d+/i.test(href) || /^\\d+$/.test(text) || /nast[eę]pn/i.test(text) || /nast[eę]pn/i.test(aria);
                if (!isPagy) continue;
                try { out.push(new URL(href, window.location.origin).toString()); } catch {}
              }
              return out;
            }"""
        )
        if isinstance(links, list):
            for link in links:
                if not isinstance(link, str):
                    continue
                parsed = urlsplit(link)
                if parsed.netloc.lower() != domain.lower():
                    continue
                if parsed.path != base_path:
                    continue
                pages_to_visit.add(_normalize_page_url(link))

    # First page + currently loaded page
    await _collect_current_page_urls()
    await _collect_pagination_links()

    visited: set[str] = set()
    for page_url in list(pages_to_visit):
        if page_url in visited:
            continue
        visited.add(page_url)
        await page.goto(page_url)
        await page.wait_for_timeout(1200)
        await _collect_current_page_urls()
        await _collect_pagination_links()

        # Best-effort click generic "more offers" controls on each page.
        for _ in range(8):
            clicked = await page.evaluate(
                """() => {
                  const pats = [/wi[eę]cej\\s*ofert/i, /poka[zż].*wi[eę]cej/i, /za[lł]aduj.*wi[eę]cej/i, /load\\s*more/i];
                  const els = Array.from(document.querySelectorAll("button,[role='button'],a[href]"));
                  for (const el of els) {
                    const txt = (el.textContent || "").trim().toLowerCase().replace(/\\s+/g, " ");
                    if (!txt) continue;
                    if (!pats.some((p) => p.test(txt))) continue;
                    const disabled = el.hasAttribute("disabled") || el.getAttribute("aria-disabled") === "true";
                    if (disabled) continue;
                    el.scrollIntoView({block: "center"});
                    el.click();
                    return true;
                  }
                  return false;
                }"""
            )
            await page.wait_for_timeout(900)
            if not clicked:
                break
            await _collect_current_page_urls()

    return list(seen_urls)


async def run_stagehand_protocol_first_offer() -> None:
    """Print all non-promotional offer URLs from theprotocol.it."""
    load_dotenv()

    target_url = os.getenv("STAGEHAND_POC_URL", PROTOCOL_POC_URL).strip()
    domain = urlsplit(target_url).netloc

    try:
        from stagehand import Stagehand, StagehandConfig  # type: ignore
    except Exception as exc:
        raise RuntimeError("Stagehand is not installed. Install deps and retry (poetry install).") from exc

    model_name = _resolve_stagehand_model_name()

    config_kwargs: dict[str, Any] = {
        "env": os.getenv("STAGEHAND_ENV", "LOCAL"),
        "enableCaching": os.getenv("STAGEHAND_ENABLE_CACHING", "true").lower() in {"1", "true", "yes"},
        "selfHeal": True,
    }

    llm_api_key = _resolve_llm_api_key()
    llm_api_base = _resolve_llm_api_base()

    if model_name:
        config_kwargs["modelName"] = model_name
    if llm_api_key:
        config_kwargs["modelApiKey"] = llm_api_key
    if llm_api_base:
        config_kwargs["modelClientOptions"] = {"base_url": llm_api_base}
    if os.getenv("BROWSERBASE_API_KEY"):
        config_kwargs["apiKey"] = os.getenv("BROWSERBASE_API_KEY")
    if os.getenv("BROWSERBASE_PROJECT_ID"):
        config_kwargs["projectId"] = os.getenv("BROWSERBASE_PROJECT_ID")

    stagehand = Stagehand(StagehandConfig(**config_kwargs))
    await stagehand.init()

    try:
        page = stagehand.page
        await page.goto(target_url)
        await page.wait_for_timeout(1500)

        await _close_cookie_overlay(page)
        await page.wait_for_timeout(500)

        offer_urls = await _collect_offer_urls_universal(page, domain)
        if len(offer_urls) < 40:
            _debug(f"under-collected via llm path ({len(offer_urls)}), running recovery sweep")
            recovered = await _collect_offer_urls_recovery(page, target_url, domain)
            if len(recovered) > len(offer_urls):
                offer_urls = recovered
        if not offer_urls:
            raise RuntimeError("No non-promotional job offers found on page.")

        offer_urls = sorted(offer_urls)

        for offer_url in offer_urls:
            console.print(offer_url)
        console.print(f"OFFERS_COUNT={len(offer_urls)}")
    finally:
        await stagehand.close()


def main() -> None:
    asyncio.run(run_stagehand_protocol_first_offer())


if __name__ == "__main__":
    main()
