from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from tubetape.durations import (
    parse_duration,
    parse_resolution,
    parse_timezone,
    system_local_timezone,
)


class TestParseDuration:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("20m", 1200.0),
            ("1200s", 1200.0),
            ("0:20:00", 1200.0),
            ("20:00", 1200.0),
            ("24h", 86400.0),
            ("1.5m", 90.0),
            ("0:00:10", 10.0),
            ("1200", 1200.0),
            ("0", 0.0),
            ("1h", 3600.0),
            ("90s", 90.0),
            ("1:00:00", 3600.0),
        ],
    )
    def test_valid(self, text, expected):
        assert parse_duration(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "abc",
            "-5",
            "-20m",
            "1:60",
            "0:75",
            "0:-1:00",
            "1:-30",
            "1:2:3:4",
            "1h30m",
            "m",
            "s",
            "20mm",
        ],
    )
    def test_invalid(self, text):
        with pytest.raises(ValueError):
            parse_duration(text)


class TestParseResolution:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("3840x2160", (3840, 2160)),
            ("1920X1080", (1920, 1080)),
            ("1920 x 1080", (1920, 1080)),
            ("640x480", (640, 480)),
        ],
    )
    def test_valid(self, text, expected):
        assert parse_resolution(text) == expected

    @pytest.mark.parametrize(
        "text",
        ["", "abc", "0x0", "-1x10", "1920", "1920x", "x1080", "1920x1080x1"],
    )
    def test_invalid(self, text):
        with pytest.raises(ValueError):
            parse_resolution(text)


class TestParseTimezone:
    def test_utc(self):
        assert str(parse_timezone("UTC")) == "UTC"

    def test_iana(self):
        tz = parse_timezone("Asia/Shanghai")
        assert isinstance(tz, ZoneInfo)
        assert tz.key == "Asia/Shanghai"

    def test_unknown(self):
        with pytest.raises(ValueError):
            parse_timezone("Not/AZone")

    def test_empty_falls_back_to_system_local(self):
        tz = parse_timezone("")
        assert tz is not None

    def test_none_falls_back_to_system_local(self):
        assert parse_timezone(None) is not None


def test_system_local_timezone_returns_tzinfo():
    tz = system_local_timezone()
    # datetime tzinfo objects expose utcoffset; ZoneInfo does too.
    assert hasattr(tz, "utcoffset")
