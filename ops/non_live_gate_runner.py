#!/usr/bin/env python3
"""Run the fixed non-live safety suite from this exact checkout."""

from __future__ import annotations

from pathlib import Path
import sys


TEST_PATHS = (
    "tests/test_non_live_release.py",
    "tests/test_intraday.py",
    "tests/test_intraday_operations.py",
    "tests/test_intraday_paper.py",
    "tests/test_intraday_live.py",
    "tests/test_intraday_crash_replay.py",
    "tests/test_state_store.py",
    "tests/test_toss_live_adapter.py",
    "tests/test_toss_client.py",
    "tests/test_toss_conditional.py",
    "tests/test_toss_stream.py",
    "tests/test_discord_approval.py",
    "tests/test_approval_consumer.py",
    "tests/test_news_digest.py",
    "tests/test_shadow_watchdog.py",
    "tests/test_shadow_heartbeat.py",
    "tests/test_paper_status.py",
    "tests/test_macos_shadow_jobs.py",
)


def main() -> int:
    if len(sys.argv) != 1:
        print("non-live gate runner accepts no arguments", file=sys.stderr)
        return 64
    root = Path(__file__).resolve().parents[1]
    source = root / "src"
    if not source.is_dir() or any(not (root / item).is_file() for item in TEST_PATHS):
        print("non-live gate checkout is incomplete", file=sys.stderr)
        return 66
    sys.path.insert(0, str(source))
    import pytest

    return int(
        pytest.main(
            [
                "-o",
                "addopts=",
                "-q",
                "--tb=short",
                *(str(root / item) for item in TEST_PATHS),
            ]
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
