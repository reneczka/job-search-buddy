from __future__ import annotations

import json

from dotenv import load_dotenv
from rich.console import Console

from .airtable_mapper import (
    dedupe_airtable_records,
    should_skip_airtable_record,
    to_airtable_record,
    validate_airtable_record,
)
from .airtable_sync import write_airtable_records, write_indeed_full_records, write_indeed_url_test_records
from .boards import selected_boards
from .detail_extraction import extract_job_detail
from .models import PipelineRunResult
from .stagehand_session import (
    StagehandRuntime,
    env_flag,
    fetch_stagehand_metrics,
    print_metrics_summary,
    print_replay_metrics,
    run_cache_probe,
)
from .url_discovery import discover_job_urls


console = Console()


async def run_pipeline(
    site: str,
    dry_run: bool,
    write_airtable: bool,
    write_airtable_indeed_url_test: bool = False,
    write_airtable_indeed_full: bool = False,
    max_jobs_per_board: int | None = None,
    max_jobs_total: int | None = None,
) -> PipelineRunResult:
    load_dotenv()
    boards = selected_boards(site)
    if not boards:
        raise RuntimeError(f"No boards matched --site {site!r}.")
    if sum(bool(value) for value in (write_airtable, write_airtable_indeed_url_test, write_airtable_indeed_full)) > 1:
        raise RuntimeError("Choose only one Airtable mode.")
    if (write_airtable_indeed_url_test or write_airtable_indeed_full) and site != "indeed":
        raise RuntimeError("The Indeed Airtable write modes require --site indeed.")

    runtime = await StagehandRuntime.create()
    discovery_results = []
    mapped_records: list[dict[str, str]] = []
    board_counts: list[tuple[str, int, int]] = []
    airtable_created = 0
    airtable_updated = 0
    airtable_skipped = 0
    remaining_jobs = max_jobs_total
    total_cache_hits = 0
    total_selector_fallbacks = 0
    total_retries_attempted = 0
    total_retries_used = 0

    try:
        if write_airtable_indeed_url_test:
            pipeline_mode = "indeed-airtable-url-test"
        elif write_airtable_indeed_full:
            pipeline_mode = "indeed-airtable-full-write"
        elif dry_run or not write_airtable:
            pipeline_mode = "dry-run"
        else:
            pipeline_mode = "write-airtable"
        console.print(f"PIPELINE_MODE={pipeline_mode}")
        console.print(f"STAGEHAND_CACHE_ENABLED={str(runtime.cache_enabled).lower()}")
        if env_flag("STAGEHAND_CACHE_PROBE", "false"):
            await run_cache_probe(runtime)
        else:
            console.print("CACHE_PROBE=disabled")

        for board in boards:
            if remaining_jobs is not None and remaining_jobs <= 0:
                console.print("DETAIL_LIMIT_TOTAL reached=0 skipping_remaining_boards=true")
                break

            discovery = await discover_job_urls(runtime, board)
            discovery_results.append(discovery)
            board_record_start = len(mapped_records)
            board_records: list[dict[str, str]] = []
            board_cache_hits = 0
            board_selector_fallbacks = 0
            board_retries_attempted = 0
            board_retries_used = 0

            console.print(
                f"DISCOVERY_RESULT site={board.name} urls={len(discovery.urls)} "
                f"selector={discovery.metadata.selector or '-'} "
                f"first_offer={discovery.metadata.first_offer_url or '-'}"
            )

            selected_urls = discovery.urls[:max_jobs_per_board] if max_jobs_per_board else discovery.urls
            if remaining_jobs is not None:
                selected_urls = selected_urls[:remaining_jobs]
                console.print(
                    f"DETAIL_LIMIT_TOTAL remaining={remaining_jobs} selected={len(selected_urls)} "
                    f"discovered={len(discovery.urls)}"
                )
            if max_jobs_per_board is not None:
                console.print(
                    f"DETAIL_LIMIT site={board.name} selected={len(selected_urls)} "
                    f"discovered={len(discovery.urls)} max_jobs_per_board={max_jobs_per_board}"
                )

            for index, url in enumerate(selected_urls, start=1):
                console.print(f"DETAIL_PROGRESS site={board.name} index={index}/{len(selected_urls)} url={url}")
                detail = await extract_job_detail(runtime, board.name, url)
                board_cache_hits += int(bool(detail.raw.get("cache_hit")))
                board_selector_fallbacks += int(bool(detail.raw.get("selector_fallback_used")))
                board_retries_attempted += int(bool(detail.raw.get("retry_attempted")))
                board_retries_used += int(bool(detail.raw.get("retry_used")))
                record = to_airtable_record(detail)
                issues = validate_airtable_record(record)
                skip_record = should_skip_airtable_record(record)
                if issues:
                    console.print(
                        f"RECORD_VALIDATION site={board.name} index={index} "
                        f"status={'skip' if skip_record else 'warn'} "
                        f"issues={' | '.join(issues)}"
                    )
                if skip_record:
                    console.print(f"DETAIL_SKIPPED site={board.name} index={index} reason=record_validation")
                    continue
                mapped_records.append(record)
                board_records.append(record)

            extracted_count = len(mapped_records) - board_record_start
            if remaining_jobs is not None:
                remaining_jobs = max(0, remaining_jobs - extracted_count)
            total_cache_hits += board_cache_hits
            total_selector_fallbacks += board_selector_fallbacks
            total_retries_attempted += board_retries_attempted
            total_retries_used += board_retries_used
            board_counts.append((board.name, len(discovery.urls), extracted_count))
            console.print(
                f"BOARD_RESULT site={board.name} discovered={len(discovery.urls)} extracted={extracted_count}"
            )
            console.print(
                f"BOARD_EXTRACTION_FLAGS site={board.name} cache_hits={board_cache_hits} "
                f"selector_fallbacks={board_selector_fallbacks} "
                f"retries_attempted={board_retries_attempted} retries_used={board_retries_used}"
            )

            if write_airtable:
                board_deduped_records = dedupe_airtable_records(board_records)
                result = write_airtable_records(board_deduped_records)
                airtable_created += int(result["created"])
                airtable_updated += int(result["updated"])
                airtable_skipped += int(result["skipped"])
                console.print(
                    f"AIRTABLE_WRITE_BOARD site={board.name} created={result['created']} "
                    f"updated={result['updated']} skipped={result['skipped']} "
                    f"candidates={len(board_deduped_records)}"
                )

        deduped_records = dedupe_airtable_records(mapped_records)
        console.print(f"RECORDS_TOTAL={len(mapped_records)}")
        console.print(f"RECORDS_DEDUPED={len(deduped_records)}")
        for site_name, discovered_count, extracted_count in board_counts:
            console.print(
                f"BOARD_SUMMARY site={site_name} discovered={discovered_count} extracted={extracted_count}"
            )
        console.print(
            f"EXTRACTION_FLAGS_TOTAL cache_hits={total_cache_hits} "
            f"selector_fallbacks={total_selector_fallbacks} "
            f"retries_attempted={total_retries_attempted} retries_used={total_retries_used}"
        )

        for record in deduped_records:
            console.print(json.dumps(record, ensure_ascii=False, indent=2))

        if write_airtable:
            console.print(
                f"AIRTABLE_WRITE created={airtable_created} "
                f"updated={airtable_updated} skipped={airtable_skipped} "
                f"candidates={len(deduped_records)}"
            )
        if write_airtable_indeed_url_test:
            result = write_indeed_url_test_records(deduped_records)
            console.print(
                f"AIRTABLE_INDEED_URL_TEST created={result['created']} "
                f"skipped={result['skipped']} candidates={len(deduped_records)}"
            )
        if write_airtable_indeed_full:
            result = write_indeed_full_records(deduped_records)
            console.print(
                f"AIRTABLE_INDEED_FULL_WRITE created={result['created']} "
                f"updated={result['updated']} skipped={result['skipped']} "
                f"candidates={len(deduped_records)}"
            )

        metrics = await fetch_stagehand_metrics(
            base_url=str(runtime.client.base_url),
            model_api_key=runtime.model_api_key,
            browserbase_api_key=runtime.browserbase_api_key,
            browserbase_project_id=runtime.browserbase_project_id,
        )
        print_metrics_summary(metrics)
        await print_replay_metrics(runtime)

        return PipelineRunResult(discovery_results=discovery_results, records=deduped_records)
    finally:
        await runtime.close()
