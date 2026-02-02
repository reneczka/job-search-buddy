import asyncio
import json
import os
from typing import List
from urllib.parse import urlparse

from rich.console import Console
from rich.panel import Panel
from dotenv import load_dotenv

from .environment_setup import validate_and_setup_environment

from pydantic_ai import Agent, UsageLimits
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.mcp import MCPServerStdio


console = Console(markup=False)

# Simple hardcoded job board URLs for the PydanticAI sandbox.
JOBBOARD_URLS: List[str] = [
    # "https://justjoin.it/job-offers/all-locations/python?experience-level=junior&orderBy=DESC&sortBy=newest",
    "https://theprotocol.it/filtry/python;t/trainee,assistant,junior;p?sort=date"
]


def build_model() -> OpenAIChatModel:
    """Create an OpenAI-compatible model for PydanticAI.

    Uses environment variables already configured for the main project
    (OPENAI_API_KEY, OPENAI_API_BASE, OPENAI_MODEL).
    """
    # Make sure .env is loaded before reading OPENAI_* variables
    load_dotenv()

    model_name = os.getenv("OPENAI_MODEL")
    base_url = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
    api_key = os.getenv("OPENAI_API_KEY")

    if not model_name:
        raise RuntimeError("OPENAI_MODEL is not set; cannot run PydanticAI sandbox.")

    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set; cannot run PydanticAI sandbox.")

    provider = OpenAIProvider(base_url=base_url, api_key=api_key)
    return OpenAIChatModel(model_name=model_name, provider=provider)


model = build_model()

playwright_server = MCPServerStdio(
    command="npx",
    args=["-y", "@playwright/mcp@latest"],
)

system_prompt = (
    "ROLE: Job link collector using Playwright MCP. "
    "TASK: Collect all job-detail URLs from a job board page. "
    "RULES: "
    "1) Stay on the same domain. "
    "2) No new tabs, no popups. "
    "3) Use browser_run_code ONCE with a function that scrolls and finds links. "
    "4) Return ONLY a list of URLs, nothing else."
)

agent: Agent[None, List[str]] = Agent(
    model,
    name="jobboard_link_collector",
    system_prompt=system_prompt,
    toolsets=[playwright_server],
)


async def collect_job_links(url: str) -> List[str]:
    """Run the jobboard agent for a single URL and return collected links."""
    url_to_use = url.strip()
    if not url_to_use:
        raise RuntimeError("No job board URL provided.")

    console.print(f"[subagent][jobboard] Starting scraping for URL: {url_to_use}")
    console.print("[subagent][jobboard] Agent will use a single browser_run_code Playwright snippet to collect job-detail links.")

    user_message = (
        f"TASK: Collect all job-detail URLs from: {url_to_use}\n\n"
        "HINTS: Look for job links with patterns like /job-offer/, /oferta/, /job/, /career/, /position/, /szczegoly/praca/. Scroll to load lazy content. Filter out ads (sponsored, promoted, ad banners). Return unique URLs. STAY ON CURRENT PAGE - DO NOT OPEN NEW TABS OR NAVIGATE TO OTHER PAGES.\n\n"
        "COPY AND RUN THIS CODE in browser_run_code:\n\n"
        "async (page) => {\n"
        "  await page.goto('{url_to_use}');\n"
        "  await page.waitForTimeout(2000);\n"
        "  const seen = new Set();\n"
        "  let prevY = 0;\n"
        "  let stable = 0;\n"
        "  for (let i = 0; i < 60; i++) {\n"
        "    await page.evaluate(() => window.scrollBy(0, window.innerHeight));\n"
        "    await page.waitForTimeout(300);\n"
        "    const { y, h } = await page.evaluate(() => ({ y: window.scrollY, h: document.body.scrollHeight }));\n"
        "    const links = await page.evaluate(() => {\n"
        "      const anchors = Array.from(document.querySelectorAll('a[href]'));\n"
        "      return anchors.map(a => a.href);\n"
        "    });\n"
        "    links.forEach(href => seen.add(href));\n"
        "    if (Math.abs(y - prevY) < 5) {\n"
        "      stable++;\n"
        "      if (stable >= 3) {\n"
        "        await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));\n"
        "        await page.waitForTimeout(500);\n"
        "        const linksAfter = await page.evaluate(() => {\n"
        "          const anchors = Array.from(document.querySelectorAll('a[href]'));\n"
        "          return anchors.map(a => a.href);\n"
        "        });\n"
        "        linksAfter.forEach(href => seen.add(href));\n"
        "        break;\n"
        "      }\n"
        "    } else {\n"
        "      stable = 0;\n"
        "    }\n"
        "    prevY = y;\n"
        "  }\n"
        "  const allLinks = Array.from(seen);\n"
        "  const jobLinks = allLinks.filter(url => {\n"
        "    const path = url.split('?')[0].toLowerCase();\n"
        "    const isAd = url.includes('sug=') || url.includes('ad=') || url.includes('sponsored');\n"
        "    return (path.includes('/job-offer/') || path.includes('/szczegoly/praca/') || \n"
        "           path.includes('/oferta/') || path.includes('/job/') || \n"
        "           path.includes('/career/') || path.includes('/position/')) && !isAd;\n"
        "  });\n"
        "  const uniqueUrls = new Set();\n"
        "  jobLinks.forEach(url => uniqueUrls.add(url.split('?')[0]));\n"
        "  return Array.from(uniqueUrls);\n"
        "}\n\n"
        "Return ONLY the array of URLs."
    )

    console.print("[subagent][jobboard] Sending task to PydanticAI agent 'jobboard_link_collector'...")

    # For the sandbox, allow generous usage limits so the agent can
    # freely use MCP tools without hitting the default request_limit.
    usage_limits = UsageLimits(
        request_limit=500,
        tool_calls_limit=200,
        response_tokens_limit=None,
    )

    try:
        result = await agent.run(user_message, usage_limits=usage_limits)
    except Exception as e:  # guard small model/tool flakiness
        console.print(f"[subagent][jobboard] Agent failed: {e}", style="red")
        return []

    console.print("[subagent][jobboard] Agent finished, parsing raw output into list of links...")

    raw_output = result.output
    if isinstance(raw_output, str):
        try:
            links = json.loads(raw_output)
        except json.JSONDecodeError:
            import re

            match = re.search(r"\[.*\]", raw_output, re.DOTALL)
            if match:
                try:
                    links = json.loads(match.group(0))
                except json.JSONDecodeError:
                    links = []
            else:
                links = []
    elif isinstance(raw_output, list):
        links = raw_output
    else:
        links = []

    if not isinstance(links, list):
        console.print("[subagent][jobboard] Parsed output is not a list, returning empty list of links.")
        return []

    # Filter links to only keep URLs from the same domain as the jobboard URL
    jobboard_domain = urlparse(url_to_use).netloc.lower()
    filtered_links: List[str] = []
    for link in links:
        if not isinstance(link, str):
            continue
        parsed = urlparse(link)
        if not parsed.scheme or not parsed.netloc:
            # Skip relative or malformed URLs
            continue
        if parsed.netloc.lower() != jobboard_domain:
            continue
        filtered_links.append(link)

    console.print(
        f"[subagent][jobboard] Successfully parsed {len(links)} raw links, "
        f"{len(filtered_links)} links kept after domain filtering for this jobboard."
    )
    return filtered_links


async def run_jobboards(urls: List[str]) -> None:
    """Run the jobboard agent for a list of URLs and log results per jobboard."""
    validate_and_setup_environment()

    console.print("[orchestrator] Starting multi-jobboard scraping pipeline...")
    console.print(f"[orchestrator] Total jobboards to process: {len(urls)}")
    console.print("[orchestrator] Max concurrent Playwright instances: 5")
    console.print("[orchestrator] Link collection limit per jobboard: None (collect all)")
    console.print("[orchestrator] Starting parallel execution...")

    semaphore = asyncio.Semaphore(5)

    async def _process_jobboard(idx: int, url: str) -> None:
        async with semaphore:
            url_to_use = (url or "").strip()
            if not url_to_use:
                return

            console.print(f"[orchestrator][jobboard #{idx}] Starting processing...")
            console.print(f"[orchestrator][jobboard #{idx}] URL: {url_to_use}")

            links = await collect_job_links(url_to_use)

            if not links:
                console.print(
                    Panel(
                        "No job links were returned by the agent.",
                        title=f"[orchestrator][jobboard #{idx}] Result",
                        style="yellow",
                    )
                )
                return

            total = len(links)
            console.print(
                Panel(
                    f"Agent returned {total} job links (collecting all, printing first 5):",
                    title=f"[orchestrator][jobboard #{idx}] Result",
                    style="green",
                )
            )

            console.print(f"[orchestrator][jobboard #{idx}] Total collected: {total} links")
            console.print(f"[orchestrator][jobboard #{idx}] Showing first 5 links:")
            for i, link in enumerate(links[:5], start=1):
                console.print(f"[orchestrator][jobboard #{idx}][{i}] {link}")

            console.print(f"[orchestrator][jobboard #{idx}] Finished processing")

    tasks = [
        _process_jobboard(idx, url)
        for idx, url in enumerate(urls, start=1)
    ]

    if tasks:
        await asyncio.gather(*tasks)

    console.print("[orchestrator] All jobboards processed. Pipeline complete.")


def main() -> None:
    asyncio.run(run_jobboards(JOBBOARD_URLS))


if __name__ == "__main__":
    main()
