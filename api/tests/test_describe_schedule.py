"""Pin describe_schedule() output for the cron patterns the routine
proposer renders. Drift here changes what operators see in the bell
when reviewing routine-mint proposals."""

from __future__ import annotations

from api.routines import describe_schedule


def test_daily_at_specific_time():
    assert describe_schedule("0 9 * * *") == "daily at 09:00"


def test_daily_at_midnight():
    assert describe_schedule("0 0 * * *") == "daily at 00:00"


def test_every_n_minutes():
    assert describe_schedule("*/15 * * * *") == "every 15 minutes"


def test_every_n_hours_on_the_hour():
    assert describe_schedule("0 */2 * * *") == "every 2 hours, on the hour"


def test_at_specific_minute_each_hour():
    assert describe_schedule("17 * * * *") == "at :17 past every hour"


def test_specific_weekday():
    assert describe_schedule("0 9 * * 1") == "every Monday at 09:00"


def test_weekdays():
    assert describe_schedule("30 8 * * 1-5") == "weekdays at 08:30"


def test_weekends():
    assert describe_schedule("0 10 * * 0,6") == "weekends at 10:00"


def test_twice_daily():
    assert describe_schedule("0 7,19 * * *") == "twice daily at 07:00 and 19:00"


def test_monthly_on_first():
    assert describe_schedule("0 0 1 * *") == "monthly on the 1st at 00:00"


def test_monthly_on_15th():
    assert describe_schedule("0 12 15 * *") == "monthly on the 15th at 12:00"


def test_yearly():
    assert describe_schedule("0 0 25 12 *") == "yearly on December 25th at 00:00"


def test_weekday_range_with_hour_range():
    # The Menya Rui Ramen Alert pattern: Wed–Fri, hourly during dinner
    assert describe_schedule("0 17-22 * * 3,4,5") == "Wed, Thu, Fri, 17:00–22:00 hourly"


def test_invalid_input_returns_as_is():
    assert describe_schedule("not a cron") == "not a cron"
    assert describe_schedule("") == ""
    assert describe_schedule("0 9 * *") == "0 9 * *"  # 4 fields, not 5


def test_fully_wildcarded():
    assert describe_schedule("* * * * *") == "every minute"
