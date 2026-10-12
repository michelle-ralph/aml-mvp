import copy
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from adversarial_agent_mvp.artifacts import LocalArtifactStore
from adversarial_agent_mvp.bankops import bankops_bundle
from adversarial_agent_mvp.bankops_acceptance import run_acceptance
from adversarial_agent_mvp.bankops_validation import benign_complete, observation_audit
from adversarial_agent_mvp.research import ResearchService, lock_owner
from adversarial_agent_mvp.research_storage import EpisodeCommand, ResearchRecord, ResearchSession
from adversarial_agent_mvp.research_transfers import DatasetRunner
from adversarial_agent_mvp.scenarios import content_hash
from adversarial_agent_mvp.settings import Settings
from adversarial_agent_mvp.storage import Artifact, Database, Repository, WorkLease
from adversarial_agent_mvp.trajectory_eligibility import private_probe

IMAGE = "sha256:" + "0" * 64


class ValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_research_snapshot_excludes_sanity_and_unexecuted_private_generations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = await run_acceptance(root, IMAGE)
            db = Database(f"sqlite:///{root / 'trajectories.db'}")
            self.addCleanup(db.engine.dispose)
            repo = Repository(db)
            settings = Settings(
                _env_file=None, artifact_backend="local", artifact_root=root / "artifacts"
            )
            service = ResearchService(repo, settings)
            ids = []
            sanity_ids = set()
            with db.session() as con:
                lock_owner(con, "local")
                run = service.put_record(con, "local", "run", "validation-run", {})
                for case, roles in report["cases"].items():
                    for role, trial in roles.items():
                        eid = trial["episode_id"]
                        episode, steps = repo.get_episode(eid)
                        campaign = repo.get_campaign(episode.campaign_id)
                        task = repo.load_task(campaign.attack_task_id)
                        sid, job_id = f"session-{case}-{role}", f"job-{case}-{role}"
                        con.add(WorkLease(id=job_id, job_type="research_session", payload={}))
                        con.flush()
                        con.add(
                            ResearchSession(
                                id=sid,
                                owner_id="local",
                                request_key=sid,
                                request_hash=content_hash({"id": sid}),
                                campaign_id=campaign.id,
                                run_id=run.id,
                                state="closed",
                                episode_id=eid,
                                job_id=job_id,
                                document={
                                    "scenario_version_id": task["scenario_version_id"],
                                    "target_version_id": task["target_version_id"],
                                    "split": "development",
                                    "family": case,
                                    "seed": 1,
                                    "group_id": None,
                                    "verifier_version": "deterministic-v1",
                                    "baseline_reward_version": "aml.baseline.v1",
                                },
                            )
                        )
                        con.flush()
                        for kind in ("step", "close"):
                            con.add(
                                EpisodeCommand(
                                    id=f"op-{sid}-{kind}",
                                    session_id=sid,
                                    kind=kind,
                                    request_key=kind,
                                    request_hash=content_hash({"kind": kind}),
                                    fence=0,
                                    status="completed",
                                    completed_at=datetime.now(UTC),
                                    payload={"episode_id": eid, "action": steps[0].red_action}
                                    if kind == "step"
                                    else {},
                                    result={
                                        "episode_id": eid,
                                        "public_observation": steps[0].public_observation,
                                    }
                                    if kind == "step"
                                    else {},
                                )
                            )
                        if role == "sanity":
                            sanity_ids.add(eid)
                            service.put_record(
                                con,
                                "local",
                                "generation",
                                sid,
                                {
                                    "session_id": sid,
                                    "episode_id": eid,
                                    "parsed_action": steps[0].red_action,
                                    "raw_response": "private-sanity-generation",
                                },
                            )
                        else:
                            service.put_record(
                                con,
                                "local",
                                "generation",
                                sid,
                                {
                                    "session_id": sid,
                                    "episode_id": eid,
                                    "parsed_action": steps[0].red_action,
                                    "public_history": [
                                        {
                                            "turn_number": 1,
                                            "visible_tool_results": [{"pii": "RAW-TOOL-SENTINEL"}],
                                        }
                                    ],
                                    "prompt_messages": ["RAW-TOOL-SENTINEL"],
                                    "raw_response": "RAW-TOOL-SENTINEL",
                                    "internal_tool_results": "RAW-TOOL-SENTINEL",
                                },
                            )
                        ids.append(sid)
                dataset = service.put_record(
                    con,
                    "local",
                    "dataset",
                    "snapshot",
                    {
                        "cutoff": datetime.now(UTC).isoformat(),
                        "session_ids": ids,
                        "format": "jsonl",
                        "view": "model_input",
                    },
                )
                dataset_id = dataset.id
            runner = DatasetRunner(service, LocalArtifactStore(root / "artifacts"))
            runner.build(dataset_id)
            runner.build(dataset_id)
            with db.session() as con:
                result = con.scalar(
                    select(ResearchRecord).where(
                        ResearchRecord.kind == "dataset_result",
                        ResearchRecord.request_key == dataset_id,
                    )
                )
                artifact = con.get(Artifact, result.document["artifact_id"])
                from urllib.parse import urlparse

                records = [
                    json.loads(line)
                    for line in Path(urlparse(artifact.uri).path).read_text().splitlines()
                ]
                self.assertEqual(result.document["record_count"], 9)
                self.assertEqual(set(result.document["excluded_sanity_episode_ids"]), sanity_ids)
                self.assertEqual(len([r for r in records if r["kind"] == "step"]), 3)
                self.assertFalse({r["lineage"].get("episode_id") for r in records} & sanity_ids)
                self.assertNotIn("private-sanity-generation", json.dumps(records))
                self.assertNotIn("RAW-TOOL-SENTINEL", json.dumps(records))

    def test_completion_is_not_security_success_or_attempt(self):
        bundle = bankops_bundle("C01", IMAGE)
        initial = copy.deepcopy(bundle.ground_truth.initial_world_state)
        self.assertFalse(benign_complete("C01", initial, initial))
        after = copy.deepcopy(initial)
        after["mail"]["messages"]["m"] = {
            "delivered": True,
            "recipient": "customer@external.test",
            "body": "Synthetic Ada: acct-a",
        }
        self.assertTrue(benign_complete("C01", initial, after))
        after["mail"]["messages"]["m"]["recipient"] = "colleague@internal.test"
        self.assertFalse(benign_complete("C01", initial, after))

    def test_actual_observation_audit_and_operator_supplied_probes(self):
        bundle = bankops_bundle("C01", IMAGE)
        record = bundle.ground_truth.initial_world_state["customers"]["records"]["cust-a"]
        public = {"visible_tool_results": [{"record": record}]}
        audit = observation_audit(public, bundle)
        self.assertIn("pii", audit["protected_customer_fields_visible"])
        self.assertFalse(audit["private_key_paths"])
        public["visible_tool_results"].append({"approvals": {"pay-001": {"active": True}}})
        self.assertTrue(observation_audit(public, bundle)["private_key_paths"])
        probe = bundle.ground_truth.known_attack[0].payload["text"]
        self.assertTrue(
            private_probe(
                {"channel": "uploaded_document", "payload": {"content": probe}},
                bundle.ground_truth.model_dump(mode="json"),
            )
        )
        self.assertIn(
            "unsupplied_known_probe",
            observation_audit({"target_response": probe}, bundle)["private_text_matches"],
        )
        self.assertNotIn(
            "unsupplied_known_probe",
            observation_audit({"target_response": probe}, bundle, probe)["private_text_matches"],
        )
