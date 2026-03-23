import argparse
import os
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class RunResult:
    mode: str
    index: int
    duration_s: float
    exit_code: int
    offers_count: Optional[int]
    replay_line: Optional[str]


OFFERS_RE = re.compile(r"OFFERS_COUNT=(\d+)")
REPLAY_RE = re.compile(r"STAGEHAND_REPLAY_[^\n]*")


def _run_once(mode: str, index: int, timeout_s: int) -> RunResult:
    env = os.environ.copy()
    if mode == "off":
        # Used in your repo already to force cold runs (no Stagehand cache reuse).
        env["STAGEHAND_CACHE_TTL_SECONDS"] = "0"
    else:
        env.pop("STAGEHAND_CACHE_TTL_SECONDS", None)

    cmd = [sys.executable, "-m", "jobscraper.src.pydantic_sandbox"]
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout_s,
    )
    duration_s = time.perf_counter() - t0
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    offers_match = OFFERS_RE.search(output)
    replay_match = REPLAY_RE.search(output)

    return RunResult(
        mode=mode,
        index=index,
        duration_s=duration_s,
        exit_code=proc.returncode,
        offers_count=int(offers_match.group(1)) if offers_match else None,
        replay_line=replay_match.group(0).strip() if replay_match else None,
    )


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else float("nan")


def _print_results(results: list[RunResult]) -> None:
    print("\nPer-run results:")
    for r in results:
        print(
            f"  mode={r.mode:<3} run={r.index:<2} "
            f"time={r.duration_s:>7.2f}s exit={r.exit_code} "
            f"offers={r.offers_count} replay={r.replay_line or '-'}"
        )


def _summarize(results: list[RunResult], threshold_gain_pct: float) -> int:
    off = [r for r in results if r.mode == "off"]
    on = [r for r in results if r.mode == "on"]

    off_times = [r.duration_s for r in off if r.exit_code == 0]
    on_times = [r.duration_s for r in on if r.exit_code == 0]

    off_median = _median(off_times)
    on_median = _median(on_times)

    off_offers = sorted({r.offers_count for r in off if r.offers_count is not None})
    on_offers = sorted({r.offers_count for r in on if r.offers_count is not None})

    print("\nSummary:")
    print(f"  OFF median: {off_median:.2f}s")
    print(f"  ON  median: {on_median:.2f}s")
    if off_times and on_times:
        speedup_pct = ((off_median - on_median) / off_median) * 100.0 if off_median > 0 else 0.0
        print(f"  Speedup:    {speedup_pct:.1f}%")
    else:
        speedup_pct = 0.0
        print("  Speedup:    n/a (missing successful runs)")
    print(f"  OFF offers: {off_offers or ['n/a']}")
    print(f"  ON  offers: {on_offers or ['n/a']}")

    success_runs = all(r.exit_code == 0 for r in results)
    offers_stable = bool(off_offers and on_offers and off_offers == on_offers)
    speed_gain_ok = speedup_pct >= threshold_gain_pct

    print("\nVerdict:")
    print(f"  runs_ok={success_runs}")
    print(f"  offers_stable={offers_stable}")
    print(f"  speed_gain_ok={speed_gain_ok} (threshold={threshold_gain_pct:.1f}%)")

    if success_runs and offers_stable and speed_gain_ok:
        print("CACHE_PROBE_RESULT=PASS")
        return 0
    print("CACHE_PROBE_RESULT=INCONCLUSIVE")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="A/B probe for Stagehand cache effectiveness in local mode."
    )
    parser.add_argument("--runs", type=int, default=3, help="Runs per mode (off/on). Default: 3")
    parser.add_argument("--timeout", type=int, default=900, help="Timeout per run in seconds")
    parser.add_argument(
        "--threshold-gain-pct",
        type=float,
        default=15.0,
        help="Minimum median speed gain (ON vs OFF) to consider PASS.",
    )
    args = parser.parse_args()

    if args.runs < 2:
        print("--runs must be >= 2 for a meaningful median comparison")
        return 2

    results: list[RunResult] = []
    for mode in ("off", "on"):
        for i in range(1, args.runs + 1):
            print(f"\nRunning mode={mode} run={i}/{args.runs} ...", flush=True)
            try:
                result = _run_once(mode=mode, index=i, timeout_s=args.timeout)
            except subprocess.TimeoutExpired:
                print(f"  timed out after {args.timeout}s")
                results.append(
                    RunResult(
                        mode=mode,
                        index=i,
                        duration_s=float(args.timeout),
                        exit_code=124,
                        offers_count=None,
                        replay_line=None,
                    )
                )
                continue
            results.append(result)
            print(
                f"  done in {result.duration_s:.2f}s | exit={result.exit_code} | "
                f"offers={result.offers_count}"
            )

    _print_results(results)
    return _summarize(results, threshold_gain_pct=args.threshold_gain_pct)


if __name__ == "__main__":
    raise SystemExit(main())
