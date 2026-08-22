"""Canonical physical-node identities used by Nexus.

Display names, SSH routing, Tailscale probe targets, and cloud-worker identities
are deliberately separate.  A physical node rename must not silently change a
transport identifier or erase the worker that currently executes there.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NodeIdentity:
    key: str
    display_name: str
    worker_identity: str
    ssh_alias: str | None
    tailscale_target: str
    health_key: str


NODES: dict[str, NodeIdentity] = {
    "alpha": NodeIdentity(
        key="alpha",
        display_name="Alpha",
        worker_identity="Worker2",
        ssh_alias=None,
        tailscale_target="alpha",
        health_key="worker2",  # legacy event/history correlation key
    ),
    "charlie": NodeIdentity(
        key="charlie",
        display_name="Charlie",
        worker_identity="Worker3",
        ssh_alias="charlie",
        tailscale_target="charlie",
        health_key="charlie",
    ),
    "delta": NodeIdentity(
        key="delta",
        display_name="Delta",
        worker_identity="Worker1",
        ssh_alias="delta",
        # The OS/SSH rename did not change this peer's current MagicDNS name.
        tailscale_target="delta",
        health_key="delta",
    ),
}


def node(key: str) -> NodeIdentity:
    try:
        return NODES[key]
    except KeyError as exc:
        raise ValueError(f"unknown physical node: {key}") from exc
