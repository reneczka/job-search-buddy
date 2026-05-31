from __future__ import annotations

import argparse
import asyncio

from rich.console import Console
from rich.panel import Panel

from .boards import supported_site_names
from .apply_workflow import (
    DEFAULT_APPLY_BATCH_SIZE,
    DEFAULT_APPLY_SESSION_STATE_PATH,
    DEFAULT_APPLY_THRESHOLD,
    approve_jobs,
    apply_to_jobs,
    build_airtable_client,
    cleanup_apply_environment,
    inspect_apply_environment,
    inspect_jobs,
    load_candidate_application_profile,
    print_apply_cleanup_summary,
    print_apply_preflight_summary,
    shortlist_jobs,
)


console = Console()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Assist with applications for top-scored jobs.")
    parser.add_argument("--mode", choices=["preflight", "cleanup", "inspect", "shortlist", "approve", "apply"], required=True)
    parser.add_argument("--threshold", type=int, default=DEFAULT_APPLY_THRESHOLD)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_APPLY_BATCH_SIZE)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--source", choices=supported_site_names(), default=None)
    parser.add_argument("--record-ids", nargs="*", default=[])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--candidate-profile", default="candidate_profile.json")
    parser.add_argument("--cv-file", default=None)
    parser.add_argument("--batch-tag", default=None)
    parser.add_argument("--session-state", default=DEFAULT_APPLY_SESSION_STATE_PATH)
    return parser.parse_args()


async def _run() -> None:
    args = parse_args()
    if args.mode == "preflight":
        print_apply_preflight_summary(inspect_apply_environment())
        return
    if args.mode == "cleanup":
        print_apply_cleanup_summary(cleanup_apply_environment())
        return

    if args.threshold < 1:
        raise RuntimeError("--threshold must be at least 1.")
    if args.batch_size < 1:
        raise RuntimeError("--batch-size must be at least 1.")
    if args.limit < 1:
        raise RuntimeError("--limit must be at least 1.")

    client = build_airtable_client()

    if args.mode == "inspect":
        inspect_jobs(client, source=args.source, limit=args.limit)
        return

    if args.mode == "shortlist":
        shortlist_jobs(
            client,
            threshold=args.threshold,
            record_ids=args.record_ids,
            source=args.source,
            dry_run=args.dry_run,
        )
        return

    if args.mode == "approve":
        approve_jobs(
            client,
            record_ids=args.record_ids,
            dry_run=args.dry_run,
        )
        return

    profile = None
    if not args.dry_run:
        profile = load_candidate_application_profile(args.candidate_profile, cv_override=args.cv_file)
    await apply_to_jobs(
        client,
        batch_size=args.batch_size,
        record_ids=args.record_ids,
        candidate_profile=profile,
        dry_run=args.dry_run,
        batch_tag=args.batch_tag,
        session_state_path=args.session_state,
        source=args.source,
    )


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("Interrupted by user.")
    except Exception as exc:
        console.print(Panel(str(exc), title="Apply Jobs Error", style="red"))
        raise


if __name__ == "__main__":
    main()
