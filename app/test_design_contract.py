"""Regression checks for the Nexus-wide typography and semantic color contract."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"


def _relative_luminance(hex_color: str) -> float:
    rgb = [int(hex_color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    channels = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in rgb]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(foreground: str, background: str) -> float:
    light, dark = sorted(
        (_relative_luminance(foreground), _relative_luminance(background)), reverse=True
    )
    return (light + 0.05) / (dark + 0.05)


class NexusDesignContractTests(unittest.TestCase):
    def test_shared_stylesheet_owns_the_type_roles(self) -> None:
        css = (STATIC / "nexus.css").read_text()
        self.assertIn('--font-display:"Fraunces",Georgia,serif', css)
        self.assertIn("--font-ui:ui-sans-serif,system-ui", css)
        self.assertIn('--font-mono:"JetBrains Mono",ui-monospace', css)
        self.assertIn(".nexus-app-body{", css)
        self.assertIn("font:400 16px/1.45 var(--font-ui)", css)

    def test_route_styles_alias_shared_roles_instead_of_forking_them(self) -> None:
        expected = {
            "activity.css": ("--a-ui:var(--font-ui)", "--a-mono:var(--font-mono)"),
            "health.css": ("--health-ui:var(--font-ui)", "--health-mono:var(--font-mono)"),
            "watchdogs.css": (
                "--watchdogs-ui:var(--font-ui)",
                "--watchdogs-mono:var(--font-mono)",
            ),
        }
        for filename, markers in expected.items():
            css = (STATIC / filename).read_text()
            for marker in markers:
                self.assertIn(marker, css, filename)

    def test_only_shared_stylesheet_declares_canonical_palette_literals(self) -> None:
        adopted = (
            "activity.css",
            "gemini.css",
            "dashboard.css",
            "detail.css",
            "health.css",
            "herospath.css",
            "jobs.css",
            "model_usage.css",
            "notifications.css",
            "watchdogs.css",
        )
        canonical = re.compile(
            r"--(?:nexus|stone|stone-2|bezel|line|ink|ink-dim|cyan|cyan-deep|amber|red|green)\s*:\s*#",
            re.IGNORECASE,
        )
        for filename in adopted:
            self.assertIsNone(canonical.search((STATIC / filename).read_text()), filename)

    def test_route_styles_do_not_bypass_shared_font_roles(self) -> None:
        direct_stack = re.compile(
            r"font(?:-family)?\s*:[^;}]*\b(?:Fraunces|JetBrains Mono|Georgia|Arial|Segoe UI)\b",
            re.IGNORECASE,
        )
        for path in STATIC.glob("*.css"):
            if path.name == "nexus.css":
                continue
            self.assertIsNone(direct_stack.search(path.read_text()), path.name)

    def test_small_metadata_token_meets_normal_text_contrast(self) -> None:
        css = (STATIC / "nexus.css").read_text()
        self.assertIn("--text-metadata:var(--ink-dim)", css)
        self.assertGreaterEqual(_contrast("#7f9b98", "#111a1d"), 4.5)

    def test_adopted_pages_enter_the_shared_application_body(self) -> None:
        templates = (
            "activity.html",
            "gemini.html",
            "conformance.html",
            "control_plane.html",
            "dashboard.html",
            "health.html",
            "notifications.html",
            "watchdogs.html",
        )
        for filename in templates:
            html = (ROOT / "templates" / filename).read_text()
            self.assertRegex(html, r'<body[^>]*class="[^"]*nexus-app-body', filename)


if __name__ == "__main__":
    unittest.main()
