from datetime import date

import pytest

from common import month_windows, months_between, parse_ts
from normalize import build_dates


def test_month_windows_are_calendar_months():
    assert month_windows("2027-11", "2028-02") == [
        ("2027-11-01", "2027-11-30"), ("2027-12-01", "2027-12-31"),
        ("2028-01-01", "2028-01-31"), ("2028-02-01", "2028-02-29"),  # leap year
    ]
    assert month_windows("2026-09", "2026-09") == [("2026-09-01", "2026-09-30")]
    with pytest.raises(ValueError):
        month_windows("2026-09", "2026-01")


def test_months_between_inclusive_across_years():
    assert months_between(date(2025, 11, 20), date(2026, 2, 1)) == ["2025-11", "2025-12", "2026-01", "2026-02"]


@pytest.mark.parametrize("raw,expected", [
    ("2026-03-03", "2026-03-03T00:00:00+00:00"),
    ("2026-03-02T10:11:00+0000", "2026-03-02T10:11:00+00:00"),
    ("2026-03-02T23:30:00-0700", "2026-03-02T23:30:00-07:00"),
    ("2026-03-02T10:11:00Z", "2026-03-02T10:11:00+00:00"),
])
def test_parse_ts_accepts_ad_library_formats(raw, expected):
    assert parse_ts(raw).isoformat() == expected
    assert parse_ts(None) is None and parse_ts("") is None


def test_build_dates_stopped_ad():
    d = build_dates({"ad_delivery_start_time": "2026-01-30", "ad_delivery_stop_time": "2026-03-01",
                     "ad_creation_time": "2026-01-29T08:00:00+0000"}, "2026-09-29T12:00:00Z")
    assert d["start_month"] == "2026-01"
    assert d["active_months"] == ["2026-01", "2026-02", "2026-03"]
    assert d["days_running"] == 31  # inclusive: Jan 30 .. Mar 1
    assert d["active_at_fetch"] is False


def test_build_dates_active_ad_runs_to_fetch_date():
    d = build_dates({"ad_delivery_start_time": "2026-09-01"}, "2026-09-29T12:00:00Z")
    assert d["delivery_stop"] is None and d["active_at_fetch"] is True
    assert d["days_running"] == 29 and d["active_months"] == ["2026-09"]


def test_build_dates_stop_in_future_counts_to_fetch():
    d = build_dates({"ad_delivery_start_time": "2026-09-01", "ad_delivery_stop_time": "2026-10-15"},
                    "2026-09-10T00:00:00Z")
    assert d["active_at_fetch"] is True and d["days_running"] == 10


def test_build_dates_missing_start():
    d = build_dates({}, "2026-09-29T12:00:00Z")
    assert d["start_month"] is None and d["active_months"] == [] and d["days_running"] is None


def test_windows_never_pass_metas_today():
    # 2026-10-01 03:49 UTC is still 2026-09-30 in Meta's (Pacific) day; Oct 1 was refused live (code 100).
    from datetime import date, datetime, timezone

    from common import api_last_day, month_windows
    assert api_last_day(datetime(2026, 10, 1, 3, 49, tzinfo=timezone.utc)) == date(2026, 9, 30)
    assert api_last_day(datetime(2026, 10, 1, 23, 0, tzinfo=timezone.utc)) == date(2026, 10, 1)
    wins = month_windows("2026-08", "2026-10", last_day=date(2026, 9, 30))
    assert wins == [("2026-08-01", "2026-08-31"), ("2026-09-01", "2026-09-30")]
    assert month_windows("2026-09", "2026-09", last_day=date(2026, 9, 12)) == [("2026-09-01", "2026-09-12")]
