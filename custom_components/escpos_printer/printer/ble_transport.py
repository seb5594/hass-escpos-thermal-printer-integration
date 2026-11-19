"""Async GATT transport for LuckPrinter raster jobs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from types import ModuleType
from typing import TYPE_CHECKING

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakError
from bleak.uuids import normalize_uuid_str
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.exceptions import HomeAssistantError

from ..const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from ..capabilities.ble_profiles import BlePrinterProfile

# Characteristic pairs exposed by LuckPrinter firmware. Use the pair on the
# same service so replies arrive on the UART used for writes.
_UARTS = (
    ("ff02", "ff01"),
    ("2af1", "2af0"),
    ("bef8d6c9-9c21-4c9e-b632-bd58c1009f9f", "bef8d6c9-9c21-4c9e-b632-bd58c1009f9f"),
    ("49535343-8841-43f4-a8d4-ecbe34729bb3", "49535343-1e4d-4bd9-ba61-23c647249616"),
)
BLE_PRINTER_SERVICES = frozenset(
    normalize_uuid_str(uuid)
    for uuid in (
        "ff00",
        "18f0",
        "e7810a71-73ae-499d-8c15-faa9aef0c3f2",
        "49535343-fe7d-4ae5-8fa9-9fafd205e455",
    )
)
BLE_ERRORS = (BleakError, OSError, TimeoutError)
_DEFAULT_ATT_PAYLOAD = 20
_MAX_CHUNK = 200
_REPLY_TIMEOUT = 5
_PRINT_TIMEOUT = 60
_FATAL_STATUS = 0x56  # Cover open, paper out, or printhead overheated.


class BlePrinterMismatchError(Exception):
    """The printer's GATT service or reported model does not match the profile."""


def _uart(
    client: BleakClientWithServiceCache,
) -> tuple[BleakGATTCharacteristic, BleakGATTCharacteristic]:
    for write_uuid, notify_uuid in _UARTS:
        for service in client.services:
            write = service.get_characteristic(normalize_uuid_str(write_uuid))
            notify = service.get_characteristic(normalize_uuid_str(notify_uuid))
            if (
                write is not None
                and notify is not None
                and {"write", "write-without-response"}.intersection(write.properties)
                and {"notify", "indicate"}.intersection(notify.properties)
            ):
                return write, notify
    raise BlePrinterMismatchError


def _match_ack(data: bytes) -> bool:
    if len(data) >= 2 and data[0] == 0xFF:
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="ble_printer_error")
    return data.startswith((b"\xaa", b"OK"))


class BlePrinterLink:
    """One BLE UART connection with serialized requests and paced GATT writes."""

    def __init__(self, client: BleakClientWithServiceCache, write: BleakGATTCharacteristic) -> None:
        self._client = client
        self._write = write
        self._with_response = "write-without-response" not in write.properties
        self._notifications: asyncio.Queue[bytes] = asyncio.Queue(maxsize=32)

    def received(self, _characteristic: BleakGATTCharacteristic, data: bytearray) -> None:
        """Queue a notification for the active request."""
        if not self._notifications.full():
            self._notifications.put_nowait(bytes(data))

    async def write(self, payload: bytes) -> None:
        """Write bounded ATT chunks, pacing writes without response."""
        size = _DEFAULT_ATT_PAYLOAD
        if not self._with_response:
            size = max(1, min(_MAX_CHUNK, self._write.max_write_without_response_size))
        for offset in range(0, len(payload), size):
            async with asyncio.timeout(_REPLY_TIMEOUT):
                await self._client.write_gatt_char(
                    self._write, payload[offset : offset + size], response=self._with_response
                )
            if not self._with_response:
                await asyncio.sleep(0.02)

    async def request(
        self, command: bytes, accepts: Callable[[bytes], bool], timeout: float = _REPLY_TIMEOUT
    ) -> bytes:
        """Send a command and wait for a reply matching it."""
        while not self._notifications.empty():
            self._notifications.get_nowait()
        await self.write(command)
        async with asyncio.timeout(timeout):
            previous = b""
            while True:
                response = await self._notifications.get()
                if accepts(response):
                    return response
                if previous == b"O" and response == b"K" and accepts(b"OK"):
                    return b"OK"
                previous = response

    async def read_model(self) -> str:
        """Read the model name without altering printer settings."""

        def is_name(data: bytes) -> bool:
            name = data.strip(b"\x00\r\n ")
            return len(name) > 2 and name.isascii() and name.decode().isprintable()

        response = await self.request(b"\x10\xff\x20\xf0", is_name)
        return response.decode("ascii").strip("\x00\r\n ")

    async def print_job(
        self, profile: BlePrinterProfile, raster: bytes, feed_dots: int, density: int | None
    ) -> None:
        """Send one raster job and wait for the printer to acknowledge completion."""
        (status,) = await self.request(
            b"\x10\xff\x40", lambda data: len(data) == 1 and data[0] < 0x80
        )
        if status & _FATAL_STATUS:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="ble_printer_error")
        if density is not None:
            await self.request(b"\x10\xff\x10\x00" + bytes([density]), _match_ack)

        aiyin = profile.dialect == "aiyin"
        if aiyin:
            await self.request(b"\x10\xff\x84\x00", _match_ack)
            await self.write(bytes(12))
            await self.write(b"\x10\xff\xfe\x01")
        else:
            await self.write(b"\x10\xff\xf1\x03")
            await self.write(bytes(12))

        if raster:
            await self.write(raster)
            await asyncio.sleep(0.5)
        if profile.label_rows:
            await self.write(b"\x1d\x0c")
            await asyncio.sleep(0.3)
        else:
            for offset in range(0, feed_dots, 255):
                await self.write(b"\x1bJ" + bytes([min(255, feed_dots - offset)]))

        stop = b"\x10\xff\xfe\x45" if aiyin else b"\x10\xff\xf1\x45"
        await self.request(stop, _match_ack, _PRINT_TIMEOUT)


def bluetooth_api(hass: HomeAssistant) -> ModuleType | None:
    """Access Home Assistant's Bluetooth API only when the integration is loaded."""
    if "bluetooth" not in hass.config.components:
        return None
    from homeassistant.components import bluetooth  # noqa: PLC0415

    return bluetooth


@asynccontextmanager
async def open_ble_printer(
    hass: HomeAssistant, mac: str, timeout: float
) -> AsyncIterator[BlePrinterLink]:
    """Connect through Home Assistant's active adapter or Bluetooth proxy."""
    bluetooth = bluetooth_api(hass)
    device = (
        bluetooth.async_ble_device_from_address(hass, mac, connectable=True) if bluetooth else None
    )
    if device is None:
        raise BleakError("BLE printer is not visible to a connectable scanner")
    async with asyncio.timeout(timeout):
        client = await establish_connection(
            BleakClientWithServiceCache, device, "BLE printer", max_attempts=2
        )
    try:
        write, notify = _uart(client)
        link = BlePrinterLink(client, write)
        async with asyncio.timeout(_REPLY_TIMEOUT):
            await client.start_notify(notify, link.received)
        yield link
    finally:
        with suppress(*BLE_ERRORS):
            async with asyncio.timeout(_REPLY_TIMEOUT):
                await client.disconnect()
