"""
Job Boards Scraper - Scrapes jobs from job board aggregator sites.
Uses the "Job Boards" column from the sources table in Airtable.

This is a thin wrapper that calls the shared scrape_runner module.
"""

import asyncio

from rich.console import Console
from rich.panel import Panel

from prompts import generate_job_board_instructions
from scrape_runner import run_scraper


console = Console()

# Default hardcoded URL for testing (used when USE_HARDCODED_SOURCE=True)
HARDCODED_JOB_BOARD_URL = "https://bulldogjob.pl/companies/jobs/s/skills,Python/experienceLevel,intern,junior/order,published,desc"


async def main() -> None:
    """Main entry point for job boards scraper."""
    await run_scraper(
        scraper_name="Job Boards",
        column_name="Job Boards",
        prompt_generator=generate_job_board_instructions,
        hardcoded_url=HARDCODED_JOB_BOARD_URL,
        sort_by="-Job Boards",
    )


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
