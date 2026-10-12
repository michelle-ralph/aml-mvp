import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from adversarial_agent_mvp.bankops import bankops_bundle
from adversarial_agent_mvp.bankops_acceptance import run_acceptance
from adversarial_agent_mvp.contracts import PublicObservation
from adversarial_agent_mvp.learning.dataset import build_dataset, dataset_bytes
from adversarial_agent_mvp.learning.features import PublicAction, features, project
from adversarial_agent_mvp.models import AttackContext, OpenAICompatibleAttackerModel
from adversarial_agent_mvp.observations import attacker_observation, model_input_generation
from adversarial_agent_mvp.runtime_registry import RegisteredAttacker
from adversarial_agent_mvp.scenarios import PublicScenario
from adversarial_agent_mvp.storage import Database, Repository


class BlackBoxTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_transport_timeout_is_private_and_indeterminate(self):
        from adversarial_agent_mvp.bundle_smoke import open_bundle
        from adversarial_agent_mvp.contracts import RedAction

        async with open_bundle(bankops_bundle("C01", "sha256:" + "0" * 64)) as session:
            before = await session.state()
            original = httpx.ASGITransport.handle_async_request
            failed_requests = []

            async def timeout_target(transport, request):
                if request.url.host == "target" and request.method == "POST" and request.url.path == "/invoke":
                    failed_requests.append(request)
                    raise httpx.ReadTimeout("private transport timeout", request=request)
                return await original(transport, request)

            with patch.object(httpx.ASGITransport, "handle_async_request", timeout_target):
                result = await session.environment.step(
                    RedAction(channel="user_message", payload={"text": "Proceed with the task."})
                )
            self.assertEqual(len(failed_requests), 1)
            self.assertEqual(result.operator_execution_status, "indeterminate")
            self.assertFalse(result.terminal_success)
            self.assertTrue(result.public_observation.terminated)
            self.assertEqual(result.public_observation.visible_errors, ["Target execution failed."])
            self.assertIsNone(result.public_observation.delivery_receipt)
            self.assertEqual(result.public_observation.visible_tool_results, [])
            self.assertEqual(await session.state(), before)
            operator = session.environment._operator_observations[-1]["observation"]
            self.assertEqual(operator["delivery_receipt"]["status"], "delivery_unknown")
            self.assertNotIn("operator_execution_status", result.model_dump(mode="json"))

    async def test_trusted_failure_status_survives_black_box_projection(self):
        from adversarial_agent_mvp.bundle_smoke import open_bundle
        from adversarial_agent_mvp.contracts import RedAction
        from aml_target_protocol import DeliveryReceipt

        async with open_bundle(bankops_bundle("C01", "sha256:" + "0" * 64)) as session:
            env = session.environment
            await env.reset(session.task)
            # Real malformed delivery through the gateway, not a constructed label.
            result = await env.step(RedAction(channel="user_message", payload={}))
            self.assertEqual(result.operator_execution_status, "invalid_action")
            self.assertIsNone(result.public_observation.delivery_receipt)
            for status in (
                "unsupported_surface",
                "malformed_action",
                "unavailable_slot",
                "target_rejected",
                "delivery_unknown",
            ):
                receipt = DeliveryReceipt(
                    receipt_id="private-receipt",
                    episode_id="private-episode",
                    scenario_version_id="private-scenario",
                    action_id=None,
                    channel="user_message",
                    status=status,
                    applied=False,
                    payload_sha256="0" * 64,
                )
                rich = PublicObservation(
                    turn_number=1,
                    visible_errors=["private failure detail"],
                    delivery_receipt=receipt,
                )
                with patch.object(env.target, "invoke", new=AsyncMock(return_value=(rich, []))):
                    result = await env.step(
                        RedAction(channel="user_message", payload={"text": "test"})
                    )
                expected = "indeterminate" if status == "delivery_unknown" else "invalid_action"
                self.assertEqual(result.operator_execution_status, expected)
                self.assertIsNone(result.public_observation.delivery_receipt)
                self.assertNotIn("operator_execution_status", result.model_dump(mode="json"))
                public = json.dumps(result.public_observation.model_dump(mode="json"))
                self.assertNotIn("private failure detail", public)
                self.assertNotIn("private-receipt", public)

    async def test_registered_attacker_http_payload_is_black_box(self):
        from adversarial_agent_mvp.contracts import AttackTask

        bundle = bankops_bundle("V01", "sha256:" + "0" * 64)
        task = AttackTask(
            target_version_id="test",
            observation_mode="black_box",
            objective="public objective",
            available_channels=["user_message"],
            forbidden_states=bundle.ground_truth.forbidden_states,
        )
        context = AttackContext(
            task,
            [
                PublicObservation(
                    turn_number=1,
                    visible_tool_results=[{"approval_before": {"secret": "INTERNAL-ONLY"}}],
                )
            ],
            [],
            [],
        )
        captured = []

        def handler(request):
            body = json.loads(request.content)
            captured.append(body["public_context"])
            self.assertNotIn("INTERNAL-ONLY", json.dumps(body))
            self.assertFalse(body["public_context"]["observations"][0]["visible_tool_results"])
            return httpx.Response(
                200,
                json={
                    "protocol": "aml.attacker.v1",
                    "checkpoint_id": "unit-checkpoint",
                    "model": "unit-model",
                    "action": {"channel": "user_message", "payload": {"text": "public attack"}},
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = RegisteredAttacker(
                {
                    "name": "unit",
                    "version": "1",
                    "endpoint": "http://attacker",
                    "model": "unit-model",
                    "checkpoint_id": "unit-checkpoint",
                },
                client=client,
            )
            await runtime.propose(OpenAICompatibleAttackerModel._public_context(context, 1), 42)
        self.assertEqual(len(captured), 1)

    def test_projection_and_features_match_runtime_boundary_and_preserve_external_disclosure(self):
        bundle = bankops_bundle("C01", "sha256:" + "0" * 64)
        from adversarial_agent_mvp.contracts import AttackTask

        task = AttackTask(
            target_version_id="target",
            observation_mode="black_box",
            objective="public objective",
            forbidden_states=bundle.ground_truth.forbidden_states,
            available_channels=["user_message"],
        )
        raw = PublicObservation(
            turn_number=1,
            target_response="External output",
            visible_errors=["approval private rejection details"],
            visible_tool_results=[{"pii": "TOOL-ONLY-SECRET", "balance_minor": 1999, "version": 7}],
        )
        projected = attacker_observation(raw, task.observation_mode)
        self.assertEqual(projected.target_response, raw.target_response)
        self.assertFalse(projected.visible_tool_results)
        self.assertEqual(projected.visible_errors, ["Target execution failed."])
        raw_ctx = AttackContext(task, [raw], [], [])
        safe_ctx = AttackContext(task, [projected], [], [])
        self.assertEqual(project(raw_ctx), project(safe_ctx))
        action = PublicAction(channel="user_message", payload={"text": "input"})
        self.assertEqual(features(project(raw_ctx), action), features(project(safe_ctx), action))
        prompt = OpenAICompatibleAttackerModel._public_context(raw_ctx, 1)
        self.assertNotIn("TOOL-ONLY-SECRET", json.dumps(prompt))
        self.assertNotIn("private rejection", json.dumps(prompt))
        self.assertEqual(prompt["observations"][0], projected.model_dump(mode="json"))
        # The legitimate external response is not redacted, even when it discloses a secret.
        disclosed = raw.model_copy(
            update={"target_response": "Externally disclosed TOOL-ONLY-SECRET"}
        )
        self.assertIn(
            "TOOL-ONLY-SECRET", attacker_observation(disclosed, "black_box").target_response
        )
        self.assertEqual(attacker_observation(raw, "inherited"), raw)
        historical = bundle.scenario.model_dump(mode="json")
        historical.pop("observation_mode")
        self.assertEqual(PublicScenario.model_validate(historical).observation_mode, "black_box")
        historical["scenario_id"] = "finance-reference"
        finance = PublicScenario.model_validate(historical)
        self.assertEqual(finance.observation_mode, "inherited")
        self.assertNotIn("observation_mode", finance.model_dump(mode="json"))

    def test_generation_projection_removes_opaque_privileged_inputs_without_mutating_operator_evidence(
        self,
    ):
        raw = PublicObservation(
            turn_number=1, visible_tool_results=[{"pii": "PRIVATE"}]
        ).model_dump(mode="json")
        document = {
            "internal_tool_results": {"pii": "PRIVATE"},
            "public_history": [raw],
            "prompt_messages": ["PRIVATE"],
            "request": {"secret": "PRIVATE"},
            "raw_response": "PRIVATE",
            "parsed_action": {"channel": "user_message", "payload": {"text": "attack"}},
        }
        safe = model_input_generation(document, "black_box")
        self.assertNotIn("PRIVATE", json.dumps(safe))
        self.assertIn("PRIVATE", json.dumps(document))
        self.assertEqual(model_input_generation(document, "inherited"), document)

    async def test_bankops_persistence_keeps_operator_tools_and_black_box_datasets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = await run_acceptance(root, "sha256:" + "0" * 64)
            db = Database(f"sqlite:///{root / 'trajectories.db'}")
            self.addCleanup(db.engine.dispose)
            repo = Repository(db)
            for case, roles in result["cases"].items():
                for role, run in roles.items():
                    loaded = repo.get_episode(run["episode_id"])
                    self.assertIsNotNone(loaded)
                    episode, steps = loaded
                    self.assertFalse(steps[0].public_observation["visible_tool_results"])
                    self.assertIsNone(steps[0].public_observation["delivery_receipt"])
                    events = repo.list_events(aggregate_type="episode", aggregate_id=episode.id)
                    operator = [
                        e.payload for e in events if e.event_type == "OPERATOR_TARGET_OBSERVATION"
                    ]
                    self.assertEqual({o["phase"] for o in operator}, {"reset", "step"})
                    self.assertTrue(
                        next(o for o in operator if o["phase"] == "step")["observation"][
                            "visible_tool_results"
                        ]
                    )
                    self.assertTrue(repo.list_effect_attempts(episode.id))
                    if case == "C01":
                        self.assertIn("SYN-BANK-C01-PRIVATE", json.dumps(operator))
                        self.assertNotIn(
                            "SYN-BANK-C01-PRIVATE", json.dumps(steps[0].public_observation)
                        )
                    if role == "benign":
                        # Reconstruct a historical privileged step using the trusted
                        # scenario policy, not the stored observation's shape.
                        steps[0].public_observation = next(
                            o for o in operator if o["phase"] == "step"
                        )["observation"]
                        # Emulate historical reads without altering append-only records.
                        with patch.object(repo, "get_episode", return_value=(episode, steps)):
                            first = dataset_bytes(build_dataset(repo, [episode.campaign_id]))
                            self.assertEqual(
                                first, dataset_bytes(build_dataset(repo, [episode.campaign_id]))
                            )
                        self.assertNotIn(b"SYN-BANK-C01-PRIVATE", first)
                        self.assertNotIn(b"balance_minor", first)
                        self.assertNotIn(b"approval_before", first)
                        for line in first.splitlines():
                            document = json.loads(line)
                            for key in ("state", "next_state"):
                                if key in document:
                                    for obs in document[key]["observations"]:
                                        self.assertFalse(obs["visible_tool_results"])
