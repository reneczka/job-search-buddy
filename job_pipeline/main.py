from __future__ import annotations

import argparse
import asyncio

from rich.console import Console
from rich.panel import Panel

from .boards import supported_site_names
from .pipeline import run_pipeline


console = Console()


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
        help="Still disabled. Use --write-airtable-indeed-url-test for the narrow Indeed-only write check.",
    )
    parser.add_argument(
        "--write-airtable-indeed-url-test",
        action="store_true",
        help="Write only Source + Link for Indeed records to Airtable. Requires --site indeed.",
    )
    return parser.parse_args()


async def _run() -> None:
    args = parse_args()
    if args.write_airtable:
        raise RuntimeError(
            "Broad Airtable sync remains disabled. Use --write-airtable-indeed-url-test with --site indeed."
        )
    if args.write_airtable_indeed_url_test and args.site != "indeed":
        raise RuntimeError("The Indeed Airtable URL-only test requires --site indeed.")

    dry_run = args.dry_run or not args.write_airtable_indeed_url_test
    await run_pipeline(
        site=args.site,
        dry_run=dry_run,
        write_airtable=args.write_airtable,
        write_airtable_indeed_url_test=args.write_airtable_indeed_url_test,
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
