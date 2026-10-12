"""Operator-only sanity provenance checks shared by both dataset exporters."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from .storage import OperationalEvent


def private_probe(action: dict[str, Any] | None, private_document: dict[str, Any]) -> bool:
    if not action:
        return False
    payload = action.get("payload", {})
    content = payload.get("text", payload.get("content"))
    for known in private_document.get("known_attack", []):
        other = known["payload"]
        if (action.get("channel") == known["channel"] and payload == other) or (
            content is not None
            and json.dumps(content, sort_keys=True)
            == json.dumps(other.get("text", other.get("content")), sort_keys=True)
        ):
            return True
    return False


def sanity_episode(db: Any, episode_id: str | None) -> bool:
    return bool(
        episode_id
        and db.scalar(
            select(OperationalEvent.id).where(
                OperationalEvent.aggregate_id == episode_id,
                OperationalEvent.event_type == "VERIFIER_SANITY_ONLY",
            )
        )
    )
