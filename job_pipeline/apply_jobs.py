from __future__ import annotations

import argparse
import asyncio

from rich.console import Console
from rich.panel import Panel

from .apply_workflow import (
    DEFAULT_APPLY_BATCH_SIZE,
    DEFAULT_APPLY_SESSION_STATE_PATH,
    DEFAULT_APPLY_THRESHOLD,
    apply_to_jobs,
    build_airtable_client,
    load_candidate_application_profile,
    shortlist_jobs,
)


console = Console()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Assist with applications for top-scored jobs.")
    parser.add_argument("--mode", choices=["shortlist", "apply"], required=True)
    parser.add_argument("--threshold", type=int, default=DEFAULT_APPLY_THRESHOLD)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_APPLY_BATCH_SIZE)
    parser.add_argument("--record-ids", nargs="*", default=[])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--candidate-profile", default="candidate_profile.json")
    parser.add_argument("--cv-file", default=None)
    parser.add_argument("--batch-tag", default=None)
    parser.add_argument("--session-state", default=DEFAULT_APPLY_SESSION_STATE_PATH)
    return parser.parse_args()


async def _run() -> None:
    args = parse_args()
    if args.threshold < 1:
        raise RuntimeError("--threshold must be at least 1.")
    if args.batch_size < 1:
        raise RuntimeError("--batch-size must be at least 1.")

    client = build_airtable_client()

    if args.mode == "shortlist":
        shortlist_jobs(
            client,
            threshold=args.threshold,
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
