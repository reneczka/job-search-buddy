"""
Career Sites Scraper - Scrapes jobs from company career pages.
Uses the "Career Sites" column from the sources table in Airtable.
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
from prompts import generate_career_site_instructions
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
    """Extract a clean source name from a career site URL."""
    if not url:
        return "unknown"
    
    # Remove protocol and www
    url = url.lower().replace("https://", "").replace("http://", "").replace("www.", "")
    
    # Extract domain name (everything before the first / or ?)
    domain = url.split('/')[0].split('?')[0]
    
    # Handle special cases for subdomains if needed
    if domain.count('.') > 1:
        # For subdomains, take the last two parts (e.g., 'careers.example.com' -> 'example.com')
        parts = domain.split('.')
        domain = '.'.join(parts[-2:]) if len(parts) > 2 else domain
    
    # Return domain in lowercase
    return domain.lower()


console = Console()

async def main() -> None:
    """Main application entry point for career sites scraper"""
    try:
        program_start_time = time.time()
        console.print(f'\n[bold blue]Career Sites scraper started at {datetime.now().strftime("%H:%M:%S")}[/]')
        
        # Validate environment and setup
        validate_and_setup_environment()

        if USE_HARDCODED_SOURCE:
            # Using a default hardcoded source for testing
            sources = [
                {
                    "fields": {
                        "Career Sites": "https://example.com/careers",
                    }
                }
            ]
            client = None
            console.print(Panel(
                "Using hardcoded career site source.",
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
            sources = client.get_all_records(sources_table_id, sort_by="-Career Sites")
            sources_fetch_end = time.time()
            sources_fetch_time = sources_fetch_end - sources_fetch_start
            console.print(Panel(f"Fetched {len(sources)} career site sources from Airtable in {format_duration(sources_fetch_time)}.", title="Sources", style="blue"))

            for i, source_record in enumerate(sources, start=1):
                console.print(Panel(f"{i}. {json.dumps(source_record, indent=4)}", title="Source", style="blue"))

            if not sources:
                console.print(Panel(
                    "No career site sources found in Airtable.",
                    title="Warning",
                    style="yellow",
                ))
                return

        all_records = []

        # Scrape jobs from all sources concurrently
        scraping_start_time = time.time()
        console.print(f'\n[bold blue]Starting concurrent career site scraping from {len(sources)} sources...[/]')
        
        async with create_playwright_server() as playwright_server:
            agent = create_playwright_agent(playwright_server)

            # Create tasks for each source
            tasks = [scrape_source(agent, source_record) for source_record in sources]

            # Run all tasks concurrently and collect results
            results = await asyncio.gather(*tasks, return_exceptions=True)

            scraping_end_time = time.time()
            scraping_duration = scraping_end_time - scraping_start_time
            console.print(f'\n[bold green]Concurrent scraping completed in {format_duration(scraping_duration)}[/]')

            # Process results
            total_records = 0
            for i, res in enumerate(results):
                if isinstance(res, Exception):
                    # Get source name for error reporting
                    source_record = sources[i]
                    source_url = source_record.get("fields", {}).get("Career Sites", "")
                    source_name = extract_source_name(source_url)
                    console.print(Panel(
                        f"Error scraping {source_name}: {res}",
                        title="Scrape Error",
                        style="red",
                    ))
                else:
                    record_count = len(res)
                    total_records += record_count
                    all_records.extend(res)
                    # Get source name from URL
                    source_record = sources[i]
                    source_url = source_record.get("fields", {}).get("Career Sites", "")
                    source_name = extract_source_name(source_url)
                    console.print(f"[dim]{source_name}: {record_count} jobs found[/]")
            
            console.print(f'\n[bold green]Total jobs scraped: {total_records} from {len(sources)} sources[/]')

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
        console.print(f'\n[bold green]🎉 Career Sites scraper completed successfully in {format_duration(total_program_time)}[/]')
        console.print(f'[dim]Breakdown:[/]')
        if not USE_HARDCODED_SOURCE:
            console.print(f'  • Sources fetching: {format_duration(sources_fetch_time)}')
        console.print(f'  • Job scraping: {format_duration(scraping_duration)}')
        if all_records and client:
            console.print(f'  • Airtable sync: {format_duration(airtable_sync_time)}')
        other_time = total_program_time - scraping_duration - (sources_fetch_time if not USE_HARDCODED_SOURCE else 0) - (airtable_sync_time if all_records and client else 0)
        console.print(f'  • Other operations: {format_duration(other_time)}')

    except (ImportError, RuntimeError, ValueError) as e:
        console.print(Panel(
            f"Application error ({type(e).__name__}): {e}",
            title="Error",
            style="red",
        ))
        console.print_exception(show_locals=False)
        raise
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


async def scrape_source(agent: Any, source_record: dict) -> list:
    """Scrape jobs from a single career site source asynchronously with retry logic."""
    fields = source_record.get("fields", {})
    source_url = fields.get("Career Sites")

    if not source_url:
        console.print(f"Skipping source {source_record.get('id')} due to missing URL.")
        return []

    source_name = extract_source_name(source_url)
    console.print(f"[dim]Processing career site: {source_name} ({source_url})[/]")

    # Build task prompt
    task_prompt = generate_career_site_instructions(url=source_url, source_name=source_name)

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
