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
        
    except Exception as e:
        console.print(f"Error loading sources from Airtable: {e}", style="red")
        raise


def display_loaded_sources(sources: List[Dict[str, str]]) -> None:
    """Display loaded sources in a formatted table."""
    console.print(f"\nLoaded {len(sources)} job sources:", style="green")
    
    sources_table = Table(show_header=True, header_style="bold magenta")
    sources_table.add_column("Source Name", style="cyan", width=20)
    sources_table.add_column("URL", style="blue", width=50)
    
    for source in sources:
        name = source.get('name', 'Unknown')
        url = source.get('url', 'No URL')
        sources_table.add_row(name, url)
    
    console.print(sources_table)


def generate_dummy_jobs_from_sources(sources: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Generate dummy job data based on loaded sources.
    Creates one job per source for testing.
    """
    from datetime import datetime
    
    dummy_jobs = []
    
    job_templates = [
        {
            "position": "Senior Python Developer",
            "company": "Tech Innovations Inc",
            "salary": "$85,000 - $105,000",
            "location": "Warsaw, Poland",
            "requirements": "5+ years Python, Django, REST APIs, PostgreSQL",
            "about_company": "Leading tech company specializing in fintech solutions",
            "remote_type": "Remote"
        },
        {
            "position": "Frontend React Developer", 
            "company": "Digital Solutions Ltd",
            "salary": "€60,000 - €75,000",
            "location": "Krakow, Poland",
            "requirements": "3+ years React, TypeScript, CSS3, Git",
            "about_company": "Fast-growing startup in e-commerce space",
            "remote_type": "Hybrid"
        },
        {
            "position": "Full Stack JavaScript Developer",
            "company": "CodeCraft Studios", 
            "salary": "12,000 - 15,000 PLN",
            "location": "Gdansk, Poland",
            "requirements": "Node.js, Express, React, MongoDB, AWS",
            "about_company": "Creative software development studio",
            "remote_type": "Remote"
        },
        {
            "position": "DevOps Engineer",
            "company": "CloudTech Solutions",
            "salary": "€70,000 - €90,000", 
            "location": "Wroclaw, Poland",
            "requirements": "Docker, Kubernetes, AWS, Terraform, CI/CD",
            "about_company": "Cloud infrastructure consulting firm",
            "remote_type": "Remote"
        },
        {
            "position": "QA Automation Engineer",
            "company": "TestPro Systems",
            "salary": "10,000 - 13,000 PLN",
            "location": "Poznan, Poland", 
            "requirements": "Selenium, Python, API testing, Agile",
            "about_company": "Quality assurance and testing specialists",
            "remote_type": "Hybrid"
        }
    ]
    
    for i, source in enumerate(sources):
        template = job_templates[i % len(job_templates)]
        
        # Create job with source information
        job = {
            "source": source.get('name', f'Source {i+1}'),
            "link": f"{source.get('url', 'https://example.com')}/job-{i+1}",
            "company": template["company"],
            "position": template["position"],
            "salary": template["salary"],
            "location": template["location"],
            "notes": f"Dummy job from {source.get('name', 'unknown source')} created on {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "requirements": template["requirements"],
            "about_company": template["about_company"],
            "remote_type": template["remote_type"]
        }
        
        dummy_jobs.append(job)
    
    return dummy_jobs


async def load_sources_with_fallback(airtable_server) -> List[Dict[str, str]]:
    """
    Load sources from Airtable with fallback to example sources.
    """
    try:
        sources = await load_sources_from_airtable(airtable_server)
        if sources:
            return sources
    except Exception as e:
        console.print(f"Failed to load sources from Airtable: {e}", style="yellow")
    
    console.print("Using fallback example sources", style="blue")
    return [
        {"name": "JustJoin.it", "url": "https://justjoin.it/"},
        {"name": "NoFluffJobs", "url": "https://nofluffjobs.com/"},
        {"name": "TheProtocol", "url": "https://theprotocol.it/"},
    ]