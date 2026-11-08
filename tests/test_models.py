"""Tests for the typed model, the key catalogue, and pure protocol parsing."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from yarl import URL

from senertec_dachs import (
    LOCAL_KEYS,
    KeyType,
    Normalization,
    Reading,
    ReadingState,
    Snapshot,
)
from senertec_dachs.errors import ProtocolError, WriteError
from senertec_dachs.local.protocol import (
    chunk_read_requests,
    encode_power_request,
    parse_read_response,
    parse_write_acknowledgement,
)

PROTOCOL_SAMPLES = json.loads(
    (Path(__file__).parent / "fixtures" / "local-protocol.json").read_text(
        encoding="utf-8"
    )
)

EXPECTED_GROUP_COUNTS = {
    "operating": 13,
    "identity": 3,
    "year_end": 6,
    "second_generator": 6,
    "hydraulics": 4,
    "temperatures": 17,
    "temperature_targets": 2,
    "actuators": 16,
    "measurements": 3,
    "daily_run": 12,
    "modules": 20,
    "module_statistics": 12,
    "maintenance": 4,
}


def reading_of(wire_key: str, raw: str | None) -> Reading[object]:
    return Reading.from_raw(LOCAL_KEYS[wire_key], raw)


# --- catalogue -----------------------------------------------------------


def test_catalogue_covers_all_documented_keys() -> None:
    assert len(LOCAL_KEYS) == 118
    counts: dict[str, int] = {}
    for descriptor in LOCAL_KEYS.values():
        counts[descriptor.group] = counts.get(descriptor.group, 0) + 1
    assert counts == EXPECTED_GROUP_COUNTS


def test_catalogue_has_no_cloud_keys() -> None:
    for wire_key, descriptor in LOCAL_KEYS.items():
        assert "@" not in wire_key
        assert descriptor.key == wire_key


def test_catalogue_spot_checks() -> None:
    serial = LOCAL_KEYS["Hka_Bd_Stat.uchSeriennummer"]
    assert serial.type is KeyType.STRING
    assert "leading zeros" in serial.notes

    energy = LOCAL_KEYS["Hka_Bd.ulArbeitElektr"]
    assert energy.type is KeyType.DECIMAL
    assert energy.normalized_unit == "kWh"
    assert energy.normalization is Normalization.IDENTITY

    runtime = LOCAL_KEYS["Hka_Bd.ulBetriebssekunden"]
    assert runtime.type is KeyType.DECIMAL
    assert runtime.normalization is Normalization.IDENTITY
    assert runtime.documented_unit == "h"
    assert runtime.normalized_unit == "h"

    outdoor = LOCAL_KEYS["Hka_Mw1.Temp.sbAussen"]
    assert outdoor.sentinel == "127"
    assert outdoor.normalization is Normalization.IDENTITY
    assert serial.sentinel is None

    status = LOCAL_KEYS["Brenner_Bd.bIstStatus"]
    assert status.type is KeyType.INTEGER

    output3 = LOCAL_KEYS["Hka_Mw1.Aktor.fProgAus3"]
    assert output3.type is KeyType.BOOLEAN
    assert "output 2" in output3.notes

    speed = LOCAL_KEYS["Hka_Mw1.usDrehzahl"]
    assert speed.type is KeyType.INTEGER
    assert speed.normalized_unit == "rpm"

    module_runtime = LOCAL_KEYS["Mm_MinMax.ModulBhMax.ulWert"]
    assert module_runtime.type is KeyType.DECIMAL
    assert module_runtime.normalization is Normalization.UNVERIFIED

# --- readings ------------------------------------------------------------


@pytest.mark.parametrize(
    ("wire_key", "raw", "state", "value"),
    [
        ("Mm[0].ModulSteuerung.fModulLaeuft", None, ReadingState.MISSING, None),
        ("Mm[0].ModulSteuerung.fModulLaeuft", "true", ReadingState.AVAILABLE, True),
        ("Mm[0].ModulSteuerung.fModulLaeuft", "false", ReadingState.AVAILABLE, False),
        ("Mm[0].ModulSteuerung.fModulLaeuft", "0", ReadingState.AVAILABLE, False),
        ("Mm[0].ModulSteuerung.fModulLaeuft", "1", ReadingState.AVAILABLE, True),
        ("Mm[0].ModulSteuerung.fModulLaeuft", "2", ReadingState.INVALID, None),
        ("Mm[0].ModulSteuerung.fModulLaeuft", "", ReadingState.INVALID, None),
        ("Hka_Bd.ulAnzahlStarts", "35289", ReadingState.AVAILABLE, 35289),
        ("Hka_Bd.ulAnzahlStarts", "1_000", ReadingState.INVALID, None),
        ("Hka_Bd.ulAnzahlStarts", "12x", ReadingState.INVALID, None),
        ("Hka_Bd.ulAnzahlStarts", "", ReadingState.INVALID, None),
        # The Dachsportal serializes integers in exponent notation.
        (
            "Hka_Bd_Stat.ulInbetriebnahmedatum",
            "3.628008E+8",
            ReadingState.AVAILABLE,
            362800800,
        ),
        (
            "Hka_Bd_Stat.ulInbetriebnahmedatum",
            "3.628008e8",
            ReadingState.AVAILABLE,
            362800800,
        ),
        (
            "Hka_Bd_Stat.ulInbetriebnahmedatum",
            "362800800",
            ReadingState.AVAILABLE,
            362800800,
        ),
        (
            "Hka_Bd_Stat.ulInbetriebnahmedatum",
            "362800800.0",
            ReadingState.AVAILABLE,
            362800800,
        ),
        (
            "Hka_Bd_Stat.ulInbetriebnahmedatum",
            "362800800.5",
            ReadingState.INVALID,
            None,
        ),
        ("Hka_Ew.HydraulikNr.bSpeicherArt", "44", ReadingState.UNVERIFIED, None),
        ("Hka_Bd.ulArbeitElektr", "315311", ReadingState.AVAILABLE, Decimal("315311")),
        ("Hka_Bd.ulArbeitElektr", "0.5", ReadingState.AVAILABLE, Decimal("0.5")),
        ("Hka_Bd.ulArbeitElektr", "1e3", ReadingState.INVALID, None),
        ("Hka_Bd.ulArbeitElektr", "abc", ReadingState.INVALID, None),
        ("Hka_Bd.ulArbeitElektr", "", ReadingState.INVALID, None),
        ("Hka_Mw1.Temp.sbAussen", "12", ReadingState.AVAILABLE, Decimal("12")),
        ("Hka_Mw1.Temp.sbAussen", "127", ReadingState.UNAVAILABLE, None),
        ("Hka_Mw1.Temp.sbAussen", "cold", ReadingState.INVALID, None),
        ("Hka_Mw1.Temp.sbAussen", "", ReadingState.INVALID, None),
        (
            "Hka_Bd_Stat.uchSeriennummer",
            "0000000001",
            ReadingState.AVAILABLE,
            "0000000001",
        ),
        ("Hka_Bd_Stat.uchSeriennummer", "", ReadingState.INVALID, None),
    ],
)
def test_reading_matrix(
    wire_key: str, raw: str | None, state: ReadingState, value: object
) -> None:
    reading = reading_of(wire_key, raw)
    assert reading.state is state
    assert reading.value == value
    assert reading.raw == raw


def test_decimal_values_are_decimal_not_float() -> None:
    reading = reading_of("Hka_Bd.ulArbeitElektr", "315311")
    assert isinstance(reading.value, Decimal)
    assert reading.value == Decimal("315311")


# --- protocol parsing ----------------------------------------------------


@pytest.mark.parametrize(
    "sample", PROTOCOL_SAMPLES["read"], ids=lambda sample: sample["name"]
)
def test_parse_read_response_samples(sample: dict) -> None:
    if "error" in sample:
        with pytest.raises(ProtocolError):
            parse_read_response(sample["response_text"], sample["requested"])
    else:
        assert (
            parse_read_response(sample["response_text"], sample["requested"])
            == sample["expected"]
        )


@pytest.mark.parametrize(
    "sample", PROTOCOL_SAMPLES["write"], ids=lambda sample: sample["name"]
)
def test_parse_write_acknowledgement_samples(sample: dict) -> None:
    expected_keys = list(sample["fields"])
    if "error" in sample:
        with pytest.raises(WriteError) as excinfo:
            parse_write_acknowledgement(sample["response_text"], expected_keys)
        assert excinfo.value.outcome_unknown is True
        assert excinfo.value.acknowledgements is not None
    else:
        acknowledgements = parse_write_acknowledgement(
            sample["response_text"], expected_keys
        )
        assert acknowledgements == {key: "ok" for key in expected_keys}


def test_encode_power_request_field_order() -> None:
    assert encode_power_request(5) == [
        ("Stromf_Ew.Anforderung_GLT.bAktiv", "1"),
        ("Stromf_Ew.Anforderung_GLT.bAnzahlModule", "5"),
    ]


def test_chunk_read_requests_respects_url_limit() -> None:
    base = URL.build(scheme="http", host="127.0.0.1", port=8080, path="/getKey")
    keys = [f"Laufraster15Min_aktTag.bDoppelstunde[{i}]" for i in range(12)]
    chunks = chunk_read_requests(keys, base, 120)
    assert [key for chunk in chunks for key in chunk] == keys
    assert len(chunks) > 1
    for chunk in chunks:
        url = base.with_query([("k", key) for key in chunk])
        assert len(str(url)) <= 120


def test_chunk_read_requests_single_oversized_key() -> None:
    base = URL.build(scheme="http", host="127.0.0.1", port=8080, path="/getKey")
    with pytest.raises(ValueError):
        chunk_read_requests(["Hka_Mw1.Temp.sbZS_Vorlauf2"], base, 40)


# --- snapshots -----------------------------------------------------------


def synthetic_raw(**overrides: str) -> dict[str, str]:
    raw: dict[str, str] = {}
    for wire_key, descriptor in LOCAL_KEYS.items():
        if descriptor.type is KeyType.BOOLEAN:
            raw[wire_key] = "1"
        elif descriptor.type is KeyType.INTEGER:
            raw[wire_key] = "7"
        elif descriptor.type is KeyType.DECIMAL:
            raw[wire_key] = "1.5"
        else:
            raw[wire_key] = "0000000001"
    raw.update(overrides)
    return raw


def test_snapshot_from_full_raw() -> None:
    raw = synthetic_raw(
        **{
            "Hka_Bd.ulArbeitElektr": "315311",
            "Hka_Bd.ulBetriebssekunden": "35289",
            "Hka_Bd.ulAnzahlStarts": "77",
            "Hka_Bd_Stat.uchSeriennummer": "0000000001",
            "Hka_Mw1.Temp.sbAussen": "12",
            "Hka_Mw1.usDrehzahl": "1500",
            "Hka_Mw1.ulMotorlaufsekunden": "1234",
            "Mm[0].ModulSteuerung.fModulLaeuft": "0",
            "Hka_Mw1.Temp.sbZS_Vorlauf2": "127",
            "Mm_MinMax.ModulStartMax.ulWert": "99",
            "Mm_MinMax.ModulBhMax.ulWert": "12345",
            "Wartung_Cache.usIntervall": "4000",
            "BD3112.Hka_Bd.ulArbeitElektr": "429311",
        }
    )
    snapshot = Snapshot.from_raw(raw)

    assert snapshot.operating.electrical_energy.value == Decimal("315311")
    assert snapshot.operating.electrical_energy.state is ReadingState.AVAILABLE
    assert snapshot.operating.runtime.state is ReadingState.AVAILABLE
    assert snapshot.operating.runtime.raw == "35289"
    assert snapshot.operating.runtime.value == Decimal("35289")
    assert snapshot.operating.starts.value == 77

    assert snapshot.identity.serial_number.value == "0000000001"
    assert snapshot.identity.commissioned_epoch_seconds.value == 7

    assert snapshot.temperatures.outdoor.value == Decimal("12")
    assert snapshot.temperatures.outdoor.state is ReadingState.AVAILABLE
    assert snapshot.temperatures.exhaust.value == Decimal("1.5")
    assert snapshot.temperatures.heating_circuit_2_supply.state is ReadingState.UNAVAILABLE
    assert snapshot.temperatures.heating_circuit_2_supply.raw == "127"

    assert snapshot.measurements.engine_speed.value == 1500
    assert snapshot.measurements.runtime_since_start.value == Decimal("1234")
    assert snapshot.measurements.active_power.value == Decimal("1.5")

    assert snapshot.actuators.fault_relay.value is True
    assert snapshot.actuators.hot_water_pump.state is ReadingState.UNVERIFIED

    assert snapshot.modules[0].running.value is False
    assert len(snapshot.modules) == 10
    assert all(module.available.value is True for module in snapshot.modules)

    assert len(snapshot.daily_run) == 12
    assert all(slot.value == 7 for slot in snapshot.daily_run)

    assert snapshot.module_statistics.max_starts.value == 99
    assert snapshot.module_statistics.max_runtime.state is ReadingState.UNVERIFIED

    assert snapshot.maintenance.interval.value == 4000
    assert snapshot.maintenance.due.value is True

    assert snapshot.second_generator.status_raw.value == 7
    assert snapshot.second_generator.starts.value == 7
    assert snapshot.second_generator.runtime.value == Decimal("1.5")
    assert snapshot.hydraulics.storage_type_raw.state is ReadingState.UNVERIFIED

    assert snapshot.year_end.electrical_energy.value == Decimal("429311")
    assert snapshot.year_end.hot_water_volume.state is ReadingState.UNVERIFIED

    assert snapshot.raw["Hka_Bd.ulArbeitElektr"] == "315311"
    with pytest.raises(TypeError):
        snapshot.raw["Hka_Bd.ulArbeitElektr"] = "0"


def test_snapshot_from_empty_raw_is_all_missing() -> None:
    snapshot = Snapshot.from_raw({})
    assert snapshot.operating.electrical_energy.state is ReadingState.MISSING
    assert snapshot.operating.electrical_energy.raw is None
    assert snapshot.operating.electrical_energy.value is None
    assert snapshot.modules[9].running.state is ReadingState.MISSING
    assert snapshot.daily_run[11].state is ReadingState.MISSING
    assert dict(snapshot.raw) == {}


def test_snapshot_missing_optional_equipment_stays_missing() -> None:
    snapshot = Snapshot.from_raw(
        {
            "Mm[0].ModulSteuerung.fModulLaeuft": "1",
            "Hka_Bd.ulAnzahlStarts": "12",
        }
    )
    assert snapshot.modules[0].running.value is True
    assert snapshot.modules[0].available.state is ReadingState.MISSING
    assert snapshot.modules[1].running.state is ReadingState.MISSING
    assert snapshot.modules[1].running.value is not False
    assert snapshot.operating.starts.value == 12
