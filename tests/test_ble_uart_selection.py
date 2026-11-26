"""The GATT layout of a Fichero D11s (item 5836 / 3212141), as captured with a BLE scanner."""

from types import SimpleNamespace

from bleak.uuids import normalize_uuid_str

from custom_components.escpos_printer.capabilities.ble_profiles import get_ble_profiles
from custom_components.escpos_printer.printer.ble_transport import _uart

WRITE = ("write", "write-without-response")
NOTIFY = ("notify",)

# Services in the order the printer lists them, with the properties it reports.
D11S_LAYOUT = {
    "1801": {},
    "1800": {"2a00": ("read", "write"), "2a01": ("read", "write"), "2a04": ("read",)},
    "18f0": {"2af0": NOTIFY, "2af1": WRITE},
    "ff00": {"ff01": NOTIFY, "ff02": WRITE, "ff03": NOTIFY},
    "e7810a71-73ae-499d-8c15-faa9aef0c3f2": {
        "bef8d6c9-9c21-4c9e-b632-bd58c1009f9f": ("notify", *WRITE),
    },
    "49535343-fe7d-4ae5-8fa9-9fafd205e455": {
        "49535343-1e4d-4bd9-ba61-23c647249616": NOTIFY,
        "49535343-8841-43f4-a8d4-ecbe34729bb3": WRITE,
        "49535343-aca3-481c-91ec-d85e28a60318": ("notify", "write"),
    },
}


def _client(layout):
    def service(characteristics):
        by_uuid = {
            normalize_uuid_str(uuid): SimpleNamespace(
                uuid=normalize_uuid_str(uuid), properties=list(props)
            )
            for uuid, props in characteristics.items()
        }
        return SimpleNamespace(get_characteristic=by_uuid.get)

    return SimpleNamespace(services=[service(chars) for chars in layout.values()])


def test_d11s_uses_the_ff00_uart():
    """FF02 takes the print data and FF01 sends the replies, both on the FF00 service."""
    write, notify = _uart(_client(D11S_LAYOUT))
    assert write.uuid == normalize_uuid_str("ff02")
    assert notify.uuid == normalize_uuid_str("ff01")


def test_the_other_uarts_work_when_ff00_is_missing():
    without_ff00 = {uuid: chars for uuid, chars in D11S_LAYOUT.items() if uuid != "ff00"}
    write, notify = _uart(_client(without_ff00))
    assert (write.uuid, notify.uuid) == (normalize_uuid_str("2af1"), normalize_uuid_str("2af0"))


def test_the_ble_name_of_the_d11s_is_recognized():
    """The printer advertises as FICHERO_5836_BLE over BLE and as FICHERO_5836 on Classic."""
    profile = get_ble_profiles()["fichero_label_d11s"]
    assert profile.matches_advertisement("FICHERO_5836_BLE")
