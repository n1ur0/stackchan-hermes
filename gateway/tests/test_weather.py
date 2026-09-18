"""Tests for the Phase E weather judgement (JMA bosai feeds)."""

import datetime as dt

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


# ---- active_warnings -------------------------------------------------


def test_active_warnings_filters_cancelled():
    data = warning_json(
        [
            {"code": "03", "status": "issued"},
            {"code": "18", "status": "continued"},
            {"code": "21", "status": "cancelled"},
        ]
    )
    assert weather.active_warnings(data, CITY) == ["Heavy Rain Warning", "Flood Advisory"]


def test_active_warnings_other_city_ignored():
    data = warning_json([{"code": "03", "status": "issued"}], city="2710000")
    assert weather.active_warnings(data, CITY) == []


def test_active_warnings_unknown_code_generic():
    data = warning_json([{"code": "99", "status": "issued"}])
    assert weather.active_warnings(data, CITY) == ["weather advisory"]


# ---- today_max_pop ---------------------------------------------------


def test_today_max_pop_picks_today_only():
    data = forecast_json(
        [
            "2026-06-12T06:00:00+09:00",
            "2026-06-12T12:00:00+09:00",
            "2026-06-13T00:00:00+09:00",
        ],
        ["20", "60", "90"],  # tomorrow's 90 must not count
    )
    assert weather.today_max_pop(data, TODAY) == 60


def test_today_max_pop_skips_blank_slots():
    data = forecast_json(
        ["2026-06-12T00:00:00+09:00", "2026-06-12T06:00:00+09:00"], ["", "30"]
    )
    assert weather.today_max_pop(data, TODAY) == 30


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


def test_judge_warning_takes_priority():
    data = warning_json([{"code": "03", "status": "issued"}])
    rainy = forecast_json(["2026-06-12T06:00:00+09:00"], ["80"])
    line = judge(data, rainy)
    assert line == "Warnings active: Heavy Rain Warning. Stay safe!"


def test_judge_two_warnings_listed():
    data = warning_json(
        [
            {"code": "03", "status": "issued"},
            {"code": "04", "status": "issued"},
            {"code": "14", "status": "issued"},  # third one not listed
        ]
    )
    line = judge(data, NORMAL_FORECAST)
    assert line == "Warnings active: Heavy Rain Warning, Flood Warning. Stay safe!"


def test_judge_rain_at_threshold():
    data = warning_json([{"code": "21", "status": "cancelled"}])
    rainy = forecast_json(["2026-06-12T12:00:00+09:00"], ["50"])
    line = judge(data, rainy)
    assert line == "Rain likely today, 50% chance. Don't forget your umbrella."


def test_judge_normal_day_is_silent():
    data = warning_json([{"code": "21", "status": "cancelled"}])
    assert judge(data, NORMAL_FORECAST) is None


def test_judge_no_pops_is_silent():
    assert judge(warning_json([]), []) is None
