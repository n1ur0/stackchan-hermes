"""Tests for the Phase E weather judgement (JMA bosai feeds)."""

import datetime as dt

import pytest

from stackchan_mcp import weather

CITY = "2720900"  # Moriguchi city
TODAY = dt.date(2026, 6, 12)


def warning_json(warnings, city=CITY):
    return {
        "reportDatetime": "2026-06-12T05:00:00+09:00",
        "areaTypes": [{"areas": [{"code": city, "warnings": warnings}]}],
    }


def forecast_json(time_defines, pops):
    """Shape observed on /bosai/forecast/data/forecast/270000.json."""
    return [
        {
            "timeSeries": [
                {
                    "timeDefines": ["2026-06-12T05:00:00+09:00"],
                    "areas": [{"area": {"code": "270000"}, "weathers": ["cloudy"]}],
                },
                {
                    "timeDefines": time_defines,
                    "areas": [{"area": {"code": "270000"}, "pops": pops}],
                },
            ]
        }
    ]


NORMAL_FORECAST = forecast_json(
    ["2026-06-12T06:00:00+09:00", "2026-06-12T12:00:00+09:00"], ["10", "0"]
)
NO_WARNINGS = warning_json([{"code": "21", "status": "cancelled"}])


# ---- active_warnings -------------------------------------------------


@pytest.mark.parametrize(
    "warnings, city, expected",
    [
        # only "issued" statuses are listed
        (
            [
                {"code": "03", "status": "issued"},
                {"code": "18", "status": "continued"},
                {"code": "21", "status": "cancelled"},
            ],
            CITY,
            ["Heavy Rain Warning", "Flood Advisory"],
        ),
        # other cities' warnings are ignored
        ([{"code": "03", "status": "issued"}], "2710000", []),
        # unknown codes fall back to a generic label
        ([{"code": "99", "status": "issued"}], CITY, ["weather advisory"]),
    ],
)
def test_active_warnings(warnings, city, expected):
    assert weather.active_warnings(warning_json(warnings, city=city), CITY) == expected


# ---- today_max_pop ---------------------------------------------------


@pytest.mark.parametrize(
    "time_defines, pops, expected",
    [
        # tomorrow's 90 must not count
        (
            [
                "2026-06-12T06:00:00+09:00",
                "2026-06-12T12:00:00+09:00",
                "2026-06-13T00:00:00+09:00",
            ],
            ["20", "60", "90"],
            60,
        ),
        # blank slots are skipped
        (["2026-06-12T00:00:00+09:00", "2026-06-12T06:00:00+09:00"], ["", "30"], 30),
    ],
)
def test_today_max_pop(time_defines, pops, expected):
    assert weather.today_max_pop(forecast_json(time_defines, pops), TODAY) == expected


def test_today_max_pop_handles_garbage():
    assert weather.today_max_pop([], TODAY) is None
    assert weather.today_max_pop([{"nope": 1}], TODAY) is None


# ---- judge_weather ---------------------------------------------------


def judge(warnings_data, forecast_data, threshold=50):
    return weather.judge_weather(
        warnings_data,
        forecast_data,
        city_code=CITY,
        pop_threshold=threshold,
        today=TODAY,
    )


@pytest.mark.parametrize(
    "warnings, forecast_data, expected",
    [
        # a warning beats even an 80% rain forecast
        (
            [{"code": "03", "status": "issued"}],
            forecast_json(["2026-06-12T06:00:00+09:00"], ["80"]),
            "Warnings active: Heavy Rain Warning. Stay safe!",
        ),
        (
            [
                {"code": "03", "status": "issued"},
                {"code": "04", "status": "issued"},
                {"code": "14", "status": "issued"},  # third one not listed
            ],
            NORMAL_FORECAST,
            "Warnings active: Heavy Rain Warning, Flood Warning. Stay safe!",
        ),
    ],
)
def test_judge_warning_takes_priority(warnings, forecast_data, expected):
    assert judge(warning_json(warnings), forecast_data) == expected


def test_judge_rain_at_threshold():
    rainy = forecast_json(["2026-06-12T12:00:00+09:00"], ["50"])
    line = judge(NO_WARNINGS, rainy)
    assert line == "Rain likely today, 50% chance. Don't forget your umbrella."


@pytest.mark.parametrize(
    "warnings_data, forecast_data",
    [
        (NO_WARNINGS, NORMAL_FORECAST),  # fine weather, no active warnings
        (warning_json([]), []),  # no pops present at all
    ],
)
def test_judge_silent(warnings_data, forecast_data):
    assert judge(warnings_data, forecast_data) is None
