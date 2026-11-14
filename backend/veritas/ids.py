"""Deterministic ids that are the same on every install (docs/data-architecture.md).

posts.uuid    = uuid5(NAMESPACE, "<platform>:post:<external_id>")     also the Qdrant point id
accounts.uuid = uuid5(NAMESPACE, "<platform>:account:<external_id>")

NAMESPACE must never change: every stored uuid depends on it.
"""
from __future__ import annotations

import uuid

NAMESPACE = uuid.UUID("6f1c2a0e-5b7d-5c43-9a8e-7d3b1f0c4e21")


def post_uuid(platform: str, external_id: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"{platform}:post:{external_id}")


def account_uuid(platform: str, external_id: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"{platform}:account:{external_id}")
