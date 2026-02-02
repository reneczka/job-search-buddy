import asyncio
import json
import os
from typing import List

from rich.console import Console
from rich.panel import Panel
from dotenv import load_dotenv

from .environment_setup import validate_and_setup_environment

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.mcp import MCPServerStdio, MCPServerStreamableHTTP


console = Console()

# MCP transport selection: True = HTTP, False = stdio (default)
USE_MCP_HTTP_TRANSPORT = False

# Simple hardcoded job board URL for the PydanticAI sandbox.
JOB_BOARD_URL = "https://justjoin.it/job-offers/all-locations/python?experience-level=junior&orderBy=DESC&sortBy=newest"


def build_model() -> OpenAIChatModel:
    """Create an OpenAI-compatible model for PydanticAI.

    Uses environment variables already configured for the main project
    (OPENAI_API_KEY, OPENAI_API_BASE, OPENAI_MODEL).
    """
    # Make sure .env is loaded before reading OPENAI_* variables
    load_dotenv()

    model_name = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    base_url = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set; cannot run PydanticAI sandbox.")

    provider = OpenAIProvider(base_url=base_url, api_key=api_key)
    return OpenAIChatModel(model_name=model_name, provider=provider)


model = build_model()

# Configure a Playwright MCP server for PydanticAI.
if USE_MCP_HTTP_TRANSPORT:
    playwright_url = os.getenv("PLAYWRIGHT_MCP_HTTP_URL", "").strip()
    if not playwright_url:
        raise RuntimeError("PLAYWRIGHT_MCP_HTTP_URL must be set when USE_MCP_HTTP_TRANSPORT=True.")
    playwright_server = MCPServerStreamableHTTP(playwright_url)
else:
    playwright_server = MCPServerStdio(
        command="npx",
        args=["-y", "@playwright/mcp@latest"],
    )

system_prompt = (
    "ROLE: Lightweight orchestrator for a job scraping toolchain using an MCP Playwright browser. "
    "You receive exactly one job board URL. "
    "Your job is to use the MCP Playwright tools to open the page in a real browser, "
    "accept cookie / privacy banners if needed so the content is visible, and "
    "collect links to individual job detail offers. "
    "Return the final result strictly as a Python list of strings (job detail URLs), "
    "with no explanations or extra text."
)

agent: Agent[None, List[str]] = Agent(
    model,
    system_prompt=system_prompt,
    toolsets=[playwright_server],
)


async def run_sandbox(url: str | None = None) -> None:
    """Run the minimal PydanticAI sandbox for a single job board URL."""
    validate_and_setup_environment()

    url_to_use = (url or JOB_BOARD_URL).strip()
    if not url_to_use:
        raise RuntimeError("No job board URL provided.")

    console.print(Panel(f"Running PydanticAI sandbox for:\n{url_to_use}", title="PydanticAI Sandbox", style="magenta"))

    # Minimal task description for the MCP Playwright-based agent.
    user_message = (
        "ROLE: Job board link collector using an MCP Playwright browser for a junior Python job scraping pipeline.\n\n"
        "CONTEXT: We have a job board page that lists many offers. "
        "Your task is ONLY to discover job detail URLs on that page, using the available MCP Playwright browser tools. "
        "You should open the page in the browser, wait for 5 seconds to allow any cookie/consent banners to appear, "
        "accept or close them if they block the content, and then "
        "collect links to individual job detail offers.\n\n"
        "TASK:\n"
        "- Use the MCP Playwright tools to navigate to the provided URL.\n"
        "- Wait for 5 seconds after page load to ensure cookie banners are visible.\n"
        "- Accept or close any cookie/consent overlays so the job list is visible.\n"
        "- Identify links that clearly correspond to individual job detail pages (not category or search pages).\n"
        "- Do NOT invent or guess URLs; only use links that actually exist on the page.\n\n"
        "OUTPUT:\n"
        "- Return ONLY a valid JSON array of strings (URLs), nothing else.\n"
        "- Example: [\"https://example.com/job/1\", \"https://example.com/job/2\"]\n"
        "- If no links found, return []\n\n"
        f"JOB_BOARD_URL: {url_to_use}"
    )

    result = await agent.run(user_message)

    # Handle both direct list output and JSON string output from agent
    raw_output = result.output
    if isinstance(raw_output, str):
        try:
            links = json.loads(raw_output)
        except json.JSONDecodeError:
            # Try to extract JSON array from string if direct parse fails
            import re
            match = re.search(r'\[.*\]', raw_output, re.DOTALL)
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

    if not links:
        console.print(Panel("No job links were returned by the agent.", title="Result", style="yellow"))
        return

    total = len(links)
    console.print(Panel(f"Agent returned {total} job links (first 5):", title="Result", style="green"))

    for i, link in enumerate(links[:5], start=1):
        console.print(f"[{i}] {link}")


def main() -> None:
    url = os.getenv("PYDANTIC_SANDBOX_URL", JOB_BOARD_URL)
    asyncio.run(run_sandbox(url))


if __name__ == "__main__":
    main()
