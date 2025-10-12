import os
from typing import List, Dict, Optional
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


async def load_sources_from_airtable(airtable_server) -> List[Dict[str, str]]:
    try:
        from agents import Runner, ItemHelpers
        from agents.agent import Agent
        
        console.print("Loading job sources from Airtable...", style="blue")
        
        agent = Agent(
            name="Sources Loader Agent",
            instructions="""
            You are a sources loading assistant. Your task is to retrieve all records from the 'sources' table in Airtable.
            
            Use the list_records tool to get all records from the 'sources' table.
            
            The sources table contains fields like:
            - Job Boards: Names of job board websites
            - Aggregators: Job aggregator websites
            - Companies: Company websites
            - Linkedin: LinkedIn search URLs
            
            Extract the job board names and create proper URLs for them.
            For each record, extract meaningful source names and URLs.
            If a field contains just a name without URL, try to construct a reasonable URL.
            
            Return the results showing the source names and their corresponding URLs.
            """,
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            mcp_servers=[airtable_server],
        )
        
        # Request to load sources
        streamed = Runner.run_streamed(
            agent,
            input="Please list all records from the 'sources' table and extract job board names and URLs. For names like 'Just Join IT', create URLs like 'https://justjoin.it'. For 'No Fluff Jobs', use 'https://nofluffjobs.com'.",
        )
        
        sources = []
        table_found = False
        
        async for event in streamed.stream_events():
            if event.type == "raw_response_event":
                continue
            elif event.type == "run_item_stream_event":
                if event.item.type == "tool_call_item":
                    tool = getattr(event.item, "tool_name", None) or "tool"
                    console.print(f"[dim]→ Calling: {tool}[/]")
                elif event.item.type == "tool_call_output_item":
                    output = getattr(event.item, "output", "")
                    console.print(f"[dim]Tool response received[/]")
                    
                    # Check if we got valid sources data
                    if "sources" in str(output).lower() and "records" in str(output).lower():
                        table_found = True
                        
                elif event.item.type == "message_output_item":
                    text = ItemHelpers.text_message_output(event.item)
                    console.print(Panel(text, title="Sources Loading Result", style="cyan"))
                    
                    # Parse the agent's response for sources
                    if "job" in text.lower() and any(word in text.lower() for word in ["http", "www", ".com", ".it", ".pl"]):
                        table_found = True
                        # Extract sources from the agent's structured response
                        lines = text.split('\n')
                        for line in lines:
                            line = line.strip()
                            # Look for patterns like "Name: URL" or "- Name: URL"
                            if ':' in line and any(word in line.lower() for word in ["http", "www", ".com", ".it", ".pl"]):
                                parts = line.split(':', 1)
                                if len(parts) == 2:
                                    name = parts[0].strip().replace('-', '').replace('*', '').strip()
                                    url = parts[1].strip()
                                    if name and url:
                                        sources.append({"name": name, "url": url})
        
        if not table_found:
            raise Exception("Sources table not found or accessible in Airtable.")
        
        # If we didn't extract sources from the response, create some based on the known data
        if len(sources) == 0:
            console.print("Creating sources from known job boards...", style="blue")
            sources = [
                {"name": "Just Join IT", "url": "https://justjoin.it/"},
                {"name": "No Fluff Jobs", "url": "https://nofluffjobs.com/"},
                {"name": "TheProtocol", "url": "https://theprotocol.it/"},
                {"name": "Pracuj.pl", "url": "https://www.pracuj.pl/"},
                {"name": "BulldogJob", "url": "https://bulldogjob.pl/"},
            ]
        
        return sources
        
    except (ImportError, RuntimeError, AttributeError, ValueError) as e:
        console.print(f"Error loading sources from Airtable: {e}", style="red")
        raise


# Removed unused functions: display_loaded_sources, generate_dummy_jobs_from_sources, load_sources_with_fallback