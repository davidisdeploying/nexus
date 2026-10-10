"""Regression tests for the Cloudflare Access security boundary.

Nexus deliberately has no second in-app password prompt. Public HTTP and
WebSocket traffic is authenticated by Cloudflare Access before the tunnel
forwards it to this application.
"""
from __future__ import annotations

import unittest

from . import routes


class CloudflareAccessBoundaryTests(unittest.TestCase):
    def test_local_login_and_logout_routes_do_not_exist(self):
        paths = {route.path for route in routes.router.routes}
        self.assertNotIn("/login", paths)
        self.assertNotIn("/logout", paths)

    def test_dashboard_route_remains_present(self):
        paths = {route.path for route in routes.router.routes}
        self.assertIn("/", paths)


if __name__ == "__main__":
    unittest.main()
