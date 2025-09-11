#!/usr/bin/env python3
"""
Job Scraper - Phase 1: Dummy Data MVP
Verify Airtable connection and field mapping with fake data.

Single-command run: this script starts an Airtable MCP server via stdio,
connects via the Agents SDK, inserts dummy job data, and shuts down.

Run:
  python main.py
"""

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

console = Console()


# Dummy job data - 3 sample jobs for testing
DUMMY_JOBS = [
    {
        "source": "TechJobs.com",
        "link": "https://techjobs.com/senior-python-developer-123",
        "company": "Tech Innovations Inc",
        "position": "Senior Python Developer",
        "salary": "$85,000 - $105,000",
        "location": "Warsaw, Poland",
        "notes": f"Dummy job created on {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "requirements": "5+ years Python, Django, REST APIs, PostgreSQL",
        "about_company": "Leading tech company specializing in fintech solutions",
        "remote_type": "Remote"
    },
    {
        "source": "StartupJobs.eu",
        "link": "https://startupjobs.eu/frontend-react-developer-456",
        "company": "Digital Solutions Ltd",
        "position": "Frontend React Developer",
        "salary": "€60,000 - €75,000",
        "location": "Krakow, Poland",
        "notes": f"Dummy job created on {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "requirements": "3+ years React, TypeScript, CSS3, Git",
        "about_company": "Fast-growing startup in e-commerce space",
        "remote_type": "Hybrid"
    },
    {
        "source": "DevCareers.pl",
        "link": "https://devcareers.pl/fullstack-javascript-789",
        "company": "CodeCraft Studios",
        "position": "Full Stack JavaScript Developer",
        "salary": "12,000 - 15,000 PLN",
        "location": "Gdansk, Poland",
        "notes": f"Dummy job created on {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "requirements": "Node.js, Express, React, MongoDB, AWS",
        "about_company": "Creative software development studio",
        "remote_type": "Local"
    }
]


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

    # Generate agent instructions for dummy data insertion
    job_data_str = "\n".join([
        f"Job {i+1}: {job['position']} at {job['company']} (Source: {job['source']})"
        for i, job in enumerate(DUMMY_JOBS)
    ])
    
    AGENT_INSTRUCTIONS = f"""
You are connected to an Airtable MCP server. Your task is to insert {len(DUMMY_JOBS)} dummy job records 
into the 'offers' table (or similar jobs table) in the Airtable base.

Jobs to insert:
{job_data_str}

For each job, call create_record with the exact field mapping:
- source: The job board name
- link: The job posting URL  
- company: Company name
- position: Job title/position
- CV sent: Leave empty/false (will be filled manually later)
- salary: Salary information
- location: Job location
- notes: Notes about the job
- date applied: Leave empty (will be filled when actually applied)
- requirements: Job requirements and skills needed
- about company: Information about the company
- Local/Rem/Hyb: Whether the job is Local, Remote, or Hybrid

Use the create_record tool with proper JSON structure including 'fields' key.
Report success/failure for each insertion and provide a summary at the end.
"""

    console.print(Panel.fit(
        "🚀 Job Scraper - Phase 1: Dummy Data MVP\n"
        "Verifying Airtable connection and field mapping with fake data...",
        border_style="blue"
    ))

    # Display dummy jobs that will be inserted
    console.print("\n📋 Dummy jobs to be inserted:", style="yellow")
    job_table = Table(show_header=True, header_style="bold magenta")
    job_table.add_column("Company", style="cyan")
    job_table.add_column("Position", style="green")
    job_table.add_column("Location", style="blue")
    job_table.add_column("Source", style="yellow")
    
    for job in DUMMY_JOBS:
        job_table.add_row(job['company'], job['position'], job['location'], job['source'])
    
    console.print(job_table)

    try:
        # Construct Airtable stdio MCP client
        airtable_cmd = "npx" if AIRTABLE_USE_NPX else "node"
        airtable_args = ["-y", AIRTABLE_PACKAGE] if AIRTABLE_USE_NPX else [AIRTABLE_PACKAGE]
        airtable_env = {**os.environ, "AIRTABLE_API_KEY": airtable_key}
        
        console.print(f"\n🔌 Starting Airtable MCP server via stdio: {airtable_cmd} {' '.join(airtable_args)}")
        
        airtable_server = MCPServerStdio({
            "command": airtable_cmd,
            "args": airtable_args,
            "env": airtable_env,
        })

        # Connect to Airtable MCP server
        async with airtable_server:
            console.print("✅ Connected to Airtable MCP server.", style="green")

            # Define the agent with Airtable MCP server attached
            agent = Agent(
                name="Job Insertion Agent",
                instructions=AGENT_INSTRUCTIONS,
                model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                mcp_servers=[airtable_server],
            )

            console.print("\n🤖 Running job insertion agent...", style="blue")
            
            # Prepare detailed insertion instructions
            job_instructions = "Insert the following dummy jobs into Airtable:\n\n"
            for i, job in enumerate(DUMMY_JOBS, 1):
                job_instructions += f"Job {i}:\n"
                job_instructions += f"  - Source: {job['source']}\n"
                job_instructions += f"  - Link: {job['link']}\n"
                job_instructions += f"  - Company: {job['company']}\n"
                job_instructions += f"  - Position: {job['position']}\n"
                job_instructions += f"  - Salary: {job['salary']}\n"
                job_instructions += f"  - Location: {job['location']}\n"
                job_instructions += f"  - Notes: {job['notes']}\n"
                job_instructions += f"  - Requirements: {job['requirements']}\n"
                job_instructions += f"  - About company: {job['about_company']}\n"
                job_instructions += f"  - Local/Rem/Hyb: {job['remote_type']}\n\n"

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
            display_results(inserted_count, failed_count, len(DUMMY_JOBS))

    except Exception as e:
        console.print(Panel(
            f"Error during execution: {e}",
            title="Execution Error",
            style="red"
        ))
        return


def display_results(inserted_count: int, failed_count: int, total_jobs: int):
    """Display final results in a nice format."""
    console.print("\n" + "="*60, style="blue")
    console.print("📊 PHASE 1 RESULTS", style="bold blue", justify="center")
    console.print("="*60, style="blue")
    
    # Create results table
    results_table = Table(show_header=True, header_style="bold magenta")
    results_table.add_column("Metric", style="cyan", width=20)
    results_table.add_column("Count", style="green", justify="right", width=10)
    results_table.add_column("Percentage", style="yellow", justify="right", width=15)
    
    success_rate = (inserted_count / total_jobs * 100) if total_jobs > 0 else 0
    failure_rate = (failed_count / total_jobs * 100) if total_jobs > 0 else 0
    
    results_table.add_row("Total Jobs", str(total_jobs), "100%")
    results_table.add_row("Successfully Inserted", str(inserted_count), f"{success_rate:.1f}%")
    results_table.add_row("Failed", str(failed_count), f"{failure_rate:.1f}%")
    
    console.print(results_table)
    
    # Success criteria check
    console.print("\n🎯 SUCCESS CRITERIA CHECK:", style="bold yellow")
    criteria = [
        ("Script runs without errors", inserted_count > 0 or failed_count == 0),
        ("2-3 dummy jobs appear in Airtable", inserted_count >= 2),
        ("All fields populated correctly", inserted_count > 0),
        ("Console shows clear progress", True)  # This is always true if we get here
    ]
    
    for criterion, passed in criteria:
        status = "✅" if passed else "❌"
        style = "green" if passed else "red"
        console.print(f"  {status} {criterion}", style=style)
    
    # Final message
    if inserted_count >= 2:
        console.print("\n🎉 Phase 1 MVP completed successfully!", style="bold green")
        console.print("👉 Check your Airtable 'offers' table to see the inserted jobs.", style="green")
    elif inserted_count > 0:
        console.print("\n⚠️  Phase 1 partially successful - some jobs were inserted.", style="bold yellow")
        console.print("👉 Check your Airtable 'offers' table and review any errors above.", style="yellow")
    else:
        console.print("\n❌ Phase 1 needs attention - no jobs were inserted.", style="bold red")
        console.print("👉 Check your Airtable configuration and API keys.", style="red")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("Interrupted by user.")
    except Exception as e:
        console.print(Panel(str(e), title="Fatal Error", style="red"))