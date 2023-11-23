"""Device communication library."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
import logging
from typing import Any, AsyncIterator
from uuid import UUID

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

COMMAND_LIGHT_ON_HEX = "84dd5042374150897ac82f39110968a8"
COMMAND_LIGHT_OFF_HEX = "79d1dba40919c246a8580ae7d11b7884"

_LOGGER = logging.getLogger(__name__)

UUID_RX = UUID("{d44bc439-abfd-45a2-b575-925416129600}")

DEVICE_NAME = "IDEAL_LED"
ANNOUNCE_PREFIX = b"HOODFJAR"
ANNOUNCE_MANUFACTURER = int.from_bytes(ANNOUNCE_PREFIX[0:2], "little")

class IdealLedError(Exception):
    pass

class IdealLedBleakError(IdealLedError):
    pass

class IdealLedTimeout(IdealLedError):
    pass


@dataclass(frozen=True)
class State:
    """Data received from characteristics."""

    light_on: bool = False
    after_cooking_fan_speed: int = 0
    after_cooking_on: bool = False
    carbon_filter_available: bool = False
    fan_speed: int = 0
    grease_filter_full: bool = False
    carbon_filter_full: bool = False
    dim_level: int = 0
    periodic_venting: int = 0
    periodic_venting_on: bool = False
    rssi: int = 0

    def replace_from_tx_char(self, databytes: bytes, **changes: Any):
        """Update state based on tx characteristics."""
        data = databytes.decode("ASCII")
        return replace(
            self,
            fan_speed=int(data[4]),
            light_on=data[5] == "L",
            after_cooking_on=data[6] == "N",
            carbon_filter_available=data[7] == "C",
            grease_filter_full=data[8] == "F",
            carbon_filter_full=data[9] == "K",
            dim_level=_range_check_dim(int(data[10:13]), self.dim_level),
            periodic_venting=_range_check_period(
                int(data[13:15]), self.periodic_venting
            ),
            **changes
        )

    def replace_from_manufacture_data(self, data: bytes, **changes: Any):
        """Update state based on broadcasted data."""
        light_on = _bittest(data[10], 0)
        dim_level = _range_check_dim(data[13], self.dim_level)
        if light_on and not self.light_on and dim_level < self.dim_level:
            light_on = False

        return replace(
            self,
            fan_speed=int(data[8]),
            after_cooking_fan_speed=int(data[9]),
            light_on=light_on,
            after_cooking_on=_bittest(data[10], 1),
            periodic_venting_on=_bittest(data[10], 2),
            grease_filter_full=_bittest(data[11], 0),
            carbon_filter_full=_bittest(data[11], 1),
            carbon_filter_available=_bittest(data[11], 2),
            dim_level=dim_level,
            periodic_venting=_range_check_period(data[14], self.periodic_venting),
            **changes
        )


def _range_check_dim(value: int, fallback: int):
    if value >= 0 and value <= 100:
        return value
    else:
        return fallback


def _range_check_period(value: int, fallback: int):
    if value >= 0 and value < 60:
        return value
    else:
        return fallback


def _bittest(data: int, bit: int):
    return (data & (1 << bit)) != 0


def device_filter(device: BLEDevice, advertisement_data: AdvertisementData) -> bool:
    uuids = advertisement_data.service_uuids
    if str(UUID_SERVICE) in uuids:
        return True

    if device.name == DEVICE_NAME:
        return True

    manufacturer_data = advertisement_data.manufacturer_data.get(ANNOUNCE_MANUFACTURER, b'')
    if manufacturer_data.startswith(ANNOUNCE_PREFIX[2:]):
        return True

    return False


class Device:
    """Communication handler."""


    def __init__(self, address: str, keycode=b"1234") -> None:
        """Initialize handler."""
        self.address = address
        self._keycode = keycode
        self.state = State()
        self._lock = asyncio.Lock()
        self._client: BleakClient | None = None
        self._client_count = 0
        self._client_stack = AsyncExitStack()

    @asynccontextmanager
    async def connect(self, address_or_ble_device: BLEDevice | str | None = None) -> AsyncIterator[Device]:
        if address_or_ble_device is None:
            address_or_ble_device = self.address

        async with self._lock:
            if not self._client:
                _LOGGER.debug("Connecting")
                try:
                    self._client = await self._client_stack.enter_async_context(BleakClient(address_or_ble_device, timeout=30))
                except asyncio.TimeoutError as exc:
                    _LOGGER.debug("Timeout on connect", exc_info=True)
                    raise IdealLedTimeout("Timeout on connect") from exc
                except BleakError as exc:
                    _LOGGER.debug("Error on connect", exc_info=True)
                    raise IdealLedBleakError("Error on connect") from exc
            else:
                _LOGGER.debug("Connection reused")
            self._client_count += 1

        try:
            async with self._lock:
                yield self
        finally:
            async with self._lock:
                self._client_count -= 1
                if self._client_count == 0:
                    self._client = None
                    _LOGGER.debug("Disconnected")
                    await self._client_stack.pop_all().aclose()

    def characteristic_callback(self, data: bytearray):
        """Handle callback on characteristic change."""
        _LOGGER.debug("Characteristic callback: %s", data)

        if data[0:4] != self._keycode:
            _LOGGER.warning("Wrong keycode in data %s", data)
            return

        self.state = self.state.replace_from_tx_char(data)

        _LOGGER.debug("Characteristic callback result: %s", self.state)

    def detection_callback(self, device: BLEDevice, advertisement_data: AdvertisementData):
        """Handle scanner data."""
        data = advertisement_data.manufacturer_data.get(ANNOUNCE_MANUFACTURER)
        if data is None:
            return
        # Recover full manufacturer data. It's breaking standard by
        # not providing a manufacturer prefix here.
        data = ANNOUNCE_PREFIX[0:2] + data
        self.detection_callback_raw(data, advertisement_data.rssi)

    def detection_callback_raw(self, data: bytes, rssi: int):

        if data[0:8] != ANNOUNCE_PREFIX:
            _LOGGER.debug("Missing key in manufacturer data %s", data)
            return

        self.state = self.state.replace_from_manufacture_data(data, rssi=rssi)

        _LOGGER.debug("Detection callback result: %s", self.state)

    async def update(self):
        """Update internal state."""
        assert self._client, "Device must be connected"
        try:
            databytes = await self._client.read_gatt_char(UUID_RX)
        except asyncio.TimeoutError as exc:
            _LOGGER.debug("Timeout on update", exc_info=True)
            raise IdealLedTimeout from exc
        except BleakError as exc:
            _LOGGER.debug("Failed to update", exc_info=True)
            raise IdealLedBleakError("Failed to update device") from exc

        self.characteristic_callback(databytes)

    async def send_command(self, cmd: str):
        """Send given command."""
        assert self._client, "Device must be connected"
        data = bytearray.fromhex(cmd)
        # data = self._keycode + cmd.encode("ASCII")
        try:
            await self._client.write_gatt_char(UUID_RX, data, True)
        except asyncio.TimeoutError as exc:
            _LOGGER.debug("Timeout on write", exc_info=True)
            raise IdealLedTimeout from exc
        except BleakError as exc:
            _LOGGER.debug("Failed to write", exc_info=True)
            raise IdealLedBleakError("Failed to write") from exc

        if cmd == COMMAND_LIGHT_ON_HEX:
            self.state = replace(self.state, light_on=True)
        elif cmd == COMMAND_LIGHT_OFF_HEX:
            self.state = replace(self.state, light_on=False)

    # async def send_dim(self, level: int):
    #     """Ask to dim to a certain level."""
    #     await self.send_command(COMMAND_FORMAT_DIM.format(level))
    #     self.state = replace(self.state, dim_level=level)
