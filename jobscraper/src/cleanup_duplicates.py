"""
Cleanup script to remove duplicate job records from Airtable.

Duplicates are identified by normalized URLs (stripped of query parameters).
Keeps the most recently created record for each unique job.

Usage:
    # Dry run (shows what would be deleted without actually deleting):
    python cleanup_duplicates.py --dry-run
    
    # Actually delete duplicates:
    python cleanup_duplicates.py
"""

import sys
from collections import defaultdict
from typing import Dict, List, Any

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table as RichTable

from airtable_client import AirtableClient, AirtableConfig, normalize_url


console = Console()


def find_duplicates(records: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Group records by normalized URL.
    
    Returns:
        Dict mapping normalized URLs to list of duplicate records
    """
    url_groups = defaultdict(list)
    
    for record in records:
        link = record.get("fields", {}).get("Link")
        if link:
            normalized = normalize_url(link)
            url_groups[normalized].append(record)
    
    # Filter to only duplicates (more than 1 record per URL)
    duplicates = {url: recs for url, recs in url_groups.items() if len(recs) > 1}
    
    return duplicates


def select_records_to_delete(duplicate_groups: Dict[str, List[Dict[str, Any]]]) -> List[str]:
    """Select which records to delete.
    
    Strategy: Keep the most recently created record, delete the rest.
    
    Returns:
        List of record IDs to delete
    """
    to_delete = []
    
    for url, records in duplicate_groups.items():
        # Sort by creation time (newest first)
        sorted_records = sorted(
            records,
            key=lambda r: r.get("createdTime", ""),
            reverse=True
        )
        
        # Keep the first (newest), delete the rest
        records_to_delete = sorted_records[1:]
        to_delete.extend([r["id"] for r in records_to_delete])
    
    return to_delete


def display_duplicates(duplicate_groups: Dict[str, List[Dict[str, Any]]]) -> None:
    """Display duplicate records in a formatted table."""
    
    for url, records in duplicate_groups.items():
        console.print(f"\n[bold yellow]Duplicate job found:[/] {url}")
        
        table = RichTable(show_header=True, header_style="bold cyan")
        table.add_column("ID", style="dim")
        table.add_column("Company", style="green")
        table.add_column("Position", style="blue")
        table.add_column("Created", style="yellow")
        table.add_column("Full Link", style="dim", overflow="fold")
        table.add_column("Action", style="bold")
        
        # Sort by creation time (newest first)
        sorted_records = sorted(
            records,
            key=lambda r: r.get("createdTime", ""),
            reverse=True
        )
        
        for i, record in enumerate(sorted_records):
            fields = record.get("fields", {})
            action = "[green]KEEP[/]" if i == 0 else "[red]DELETE[/]"
            
            table.add_row(
                record["id"][:8] + "...",
                fields.get("Company", "N/A"),
                fields.get("Position", "N/A"),
                record.get("createdTime", "N/A")[:10],
                fields.get("Link", "N/A"),
                action
            )
        
        console.print(table)


def main():
    """Main cleanup function."""
    dry_run = "--dry-run" in sys.argv
    
    console.print(Panel(
        "🧹 Airtable Duplicate Cleanup Script\n" +
        ("DRY RUN MODE - No records will be deleted" if dry_run else "⚠️  LIVE MODE - Records will be deleted"),
        style="bold blue" if dry_run else "bold red"
    ))
    
    # Load environment variables
    load_dotenv()
    
    # Load configuration
    config = AirtableConfig.from_env()
    if not config.is_configured():
        console.print(Panel(
            "Airtable configuration is incomplete. Check your .env file.",
            title="Error",
            style="red"
        ))
        return
    
    client = AirtableClient(config)
    
    # Fetch all records
    console.print("\n[dim]Fetching all job records from Airtable...[/]")
    all_records = client.get_all_records(config.offers_table_id)
    console.print(f"[dim]Found {len(all_records)} total records[/]")
    
    # Find duplicates
    console.print("\n[dim]Analyzing for duplicates...[/]")
    duplicate_groups = find_duplicates(all_records)
    
    if not duplicate_groups:
        console.print(Panel(
            "✨ No duplicates found! Your Airtable is clean.",
            title="Success",
            style="green"
        ))
        return
    
    # Count total duplicates
    total_duplicates = sum(len(recs) - 1 for recs in duplicate_groups.values())
    
    console.print(Panel(
        f"Found {len(duplicate_groups)} unique jobs with duplicates\n" +
        f"Total duplicate records to remove: {total_duplicates}",
        title="Analysis Complete",
        style="yellow"
    ))
    
    # Display duplicates
    display_duplicates(duplicate_groups)
    
    # Get records to delete
    to_delete = select_records_to_delete(duplicate_groups)
    
    if dry_run:
        console.print(Panel(
            f"[bold]DRY RUN:[/] Would delete {len(to_delete)} duplicate records.\n" +
            "Run without --dry-run to actually delete them.",
            title="Dry Run Complete",
            style="blue"
        ))
        return
    
    # Confirm deletion
    console.print(f"\n[bold red]⚠️  This will delete {len(to_delete)} records from Airtable![/]")
    response = console.input("[yellow]Type 'DELETE' to confirm: [/]")
    
    if response.strip() != "DELETE":
        console.print("[yellow]Cancelled. No records were deleted.[/]")
        return
    
    # Delete duplicates
    console.print("\n[dim]Deleting duplicate records...[/]")
    table = client._connect()
    
    # Delete in batches (Airtable API limit is 10 per batch)
    batch_size = 10
    deleted_count = 0
    
    for i in range(0, len(to_delete), batch_size):
        batch = to_delete[i:i + batch_size]
        table.batch_delete(batch)
        deleted_count += len(batch)
        console.print(f"[dim]Deleted {deleted_count}/{len(to_delete)} records...[/]")
    
    console.print(Panel(
        f"✅ Successfully deleted {deleted_count} duplicate records!",
        title="Cleanup Complete",
        style="green"
    ))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled by user.[/]")
    except (ImportError, RuntimeError, ValueError, AttributeError) as e:
        console.print(Panel(
            f"Error: {e}",
            title="Error",
            style="red"
        ))
        console.print_exception(show_locals=False)
        sys.exit(1)
