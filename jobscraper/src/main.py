"""
Main Scraper - Orchestrates both Job Boards and Career Sites scrapers.
Runs both scrapers sequentially and syncs all results to Airtable.
"""

import asyncio
import json
from typing import Any, List

from rich.console import Console
from rich.panel import Panel

from config import USE_HARDCODED_SOURCE

from environment_setup import validate_and_setup_environment
from server_manager import create_playwright_server
from agent_runner import create_playwright_agent, run_agent_with_task
from prompts import generate_job_board_instructions, generate_career_site_instructions
from airtable_client import AirtableClient, AirtableConfig
import time
from datetime import datetime


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
    """Extract a clean source name from a job board URL."""
    if not url:
        return "unknown"
    
    # Remove protocol and www
    url = url.lower().replace("https://", "").replace("http://", "").replace("www.", "")
    
    # Extract domain name (everything before the first / or ?)
    domain = url.split('/')[0].split('?')[0]
    
    # Handle special cases for subdomains if needed
    if domain.count('.') > 1:
        # For subdomains, take the last two parts (e.g., 'jobs.example.com' -> 'example.com')
        parts = domain.split('.')
        domain = '.'.join(parts[-2:]) if len(parts) > 2 else domain
    
    # Return domain in lowercase
    return domain.lower()


# Removed unused imports and constants

console = Console()

async def main() -> None:
    """Main application entry point - clean orchestration of all components"""
    try:
        program_start_time = time.time()
        console.print(f'\n[bold blue]Complete job scraping program started at {datetime.now().strftime("%H:%M:%S")}[/]')
        console.print('[dim]Running both Job Boards and Career Sites scrapers[/]')
        
        # Validate environment and setup
        validate_and_setup_environment()

        if USE_HARDCODED_SOURCE:
            # Using a default hardcoded source for testing
            sources = [
                {
                    "fields": {
                        "Job Boards": "https://bulldogjob.pl/companies/jobs/s/skills,Python/experienceLevel,intern,junior/order,published,desc",
                        "Career Sites": "https://example.com/careers",
                    }
                }
            ]
            client = None
            console.print(Panel(
                "Using hardcoded job source.",
                title="Sources",
                style="blue",
            ))
        else:
            airtable_config = AirtableConfig.from_env()
            airtable_enabled = airtable_config.is_configured()

            if not airtable_enabled:
                console.print(Panel(
                    "Airtable not configured. Cannot fetch sources.",
                    title="Error",
                    style="red",
                ))
                return

            # Fetch sources from Airtable
            sources_fetch_start = time.time()
            client = AirtableClient(airtable_config)
            sources_table_id = airtable_config.sources_table_id
            if not sources_table_id:
                console.print(Panel(
                    "AIRTABLE_SOURCES_TABLE_ID not set in environment.",
                    title="Error",
                    style="red",
                ))
                return
            sources = client.get_all_records(sources_table_id)
            sources_fetch_end = time.time()
            sources_fetch_time = sources_fetch_end - sources_fetch_start

            # Count sources by type
            job_boards_count = sum(1 for s in sources if s.get("fields", {}).get("Job Boards"))
            career_sites_count = sum(1 for s in sources if s.get("fields", {}).get("Career Sites"))
            console.print(Panel(
                f"Fetched {len(sources)} sources from Airtable in {format_duration(sources_fetch_time)}\n"
                f"  • Job Boards: {job_boards_count}\n"
                f"  • Career Sites: {career_sites_count}",
                title="Sources",
                style="blue"
            ))

            for i, source_record in enumerate(sources, start=1):
                console.print(Panel(f"{i}. {json.dumps(source_record, indent=4)}", title="Source", style="blue"))

            if not sources:
                console.print(Panel(
                    "No sources found in Airtable.",
                    title="Warning",
                    style="yellow",
                ))
                return

        all_records = []
        total_scraping_time = 0

        async with create_playwright_server() as playwright_server:
            agent = create_playwright_agent(playwright_server)

            # Phase 1: Scrape Job Boards
            console.print(f'\n[bold blue]═══ Phase 1: Job Boards Scraping ═══[/]')
            job_boards_start = time.time()
            job_boards_results = await scrape_sources(
                agent, sources, "Job Boards", generate_job_board_instructions
            )
            job_boards_duration = time.time() - job_boards_start
            all_records.extend(job_boards_results)
            console.print(f'[bold green]✓ Job Boards: {len(job_boards_results)} jobs in {format_duration(job_boards_duration)}[/]')

            # Phase 2: Scrape Career Sites
            console.print(f'\n[bold blue]═══ Phase 2: Career Sites Scraping ═══[/]')
            career_sites_start = time.time()
            career_sites_results = await scrape_sources(
                agent, sources, "Career Sites", generate_career_site_instructions
            )
            career_sites_duration = time.time() - career_sites_start
            all_records.extend(career_sites_results)
            console.print(f'[bold green]✓ Career Sites: {len(career_sites_results)} jobs in {format_duration(career_sites_duration)}[/]')

            total_scraping_time = job_boards_duration + career_sites_duration
            console.print(f'\n[bold green]Total jobs scraped: {len(all_records)} ({len(job_boards_results)} from job boards + {len(career_sites_results)} from career sites)[/]')
        
        # Sync all results to Airtable offers table
        if all_records:
            airtable_sync_start = time.time()
            if client:
                result = client.create_records(all_records)
                airtable_sync_end = time.time()
                airtable_sync_time = airtable_sync_end - airtable_sync_start
                console.print(f'\n[bold green]Airtable sync completed in {format_duration(airtable_sync_time)} - created {result["created"]} new records, skipped {result["skipped"]} duplicates[/]')
            else:
                console.print(Panel(
                    "Skipping Airtable sync because hardcoded mode is enabled.",
                    title="Airtable",
                    style="yellow",
                ))
        else:
            console.print(Panel(
                "No records to add to Airtable.",
                title="Airtable",
                style="yellow",
            ))

        # Final program timing
        program_end_time = time.time()
        total_program_time = program_end_time - program_start_time
        console.print(f'\n[bold green]🎉 Complete scraping program finished in {format_duration(total_program_time)}[/]')
        console.print(f'[dim]Breakdown:[/]')
        if not USE_HARDCODED_SOURCE:
            console.print(f'  • Sources fetching: {format_duration(sources_fetch_time)}')
        console.print(f'  • Job scraping: {format_duration(total_scraping_time)}')
        if all_records and client:
            console.print(f'  • Airtable sync: {format_duration(airtable_sync_time)}')
        other_time = total_program_time - total_scraping_time - (sources_fetch_time if not USE_HARDCODED_SOURCE else 0) - (airtable_sync_time if all_records and client else 0)
        console.print(f'  • Other operations: {format_duration(other_time)}')

    except (ImportError, RuntimeError, ValueError) as e:
        console.print(Panel(
            f"Application error ({type(e).__name__}): {e}",
            title="Error",
            style="red",
        ))
        console.print_exception(show_locals=False)
        raise
    except asyncio.CancelledError:
        console.print(Panel(
            "Scraping was cancelled (likely due to Playwright server disconnect/cleanup).",
            title="Cancelled",
            style="yellow",
        ))
        return
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/]")
        raise


def _parse_records(output_text: str):
    """Parse agent output to extract job records.
    
    Args:
        output_text: Raw text output from the agent
        
    Returns:
        List of parsed records or None if parsing fails
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

    # Handle console panels where JSON is enclosed between lines and optional narration
    if stripped.startswith("[") and stripped.endswith("]"):
        candidate = stripped
    else:
        # Try to locate the first '[' and last ']' to extract a JSON array
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


async def scrape_sources(agent: Any, sources: List[dict], column_name: str, prompt_generator) -> list:
    """Scrape jobs from all sources for a specific column type."""
    # Filter sources that have this column
    relevant_sources = [s for s in sources if s.get("fields", {}).get(column_name)]

    if not relevant_sources:
        console.print(f"[dim]No {column_name} sources found, skipping...[/]")
        return []

    console.print(f"[dim]Processing {len(relevant_sources)} {column_name} sources...[/]")

    # Create tasks for each source
    tasks = [
        scrape_source(agent, source_record, column_name, prompt_generator)
        for source_record in relevant_sources
    ]

    # Run all tasks concurrently and collect results
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Process results
    all_records = []
    for i, res in enumerate(results):
        if isinstance(res, Exception):
            source_record = relevant_sources[i]
            source_url = source_record.get("fields", {}).get(column_name, "")
            source_name = extract_source_name(source_url)
            console.print(Panel(
                f"Error scraping {source_name}: {res}",
                title="Scrape Error",
                style="red",
            ))
        else:
            all_records.extend(res)
            source_record = relevant_sources[i]
            source_url = source_record.get("fields", {}).get(column_name, "")
            source_name = extract_source_name(source_url)
            console.print(f"[dim]{source_name}: {len(res)} jobs[/]")

    return all_records


async def scrape_source(agent: Any, source_record: dict, column_name: str, prompt_generator) -> list:
    """Scrape jobs from a single source asynchronously with retry logic."""
    fields = source_record.get("fields", {})
    source_url = fields.get(column_name)

    if not source_url:
        console.print(f"Skipping source {source_record.get('id')} due to missing URL.")
        return []

    source_name = extract_source_name(source_url)
    console.print(f"[dim]Processing: {source_name} ({source_url})[/]")

    # Build task prompt
    task_prompt = prompt_generator(url=source_url, source_name=source_name)

    # Execute the task with retry logic
    max_retries = 1  # Retry once on failure
    for attempt in range(max_retries + 1):
        try:
            if attempt > 0:
                console.print(f"[yellow]Retrying scraping for {source_name} (attempt {attempt + 1}/{max_retries + 1})...[/]")
                
            # Execute the task
            result = await run_agent_with_task(agent, task_prompt)

            # Parse and collect records
            final_output = getattr(result, "final_output", None)
            records = _parse_records(final_output) if final_output else None
            
            if records:
                if attempt > 0:
                    console.print(f"[green]Retry successful for {source_name} - got {len(records)} records[/]")
                return records
            else:
                if attempt == 0:
                    console.print(f"No valid records from {source_name}.")
                else:
                    console.print(f"[red]Retry failed for {source_name} - still no valid records[/]")
                    
        except (asyncio.TimeoutError, ConnectionError, RuntimeError) as e:
            console.print(f"[red]Error during scraping attempt {attempt + 1} for {source_name}: {e}[/]")
            if attempt == max_retries:
                console.print(f"[red]All retry attempts failed for {source_name}[/]")
                return []
            continue
    
    return []


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
