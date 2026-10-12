import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


class NotificationDevicePickerTests(unittest.TestCase):
    def test_picker_lists_supported_device_labels_in_order(self) -> None:
        template = (REPO_ROOT / "templates" / "notifications.html").read_text()
        picker = template.split('id="push-device-picker"', 1)[1].split("</div>", 1)[0]
        devices = re.findall(
            r'class="push-device" data-device="([^"]+)">([^<]+)</button>',
            picker,
        )

        self.assertEqual(
            devices,
            [("iphone", "iPhone"), ("ipad", "iPad"), ("macbook", "MacBook")],
        )

    def test_hidden_push_controls_stay_hidden_until_client_state_allows_them(self) -> None:
        css = (REPO_ROOT / "static" / "notifications.css").read_text()
        self.assertIn(".notifications-shell [hidden]{display:none!important}", css)


if __name__ == "__main__":
    unittest.main()
