import asyncio
import json
import time
from datetime import datetime
from typing import Any, List

from rich.console import Console
from rich.panel import Panel

from .environment_setup import validate_and_setup_environment
from .server_manager import create_playwright_server
from .agent_runner import AgentRunner
from .prompts import generate_job_board_instructions, generate_career_site_instructions

from .config import MCP_MAX_TURNS


console = Console()


# =============================
#  SIMPLE SANDBOX CONFIG AREA
# =============================
# Set exactly ONE of these to True at a time for easiest use.
RUN_JOB_BOARD_SANDBOX = True
RUN_CAREER_SITE_SANDBOX = False
RUN_SINGLE_JOB_DETAIL_SANDBOX = False

# Hard-coded URLs for sandbox runs.
# Edit these values to test different sources without touching Airtable.
JOB_BOARD_URL = "https://justjoin.it/job-offers/all-locations/python?experience-level=junior&orderBy=DESC&sortBy=newest"
CAREER_SITE_URL = "https://example.com/careers"
SINGLE_JOB_DETAIL_URL = ""

# Optional: label for manually tested single job detail source
SINGLE_JOB_DETAIL_SOURCE_NAME = "manual-single-job"

# Control how much detail you see in the output
VERBOSE_OUTPUT = True
MAX_JOBS_TO_SHOW = 5


def format_duration(seconds: float) -> str:
    """Format duration in seconds to a human-readable string.
    
    Shows seconds for times under 60 seconds, minutes for longer times.
    """
    if seconds < 60:
        return f"{seconds:.2f}s"
    else:
        minutes = seconds / 60
        return f"{minutes:.2f} min"


def extract_source_name(url: str) -> str:
    """Extract a clean source name from a URL."""
    if not url:
        return "unknown"
    
    url = url.lower().replace("https://", "").replace("http://", "").replace("www.", "")
    domain = url.split("/")[0].split("?")[0]

    if domain.count(".") > 1:
        parts = domain.split(".")
        domain = ".".join(parts[-2:]) if len(parts) > 2 else domain

    return domain.lower()


def _parse_records(output_text: str):
    """Parse agent output to extract job records.
    
    Accepts raw text from the agent and tries to extract a JSON array.
    Returns a list of records or None.
    """
    if not output_text:
        return None

    if not isinstance(output_text, str):
        console.print(Panel(
            f"Unexpected agent output type: {type(output_text).__name__}",
            title="Parse Error",
            style="red",
        ))
        return None

    stripped = output_text.strip()

    if stripped.startswith("[") and stripped.endswith("]"):
        candidate = stripped
    else:
        start = stripped.find("[")
        end = stripped.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return None
        candidate = stripped[start : end + 1]

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None

    if isinstance(parsed, list):
        return parsed
    return None


def _print_records_summary(records: List[dict], label: str) -> None:
    """Pretty-print a small summary of scraped records."""
    if not records:
        console.print(Panel(
            f"No records returned for {label}.",
            title="Sandbox Result",
            style="yellow",
        ))
        return

    total = len(records)
    to_show = records[:MAX_JOBS_TO_SHOW]

    console.print(Panel(
        f"Got {total} record(s) for {label}. Showing up to {len(to_show)}.",
        title="Sandbox Result",
        style="green",
    ))

    for i, rec in enumerate(to_show, start=1):
        fields = rec.get("fields", rec)
        title = fields.get("Position") or fields.get("Title") or "(no title)"
        company = fields.get("Company", "(no company)")
        location = fields.get("Location", "(no location)")
        salary = fields.get("Salary", "(no salary)")
        link = fields.get("Link", "(no link)")

        body_lines = [
            f"#{i}",
            f"Title   : {title}",
            f"Company : {company}",
            f"Location: {location}",
            f"Salary  : {salary}",
            f"Link    : {link}",
        ]

        if VERBOSE_OUTPUT:
            body_lines.append("\nFull record JSON:")
            body_lines.append(json.dumps(fields, indent=2, ensure_ascii=False))

        console.print(Panel("\n".join(body_lines), title=f"Job {i}", style="cyan"))


async def run_job_board_sandbox(runner: AgentRunner, agent: Any) -> None:
    url = JOB_BOARD_URL.strip()
    if not url:
        console.print(Panel(
            "JOB_BOARD_URL is empty. Set it at the top of sandbox_runner.py.",
            title="Config Error",
            style="red",
        ))
        return

    source_name = extract_source_name(url)
    task_prompt = generate_job_board_instructions(url=url, source_name=source_name)

    console.print(Panel(
        f"Running JOB BOARD sandbox for:\n{url}",
        title="Sandbox: Job Board",
        style="blue",
    ))

    start = time.time()
    result = await runner.run_agent_with_retry(agent, task_prompt, max_turns=MCP_MAX_TURNS)
    duration = time.time() - start

    final_output = getattr(result, "final_output", None)
    records = _parse_records(final_output) or []

    console.print(f"[dim]Job board sandbox finished in {format_duration(duration)}[/]")
    _print_records_summary(records, label=f"job board {source_name}")


async def run_career_site_sandbox(runner: AgentRunner, agent: Any) -> None:
    url = CAREER_SITE_URL.strip()
    if not url:
        console.print(Panel(
            "CAREER_SITE_URL is empty. Set it at the top of sandbox_runner.py.",
            title="Config Error",
            style="red",
        ))
        return

    source_name = extract_source_name(url)
    task_prompt = generate_career_site_instructions(url=url, source_name=source_name)

    console.print(Panel(
        f"Running CAREER SITE sandbox for:\n{url}",
        title="Sandbox: Career Site",
        style="blue",
    ))

    start = time.time()
    result = await runner.run_agent_with_retry(agent, task_prompt, max_turns=MCP_MAX_TURNS)
    duration = time.time() - start

    final_output = getattr(result, "final_output", None)
    records = _parse_records(final_output) or []

    console.print(f"[dim]Career site sandbox finished in {format_duration(duration)}[/]")
    _print_records_summary(records, label=f"career site {source_name}")


async def run_single_job_detail_sandbox(runner: AgentRunner, agent: Any) -> None:
    url = SINGLE_JOB_DETAIL_URL.strip()
    if not url:
        console.print(Panel(
            "SINGLE_JOB_DETAIL_URL is empty. Set it at the top of sandbox_runner.py.",
            title="Config Error",
            style="red",
        ))
        return

    source_name = SINGLE_JOB_DETAIL_SOURCE_NAME or extract_source_name(url)

    # We reuse the job board instructions but point it directly at a single job detail URL.
    task_prompt = generate_job_board_instructions(url=url, source_name=source_name)

    console.print(Panel(
        f"Running SINGLE JOB DETAIL sandbox for:\n{url}",
        title="Sandbox: Single Job Detail",
        style="blue",
    ))

    start = time.time()
    result = await run_agent_with_task(agent, task_prompt, max_turns=MCP_MAX_TURNS)
    duration = time.time() - start

    final_output = getattr(result, "final_output", None)
    records = _parse_records(final_output) or []

    console.print(f"[dim]Single job detail sandbox finished in {format_duration(duration)}[/]")
    _print_records_summary(records, label=f"single job detail {source_name}")


def _create_sandbox_agent(playwright_server: Any) -> tuple[AgentRunner, Any]:
    """Create a lightweight sandbox agent with minimal instructions.

    This avoids the very long default narrative instructions used in the
    main program, which can exceed the model's context window.
    """
    runner = AgentRunner()

    sandbox_instructions = (
        "You are a focused web scraping assistant. "
        "You receive a single text task that already contains detailed "
        "instructions about how to scrape one job board or career site "
        "using the Playwright MCP browser. "
        "Follow the task exactly, visit the given URL(s), and return ONLY "
        "a valid JSON array of job objects as described there. "
        "Do not add explanations or commentary outside the JSON."
    )

    agent = runner.create_agent(
        name="Sandbox Agent",
        instructions=sandbox_instructions,
        mcp_servers=[playwright_server],
    )

    return runner, agent


async def main() -> None:
    console.print(f"\n[bold blue]Sandbox runner started at {datetime.now().strftime('%H:%M:%S')}[/]")
    console.print("[dim]This script does NOT read from or write to Airtable.[/]")

    # Basic environment checks (API keys, etc.)
    validate_and_setup_environment()

    async with create_playwright_server() as playwright_server:
        runner, agent = _create_sandbox_agent(playwright_server)

        if RUN_JOB_BOARD_SANDBOX:
            await run_job_board_sandbox(runner, agent)

        if RUN_CAREER_SITE_SANDBOX:
            await run_career_site_sandbox(runner, agent)

        if RUN_SINGLE_JOB_DETAIL_SANDBOX:
            await run_single_job_detail_sandbox(runner, agent)

    console.print("\n[bold green]Sandbox runner finished.[/]")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("Interrupted by user.")
    except (ImportError, RuntimeError, ValueError) as e:
        console.print(Panel(
            f"Fatal error ({type(e).__name__}): {e}",
            title="Fatal Error",
            style="red",
        ))
        console.print_exception(show_locals=False)
