"""Trusted, scenario-scoped Red projection; operator telemetry is a separate path."""

import copy
from typing import Any, Literal

from .contracts import PublicObservation


def execution_status(
    observation: PublicObservation,
) -> Literal["ok", "invalid_action", "target_error", "indeterminate"]:
    """Classify trusted delivery evidence before removing it from Red's view."""
    receipt = observation.delivery_receipt
    status = receipt.status if receipt else None
    if status == "delivery_unknown":
        return "indeterminate"
    if status in {"unsupported_surface", "malformed_action", "unavailable_slot", "target_rejected"}:
        return "invalid_action"
    return "target_error" if observation.visible_errors else "ok"


def attacker_observation(
    observation: PublicObservation, mode: Literal["inherited", "black_box"]
) -> PublicObservation:
    if mode == "inherited":
        return observation.model_copy(deep=True)
    # No tool/receipt/error text supplied by internal execution enters Red. The
    # external textual response is deliberately preserved, including disclosures.
    return PublicObservation(
        target_response=observation.target_response,
        visible_errors=["Target execution failed."] if observation.visible_errors else [],
        turn_number=observation.turn_number,
        terminated=observation.terminated,
    )


def model_input_generation(
    document: dict[str, Any] | None, mode: Literal["inherited", "black_box"]
) -> dict[str, Any] | None:
    """Opaque historical prompts/responses are operator evidence, never learning input."""
    if document is None or mode == "inherited":
        return copy.deepcopy(document)
    result = {
        field: copy.deepcopy(document[field])
        for field in ("parsed_action", "public_history")
        if field in document
    }
    history = result.get("public_history")
    if history is not None:
        result["public_history"] = [
            attacker_observation(PublicObservation.model_validate(value), mode).model_dump(
                mode="json"
            )
            for value in history
        ]
    return result
