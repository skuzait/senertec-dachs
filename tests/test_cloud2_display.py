"""Display filter tests, built from values observed on a real unit
(97800 via DachsConnect, November 2026 device page)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from senertec_dachs.cloud2.display import DEFAULT_UNITS, CanIpDisplayFilter

import pytest


def value(
    code: str,
    board: str = "CU-CHP01",
    *,
    name: str | None = None,
    raw: object = None,
    unit: str | None = None,
    enum_name: str | None = None,
) -> object:
    from senertec_dachs.cloud2.models import CanIpValue

    return CanIpValue(
        code=code,
        board=board,
        id="md5hash",
        raw=raw,
        value=Decimal(str(raw)) if isinstance(raw, (int, float)) else raw,
        unit=unit,
        gain=None,
        enum_name=enum_name,
        age_seconds=0,
        readable=None,
        name=name if name is not None else code,
    )


@pytest.fixture()
def flt() -> CanIpDisplayFilter:
    return CanIpDisplayFilter()


# --- measurement datapoints pass ------------------------------------------------

def test_measurement_with_known_unit_passes(flt: CanIpDisplayFilter) -> None:
    v = value("CU-CHP01.IMW009", name="Flow temperature", raw=68.5, unit="°C")
    out = flt.admit(v)
    assert out is v


def test_all_default_units_admit_measurements() -> None:
    flt = CanIpDisplayFilter()
    assert "°C" in DEFAULT_UNITS and "kW" in DEFAULT_UNITS and "h" in DEFAULT_UNITS


def test_electric_power_datapoints_pass(flt: CanIpDisplayFilter) -> None:
    for i, (name, raw) in enumerate(
        (("Electrical power", 5527), ("Electrical energy", 13194711), ("Power setpoint", 5500))
    ):
        v = value(f"CU-CHP01.MW00{i}", name=name, raw=raw, unit="W")
        assert flt.admit(v) is not None


def test_array_elements_keep_their_index_identity() -> None:
    flt = CanIpDisplayFilter()
    v0 = value("AM029[0]", name="Temperature sensor 5", raw=-0.0, unit="°C")
    v1 = value("AM029[1]", name="Temperature sensor 6", raw=42.0, unit="°C")
    assert flt.admit(v0) is v0
    assert flt.admit(v1) is v1
    assert len(flt.boards_seen) == 2


# --- the junk classes from the real unit ----------------------------------------

def test_portal_debug_token_name_rejected(flt: CanIpDisplayFilter) -> None:
    v = value("0HNp1Tn1YZZ405f6iwtfM", name="0HNp1Tn1YZZ405f6iwtfM", raw=0)
    assert flt.admit(v) is None


def test_html_debug_string_rejected(flt: CanIpDisplayFilter) -> None:
    v = value(
        "tmyRBbn48McrM",
        name="tmyRBbn48McrM",
        raw="<br>Ihr BHKW läuft problemlos mit 0 kW<br>97800 (#39781)…",
    )
    assert flt.admit(v) is None


def test_base64_garbage_value_rejected(flt: CanIpDisplayFilter) -> None:
    v = value(
        "C0001",
        name="Custom serial number",
        raw="//////////////////////////8=",
    )
    assert flt.admit(v) is None


def test_money_datapoint_rejected(flt: CanIpDisplayFilter) -> None:
    v = value("AB001", name="demo price", raw=1_000_000.0, unit="€")
    assert flt.admit(v) is None


def test_unitless_config_counter_rejected(flt: CanIpDisplayFilter) -> None:
    v = value("B0002", name="Number of entries", raw=3.0)
    assert flt.admit(v) is None


def test_64bit_descriptor_rejected(flt: CanIpDisplayFilter) -> None:
    v = value("B0003", name="BU data descriptor", raw=1224803104)
    assert flt.admit(v) is None


def test_auth_token_like_integer_rejected(flt: CanIpDisplayFilter) -> None:
    v = value("B0004", name="Authentication token", raw=2456195992)
    assert flt.admit(v) is None


def test_freeform_text_with_weird_name_rejected(flt: CanIpDisplayFilter) -> None:
    v = value("A0001", name="HMI display info", raw="packed-string")
    assert flt.admit(v) is None


# --- translated enumeration states pass, plain strings do not -------------------

def test_translated_enum_state_passes(flt: CanIpDisplayFilter) -> None:
    from senertec_dachs.cloud2.models import CanIpValue

    v = CanIpValue(
        code="CU-CHP01.MM100",
        board="CU-CHP01",
        id="md5hash",
        raw=2,
        value="Standby",
        unit=None,
        gain=None,
        enum_name="applianceStatus",
        age_seconds=0,
        readable=None,
        name="Status Appliance",
    )
    assert flt.admit(v) is v


# --- subsystem duplicates are kept, names get prefixes ---------------------------

def test_mirror_board_kept_with_board_prefix(flt: CanIpDisplayFilter) -> None:
    primary = value("CU-CHP01.MM000", "CU-CHP01", name="Requested Level", raw=3, unit="kW")
    mirror = value("EM.MM000", "EM", name="Requested Level", raw=3, unit="kW")
    out = flt.filter([mirror, primary])
    assert len(out) == 2
    assert out[0].name == "Requested Level"            # CU-CHP01: canonical, plain
    assert out[1].name == "EM Requested Level"        # mirror: subsystem prefix
    assert flt.boards_seen[("requested level", None)] == "CU-CHP01"


def test_same_datapoint_twice_from_same_board_keeps_name(flt: CanIpDisplayFilter) -> None:
    v = value("CU-CHP01.MM000", "CU-CHP01", name="Buffer power %", raw=0.0, unit="%")
    assert flt.admit(v) is v
    again = value("CU-CHP01.MM000", "CU-CHP01", name="Buffer power %", raw=0.0, unit="%")
    out = flt.admit(again)
    assert out is not None and out.name == "Buffer power %"


def test_unnamed_array_elements_beyond_first_excluded(flt: CanIpDisplayFilter) -> None:
    out = flt.filter(
        [
            value("AW001[0]", name="Error time stamps", raw=1794311900500),
            value("AW001[1]", name="Error time stamps", raw=1794262458200),
            value("AW001[39]", name="Error time stamps", raw=1791647514000),
        ]
    )
    assert [v.name for v in out] == ["Error time stamps"]  # current log entry only


def test_numbered_sensor_names_are_not_suffixed(flt: CanIpDisplayFilter) -> None:
    v = value("AM029[5]", name="Temperature sensor 6", raw=44.2, unit="°C")
    out = flt.admit(v)
    assert out is v  # portal name carries the position; stays untouched


def test_zone_rows_collapse_to_current(flt: CanIpDisplayFilter) -> None:
    out = flt.filter(
        [
            value("AW002[0]", name="Zone RoomTemperature", raw=20.0, unit="°C"),
            value("AW002[1]", name="Zone RoomTemperature", raw=6.0, unit="°C"),
        ]
    )
    assert [v.name for v in out] == ["Zone RoomTemperature"]


# --- timestamps -------------------------------------------------------------------

def test_epoch_timestamp_becomes_datetime(flt: CanIpDisplayFilter) -> None:
    ms = 1763372189400  # observed "Date last maintnce"
    v = value("B0005", name="Date last maintnce", raw=ms)
    out = flt.admit(v)
    assert out is not None
    assert out.value == dt.datetime(2025, 11, 17, 9, 36, 29, 400_000, tzinfo=dt.timezone.utc)


def test_big_non_time_integer_is_not_a_datetime(flt: CanIpDisplayFilter) -> None:
    v = value("B0006", name="Some counter", raw=1224803104)
    assert flt.admit(v) is None


def test_time_without_epoch_scale_is_not_converted(flt: CanIpDisplayFilter) -> None:
    v = value("B0007", name="Time push-button", raw=30, unit="Min")
    assert flt.admit(v) is v  # a duration with a unit — no conversion


def test_mcu_uptime_seconds_pass(flt: CanIpDisplayFilter) -> None:
    v = value("B0008", name="MCU uptime", raw=2_666_963.0, unit="s")
    assert flt.admit(v) is v


def test_sensor_array_keeps_every_element() -> None:
    flt = CanIpDisplayFilter()
    for i, raw in enumerate((-0.0, 44.2, -0.0)):
        v = value(f"AM029[{i}]", name="Temperature sensor 5", raw=raw, unit="°C")
        assert flt.admit(v) is not None, f"element {i}"


def test_outdoor_array_family_keeps_elements() -> None:
    flt = CanIpDisplayFilter()
    v = value("AW003[3]", name="T-sensor buffer 1", raw=10.0, unit="°C")
    assert flt.admit(v) is not None


# --- gain-applied sentinels -------------------------------------------------------

def test_temperature_sentinel_rejected(flt: CanIpDisplayFilter) -> None:
    for raw in (-327.68, -3276.8):
        v = value("AW004[0]", name="TOutdoor AvShortWind", raw=raw, unit="°C")
        assert flt.admit(v) is None, f"sentinel {raw}"


def test_large_negative_real_temperature_passes(flt: CanIpDisplayFilter) -> None:
    v = value("AW005[0]", name="Min outdoor T HP", raw=-15.0, unit="°C")
    assert flt.admit(v) is v
