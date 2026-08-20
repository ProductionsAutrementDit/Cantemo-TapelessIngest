"""Tier 1: pure date-window helper tests (FR-21, FR-25, FR-33). No DB.

Covers the I/O & edge-case matrix of story 1.4: calendar-aware m/y units,
regression parity for d/w, malformed/unknown-unit/future rejection, and the
single-range-line window log format.
"""

import re
from datetime import datetime, timedelta

import pytest
from django.core.management.base import CommandError

from portal.plugins.TapelessIngest.management.commands.scan_tapeless_dir import (
    compute_date_window,
    format_window_log,
    parse_from,
    parse_since,
)

NOW = datetime(2026, 8, 20, 12, 30, 0)


def test_parse_since_days_matches_timedelta():
    # Regression unit: identical to the historical timedelta result.
    assert parse_since("10d", NOW) == NOW - timedelta(days=10)


def test_parse_since_weeks_matches_timedelta():
    # Regression unit: identical to the historical timedelta result.
    assert parse_since("2w", NOW) == NOW - timedelta(weeks=2)


def test_parse_since_months_calendar_aware():
    assert parse_since("3m", NOW) == datetime(2026, 5, 20, 12, 30, 0)


def test_parse_since_years_calendar_aware():
    assert parse_since("1y", NOW) == datetime(2025, 8, 20, 12, 30, 0)


def test_parse_since_month_clamps_day():
    # Mar 31 minus one month clamps to Feb 28 (relativedelta day clamping).
    assert parse_since("1m", datetime(2026, 3, 31)) == datetime(2026, 2, 28)


@pytest.mark.parametrize("value", ["5h", "abc", "3", "m", "", "1.5d", "-1d", "d3"])
def test_parse_since_rejects_malformed(value):
    expected = (
        f"Invalid --since '{value}': expected <number><unit>, unit one of d/w/m/y"
    )
    with pytest.raises(CommandError, match=re.escape(expected)):
        parse_since(value, NOW)


def test_parse_from_valid():
    assert parse_from("2026-08-01") == datetime(2026, 8, 1)


@pytest.mark.parametrize("value", ["2026-13-01", "not-a-date", "20260801"])
def test_parse_from_rejects_malformed(value):
    expected = f"Invalid --from '{value}': expected YYYY-MM-DD"
    with pytest.raises(CommandError, match=re.escape(expected)):
        parse_from(value)


def test_compute_date_window_rejects_future_start():
    with pytest.raises(CommandError, match=re.escape("--from date is in the future")):
        compute_date_window(datetime(2027, 1, 1), NOW)


def test_compute_date_window_since_1w_inclusive_bounds():
    # --since 1w on 2026-08-20: 8 day folders, boundaries inclusive.
    window = compute_date_window(parse_since("1w", NOW), NOW)
    assert window == [
        "20260813",
        "20260814",
        "20260815",
        "20260816",
        "20260817",
        "20260818",
        "20260819",
        "20260820",
    ]


def test_compute_date_window_from_midnight_start():
    # --from style start (midnight) up to a mid-day now: 20 day folders.
    window = compute_date_window(parse_from("2026-08-01"), NOW)
    assert window[0] == "20260801"
    assert window[-1] == "20260820"
    assert len(window) == 20


def test_compute_date_window_single_day():
    assert compute_date_window(NOW, NOW) == ["20260820"]


def test_window_logged_as_single_range_line():
    window = compute_date_window(parse_since("1w", NOW), NOW)
    assert (
        format_window_log(window)
        == "Scanning folders from 20260813 to 20260820 (8 day folders)"
    )
