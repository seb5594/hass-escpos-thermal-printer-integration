# ruff: noqa: F821, UP037, N802
# mypy: disable-error-code="no-untyped-def,name-defined"
#
# This module deliberately has no ``from __future__ import annotations``:
# dbus-fast reads each D-Bus signature ("o", "s", "u", ...) from the method
# annotations, and postponed evaluation would wrap them in a second pair of
# quotes. Bluez also dictates the CamelCase method names of ``Agent1``.
"""The throw-away ``org.bluez.Agent1`` that answers pairing requests."""

from typing import Any

# Legacy PIN used when the user didn't supply one; printers almost always ship with it.
DEFAULT_AGENT_PIN = "0000"
# Largest passkey the Bluetooth specification allows (six digits).
MAX_PASSKEY = 999_999


def build_pairing_agent_class() -> type[Any]:
    """Return the agent service class (dbus-fast is imported lazily)."""
    from dbus_fast import DBusError  # noqa: PLC0415
    from dbus_fast.service import ServiceInterface, method  # noqa: PLC0415

    class PairingAgent(ServiceInterface):
        """Accepts every pairing request and answers PIN prompts with ``pin``."""

        def __init__(self, pin: str = DEFAULT_AGENT_PIN, device_path: str | None = None) -> None:
            super().__init__("org.bluez.Agent1")
            self.pin = pin
            self.device_path = device_path

        def _expect(self, device: str) -> None:
            """Refuse requests for any device other than the one being paired."""
            if self.device_path is not None and str(device) != self.device_path:
                raise DBusError("org.bluez.Error.Rejected", "Not the device being paired")

        @method()
        def Release(self):
            """Bluez dropped the agent; nothing to clean up."""

        @method()
        def RequestPinCode(self, device: "o") -> "s":
            self._expect(device)
            return self.pin

        @method()
        def DisplayPinCode(self, device: "o", pincode: "s"):
            """Printers have no display; nothing to show."""

        @method()
        def RequestPasskey(self, device: "o") -> "u":
            self._expect(device)
            # A passkey is a number of at most six digits; anything else can't be sent.
            if self.pin.isascii() and self.pin.isdecimal() and int(self.pin) <= MAX_PASSKEY:
                return int(self.pin)
            raise DBusError("org.bluez.Error.Rejected", "The PIN is not a valid passkey")

        @method()
        def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
            """Printers have no keypad; nothing to show."""

        @method()
        def RequestConfirmation(self, device: "o", passkey: "u"):
            """Numeric comparison: returning without an error accepts."""
            self._expect(device)

        @method()
        def RequestAuthorization(self, device: "o"):
            """Accept an incoming pairing request."""
            self._expect(device)

        @method()
        def AuthorizeService(self, device: "o", uuid: "s"):
            """Accept service authorization (e.g. Serial Port Profile)."""
            self._expect(device)

        @method()
        def Cancel(self):
            """Bluez cancelled the request in flight."""

    return PairingAgent
