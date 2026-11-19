"""Config flow and print-job regressions for Bluetooth LE."""

from contextlib import asynccontextmanager
from struct import unpack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bleak.exc import BleakError
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from PIL import Image
import pytest

from custom_components.escpos_printer import _bluetooth_config
from custom_components.escpos_printer._config_flow import ble_steps
from custom_components.escpos_printer.capabilities.ble_profiles import BlePrinterProfile
from custom_components.escpos_printer.const import CONF_CONNECTION_TYPE, DOMAIN
from custom_components.escpos_printer.device_action.capabilities import (
    async_get_action_capabilities,
    async_get_actions,
)
from custom_components.escpos_printer.diagnostics import async_get_config_entry_diagnostics
from custom_components.escpos_printer.printer import (
    ble_adapter,
    ble_transport,
    create_printer_adapter,
)
from custom_components.escpos_printer.printer.ble_adapter import BleRasterJob
from custom_components.escpos_printer.printer.ble_transport import BlePrinterLink
from custom_components.escpos_printer.sample_print import async_print_sample

_MAC = "AA:BB:CC:DD:EE:FF"
_MODEL = BlePrinterProfile(
    name="Pocket printer", width=384, dialect="lujiang", model_names=("DP_D1",)
)


async def test_ble_config_flow_and_print_service(hass):  # type: ignore[no-untyped-def]
    """Discover an unnamed BLE device, reject the wrong firmware, then print a job."""
    api = Mock()
    api.async_discovered_service_info.return_value = [
        SimpleNamespace(address=_MAC, name="Unknown", service_uuids=[])
    ]
    link = AsyncMock()
    link.read_model.side_effect = ["D11s", "DP-D1"]

    @asynccontextmanager
    async def open_link(*args):
        yield link

    with (
        patch.object(ble_steps, "bluetooth_api", return_value=api),
        patch.object(ble_steps, "get_ble_profiles", return_value={"pocket": _MODEL}),
        patch.object(ble_steps, "open_ble_printer", open_link),
        patch.object(ble_adapter, "open_ble_printer", open_link),
        patch("custom_components.escpos_printer.get_ble_profiles", return_value={"pocket": _MODEL}),
    ):
        flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        flow_id = flow["flow_id"]
        flow = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_CONNECTION_TYPE: "bluetooth_le"}
        )
        assert flow["step_id"] == "bluetooth_le"
        data = {"ble_profile": "pocket", "bt_mac": "__refresh__"}
        refreshed = await hass.config_entries.flow.async_configure(flow_id, data)
        assert refreshed["step_id"] == "bluetooth_le"
        selector = refreshed["data_schema"].schema["bt_mac"]
        assert selector.config["options"][0]["value"] == _MAC
        data["bt_mac"] = _MAC.lower()
        mismatch = await hass.config_entries.flow.async_configure(flow_id, data)
        assert mismatch["errors"] == {"base": "ble_model_mismatch"}
        result = await hass.config_entries.flow.async_configure(flow_id, data)
        assert result["type"] == "create_entry"
        assert result["data"]["bt_mac"] == _MAC
        assert result["data"]["line_width"] == 32
        entry = result["result"]
        assert entry.unique_id == f"bt:{_MAC.lower()}"
        await hass.async_block_till_done()
        assert isinstance(entry.runtime_data.adapter, ble_adapter.BlePrinterAdapter)

        await hass.services.async_call(DOMAIN, "print_text", {"text": "Grüße"}, blocking=True)
        profile, raster, feed, density = link.print_job.await_args.args
        assert profile is _MODEL
        assert raster.startswith(b"\x1dv0\x00\x30\x00")
        assert any(raster[8:])
        assert feed == 80
        assert density is None
        assert entry.runtime_data.adapter.get_diagnostics()["last_print"] is not None

        await async_print_sample(hass, entry)
        diag = await async_get_config_entry_diagnostics(hass, entry)
        assert diag["entry"]["data"]["ble_profile"] == "pocket"
        assert _MAC not in str(diag)
        (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        actions = await async_get_actions(hass, device.id)
        assert {"beep", "cut"}.isdisjoint(action["type"] for action in actions)
        caps = await async_get_action_capabilities(
            hass, next(action for action in actions if action["type"] == "print_text")
        )
        assert caps["extra_fields"]({"text": "Test", "cut": "none"})["cut"] == "none"

        reconfigure = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
        )
        assert reconfigure["step_id"] == "bluetooth_le"
        link.read_model.side_effect = None
        link.read_model.return_value = "DP-D1"
        result = await hass.config_entries.flow.async_configure(reconfigure["flow_id"], data)
        assert result["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()

        options = await hass.config_entries.options.async_init(entry.entry_id)
        assert options["step_id"] == "ble_settings"
        result = await hass.config_entries.options.async_configure(
            options["flow_id"], {"status_interval": 30}
        )
        assert result["errors"] == {"base": "bt_status_interval_too_low"}
        result = await hass.config_entries.options.async_configure(
            options["flow_id"], {"status_interval": 0}
        )
        assert result["type"] == "create_entry"
        await hass.async_block_till_done()


async def test_ble_job_failure_does_not_mark_success(hass):  # type: ignore[no-untyped-def]
    """A lost BLE connection cannot record a successful print."""
    config = await _bluetooth_config(
        hass,
        SimpleNamespace(data={"bt_mac": _MAC, "ble_profile": "luckprinter_384"}),
        {},
    )
    adapter = create_printer_adapter(config)
    link = AsyncMock()
    link.print_job.side_effect = BleakError("connection lost")

    @asynccontextmanager
    async def open_link(*args):
        yield link

    with (
        patch.object(ble_adapter, "open_ble_printer", open_link),
        pytest.raises(HomeAssistantError, match="ble_print_failed"),
    ):
        await adapter.print_text(hass, text="Test print")
    assert adapter.get_diagnostics()["last_print"] is None


@pytest.mark.parametrize(("dialect", "width", "rows"), [("lujiang", 384, 0), ("aiyin", 96, 240)])
async def test_ble_wire_protocol(dialect, width, rows):  # type: ignore[no-untyped-def]
    """Chunk writes to the advertised limit and use the matching job commands."""
    profile = BlePrinterProfile(name=dialect, width=width, dialect=dialect, label_rows=rows)
    writes = []
    char = SimpleNamespace(
        properties=["write-without-response"], max_write_without_response_size=20
    )
    client = AsyncMock()
    link = BlePrinterLink(client, char)

    async def write_reply(characteristic, payload, *, response):
        assert characteristic is char
        assert len(payload) <= 20
        assert response is False
        writes.append(payload)
        replies = {
            b"\x10\xff\x20\xf0": [b"DP_D1\x00"],
            b"\x10\xff\x40": [b"\x00"],
            b"\x10\xff\x84\x00": [b"OK"],
            b"\x10\xff\x10\x00\x01": [b"OK"],
            b"\x10\xff\xf1\x45": [b"O", b"K"],
            b"\x10\xff\xfe\x45": [b"\xaa"],
        }
        for reply in replies.get(payload, []):
            link.received(characteristic, bytearray(reply))

    client.write_gatt_char.side_effect = write_reply
    assert await link.read_model() == "DP_D1"

    job = BleRasterJob(profile)
    with Image.new("L", (width, 1), 255) as image:
        image.putpixel((0, 0), 0)
        job.image(image)
    raster = job.raster()
    assert unpack("<HH", raster[4:8]) == (width // 8, 1)
    assert raster[8:] == bytes([0x80]) + bytes(width // 8 - 1)

    await link.print_job(profile, raster, job.paper_feed(), 1)
    assert writes[:3] == [b"\x10\xff\x20\xf0", b"\x10\xff\x40", b"\x10\xff\x10\x00\x01"]
    if dialect == "aiyin":
        assert writes[3:] == [
            b"\x10\xff\x84\x00",
            bytes(12),
            b"\x10\xff\xfe\x01",
            raster,
            b"\x1d\x0c",
            b"\x10\xff\xfe\x45",
        ]
    else:
        assert writes[3:5] == [b"\x10\xff\xf1\x03", bytes(12)]
        assert b"".join(writes[5:-2]) == raster
        assert writes[-2:] == [b"\x1bJP", b"\x10\xff\xf1\x45"]


async def test_gatt_connection_uses_matching_characteristics_and_disconnects(hass):  # type: ignore[no-untyped-def]
    """Home Assistant may route a print through a proxy with several other GATT services."""
    api = Mock()
    api.async_ble_device_from_address.return_value = device = object()
    writer = SimpleNamespace(properties=["write"])
    notifier = SimpleNamespace(properties=["notify"])
    service = Mock()
    service.get_characteristic.side_effect = {
        "0000ff02-0000-1000-8000-00805f9b34fb": writer,
        "0000ff01-0000-1000-8000-00805f9b34fb": notifier,
    }.get
    client = AsyncMock()
    client.services = [service]

    with (
        patch.object(ble_transport, "bluetooth_api", return_value=api),
        patch.object(ble_transport, "establish_connection", return_value=client) as connect,
    ):
        async with ble_transport.open_ble_printer(hass, _MAC, 1) as link:
            client.start_notify.assert_awaited_once_with(notifier, link.received)
            await link.write(bytes(30))
    api.async_ble_device_from_address.assert_called_once_with(hass, _MAC, connectable=True)
    assert connect.await_args.args[1] is device
    assert [call.args[1] for call in client.write_gatt_char.await_args_list] == [
        bytes(20),
        bytes(10),
    ]
    assert all(call.kwargs["response"] for call in client.write_gatt_char.await_args_list)
    client.disconnect.assert_awaited_once()


def test_ble_label_bounds_and_body_spacing():
    """Oversized content fails before transmission and batch feeds stay ordered."""
    job = BleRasterJob(BlePrinterProfile(name="Label", width=96, dialect="aiyin", label_rows=240))
    job.ln(1)
    with Image.new("L", (96, 1), 0) as image:
        job.image(image)
    raster = job.raster()
    assert unpack("<HH", raster[4:8]) == (12, 25)
    assert raster[8:296] == bytes(288)
    assert raster[296:] == b"\xff" * 12

    with (
        Image.new("L", (96, 216), 0) as image,
        pytest.raises(HomeAssistantError, match="ble_job_too_long"),
    ):
        job.image(image)
    with (
        Image.new("L", (97, 1), 0) as image,
        pytest.raises(HomeAssistantError, match="ble_image_too_wide"),
    ):
        job.image(image)


async def test_ble_batch_rasterizes_qr_barcode_and_styled_text(hass):  # type: ignore[no-untyped-def]
    """Each service uses the same raster path, with alignment and software barcode output."""
    config = await _bluetooth_config(
        hass,
        SimpleNamespace(data={"bt_mac": _MAC, "ble_profile": "luckprinter_384"}),
        {},
    )
    adapter = create_printer_adapter(config)
    link = AsyncMock()

    @asynccontextmanager
    async def open_link(*args):
        yield link

    with patch.object(ble_adapter, "open_ble_printer", open_link):
        await adapter.print_text(
            hass,
            text="Ä",
            align="right",
            bold=True,
            underline="double",
            width=2,
            height=3,
            invert=True,
            density=6,
            font="b",
        )
        assert link.print_job.await_args.args[3] == 2
        await adapter.print_qr(hass, data="https://example.com", align="center")
        assert any(link.print_job.await_args.args[1][8:])
        await adapter.print_barcode(hass, code="123456", bc="CODE128", width=2)
        assert any(link.print_job.await_args.args[1][8:])
        await adapter.feed(hass, lines=2)
        assert link.print_job.await_args.args[1:3] == (b"", 48)

    with pytest.raises(HomeAssistantError, match="ble_unsupported"):
        await adapter.beep(hass)
    with pytest.raises(HomeAssistantError, match="ble_unsupported"):
        await adapter.cut(hass, mode="full")


async def test_ble_gatt_rejects_wrong_service_and_bad_status(hass):  # type: ignore[no-untyped-def]
    """Skip mock GATT services with no UART and reject open-cover status before printing."""
    assert ble_transport.bluetooth_api(hass) is None
    with pytest.raises(BleakError, match="not visible"):
        await ble_transport.open_ble_printer(hass, _MAC, 1).__aenter__()

    api = Mock()
    client = AsyncMock()
    client.services = []
    with (
        patch.object(ble_transport, "bluetooth_api", return_value=api),
        patch.object(ble_transport, "establish_connection", return_value=client),
    ):
        with pytest.raises(ble_transport.BlePrinterMismatchError):
            await ble_transport.open_ble_printer(hass, _MAC, 1).__aenter__()
        client.disconnect.assert_awaited_once()

    char = SimpleNamespace(properties=["write"])
    link = BlePrinterLink(client, char)

    async def open_cover(*args, **kwargs):
        link.received(char, bytearray(b"\x02"))

    client.write_gatt_char.side_effect = open_cover
    with pytest.raises(HomeAssistantError, match="ble_printer_error"):
        await link.print_job(_MODEL, b"image", 80, None)
    assert client.write_gatt_char.await_count == 1
