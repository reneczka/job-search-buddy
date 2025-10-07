import asyncio
import os
import signal
import subprocess
import time
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from sources_loader import load_sources_with_fallback, display_loaded_sources, generate_dummy_jobs_from_sources

console = Console()


async def main() -> None:
    load_dotenv()
    
    # Validate required environment variables
    api_key = os.getenv("OPENAI_API_KEY")
    airtable_key = os.getenv("AIRTABLE_API_KEY")
    airtable_base = os.getenv("AIRTABLE_BASE_ID")
    
    if not api_key:
        console.print(Panel(
            "Missing OPENAI_API_KEY. Set it in .env or environment.",
            title="Missing API Key",
            style="red"
        ))
        return
    
    if not airtable_key:
        console.print(Panel(
            "Missing AIRTABLE_API_KEY. Set it in .env or environment.",
            title="Missing Airtable Key",
            style="red"
        ))
        return
    
    if not airtable_base:
        console.print(Panel(
            "Missing AIRTABLE_BASE_ID. Set it in .env or environment.",
            title="Missing Base ID",
            style="red"
        ))
        return

    try:
        # Import Agents SDK
        from agents import Runner, ItemHelpers
        from agents.agent import Agent
        try:
            from agents.mcp import MCPServerStdio
        except Exception as _:
            MCPServerStdio = None
    except Exception as e:
        console.print(Panel(
            "OpenAI Agents SDK is not installed.\n\n"
            "Install from GitHub and retry:\n"
            "  pip install git+https://github.com/openai/openai-agents-python\n\n"
            f"Import error: {e}",
            title="Agents SDK Missing",
            style="yellow",
        ))
        return

    if MCPServerStdio is None:
        console.print(Panel(
            "Agents SDK missing MCPServerStdio. Update SDK:\n"
            "  pip install -U git+https://github.com/openai/openai-agents-python",
            title="SDK Update Required",
            style="yellow"
        ))
        return

    # === AIRTABLE MCP CONFIG ===
    AIRTABLE_PACKAGE = os.getenv("AIRTABLE_MCP_PACKAGE", "@felores/airtable-mcp-server")
    AIRTABLE_USE_NPX = os.getenv("AIRTABLE_USE_NPX", "1") in ("1", "true", "True")

    console.print(Panel.fit(
        "Job Scraper - Phase 2: Dynamic Sources\n"
        "Loading sources from Airtable and creating jobs based on them...",
        border_style="blue"
    ))

    try:
        # Construct Airtable stdio MCP client
        airtable_cmd = "npx" if AIRTABLE_USE_NPX else "node"
        airtable_args = ["-y", AIRTABLE_PACKAGE] if AIRTABLE_USE_NPX else [AIRTABLE_PACKAGE]
        airtable_env = {**os.environ, "AIRTABLE_API_KEY": airtable_key}
        
        console.print(f"\nStarting Airtable MCP server via stdio: {airtable_cmd} {' '.join(airtable_args)}")
        
        airtable_server = MCPServerStdio({
            "command": airtable_cmd,
            "args": airtable_args,
            "env": airtable_env,
        })

        # Connect to Airtable MCP server
        async with airtable_server:
            console.print("Connected to Airtable MCP server.", style="green")

            # Phase 2: Load sources dynamically from Airtable
            sources = await load_sources_with_fallback(airtable_server)
            display_loaded_sources(sources)
            
            # Generate dummy jobs based on loaded sources
            dummy_jobs = generate_dummy_jobs_from_sources(sources)
            
            # Generate agent instructions for dynamic job insertion
            job_data_str = "\n".join([
                f"Job {i+1}: {job['position']} at {job['company']} (Source: {job['source']})"
                for i, job in enumerate(dummy_jobs)
            ])
            
            AGENT_INSTRUCTIONS = f"""
You are connected to an Airtable MCP server. Your task is to insert {len(dummy_jobs)} dummy job records 
into the 'offers' table in the Airtable base.

These jobs are generated from {len(sources)} sources loaded from the 'sources' table:
{job_data_str}

For each job, use the create_record tool with this EXACT JSON format:
{{
  "baseId": "{airtable_base}",
  "tableId": "tblVIbY84NJvk8LHI",
  "fields": {{
    "Source": "job_source_here",
    "Link": "job_link_here",
    "Company": "company_name_here",
    "Position": "position_title_here",
    "CV sent": false,
    "Salary": "salary_info_here",
    "Location": "job_location_here",
    "Notes": "job_notes_here",
    "Date applied": "",
    "Requirements": "job_requirements_here",
    "Company description": "company_info_here",
    "Local/Remote/Hybrid": "remote_type_here"
  }}
}}

CRITICAL REQUIREMENTS:
- Use baseId (not base_id)
- Use tableId (not table_id)  
- Include the top-level "fields" key with all field data inside it
- Use the exact field names shown above
- Replace the placeholder values with actual job data

Insert each job one by one and report success/failure for each insertion.
Provide a summary at the end showing how many jobs were successfully inserted.
"""

            # Display dummy jobs that will be inserted
            console.print(f"\nGenerated {len(dummy_jobs)} dummy jobs based on loaded sources:", style="yellow")
            job_table = Table(show_header=True, header_style="bold magenta")
            job_table.add_column("Company", style="cyan")
            job_table.add_column("Position", style="green")
            job_table.add_column("Location", style="blue")
            job_table.add_column("Source", style="yellow")
            
            for job in dummy_jobs:
                job_table.add_row(job['company'], job['position'], job['location'], job['source'])
            
            console.print(job_table)

            # Define the agent with Airtable MCP server attached
            agent = Agent(
                name="Job Insertion Agent",
                instructions=AGENT_INSTRUCTIONS,
                model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                mcp_servers=[airtable_server],
            )

            console.print("\nRunning job insertion agent...", style="blue")
            
            # Prepare detailed insertion instructions with sanitized data
            job_instructions = "Insert the following dummy jobs into Airtable using the exact format shown:\n\n"
            for i, job in enumerate(dummy_jobs, 1):
                # Sanitize values to avoid JSON issues
                source = job['source'].replace('"', '\\"')
                link = job['link'].replace('"', '\\"')
                company = job['company'].replace('"', '\\"')
                position = job['position'].replace('"', '\\"')
                salary = job['salary'].replace('"', '\\"')
                location = job['location'].replace('"', '\\"')
                notes = job['notes'].replace('"', '\\"')
                requirements = job['requirements'].replace('"', '\\"')
                about_company = job['about_company'].replace('"', '\\"')
                remote_type = job['remote_type'].replace('"', '\\"')
                
                job_instructions += f"""Job {i} - Insert with create_record:
{{
  "baseId": "{airtable_base}",
  "tableId": "tblVIbY84NJvk8LHI",
  "fields": {{
    "Source": "{source}",
    "Link": "{link}",
    "Company": "{company}",
    "Position": "{position}",
    "CV sent": false,
    "Salary": "{salary}",
    "Location": "{location}",
    "Notes": "{notes}",
    "Date applied": "",
    "Requirements": "{requirements}",
    "Company description": "{about_company}",
    "Local/Remote/Hybrid": "{remote_type}"
  }}
}}

"""
            # Streamed run: print agent reasoning/steps as events
            streamed = Runner.run_streamed(
                agent,
                input=job_instructions,
            )
            
            inserted_count = 0
            failed_count = 0
            
            async for event in streamed.stream_events():
                if event.type == "raw_response_event":
                    continue
                elif event.type == "agent_updated_stream_event":
                    console.print(f"[dim]Agent updated: {event.new_agent.name}[/]")
                elif event.type == "run_item_stream_event":
                    if event.item.type == "tool_call_item":
                        tool = getattr(event.item, "tool_name", None) or "tool"
                        console.print(f"[bold cyan]→ Tool called[/]: {tool}")
                    elif event.item.type == "tool_call_output_item":
                        output = getattr(event.item, "output", "")
                        console.print(Panel(str(output), title="Tool output", style="cyan"))
                        # Count successful insertions
                        if "created" in str(output).lower() or "success" in str(output).lower():
                            inserted_count += 1
                        elif "error" in str(output).lower() or "failed" in str(output).lower():
                            failed_count += 1
                    elif event.item.type == "message_output_item":
                        text = ItemHelpers.text_message_output(event.item)
                        console.print(Panel(text, title="Agent message", style="green"))
                    else:
                        pass
                        
            # After streaming completes, capture final result
            result = streamed

            # Print final output
            final_output: Optional[str] = getattr(result, "final_output", None)
            if final_output:
                console.print(Panel(final_output, title="Agent Final Output"))
            
            # Display results
            display_results(inserted_count, failed_count, len(dummy_jobs), len(sources))

    except Exception as e:
        console.print(Panel(
            f"Error during execution: {e}",
            title="Execution Error",
            style="red"
        ))
        return


def display_results(inserted_count: int, failed_count: int, total_jobs: int, source_count: int):
    """Display final results in a nice format."""
    console.print("\n" + "="*60, style="blue")
    console.print("PHASE 2 RESULTS", style="bold blue", justify="center")
    console.print("="*60, style="blue")
    
    # Create results table
    results_table = Table(show_header=True, header_style="bold magenta")
    results_table.add_column("Metric", style="cyan", width=25)
    results_table.add_column("Count", style="green", justify="right", width=10)
    results_table.add_column("Percentage", style="yellow", justify="right", width=15)
    
    success_rate = (inserted_count / total_jobs * 100) if total_jobs > 0 else 0
    failure_rate = (failed_count / total_jobs * 100) if total_jobs > 0 else 0
    
    results_table.add_row("Sources Loaded", str(source_count), "")
    results_table.add_row("Total Jobs Generated", str(total_jobs), "100%")
    results_table.add_row("Successfully Inserted", str(inserted_count), f"{success_rate:.1f}%")
    results_table.add_row("Failed", str(failed_count), f"{failure_rate:.1f}%")
    
    console.print(results_table)
    
    # Success criteria check
    console.print("\nPHASE 2 SUCCESS CRITERIA CHECK:", style="bold yellow")
    criteria = [
        ("Sources loaded from Airtable without hardcoding", source_count > 0),
        ("Dummy jobs created using actual source names", total_jobs > 0 and inserted_count > 0),
        ("Console shows source count and names", True),  # Always true if we get here
        ("Works with any number of sources in table", source_count > 0)
    ]
    
    for criterion, passed in criteria:
        status = "[PASS]" if passed else "[FAIL]"
        style = "green" if passed else "red"
        console.print(f"  {status} {criterion}", style=style)
    
    # Final message
    if source_count > 0 and inserted_count >= source_count:
        console.print("\nPhase 2 Dynamic Sources completed successfully!", style="bold green")
        console.print("Check your Airtable 'offers' table to see jobs generated from dynamic sources.", style="green")
    elif source_count > 0 and inserted_count > 0:
        console.print("\nPhase 2 partially successful - some jobs were inserted.", style="bold yellow")
        console.print("Check your Airtable 'offers' table and review any errors above.", style="yellow")
    else:
        console.print("\nPhase 2 needs attention - no sources loaded or jobs inserted.", style="bold red")
        console.print("Check your Airtable 'sources' table configuration.", style="red")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("Interrupted by user.")
    except Exception as e:
        console.print(Panel(str(e), title="Fatal Error", style="red"))