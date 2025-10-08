"""
OpenAI Agents SDK + Playwright MCP POC with optional Airtable syncing.
"""

import asyncio
import json
from typing import Any

from rich.console import Console
from rich.panel import Panel

from config import DEFAULT_DELAY_BETWEEN_SOURCES, USE_HARDCODED_SOURCE

from environment_setup import validate_and_setup_environment
from server_manager import create_playwright_server
from agent_runner import create_playwright_agent, run_agent_with_task
from prompts import generate_agent_instructions
from airtable_client import AirtableClient, AirtableConfig
import time
from datetime import datetime


HARDCODED_SOURCE_URL = (
    "https://bulldogjob.pl/companies/jobs/s/skills,Python/experienceLevel,intern,junior/order,published,desc"
)

console = Console()


async def main() -> None:
    """Main application entry point - clean orchestration of all components"""
    try:
        program_start_time = time.time()
        console.print(f'\n[bold blue]Job scraping program started at {datetime.now().strftime("%H:%M:%S")}[/]')
        
        # Validate environment and setup
        validate_and_setup_environment()

        if USE_HARDCODED_SOURCE:
            sources = [
                {
                    "fields": {
                        "Job Boards": HARDCODED_SOURCE_URL,
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
            console.print(Panel(f"Fetched {len(sources)} sources from Airtable in {sources_fetch_time:.2f} seconds.", title="Sources", style="blue"))

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

        # Scrape jobs from all sources concurrently
        scraping_start_time = time.time()
        console.print(f'\n[bold blue]Starting concurrent job scraping from {len(sources)} sources...[/]')
        
        async with create_playwright_server() as playwright_server:
            agent = create_playwright_agent(playwright_server)

            # Create tasks for each source
            tasks = [scrape_source(agent, source_record) for source_record in sources]

            # Run all tasks concurrently and collect results
            results = await asyncio.gather(*tasks, return_exceptions=True)

            scraping_end_time = time.time()
            scraping_duration = scraping_end_time - scraping_start_time
            console.print(f'\n[bold green]Concurrent scraping completed in {scraping_duration:.2f} seconds[/]')

            # Process results
            total_records = 0
            for i, res in enumerate(results):
                if isinstance(res, Exception):
                    console.print(Panel(
                        f"Error scraping source {i+1}: {res}",
                        title="Scrape Error",
                        style="red",
                    ))
                else:
                    record_count = len(res)
                    total_records += record_count
                    all_records.extend(res)
                    console.print(f"[dim]Source {i+1}: {record_count} jobs found[/]")
            
            console.print(f'\n[bold green]Total jobs scraped: {total_records} from {len(sources)} sources[/]')

        # Sync all results to Airtable offers table
        if all_records:
            airtable_sync_start = time.time()
            if client:
                client.create_records(all_records)
                airtable_sync_end = time.time()
                airtable_sync_time = airtable_sync_end - airtable_sync_start
                console.print(f'\n[bold green]Airtable sync completed in {airtable_sync_time:.2f} seconds - created {len(all_records)} records[/]')
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
        console.print(f'\n[bold green]🎉 Program completed successfully in {total_program_time:.2f} seconds[/]')
        console.print(f'[dim]Breakdown:[/]')
        if not USE_HARDCODED_SOURCE:
            console.print(f'  • Sources fetching: {sources_fetch_time:.2f}s')
        console.print(f'  • Job scraping: {scraping_duration:.2f}s')
        if all_records and client:
            console.print(f'  • Airtable sync: {airtable_sync_time:.2f}s')
        other_time = total_program_time - scraping_duration - (sources_fetch_time if not USE_HARDCODED_SOURCE else 0) - (airtable_sync_time if all_records and client else 0)
        console.print(f'  • Other operations: {other_time:.2f}s')

    except Exception as e:
        console.print(Panel(
            f"Application error ({type(e).__name__}): {e}",
            title="Error",
            style="red",
        ))
        console.print_exception(show_locals=False)
        raise


def _parse_records(output_text: str):
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
    """Scrape jobs from a single source asynchronously with retry logic."""
    fields = source_record.get("fields", {})
    source_url = fields.get("Job Boards")

    if not source_url:
        console.print(f"Skipping source {source_record.get('id')} due to missing URL.")
        return []

    source_name = source_url

    # Build task prompt
    task_prompt = generate_agent_instructions(url=source_url, source_name=source_name)

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
                    
        except Exception as e:
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
    except Exception as e:
        console.print(Panel(
            f"Fatal error ({type(e).__name__}): {e}",
            title="Fatal Error",
            style="red",
        ))
        console.print_exception(show_locals=False)
