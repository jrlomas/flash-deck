import importlib.util
import unittest
from pathlib import Path


APP_PATH = Path(__file__).resolve().parents[1] / "stm32-flash-deck.py"
SPEC = importlib.util.spec_from_file_location("flash_deck_test_app", APP_PATH)
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class SerialDiscoveryTests(unittest.TestCase):
    def record(self, description="USB device", manufacturer="Vendor"):
        return {
            "port": "ttyACM9",
            "location": "/dev/ttyACM9",
            "description": description,
            "manufacturer": manufacturer,
        }

    def test_katapult_usb_id_is_first_class_bootloader(self):
        device, reason = APP.FlashDeck.classify_serial_device(
            self.record("Katapult"), {
                "ID_VENDOR_ID": "1d50",
                "ID_MODEL_ID": "6177",
                "ID_VENDOR": "katapult",
                "ID_MODEL": "stm32f072xb",
                "ID_SERIAL_SHORT": "abc123",
            })
        self.assertIsNone(reason)
        self.assertEqual(device["kind"], "katapult")
        self.assertEqual(device["serial"], "abc123")

    def test_klipper_application_is_not_called_uart_bootloader(self):
        device, reason = APP.FlashDeck.classify_serial_device(
            self.record("Klipper"), {
                "ID_VENDOR_ID": "1d50",
                "ID_MODEL_ID": "614e",
                "ID_VENDOR": "Klipper",
            })
        self.assertIsNone(device)
        self.assertIn("Klipper application", reason)

    def test_stlink_vcp_is_ignored(self):
        device, reason = APP.FlashDeck.classify_serial_device(
            self.record("STLINK-V3", "STMicroelectronics"), {
                "ID_VENDOR_ID": "0483",
                "ID_MODEL_ID": "374f",
                "ID_MODEL": "STLINK-V3",
            })
        self.assertIsNone(device)
        self.assertIn("ST-LINK", reason)

    def test_helix_carrier_is_not_an_individual_uart_target(self):
        device, reason = APP.FlashDeck.classify_serial_device(
            self.record("Helix CAN-FD Bridge", "OpenAMS"), {
                "ID_VENDOR_ID": "1d50",
                "ID_MODEL_ID": "606f",
                "ID_MODEL": "Helix_CAN-FD_Bridge",
            })
        self.assertIsNone(device)
        self.assertIn("carrier", reason)

    def test_common_usb_serial_adapter_remains_available_but_unverified(self):
        device, reason = APP.FlashDeck.classify_serial_device(
            self.record("USB Single Serial", "1a86"), {
                "ID_VENDOR_ID": "1a86",
                "ID_MODEL_ID": "55d4",
                "ID_MODEL": "USB_Single_Serial",
            })
        self.assertIsNone(reason)
        self.assertEqual(device["kind"], "uart")
        self.assertIn("unverified", device["label"])

    def test_cubeprogrammer_dfu_serial_label_is_case_insensitive(self):
        output = """
  Device Index           : USB1
  Product ID             : STM32  BOOTLOADER
  Serial number          : FFFFFFFEFFFF
"""
        serials = APP.FlashDeck.parse_dfu_serials(output)
        self.assertEqual(serials, ["FFFFFFFEFFFF"])


if __name__ == "__main__":
    unittest.main()
