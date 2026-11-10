"""Minimal asynchronous Modbus TCP transport for the Dachs Gen2 GLT.

Derived from the interface spec ``8098.119.000.02`` §4.3/§6: Modbus TCP
on port 502, big-endian byte order, function code 0x04 (read input
registers) and 0x06 (write single register). No third-party Modbus
library — the two function codes the Dachs speaks need roughly a
hundred lines and remove a heavy dependency from a device-access
library.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from dataclasses import dataclass

from ..errors import ConnectionError, DachsError, ProtocolError, RequestTimeoutError

_HEADER = struct.Struct(">HHHB")
_ADDR_QTY = struct.Struct(">HH")
_ADDR_VAL = struct.Struct(">HH")

_FIRST_READ_REGISTER = 0x04
_FIRST_WRITE_REGISTER = 0x06

_MODBUS_EXCEPTIONS = {
    0x01: "Illegal function (the device does not support this function code)",
    0x02: "Illegal data address (the register address does not exist)",
    0x03: "Illegal data value (quantity or value rejected)",
    0x04: "Slave device failure (controller error while executing)",
    0x05: "Acknowledge (processing, answer will follow)",
    0x06: "Slave device busy (retry later)",
    0x08: "Memory parity error",
    0x0A: "Gateway path unavailable",
    0x0B: "Gateway target device failed to respond",
}


@dataclass(frozen=True)
class ModbusError(DachsError):
    """A device-side Modbus exception answer.

    Carries the raw function code and exception code of the answer
    frame; ``str`` names the standard meaning. The controller rejected
    the request itself — the cause is :attr:`exception_code`, not the
    frame bytes.
    """

    function_code: int
    exception_code: int

    def __str__(self) -> str:
        meaning = _MODBUS_EXCEPTIONS.get(
            self.exception_code, f"unknown exception {self.exception_code:#x}"
        )
        return f"Modbus exception {self.exception_code:#x} in FC {self.function_code:#x}: {meaning}"


class ModbusTcpClient:
    """An asynchronous Modbus TCP connection to the GLT interface.

    Supported: FC 0x04 read input registers and FC 0x06 write single
    register — exactly the set the Dachs Gen2 documents. Requests are
    serialized (one transaction in flight, an ``asyncio.Lock``), the
    transaction id is echoed and matched before decoding, and answers
    are length-checked.
    """

    def __init__(
        self,
        host: str,
        port: int = 502,
        *,
        unit_id: int = 1,
        timeout: float = 4.0,
    ) -> None:
        self._host = host
        self._port = port
        self._unit_id = unit_id
        self._timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._transaction = 0

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def _ensure_socket(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._reader is not None and self._writer is not None:
            if not self._writer.is_closing():
                return self._reader, self._writer
            await self.close()
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=self._timeout,
            )
        except (OSError, asyncio.TimeoutError) as error:
            self._reader = None
            self._writer = None
            raise ConnectionError(
                f"Could not open the GLT Modbus TCP connection to "
                f"{self._host}:{self._port}: {error}"
            ) from error
        return self._reader, self._writer

    async def close(self) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=2.0)

    async def _transact(self, pdu: bytes) -> tuple[int, bytes]:
        """One request/answer round with strict answer checking.

        An exception answer or a broken frame closes the connection:
        the device answered wrong and the safest read is a fresh frame
        on a fresh socket.
        """
        async with self._lock:
            reader, writer = await self._ensure_socket()
            self._transaction = (self._transaction + 1) & 0xFFFF
            transaction = self._transaction
            writer.write(_HEADER.pack(transaction, 0, len(pdu) + 1, self._unit_id) + pdu)
            await writer.drain()

            try:
                head = await asyncio.wait_for(
                    reader.readexactly(_HEADER.size), timeout=self._timeout,
                )
            except asyncio.IncompleteReadError as error:
                await self.close()
                raise ConnectionError(
                    f"The GLT Modbus connection closed mid-header "
                    f"(received {len(error.partial)} bytes)"
                ) from error
            except asyncio.TimeoutError as error:
                raise RequestTimeoutError(
                    f"The GLT Modbus device did not answer transaction "
                    f"{transaction} within {self._timeout}s"
                ) from error
            answer_tid, _pid, length, _unit = _HEADER.unpack(head)
            if length < 2 or length > 254:
                await self.close()
                raise ProtocolError(
                    f"Modbus answer with impossible length byte {length}"
                )
            try:
                tail = await asyncio.wait_for(
                    reader.readexactly(length - 1), timeout=self._timeout,
                )
            except asyncio.IncompleteReadError as error:
                await self.close()
                raise ConnectionError(
                    f"The GLT Modbus connection closed mid-answer "
                    f"(received {len(error.partial)} bytes)"
                ) from error
            except asyncio.TimeoutError as error:
                raise RequestTimeoutError(
                    f"The GLT Modbus device cut the answer of transaction "
                    f"{transaction} mid-frame"
                ) from error
            # tail = function code byte + payload
            if answer_tid != transaction:
                await self.close()
                raise ProtocolError(
                    f"Modbus answer transaction id {answer_tid} does not "
                    f"match request {transaction}"
                )
            function_code = tail[0]
            payload = tail[1:]
            if function_code & 0x80:
                await self.close()
                if not payload:
                    raise ProtocolError("Modbus exception answer without a code")
                raise ModbusError(function_code & 0x7F, payload[0])
            return function_code, payload

    async def read_input_registers(
        self, address: int, quantity: int
    ) -> list[int]:
        """FC 0x04 — read ``quantity`` input registers from ``address``."""
        if not 1 <= quantity <= 125:
            raise ValueError("Modbus FC 0x04 reads 1..125 registers")
        _, payload = await self._transact(
            struct.pack(">B", _FIRST_READ_REGISTER)
            + _ADDR_QTY.pack(address, quantity)
        )
        if not payload:
            raise ProtocolError("Modbus FC 0x04 answer without byte count")
        byte_count = payload[0]
        body = payload[1:]
        if byte_count != quantity * 2 or len(body) != byte_count:
            await self.close()
            raise ProtocolError(
                f"Modbus FC 0x04 announced {byte_count} bytes but delivered "
                f"{len(body)} (requested {quantity} registers)"
            )
        return [
            (body[i] << 8) | body[i + 1]
            for i in range(0, len(body), 2)
        ]

    async def write_register(self, address: int, value: int) -> None:
        """FC 0x06 — write ``value`` into register ``address``.

        The echo of the request is the only acknowledgement; whether
        the device logically accepted the value is outside the frame
        (see :meth:`senertec_dachs.glt.client.GltClient.set_power_setpoint`).
        """
        if not 0 <= value <= 0xFFFF:
            raise ValueError("Modbus FC 0x06 writes values 0..65535")
        request_pdu = (
            struct.pack(">B", _FIRST_WRITE_REGISTER)
            + _ADDR_VAL.pack(address, value)
        )
        function_code, payload = await self._transact(request_pdu)
        if not payload:
            await self.close()
            raise ProtocolError("Modbus FC 0x06 answer was dropped by the device")
        if payload != request_pdu[1:] or function_code != _FIRST_WRITE_REGISTER:
            await self.close()
            raise ProtocolError(
                f"Modbus FC 0x06 echo mismatch: wanted {request_pdu[1:]!r}, "
                f"got FC {function_code:#x} {payload!r}"
            )
