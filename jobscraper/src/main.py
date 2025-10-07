"""
OpenAI Agents SDK + Playwright MCP POC with optional Airtable syncing.
"""

import asyncio
import json

from rich.console import Console
from rich.panel import Panel

from config import DEFAULT_DELAY_BETWEEN_SOURCES

from environment_setup import validate_and_setup_environment
from server_manager import create_playwright_server
from agent_runner import create_playwright_agent, run_agent_with_task
from prompts import NARRATIVE_INSTRUCTIONS, generate_agent_instructions
from airtable_client import AirtableClient, AirtableConfig


HARDCODED_SOURCE_URL = (
    "https://bulldogjob.pl/companies/jobs/s/skills,Python/experienceLevel,intern,junior/order,published,desc"
)
USE_HARDCODED_SOURCE = False

console = Console()


async def main() -> None:
    """Main application entry point - clean orchestration of all components"""
    try:
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

            console.print(Panel(f"Fetched {len(sources)} sources from Airtable.", title="Sources", style="blue"))

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

        # For each source, scrape jobs
        async with create_playwright_server() as playwright_server:
            agent = create_playwright_agent(playwright_server)

            for source_record in sources:
                fields = source_record.get("fields", {})
                source_url = fields.get("Job Boards")

                if not source_url:
                    console.print(f"Skipping source {source_record.get('id')} due to missing URL.")
                    continue

                source_name = source_url

                # Build task prompt
                task_prompt = generate_agent_instructions(url=source_url, source_name=source_name)

                # Execute the task
                result = await run_agent_with_task(agent, task_prompt)

                # Parse and collect records
                final_output = getattr(result, "final_output", None)
                records = _parse_records(final_output) if final_output else None
                if records:
                    all_records.extend(records)
                else:
                    console.print(f"No valid records from {source_name}.")

                await asyncio.sleep(DEFAULT_DELAY_BETWEEN_SOURCES)

        # Sync all results to Airtable offers table
        if all_records:
            if client:
                client.create_records(all_records)
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
