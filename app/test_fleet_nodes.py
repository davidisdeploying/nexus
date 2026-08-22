import unittest

from .config import FLEET
from .fleet_nodes import NODES
from .gemini_remote import HOSTS


class FleetNodeRegistryTests(unittest.TestCase):
    def test_adopted_node_labels_and_worker_identities_are_separate(self) -> None:
        self.assertEqual(
            [NODES[key].display_name for key in ("charlie", "delta", "alpha")],
            ["Charlie", "Delta", "Alpha"],
        )
        self.assertEqual(NODES["charlie"].worker_identity, "Worker3")
        self.assertEqual(NODES["delta"].worker_identity, "Worker1")
        self.assertEqual(NODES["alpha"].worker_identity, "Worker2")

    def test_delta_uses_ssh_alias_and_magicdns_target_independently(self) -> None:
        delta = NODES["delta"]
        self.assertEqual(delta.ssh_alias, "delta")
        self.assertEqual(delta.tailscale_target, "delta")
        health = next(item for item in FLEET if item.name == delta.health_key)
        self.assertEqual(health.address, "delta")
        self.assertEqual(health.ssh_host, "delta")

    def test_control_surface_uses_physical_labels(self) -> None:
        self.assertEqual(HOSTS["alpha"].label, "Alpha")
        self.assertEqual(HOSTS["charlie"].label, "Charlie")
        self.assertEqual(HOSTS["delta"].label, "Delta")
