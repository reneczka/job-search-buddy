"""
Career Sites Scraper - Scrapes jobs from company career pages.
Uses the "Career Sites" column from the sources table in Airtable.

This is a thin wrapper that calls the shared scrape_runner module.
"""

import asyncio

from rich.console import Console
from rich.panel import Panel

from prompts import generate_career_site_instructions
from scrape_runner import run_scraper


console = Console()

# Default hardcoded URL for testing (used when USE_HARDCODED_SOURCE=True)
HARDCODED_CAREER_SITE_URL = "https://example.com/careers"


async def main() -> None:
    """Main entry point for career sites scraper."""
    await run_scraper(
        scraper_name="Career Sites",
        column_name="Career Sites",
        prompt_generator=generate_career_site_instructions,
        hardcoded_url=HARDCODED_CAREER_SITE_URL,
        sort_by="-Career Sites",
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
