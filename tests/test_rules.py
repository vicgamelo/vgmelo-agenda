from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.conftest import BRT, T0, brt, make_settings
from vgmelo_agenda import rules
from vgmelo_agenda.rules import Interval


@pytest.fixture
def settings(tmp_path: Any) -> Any:
    return make_settings(tmp_path)


def local(starts: list[datetime]) -> list[str]:
    return [s.astimezone(BRT).strftime("%a %d %H:%M") for s in starts]


def test_durations_follow_victors_bounds(settings: Any) -> None:
    assert rules.durations(settings, "job") == [15, 30, 45, 60]
    assert rules.durations(settings, "consulting") == [30, 45, 60, 75, 90]
    assert rules.is_valid_duration(settings, "job", 45)
    assert not rules.is_valid_duration(settings, "job", 75)
    assert not rules.is_valid_duration(settings, "consulting", 15)
    assert not rules.is_valid_duration(settings, "job", 40)
    assert not rules.is_valid_duration(settings, "nope", 30)


def test_min_notice_skips_tonight_and_first_slot_is_tomorrow_morning(settings: Any) -> None:
    # Monday 09:00 + 12h = 21:00: tonight's 18-20 window is out.
    starts = rules.candidate_starts(settings, 30, T0.astimezone(UTC))
    assert local(starts)[:3] == ["Tue 10 08:00", "Tue 10 08:15", "Tue 10 08:30"]
    assert all(s >= T0 + timedelta(hours=12) for s in starts)


def test_min_notice_is_exactly_12h(settings: Any) -> None:
    now = brt(9, 20, 0)  # Monday 20:00 -> earliest is Tuesday 08:00 exactly
    assert local(rules.candidate_starts(settings, 30, now))[0] == "Tue 10 08:00"
    now = brt(9, 20, 1)
    assert local(rules.candidate_starts(settings, 30, now))[0] == "Tue 10 08:15"


def test_horizon_is_14_days(settings: Any) -> None:
    starts = rules.candidate_starts(settings, 30, T0)
    last = max(starts)
    assert last <= T0 + timedelta(days=14)
    assert last.astimezone(BRT) == brt(23, 9, 0)  # Monday 23/11 09:00 is exactly 14 days ahead


def test_no_sundays_and_only_the_three_windows(settings: Any) -> None:
    starts = rules.candidate_starts(settings, 15, T0)
    assert not [s for s in starts if s.astimezone(BRT).weekday() == 6]
    hours = {s.astimezone(BRT).hour for s in starts}
    assert hours == {8, 9, 12, 13, 18, 19}
    assert any(s.astimezone(BRT).weekday() == 5 for s in starts)  # Saturday is open


def test_call_must_fit_inside_the_window(settings: Any) -> None:
    tuesday = [s for s in rules.candidate_starts(settings, 90, T0) if s.astimezone(BRT).day == 10]
    assert local(tuesday) == [
        "Tue 10 08:00", "Tue 10 08:15", "Tue 10 08:30",
        "Tue 10 12:00", "Tue 10 12:15", "Tue 10 12:30",
        "Tue 10 18:00", "Tue 10 18:15", "Tue 10 18:30",
    ]  # fmt: skip


def test_buffer_of_15_min_around_taken_and_busy(settings: Any) -> None:
    taken = [Interval(brt(10, 8, 30), brt(10, 9, 0))]
    starts = local(rules.free_starts(settings, 30, T0, taken, [], []))
    tuesday_morning = [s for s in starts if s.startswith("Tue 10 0")]
    # 08:00 would end at 08:30 with no gap; 09:15 is the first start 15 min after 09:00.
    assert tuesday_morning == ["Tue 10 09:15", "Tue 10 09:30"]

    busy = [Interval(brt(10, 12, 45), brt(10, 13, 0))]
    noon = [s for s in local(rules.free_starts(settings, 30, T0, [], busy, [])) if s.startswith("Tue 10 1")]
    assert "Tue 10 12:00" in noon and "Tue 10 12:15" not in noon and "Tue 10 13:15" in noon


def test_full_day_has_no_slots(settings: Any) -> None:
    starts = rules.free_starts(settings, 30, T0, [], [], ["2026-11-10"])
    assert not [s for s in starts if s.astimezone(BRT).day == 10]
    assert [s for s in starts if s.astimezone(BRT).day == 11]


def test_group_by_day_in_the_guests_timezone(settings: Any) -> None:
    # 18:00 in São Paulo is 21:00 in Lisbon (WET, UTC+0 in November) and 06:00 next day in Tokyo.
    starts = [brt(10, 18, 0)]
    assert rules.group_by_day(settings, starts, "Europe/Lisbon") == {"2026-11-10": ["21:00"]}
    assert rules.group_by_day(settings, starts, "Asia/Tokyo") == {"2026-11-11": ["06:00"]}
    assert rules.group_by_day(settings, starts) == {"2026-11-10": ["18:00"]}


def test_local_day_uses_owner_timezone(settings: Any) -> None:
    # 01:00 UTC on the 11th is still the 10th in São Paulo.
    assert rules.local_day(settings, datetime(2026, 11, 11, 1, 0, tzinfo=UTC)) == "2026-11-10"


def test_settings_reject_bad_windows(tmp_path: Any) -> None:
    with pytest.raises(ValueError):
        make_settings(tmp_path, windows="10:00-08:00")
    with pytest.raises(ValueError):
        make_settings(tmp_path, windows="8h-10h")
    with pytest.raises(ValueError):
        make_settings(tmp_path, step_minutes=7)
