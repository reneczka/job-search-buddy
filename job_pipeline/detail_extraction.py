from __future__ import annotations

import re
from typing import Any

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

    requirements: list[str] = []
    raw_requirements = payload.get("requirements")
    if isinstance(raw_requirements, list):
        requirements = [item.strip() for item in raw_requirements if isinstance(item, str) and item.strip()]

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
        raw=dict(payload),
    )


async def _extract_payload(runtime: StagehandRuntime) -> dict[str, Any]:
    try:
        extracted = await runtime.session.extract(
            instruction=(
                "Extract job details from this job offer page. "
                "Return concise plain text. "
                "If a field is missing, return an empty string. "
                "Requirements should be short text items, not full paragraphs."
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
                };
            }"""
        )
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _clean(value: Any) -> str:
    return str(value or "").strip()


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
