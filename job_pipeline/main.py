from __future__ import annotations

import argparse
import asyncio

from rich.console import Console
from rich.panel import Panel

from .boards import supported_site_names
from .pipeline import run_pipeline


console = Console()
DEFAULT_MAX_JOBS_TOTAL = 3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="New dry-run job pipeline.")
    parser.add_argument(
        "--site",
        choices=["all", *supported_site_names()],
        default="all",
        help="Run one supported board or all supported boards.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print Airtable-ready records to the console without writing them.",
    )
    parser.add_argument(
        "--write-airtable",
        action="store_true",
        help="Write the full Airtable payload for the selected site or all sites. No delete behavior.",
    )
    parser.add_argument(
        "--write-airtable-indeed-url-test",
        action="store_true",
        help="Write only Source + Link for Indeed records to Airtable. Requires --site indeed.",
    )
    parser.add_argument(
        "--write-airtable-indeed-full",
        action="store_true",
        help="Write the full Airtable payload for Indeed records only. Requires --site indeed.",
    )
    parser.add_argument(
        "--max-jobs-per-board",
        type=int,
        default=None,
        help="Limit extraction and Airtable writes to the first N discovered jobs per board.",
    )
    parser.add_argument(
        "--max-jobs-total",
        type=int,
        default=DEFAULT_MAX_JOBS_TOTAL,
        help="Limit the whole run to the first N extracted jobs across all boards (default: 3).",
    )
    return parser.parse_args()


async def _run() -> None:
    args = parse_args()
    if (args.write_airtable_indeed_url_test or args.write_airtable_indeed_full) and args.site != "indeed":
        raise RuntimeError("The Indeed Airtable write modes require --site indeed.")
    if args.max_jobs_per_board is not None and args.max_jobs_per_board < 1:
        raise RuntimeError("--max-jobs-per-board must be at least 1.")
    if args.max_jobs_total is not None and args.max_jobs_total < 1:
        raise RuntimeError("--max-jobs-total must be at least 1.")

    dry_run = args.dry_run or not (
        args.write_airtable or args.write_airtable_indeed_url_test or args.write_airtable_indeed_full
    )
    await run_pipeline(
        site=args.site,
        dry_run=dry_run,
        write_airtable=args.write_airtable,
        write_airtable_indeed_url_test=args.write_airtable_indeed_url_test,
        write_airtable_indeed_full=args.write_airtable_indeed_full,
        max_jobs_per_board=args.max_jobs_per_board,
        max_jobs_total=args.max_jobs_total,
    )


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("Interrupted by user.")
    except Exception as exc:
        console.print(Panel(str(exc), title="Pipeline Error", style="red"))
        raise


if __name__ == "__main__":
    main()
