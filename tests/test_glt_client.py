"""Loopback tests for the Dachs Gen2 GLT (MSR3) Modbus TCP backend.

The fake serves the register space of the spec ``8098.119.000.02``:
FC 0x04 answers from a register map, FC 0x06 echoes and records, and
deterministic failure modes (exception answers, short answers, mid-
frame disconnects) exercise the strict answer handling.
"""

from __future__ import annotations

import asyncio
import struct
from decimal import Decimal

import pytest

from senertec_dachs import Dachs, GltClient
from senertec_dachs.errors import ConnectionError, ProtocolError
from senertec_dachs.errors import DachsError, RequestTimeoutError
from senertec_dachs.glt.modbus import ModbusError
from senertec_dachs.models import ReadingState

_HEADER = struct.Struct(">HHHB")


def _serial_words(serial: str) -> list[int]:
    raw = serial.encode("utf-8")[:20].ljust(20, b"\x00")
    return [(raw[i] << 8) | raw[i + 1] for i in range(0, 20, 2)]


def _u32(value: int) -> tuple[int, int]:
    return ((value >> 16) & 0xFFFF, value & 0xFFFF)


def _fixture_registers() -> dict[int, int]:
    """A consistent full fixture consistent with the spec tables."""
    regs: dict[int, int] = {}
    # identity
    regs[8000] = (2 << 8) | 4
    regs[8001] = (26 << 8) | 1
    words = _serial_words("2110123456789ABC")
    for offset, value in enumerate(words):
        regs[8002 + offset] = value
    regs[8012] = 5500
    # status
    regs[8013] = 2  # Läuft
    regs[8014] = 55  # 5.5 kW
    regs[8015] = 3  # Stromanforderung
    regs[8016] = 31  # 3.1 h
    regs[8017] = 3  # Fehler
    regs[8018] = 1  # pump Ein
    regs[8019] = 505  # 50.5 °C outlet
    regs[8020] = 448  # 44.8 °C inlet
    # configuration
    regs[8021] = 2
    regs[8022] = 30
    regs[8023] = 900
    regs[8024] = 1
    regs[8025] = 2
    regs[8026] = 1
    # operating data
    hi, lo = _u32(12657)
    regs[8027], regs[8028] = hi, lo
    hi, lo = _u32(4864)
    regs[8029], regs[8030] = hi, lo
    hi, lo = _u32(4300763)  # 430076.3 kWh
    regs[8031], regs[8032] = hi, lo
    hi, lo = _u32(10557500)  # 1055750.0 kWh
    regs[8033], regs[8034] = hi, lo
    hi, lo = _u32(1500)
    regs[8035], regs[8036] = hi, lo
    hi, lo = _u32(3000)
    regs[8037], regs[8038] = hi, lo
    hi, lo = _u32(8157)
    regs[8039], regs[8040] = hi, lo
    # system data
    regs[8041] = 228 - 65536 if False else 228  # 22.8 °C outdoor
    regs[8042] = 450  # buffer T1 45.0
    regs[8043] = 600  # 60.0
    regs[8044] = 312  # 31.2
    regs[8045] = 250  # 25.0
    regs[8046] = 0
    regs[8047] = 0
    for address, value in zip(range(8048, 8052), (0, 0, 0, 0)):
        regs[address] = value
    for offset, value in zip(range(8052, 8056), (150, 160, 170, 180)):
        regs[offset] = value  # 15.0/16.0/17.0/18.0 °C (F5..F8)
    regs[8056] = 40
    # plant configuration
    regs[8057] = 1
    regs[8058] = 500
    regs[8059] = 2
    regs[8060], regs[8061], regs[8062], regs[8063] = 25, 60, 95, 130
    regs[8064] = 2
    regs[8065] = 1
    # multi-module
    regs[8066] = 55
    regs[8067], regs[8068], regs[8069], regs[8070], regs[8071] = 1, 1, 0, 1, 1
    regs[8072] = 55
    # second heat generator
    regs[8073] = 0
    regs[8074] = 0
    hi, lo = _u32(100)
    regs[8075], regs[8076] = hi, lo
    hi, lo = _u32(450)
    regs[8077], regs[8078] = hi, lo
    regs[8079], regs[8080] = 1, 0
    regs[8081] = 240
    regs[8082] = 30
    regs[8083] = 1
    # write area mirror (readable on the fake for convenience)
    regs[8300] = 0
    regs[8301] = 0
    regs[8302] = 0
    return regs


class FakeGltServer:
    """Modbus TCP loopback with the documented function codes."""

    def __init__(self) -> None:
        self.registers = _fixture_registers()
        self.written: dict[int, list[int]] = {}
        self.exception_next: int | None = None
        self.short_next = False
        self.drop_next = False
        self.rst_next = False
        self.delay_next: float | None = None
        self._server: asyncio.AbstractServer | None = None
        self.host = "127.0.0.1"
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                head = await reader.readexactly(_HEADER.size)
                transaction, _proto, length, unit_id = _HEADER.unpack(head)
                body = await reader.readexactly(length - 1)
                function_code = body[0]
                payload = body[1:]
                if self.drop_next:
                    self.drop_next = False
                    break
                if self.delay_next:
                    await asyncio.sleep(self.delay_next)
                    self.delay_next = None
                if self.rst_next:
                    self.rst_next = False
                    writer.transport.abort()  # hard reset
                    return
                if self.exception_next is not None and function_code == 0x04:
                    code = self.exception_next
                    self.exception_next = None
                    answer_pdu = struct.pack(">BB", 0x84, code)
                    writer.write(_HEADER.pack(transaction, 0, len(answer_pdu) + 1, unit_id) + answer_pdu)
                    await writer.drain()
                    continue
                if function_code == 0x04:
                    address, quantity = struct.unpack(">HH", payload[:4])
                    if self.short_next:
                        self.short_next = False
                        words = [
                            self.registers.get(address + index, 0)
                            for index in range(quantity - 1)
                        ]
                    else:
                        words = [
                            self.registers.get(address + index, 0)
                            for index in range(quantity)
                        ]
                    byte_count = len(words) * 2
                    answer_pdu = struct.pack(">BB", 0x04, byte_count)
                    for value in words:
                        answer_pdu += struct.pack(">H", value)
                    writer.write(
                        _HEADER.pack(transaction, 0, len(answer_pdu) + 1, unit_id)
                        + answer_pdu
                    )
                    await writer.drain()
                    continue
                if function_code == 0x06:
                    address, value = struct.unpack(">HH", payload[:4])
                    self.written.setdefault(address, []).append(value)
                    self.registers[address] = value
                    writer.write(
                        _HEADER.pack(transaction, 0, len(body) + 1, unit_id) + body
                    )
                    await writer.drain()
                    continue
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    def writes(self, address: int) -> list[int]:
        return self.written.get(address, [])


@pytest.fixture()
async def glt_service():
    service = FakeGltServer()
    await service.start()
    try:
        yield service
    finally:
        await service.stop()


@pytest.fixture()
async def connected_glt(glt_service: FakeGltServer):
    client = GltClient(glt_service.host, glt_service.port)
    try:
        yield client
    finally:
        await client.close()


async def test_update_projects_shared_keys(connected_glt: GltClient) -> None:
    snapshot = await connected_glt.update()
    raw = dict(snapshot.raw)
    assert raw["Hka_Bd.ulBetriebssekunden"] == "12657"
    assert raw["Hka_Bd.ulAnzahlStarts"] == "4864"
    assert raw["Hka_Bd.ulArbeitElektr"] == "430076.3"
    assert raw["Hka_Bd.ulArbeitThermHka"] == "1055750"
    assert raw["Hka_Mw1.sWirkleistung"] == "5.5"
    assert raw["Hka_Mw1.ulMotorlaufsekunden"] == "3.1"
    assert raw["Hka_Mw1.Temp.sbAussen"] == "22.8"
    assert raw["Hka_Mw1.Temp.sbGen"] == "44.8"
    assert raw["Hka_Mw1.Temp.sbVorlauf"] == "50.5"
    assert raw["Hka_Bd.Anforderung.ModulAnzahl"] == "0"
    # properties that GLT does not expose stay honestly missing
    assert "Hka_Bd_Stat.ulInbetriebnahmedatum" not in raw
    assert "Hka_Bd.bStoerung" not in raw
    assert "Wartung_Cache.fStehtAn" not in raw
    assert snapshot.operating.fault_raw.state is ReadingState.MISSING
    assert snapshot.identity.commissioned_epoch_seconds.state is ReadingState.MISSING
    assert snapshot.maintenance.due.state is ReadingState.MISSING
    assert snapshot.daily_run[0].state is ReadingState.MISSING


async def test_typed_glt_values(connected_glt: GltClient) -> None:
    await connected_glt.update()
    profile = connected_glt.profile()
    assert profile.glt_version == "2.4"
    assert profile.device_type_name == "Dachs 5.5 (G5.5)"
    assert profile.serial_number == "2110123456789ABC"
    assert profile.rated_power_watts == 5500

    status = connected_glt.status()
    assert status.plant_status_raw == 2
    assert status.plant_status_text == "Läuft"
    assert status.request_kind_text == "Stromanforderung"
    assert status.shutdown_reason_text == "Fehler"
    assert status.electric_output_watts == Decimal("550")
    assert status.runtime_since_start_hours == Decimal("3.1")
    assert status.heating_pump_raw == 1

    counts = connected_glt.module_counts()
    assert counts.running == 1
    assert counts.requested == 0
    assert counts.total_power_kw == Decimal("5.5")

    second = connected_glt.second_generator()
    assert second.status_text == "Aus"
    assert second.available_text == "Ja"
    assert second.released_text == "Nein"
    assert second.rated_power_kw == Decimal("24")


async def test_signed_register_decode(
    glt_service: FakeGltServer, connected_glt: GltClient
) -> None:
    glt_service.registers[8041] = 0xFF9C  # -100 -> -10.0 °C
    await connected_glt.update()
    assert connected_glt.snapshot.temperatures.outdoor.value == Decimal("-10")


async def test_facade_integration(glt_service: FakeGltServer) -> None:
    device = Dachs.glt(glt_service.host, glt_service.port)
    try:
        snapshot = await device.update()
        assert device.device_online is True
        assert device.serial_number == "2110123456789ABC"
        assert device.runtime_hours == Decimal("12657")
        assert device.electrical_energy_kwh == Decimal("430076.3")
        assert device.engine_runtime_hours == Decimal("3.1")
        assert device.outdoor_temperature == Decimal("22.8")
        assert device.supply_temperature == Decimal("50.5")
        assert device.part_number is None
        assert device.commissioned_epoch_seconds is None
        assert device.power_w == 5500  # 5.5 kW -> exact watt conversion
        assert snapshot.raw["Hka_Mw1.sWirkleistung"] == "5.5"
    finally:
        await device.close()


async def test_factory_owns_and_shares_glt_client(
    glt_service: FakeGltServer,
) -> None:
    device = Dachs.glt(glt_service.host, glt_service.port)
    assert isinstance(device.client, GltClient)
    await device.update()
    await device.close()
    assert device.client.connected is False


async def test_modbus_exception_surfaces(glt_service: FakeGltServer) -> None:
    glt_service.exception_next = 0x02  # illegal data address
    client = GltClient(glt_service.host, glt_service.port)
    try:
        with pytest.raises(ModbusError) as error:
            await client.update()
        assert "Illegal data address" in str(error.value)
    finally:
        await client.close()


async def test_short_answer_rejected(glt_service: FakeGltServer) -> None:
    glt_service.short_next = True
    client = GltClient(glt_service.host, glt_service.port)
    try:
        with pytest.raises(ProtocolError):
            await client.update()
    finally:
        await client.close()


async def test_mid_frame_drop_raises_connection_error(
    glt_service: FakeGltServer,
) -> None:
    glt_service.drop_next = True
    client = GltClient(glt_service.host, glt_service.port)
    try:
        with pytest.raises(ConnectionError):
            await client.update()
    finally:
        await client.close()


async def test_hard_reset_raises_connection_error(
    glt_service: FakeGltServer,
) -> None:
    glt_service.rst_next = True
    client = GltClient(glt_service.host, glt_service.port)
    try:
        with pytest.raises((ConnectionError, OSError, asyncio.TimeoutError)):
            await client.update()
    finally:
        await client.close()


async def test_timeout_raises_request_timeout(glt_service: FakeGltServer) -> None:
    glt_service.delay_next = 0.5
    client = GltClient(glt_service.host, glt_service.port, timeout=0.1)
    try:
        with pytest.raises(RequestTimeoutError):
            await client.update()
    finally:
        await client.close()


async def test_write_setpoint_without_pin(
    glt_service: FakeGltServer, connected_glt: GltClient
) -> None:
    await connected_glt.set_power_setpoint(5500)
    # no pin configured -> only the setpoint register is written
    assert glt_service.writes(8300) == []
    assert glt_service.writes(8301) == [5500]


async def test_invalid_setpoints_rejected(connected_glt: GltClient) -> None:
    for bad in (5503, -10):
        with pytest.raises(ValueError):
            await connected_glt.set_power_setpoint(bad)
    with pytest.raises(ValueError):
        await connected_glt.set_pin(12345)


async def test_pin_then_setpoint_wire_shape(
    glt_service: FakeGltServer,
) -> None:
    client = GltClient(glt_service.host, glt_service.port, pin=1234)
    try:
        await client.set_power_setpoint(2500)
        assert glt_service.writes(8300) == [1234]
        assert glt_service.writes(8301) == [2500]
        await client.set_locked(True)
        assert glt_service.writes(8302) == [1]
    finally:
        await client.close()


async def test_pin_resent_after_connection_drop(
    glt_service: FakeGltServer,
) -> None:
    client = GltClient(glt_service.host, glt_service.port, pin=1234)
    try:
        await client.set_power_setpoint(1000)
        assert glt_service.writes(8300) == [1234]
        # simulate a connection drop: the unit forgets the pin
        await client._modbus.close()
        await client.set_power_setpoint(2000)
        assert glt_service.writes(8300) == [1234, 1234]
        assert glt_service.writes(8301) == [1000, 2000]
    finally:
        await client.close()


async def test_explicit_pin_write(glt_service: FakeGltServer) -> None:
    client = GltClient(glt_service.host, glt_service.port)
    try:
        await client.set_pin(4321)
        assert glt_service.writes(8300) == [4321]
    finally:
        await client.close()


async def test_heartbeat_rewrites_state(
    glt_service: FakeGltServer,
) -> None:
    client = GltClient(glt_service.host, glt_service.port, pin=1111)
    try:
        task = client.start_heartbeat(interval=0.01)
        # nothing written yet: ticks are honest no-ops
        await asyncio.sleep(0.05)
        assert glt_service.writes(8301) == []
        await client.set_power_setpoint(2000)
        assert glt_service.writes(8301) == [2000]
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(glt_service.writes(8301)) > 1
        assert glt_service.writes(8300) == [1111]
    finally:
        await client.close()


async def test_request_run_honest_failure(connected_glt: GltClient) -> None:
    from senertec_dachs.errors import WriteError

    with pytest.raises(WriteError) as error:
        await connected_glt.request_run()
    assert error.value.outcome_unknown is False


async def test_device_online_flips_false_on_failure(
    glt_service: FakeGltServer,
) -> None:
    device = Dachs.glt(glt_service.host, glt_service.port)
    try:
        await device.update()
        assert device.device_online is True
        glt_service.exception_next = 0x06  # slave busy
        with pytest.raises(DachsError):
            await device.update()
        assert device.device_online is False
    finally:
        await device.close()
