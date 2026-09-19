import copy
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import traceback
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

with (
    patch("socket.create_connection", side_effect=AssertionError("Network forbidden")),
    patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")),
    patch("socket.socket.connect_ex", side_effect=AssertionError("Network forbidden")),
):
    import agent_recovery


class AgentRecoveryTests(unittest.TestCase):
    def start_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def setUp(self):
        self.role_assignments_type = agent_recovery.RoleAssignments
        for target in (
            "socket.create_connection",
            "socket.getaddrinfo",
            "socket.socket.connect",
            "socket.socket.connect_ex",
            "socket.socket.sendto",
        ):
            self.start_patch(
                patch(target, side_effect=AssertionError("Network forbidden"))
            )
        self.credential_guard = self.start_patch(
            patch.object(
                agent_recovery,
                "DefaultAzureCredential",
                side_effect=AssertionError("Use a fake credential"),
            )
        )
        self.project_guard = self.start_patch(
            patch.object(
                agent_recovery,
                "AIProjectClient",
                side_effect=AssertionError("Use a fake project client"),
            )
        )
        directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def recovery_manifest(self, hosted=False):
        raw = self.hosted_version() if hosted else self.prompt_version()
        return {
            "schema_version": 1,
            "state": "exported",
            "project_endpoint": "https://example.test/api/projects/project",
            "identity_role_assignments": (
                {
                    "old-principal": {
                        "role_assignments": [
                            {
                                "scope": "/subscriptions/test/resourceGroups/agents",
                                "role_definition_id": (
                                    "/subscriptions/test/providers/"
                                    "Microsoft.Authorization/roleDefinitions/test"
                                ),
                                "condition": "@Resource[test] StringEquals 'test'",
                                "condition_version": "2.0",
                                "description": "Restore direct RBAC",
                            }
                        ]
                    }
                }
                if hosted
                else {}
            ),
            "agents": [
                {
                    "name": raw["name"],
                    "state": "enabled",
                    "endpoint_update_body": {
                        "agent_endpoint": {
                            "version_selector": {
                                "version_selection_rules": [{"agent_version": "1"}]
                            }
                        }
                    },
                    "versions": [
                        agent_recovery.analyze_version(
                            raw, selected=True, skip_unsupported_agents=False
                        )
                    ],
                }
            ],
        }

    def fake_restore_cli(
        self,
        manifest=None,
        responses=None,
        reused=None,
        extra_args=(),
        role_client=None,
    ):
        if manifest is None:
            manifest = self.recovery_manifest()
        if responses is None:
            responses = [{"version": "7", "status": "active"}]
        responses = copy.deepcopy(responses)
        manifest_path = self.directory / "manifest.json"
        output_path = self.directory / "report.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        agents = Mock()
        agents.list_versions.return_value = reused or []
        agents.create_version.return_value = SimpleNamespace(version="7")
        agents.get_version.side_effect = lambda *_args: (
            responses.pop(0) if len(responses) > 1 else responses[0]
        )
        project = MagicMock()
        project.__enter__.return_value = SimpleNamespace(agents=agents)
        if role_client is None:
            role_client = Mock()
            role_client.create_for_principal.return_value = "/roleAssignments/restored"
        self.start_patch(
            patch.object(agent_recovery, "DefaultAzureCredential", return_value=Mock())
        )
        self.start_patch(
            patch.object(agent_recovery, "AIProjectClient", return_value=project)
        )
        self.start_patch(
            patch.object(agent_recovery, "RoleAssignments", return_value=role_client)
        )
        clock = SimpleNamespace(seconds=0)
        self.start_patch(
            patch.object(
                agent_recovery.time, "monotonic", side_effect=lambda: clock.seconds
            )
        )
        sleep = self.start_patch(
            patch.object(
                agent_recovery.time,
                "sleep",
                side_effect=lambda delay: setattr(clock, "seconds", clock.seconds + delay),
            )
        )
        self.start_patch(
            patch.object(
                sys,
                "argv",
                [
                    "agent_recovery.py",
                    "restore",
                    "--project-endpoint",
                    manifest["project_endpoint"],
                    "--manifest",
                    str(manifest_path),
                    "--output",
                    str(output_path),
                    "--version-timeout-seconds",
                    "20",
                    *extra_args,
                ],
            )
        )
        return SimpleNamespace(
            agents=agents, roles=role_client, report=output_path, sleep=sleep
        )

    def fake_arm_role_client(self, handler):
        credential = Mock()
        credential.get_token.return_value = SimpleNamespace(token="sentinel")
        client = agent_recovery.httpx.Client(
            transport=agent_recovery.httpx.MockTransport(handler)
        )
        self.addCleanup(client.close)
        with patch.object(agent_recovery.httpx, "Client", return_value=client):
            roles = self.role_assignments_type(credential)
        return roles

    def existing_role_assignment(self):
        assignment = self.recovery_manifest(hosted=True)["identity_role_assignments"][
            "old-principal"
        ]["role_assignments"][0]
        return {
            "id": (
                f"{assignment['scope']}/providers/Microsoft.Authorization/"
                "roleAssignments/11111111-2222-3333-4444-555555555555"
            ),
            "properties": {
                "principalId": "old-principal",
                "scope": assignment["scope"],
                "roleDefinitionId": assignment["role_definition_id"],
                "condition": assignment["condition"],
                "conditionVersion": assignment["condition_version"],
            },
        }

    def fake_arm_restore(self, handler, manifest=None):
        roles = self.fake_arm_role_client(handler)
        current = self.hosted_version("7")
        return self.fake_restore_cli(
            manifest or self.recovery_manifest(hosted=True),
            responses=[current],
            reused=[current],
            role_client=roles,
        )

    def assert_role_restore_failed(self, cloud):
        cloud.agents.disable.assert_called_once()
        cloud.agents.enable.assert_not_called()
        cloud.agents.update_details.assert_not_called()
        report = json.loads(cloud.report.read_text())
        self.assertEqual(report["state"], "error")
        self.assertEqual(report["agents"][0]["state"], "temporarily-disabled")
        self.assertEqual(report["role_assignments"], [])

    def run_cli(self):
        with redirect_stdout(io.StringIO()) as output:
            agent_recovery.main()
        return json.loads(output.getvalue())

    def prompt_version(self, version="1"):
        return {
            "id": f"agent:prompt:{version}",
            "name": "prompt-agent",
            "version": version,
            "status": "active",
            "created_at": "2026-09-14T00:00:00Z",
            "metadata": {"owner": "sample"},
            "definition": {
                "kind": "prompt",
                "model": "sample-model",
                "instructions": "Answer briefly.",
            },
        }

    def hosted_version(self, version="1"):
        return {
            "id": f"agent:hosted:{version}",
            "name": "hosted-agent",
            "version": version,
            "status": "active",
            "metadata": {},
            "definition": {
                "kind": "hosted",
                "cpu": "1",
                "memory": "2Gi",
                "container_configuration": {
                    "image": "sample.azurecr.io/agent@sha256:abc"
                },
                "protocol_versions": [{"protocol": "responses", "version": "2.0.0"}],
            },
            "instance_identity": {"principal_id": "old-principal"},
        }

    def test_role_assignment_client_uses_bearer_token(self):
        class Credential:
            def get_token(self, _scope):
                return type("Token", (), {"token": "sentinel"})()

        client = agent_recovery.RoleAssignments(Credential())
        try:
            self.assertEqual(
                client._headers()["Authorization"],
                "Bearer sentinel",
            )
        finally:
            client.close()

    def test_selected_versions_include_latest_and_endpoint_route(self):
        agent = {
            "versions": {"latest": {"version": "3"}},
            "agent_endpoint": {
                "version_selector": {
                    "version_selection_rules": [
                        {"type": "FixedRatio", "agent_version": "2"}
                    ]
                }
            },
        }
        versions = [
            self.prompt_version("1"),
            self.prompt_version("2"),
            self.prompt_version("3"),
        ]
        self.assertEqual(
            agent_recovery.selected_version_ids(agent, versions, False),
            {"2", "3"},
        )
        self.assertEqual(
            agent_recovery.selected_version_ids(agent, versions, True),
            {"1", "2", "3"},
        )

    def test_unavailable_routed_version_fails_export_preflight(self):
        agent = {
            "versions": {"latest": {"version": "1"}},
            "agent_endpoint": {
                "version_selector": {
                    "version_selection_rules": [{"agent_version": "9"}]
                }
            },
        }
        with self.assertRaisesRegex(
            agent_recovery.RecoveryError, "unavailable version"
        ):
            agent_recovery.selected_version_ids(
                agent, [self.prompt_version("1")], False
            )

    def test_prompt_version_uses_supported_cosmos_backed_export(self):
        result = agent_recovery.analyze_version(
            self.prompt_version(), selected=True, skip_unsupported_agents=False
        )
        self.assertTrue(result["supported"])
        self.assertEqual(result["recovery_source"], "cosmos-backed-foundry-api-export")
        self.assertNotIn("id", result["create_body"])
        self.assertNotIn("status", result["create_body"])

    def test_hosted_version_requires_container_image(self):
        result = agent_recovery.analyze_version(
            self.hosted_version(), selected=True, skip_unsupported_agents=False
        )
        self.assertTrue(result["supported"])
        self.assertEqual(result["recovery_source"], "container-image")
        code_hosted = self.hosted_version()
        code_hosted["definition"].pop("container_configuration")
        code_hosted["definition"]["code_configuration"] = {
            "runtime": "python",
            "entry_point": "main.py",
        }
        with self.assertRaisesRegex(agent_recovery.RecoveryError, "original code"):
            agent_recovery.analyze_version(
                code_hosted, selected=True, skip_unsupported_agents=False
            )

    def test_agent_card_export_keeps_only_writable_fields(self):
        body = agent_recovery.endpoint_body(
            {
                "agent_card": {
                    "version": "1.0",
                    "description": "Recovery card",
                    "skills": [{"id": "recover", "name": "Recover"}],
                    "service_generated_field": "omit",
                }
            }
        )
        self.assertEqual(
            body["agent_card"],
            {
                "version": "1.0",
                "description": "Recovery card",
                "skills": [{"id": "recover", "name": "Recover"}],
            },
        )

    def test_endpoint_versions_are_remapped_without_mutating_manifest(self):
        body = {
            "agent_endpoint": {
                "version_selector": {
                    "version_selection_rules": [
                        {
                            "type": "FixedRatio",
                            "agent_version": "4",
                            "traffic_percentage": 100,
                        }
                    ]
                },
                "protocol_configuration": {"responses": {}},
            }
        }
        original = copy.deepcopy(body)
        remapped = agent_recovery.remap_endpoint(body, {"4": "1"})
        self.assertEqual(
            remapped["agent_endpoint"]["version_selector"]["version_selection_rules"][
                0
            ]["agent_version"],
            "1",
        )
        self.assertEqual(body, original)

    def test_missing_routed_version_is_an_explicit_error(self):
        body = {
            "agent_endpoint": {
                "version_selector": {
                    "version_selection_rules": [{"agent_version": "9"}]
                }
            }
        }
        with self.assertRaisesRegex(agent_recovery.RecoveryError, "wasn't restored"):
            agent_recovery.remap_endpoint(body, {"1": "1"})

    def test_reusable_versions_preserve_identical_version_cardinality(self):
        version_one = self.prompt_version("1")
        version_two = self.prompt_version("2")
        pools = agent_recovery.reusable_version_pools([version_two, version_one])
        body_hash = agent_recovery.fingerprint(agent_recovery.create_body(version_one))
        self.assertEqual(
            [version["version"] for version in pools[body_hash]],
            ["1", "2"],
        )

    def test_failed_versions_are_not_reused(self):
        failed = self.prompt_version("1")
        failed["status"] = "failed"
        self.assertEqual(agent_recovery.reusable_version_pools([failed]), {})

    def test_file_and_vector_references_are_reported(self):
        warnings = agent_recovery.find_external_asset_warnings(
            {
                "tools": [
                    {
                        "type": "file_search",
                        "file_ids": ["file-1"],
                        "vector_store_ids": ["vs-1"],
                    }
                ]
            }
        )
        self.assertEqual(len(warnings), 2)

    def test_manifest_hash_validation_detects_edits(self):
        version = agent_recovery.analyze_version(
            self.prompt_version(), selected=True, skip_unsupported_agents=False
        )
        manifest = {
            "schema_version": 1,
            "state": "exported",
            "project_endpoint": "https://example.test/api/projects/project",
            "identity_role_assignments": {},
            "agents": [
                {
                    "name": "prompt-agent",
                    "state": "enabled",
                    "versions": [version],
                }
            ],
        }
        agent_recovery.validate_manifest(manifest)
        manifest["agents"][0]["versions"][0]["create_body"]["definition"][
            "instructions"
        ] = "Changed after export."
        with self.assertRaisesRegex(agent_recovery.RecoveryError, "hash mismatch"):
            agent_recovery.validate_manifest(manifest)

    def test_restore_preflight_rejects_selected_unsupported_version(self):
        unsupported = agent_recovery.analyze_version(
            {
                **self.hosted_version(),
                "definition": {
                    "kind": "hosted",
                    "cpu": "1",
                    "memory": "2Gi",
                    "code_configuration": {
                        "runtime": "python",
                        "entry_point": "main.py",
                    },
                },
            },
            selected=True,
            skip_unsupported_agents=True,
        )
        agent = {"name": "hosted-agent", "versions": [unsupported]}
        with self.assertRaisesRegex(agent_recovery.RecoveryError, "can't be restored"):
            agent_recovery.versions_for_restore(agent, False)

    def test_restore_preflight_allows_unselected_unsupported_history(self):
        supported = agent_recovery.analyze_version(
            self.hosted_version("2"),
            selected=True,
            skip_unsupported_agents=False,
        )
        unsupported_raw = self.hosted_version("1")
        unsupported_raw["definition"].pop("container_configuration")
        unsupported_raw["definition"]["code_configuration"] = {
            "runtime": "python",
            "entry_point": "main.py",
        }
        unsupported = agent_recovery.analyze_version(
            unsupported_raw,
            selected=False,
            skip_unsupported_agents=True,
        )
        agent = {
            "name": "hosted-agent",
            "versions": [unsupported, supported],
        }
        self.assertEqual(
            agent_recovery.versions_for_restore(agent, False),
            [supported],
        )

    def test_restore_temporarily_disables_then_reenables_agent(self):
        version = agent_recovery.analyze_version(
            self.prompt_version(),
            selected=True,
            skip_unsupported_agents=False,
        )
        manifest = {
            "schema_version": 1,
            "state": "exported",
            "project_endpoint": "https://example.test/api/projects/project",
            "identity_role_assignments": {},
            "agents": [
                {
                    "name": "prompt-agent",
                    "state": "enabled",
                    "endpoint_update_body": None,
                    "versions": [version],
                }
            ],
        }
        calls = []

        class Agents:
            def list_versions(self, **_kwargs):
                calls.append("list")
                return []

            def create_version(self, **_kwargs):
                calls.append("create")
                return SimpleNamespace(version="1")

            def disable(self, **_kwargs):
                calls.append("disable")

            def get_version(self, *_args, **_kwargs):
                calls.append("get")
                return {
                    "version": "1",
                    "status": "active",
                    "instance_identity": None,
                }

            def enable(self, **_kwargs):
                calls.append("enable")

        class Project:
            agents = Agents()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class Credential:
            def close(self):
                pass

        with tempfile.TemporaryDirectory(dir=self.directory) as directory:
            manifest_path = Path(directory) / "agents.json"
            output_path = Path(directory) / "report.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            args = SimpleNamespace(
                manifest=manifest_path,
                project_endpoint=manifest["project_endpoint"],
                allow_project_change=False,
                restore_all_versions=False,
                skip_role_assignments=True,
                output=output_path,
                dry_run=False,
                version_timeout_seconds=1,
            )
            with (
                patch.object(
                    agent_recovery,
                    "DefaultAzureCredential",
                    return_value=Credential(),
                ),
                patch.object(
                    agent_recovery,
                    "AIProjectClient",
                    return_value=Project(),
                ),
            ):
                report = agent_recovery.restore_manifest(args)

        self.assertEqual(calls, ["list", "create", "disable", "get", "enable"])
        self.assertEqual(report["agents"][0]["state"], "enabled")

    def test_private_json_writer_is_atomic_and_valid(self):
        with tempfile.TemporaryDirectory(dir=self.directory) as directory:
            path = Path(directory) / "manifest.json"
            agent_recovery.write_private_json(path, {"state": "exported"})
            self.assertEqual(json.loads(path.read_text()), {"state": "exported"})
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_network_and_unmocked_sdk_are_blocked(self):
        with self.assertRaisesRegex(AssertionError, "Network forbidden"):
            socket.create_connection(("example.test", 443))
        with self.assertRaisesRegex(AssertionError, "Use a fake credential"):
            agent_recovery.DefaultAzureCredential()
        with self.assertRaisesRegex(AssertionError, "Use a fake project"):
            agent_recovery.AIProjectClient()

    def test_missing_required_principal_fails_cli_restore_without_enabling(self):
        for reuse in (False, True):
            with self.subTest(reuse=reuse):
                current = self.hosted_version("7")
                current["instance_identity"] = None
                cloud = self.fake_restore_cli(
                    self.recovery_manifest(hosted=True),
                    responses=[current],
                    reused=[current] if reuse else None,
                )
                with self.assertRaisesRegex(
                    agent_recovery.RecoveryError, "required identity principal ID"
                ):
                    self.run_cli()
                self.assertEqual(cloud.agents.get_version.call_count, 3)
                cloud.roles.create_for_principal.assert_not_called()
                cloud.agents.enable.assert_not_called()
                cloud.agents.update_details.assert_not_called()
                self.assertEqual(json.loads(cloud.report.read_text())["state"], "error")

    def test_pending_disable_is_retried_even_when_principal_never_arrives(self):
        current = self.hosted_version("7")
        current["instance_identity"] = None
        cloud = self.fake_restore_cli(
            self.recovery_manifest(hosted=True), responses=[current]
        )
        conflict = agent_recovery.HttpResponseError("Agent is still provisioning")
        conflict.status_code = 409
        cloud.agents.disable.side_effect = [conflict, None]
        with self.assertRaisesRegex(
            agent_recovery.RecoveryError, "required identity principal ID"
        ):
            self.run_cli()
        self.assertEqual(cloud.agents.disable.call_count, 2)
        cloud.agents.enable.assert_not_called()
        report = json.loads(cloud.report.read_text())
        self.assertEqual(report["agents"][0]["state"], "temporarily-disabled")

    def test_delayed_principal_replays_required_roles_before_endpoint_and_enable(self):
        for reuse in (False, True):
            with self.subTest(reuse=reuse):
                current = self.hosted_version("7")
                current["instance_identity"] = None
                ready = copy.deepcopy(current)
                ready["instance_identity"] = {"principal_id": "new-principal"}
                manifest = self.recovery_manifest(hosted=True)
                cloud = self.fake_restore_cli(
                    manifest, [current, ready], [current] if reuse else None
                )
                order = Mock()
                order.attach_mock(cloud.agents, "agents")
                order.attach_mock(cloud.roles, "roles")
                result = self.run_cli()
                self.assertEqual(result["state"], "completed")
                assignment = manifest["identity_role_assignments"]["old-principal"][
                    "role_assignments"
                ][0]
                cloud.roles.create_for_principal.assert_called_once_with(
                    assignment, "new-principal"
                )
                calls = [call[0] for call in order.mock_calls]
                self.assertLess(
                    calls.index("roles.create_for_principal"),
                    calls.index("agents.update_details"),
                )
                self.assertLess(
                    calls.index("agents.update_details"), calls.index("agents.enable")
                )
                restored_body = cloud.agents.update_details.call_args.kwargs["body"]
                self.assertEqual(
                    agent_recovery.routed_versions(restored_body), {"7"}
                )
                if reuse:
                    cloud.agents.create_version.assert_not_called()
                else:
                    self.assertEqual(
                        cloud.agents.create_version.call_args.kwargs["body"],
                        manifest["agents"][0]["versions"][0]["create_body"],
                    )
                report = json.loads(cloud.report.read_text())
                self.assertEqual(
                    report["identity_mappings"][0]["new_principal_id"], "new-principal"
                )
                self.assertEqual(len(report["role_assignments"]), 1)

    def test_missing_and_unknown_status_never_count_as_ready(self):
        for status in (None, "", "unexpected-status"):
            for reuse in (False, True):
                with self.subTest(status=status, reuse=reuse):
                    current = self.prompt_version("7")
                    current.pop("status")
                    if status is not None:
                        current["status"] = status
                    cloud = self.fake_restore_cli(
                        responses=[current], reused=[current] if reuse else None
                    )
                    with self.assertRaisesRegex(
                        agent_recovery.RecoveryError, "Timed out waiting"
                    ):
                        self.run_cli()
                    self.assertEqual(cloud.agents.get_version.call_count, 3)
                    self.assertEqual(cloud.sleep.call_count, 2)
                    cloud.agents.enable.assert_not_called()
                    self.assertEqual(
                        json.loads(cloud.report.read_text())["state"], "error"
                    )

    def test_missing_status_can_become_active_within_timeout(self):
        cloud = self.fake_restore_cli(
            responses=[{"version": "7"}, {"version": "7", "status": "active"}]
        )
        self.assertEqual(self.run_cli()["state"], "completed")
        self.assertEqual(cloud.agents.get_version.call_count, 2)
        cloud.agents.enable.assert_called_once()

    def test_terminal_provisioning_states_still_fail_immediately(self):
        for status in ("failed", "deleting", "deleted"):
            with self.subTest(status=status):
                cloud = self.fake_restore_cli(
                    responses=[{"version": "7", "status": status}]
                )
                with self.assertRaises(agent_recovery.RecoveryError):
                    self.run_cli()
                self.assertEqual(cloud.agents.get_version.call_count, 1)
                cloud.sleep.assert_not_called()
                cloud.agents.enable.assert_not_called()

    def test_poll_sleep_does_not_exceed_remaining_timeout(self):
        cloud = self.fake_restore_cli(
            responses=[{"version": "7"}],
            extra_args=("--version-timeout-seconds", "1"),
        )
        with self.assertRaisesRegex(agent_recovery.RecoveryError, "Timed out"):
            self.run_cli()
        cloud.sleep.assert_called_once_with(1)

    def test_explicit_role_skip_and_dry_run_do_not_require_new_principal(self):
        for flag in ("--skip-role-assignments", "--dry-run"):
            with self.subTest(flag=flag):
                cloud = self.fake_restore_cli(
                    self.recovery_manifest(hosted=True), extra_args=(flag,)
                )
                self.assertEqual(
                    self.run_cli()["state"],
                    "planned" if flag == "--dry-run" else "completed",
                )
                cloud.roles.create_for_principal.assert_not_called()
                report = json.loads(cloud.report.read_text())
                self.assertEqual(len(report["role_assignments"]), 1)
                self.assertEqual(
                    report["role_assignments"][0]["result"],
                    "planned" if flag == "--dry-run" else "skipped",
                )
                if flag == "--dry-run":
                    cloud.agents.create_version.assert_not_called()
                    cloud.agents.get_version.assert_not_called()
                    cloud.agents.disable.assert_not_called()
                    cloud.agents.enable.assert_not_called()

    def test_role_failure_does_not_enable_restored_agent(self):
        cloud = self.fake_restore_cli(
            self.recovery_manifest(hosted=True),
            responses=[
                {
                    "version": "7",
                    "status": "active",
                    "instance_identity": {"principal_id": "new-principal"},
                }
            ],
        )
        cloud.roles.create_for_principal.side_effect = RuntimeError("RBAC denied")
        with self.assertRaisesRegex(RuntimeError, "RBAC denied"):
            self.run_cli()
        cloud.agents.enable.assert_not_called()
        cloud.agents.update_details.assert_not_called()
        self.assertEqual(json.loads(cloud.report.read_text())["state"], "error")

    def test_restore_reuses_existing_equivalent_role_assignment_after_conflict(self):
        existing = self.existing_role_assignment()
        requests = []

        def arm(request):
            requests.append(request)
            if request.method == "PUT":
                self.assertNotEqual(request.url.path, existing["id"])
                return agent_recovery.httpx.Response(
                    409, json={"error": {"code": "RoleAssignmentExists"}}
                )
            self.assertEqual(request.method, "GET")
            return agent_recovery.httpx.Response(
                200, json={"value": [existing]}
            )

        cloud = self.fake_arm_restore(arm)
        result = self.run_cli()
        self.assertEqual(result["state"], "completed")
        cloud.agents.create_version.assert_not_called()
        cloud.agents.disable.assert_called_once()
        cloud.agents.enable.assert_called_once()
        cloud.agents.update_details.assert_called_once()
        report = json.loads(cloud.report.read_text())
        self.assertEqual(report["role_assignments"][0]["result"], existing["id"])
        self.assertEqual([request.method for request in requests], ["PUT", "GET"])

    def test_role_conflict_lookup_preserves_pagination_tokens_and_query(self):
        existing = self.existing_role_assignment()
        for key in ("principalId", "scope", "roleDefinitionId"):
            existing["properties"][key] = existing["properties"][key].upper()
        existing["properties"]["description"] = "Description does not grant rights"
        wrong_principal = copy.deepcopy(existing)
        wrong_principal["properties"]["principalId"] = "different-principal"
        collection = (
            "https://management.azure.com/subscriptions/test/resourceGroups/agents/"
            "providers/Microsoft.Authorization/roleAssignments"
        )
        second = f"{collection}?api-version=2022-04-01&$skiptoken=second"
        third = f"{collection}?api-version=2022-04-01&$skiptoken=third"
        pages = iter(
            [
                {"value": [wrong_principal], "nextLink": second},
                {"value": [existing], "nextLink": third},
                {"value": []},
            ]
        )
        requests = []

        def arm(request):
            requests.append(request)
            cloud.agents.disable.assert_called_once()
            cloud.agents.enable.assert_not_called()
            cloud.agents.update_details.assert_not_called()
            self.assertEqual(request.headers["Authorization"], "Bearer sentinel")
            if request.method == "PUT":
                return agent_recovery.httpx.Response(
                    409, json={"error": {"code": "RoleAssignmentExists"}}
                )
            self.assertEqual(request.method, "GET")
            return agent_recovery.httpx.Response(200, json=next(pages))

        cloud = self.fake_arm_restore(arm)
        self.assertEqual(self.run_cli()["state"], "completed")
        self.assertEqual(
            [request.method for request in requests], ["PUT"] + ["GET"] * 3
        )
        self.assertEqual(str(requests[1].url).split("?")[0], collection)
        self.assertEqual(
            dict(requests[1].url.params),
            {"api-version": "2022-04-01", "$filter": "principalId eq 'old-principal'"},
        )
        self.assertEqual(str(requests[2].url), second)
        self.assertEqual(str(requests[3].url), third)
        self.assertEqual(cloud.roles.credential.get_token.call_count, 4)
        for call in cloud.roles.credential.get_token.call_args_list:
            self.assertEqual(call.args, ("https://management.azure.com/.default",))
        cloud.agents.enable.assert_called_once()
        report = json.loads(cloud.report.read_text())
        self.assertEqual(report["role_assignments"][0]["result"], existing["id"])

    def test_role_conflict_requires_exact_identity_scope_role_and_condition(self):
        original = self.existing_role_assignment()["properties"]
        mismatches = [
            ("principalId", "different-principal"),
            ("principalId", None),
            ("scope", "/subscriptions/test"),
            (
                "scope",
                original["scope"] + "/providers/Microsoft.Storage/storageAccounts/a",
            ),
            ("scope", original["scope"] + "-other"),
            ("scope", original["scope"] + "/"),
            ("scope", None),
            ("roleDefinitionId", original["roleDefinitionId"] + "-other"),
            ("roleDefinitionId", None),
            ("condition", None),
            ("condition", ""),
            ("condition", original["condition"] + " "),
            ("condition", original["condition"].upper()),
            ("condition", original["condition"].replace("'test'", "'other'")),
            ("conditionVersion", None),
            ("conditionVersion", "1.0"),
            ("conditionVersion", 2.0),
        ]
        for key, value in mismatches:
            with self.subTest(property=key, value=value):
                existing = self.existing_role_assignment()
                existing["properties"][key] = value

                def arm(request):
                    if request.method == "PUT":
                        return agent_recovery.httpx.Response(
                            409, json={"error": {"code": "RoleAssignmentExists"}}
                        )
                    self.assertEqual(request.method, "GET")
                    return agent_recovery.httpx.Response(
                        200, json={"value": [existing]}
                    )

                cloud = self.fake_arm_restore(arm)
                with self.assertRaisesRegex(
                    agent_recovery.RecoveryError, "without a verified equivalent"
                ):
                    self.run_cli()
                self.assert_role_restore_failed(cloud)

    def test_unconditional_role_conflict_rejects_conditional_grants(self):
        for existing_condition in ("absent", "null", "conditional"):
            with self.subTest(existing_condition=existing_condition):
                manifest = self.recovery_manifest(hosted=True)
                assignment = manifest["identity_role_assignments"]["old-principal"][
                    "role_assignments"
                ][0]
                del assignment["condition"]
                del assignment["condition_version"]
                existing = self.existing_role_assignment()
                if existing_condition == "absent":
                    del existing["properties"]["condition"]
                    del existing["properties"]["conditionVersion"]
                elif existing_condition == "null":
                    existing["properties"]["condition"] = None
                    existing["properties"]["conditionVersion"] = None

                def arm(request):
                    if request.method == "PUT":
                        properties = json.loads(request.content)["properties"]
                        self.assertNotIn("condition", properties)
                        self.assertNotIn("conditionVersion", properties)
                        return agent_recovery.httpx.Response(
                            409, json={"error": {"code": "RoleAssignmentExists"}}
                        )
                    self.assertEqual(request.method, "GET")
                    return agent_recovery.httpx.Response(
                        200, json={"value": [existing]}
                    )

                cloud = self.fake_arm_restore(arm, manifest)
                if existing_condition == "conditional":
                    with self.assertRaisesRegex(
                        agent_recovery.RecoveryError, "without a verified equivalent"
                    ):
                        self.run_cli()
                    self.assert_role_restore_failed(cloud)
                else:
                    self.assertEqual(self.run_cli()["state"], "completed")
                    cloud.agents.enable.assert_called_once()

    def test_role_conflict_without_verified_match_checks_every_page_and_fails(self):
        pages = iter(
            [
                {
                    "value": [],
                    "nextLink": (
                        "https://management.azure.com/subscriptions/test/"
                        "resourceGroups/agents/providers/"
                        "Microsoft.Authorization/roleAssignments"
                        "?api-version=2022-04-01&$skiptoken=last"
                    ),
                },
                {"value": []},
            ]
        )
        requests = []

        def arm(request):
            requests.append(request)
            if request.method == "PUT":
                return agent_recovery.httpx.Response(
                    409, json={"error": {"code": "RoleAssignmentExists"}}
                )
            return agent_recovery.httpx.Response(200, json=next(pages))

        cloud = self.fake_arm_restore(arm)
        with self.assertRaisesRegex(
            agent_recovery.RecoveryError, "without a verified equivalent"
        ):
            self.run_cli()
        self.assertEqual(
            [request.method for request in requests], ["PUT", "GET", "GET"]
        )
        self.assert_role_restore_failed(cloud)

    def test_other_role_errors_never_lookup_or_enable(self):
        responses = [
            (409, {"error": {"code": "RoleAssignmentUpdateNotPermitted"}}),
            (409, {"error": {"code": "roleassignmentexists"}}),
            (403, {"error": {"code": "RoleAssignmentExists"}}),
            (500, {"error": {"code": "RoleAssignmentExists"}}),
            (409, {}),
            (409, {"error": None}),
            (409, {"error": "RoleAssignmentExists"}),
            (409, []),
            (409, "not-json"),
        ]
        for status, body in responses:
            with self.subTest(status=status, body=body):
                requests = []

                def arm(request):
                    requests.append(request)
                    self.assertEqual(request.method, "PUT")
                    if isinstance(body, str):
                        return agent_recovery.httpx.Response(status, text=body)
                    return agent_recovery.httpx.Response(status, json=body)

                cloud = self.fake_arm_restore(arm)
                with self.assertRaises(agent_recovery.httpx.HTTPStatusError) as raised:
                    self.run_cli()
                self.assertEqual(raised.exception.response.status_code, status)
                self.assertEqual(len(requests), 1)
                self.assert_role_restore_failed(cloud)

    def test_role_lookup_failures_never_enable_even_after_equivalent_match(self):
        for after_match in (False, True):
            for failure in (403, 500, "invalid-json", "transport"):
                with self.subTest(after_match=after_match, failure=failure):
                    requests = []

                    def arm(request):
                        requests.append(request)
                        if request.method == "PUT":
                            return agent_recovery.httpx.Response(
                                409, json={"error": {"code": "RoleAssignmentExists"}}
                            )
                        self.assertEqual(request.method, "GET")
                        if after_match and len(requests) == 2:
                            return agent_recovery.httpx.Response(
                                200,
                                json={
                                    "value": [self.existing_role_assignment()],
                                    "nextLink": str(request.url) + "&$skiptoken=next",
                                },
                            )
                        if failure == "transport":
                            raise agent_recovery.httpx.ConnectError(
                                "Fake lookup unavailable", request=request
                            )
                        if failure == "invalid-json":
                            return agent_recovery.httpx.Response(200, text="not-json")
                        return agent_recovery.httpx.Response(
                            failure, json={"error": {}}
                        )

                    cloud = self.fake_arm_restore(arm)
                    expected = {
                        "transport": agent_recovery.httpx.ConnectError,
                        "invalid-json": json.JSONDecodeError,
                    }.get(failure, agent_recovery.httpx.HTTPStatusError)
                    with self.assertRaises(expected):
                        self.run_cli()
                    self.assertEqual(len(requests), 3 if after_match else 2)
                    self.assert_role_restore_failed(cloud)

    def test_equivalent_role_without_assignment_id_fails_closed(self):
        for assignment_id in (None, "", 123):
            with self.subTest(assignment_id=assignment_id):
                existing = self.existing_role_assignment()
                existing["id"] = assignment_id

                def arm(request):
                    if request.method == "PUT":
                        return agent_recovery.httpx.Response(
                            409, json={"error": {"code": "RoleAssignmentExists"}}
                        )
                    return agent_recovery.httpx.Response(
                        200, json={"value": [existing]}
                    )

                cloud = self.fake_arm_restore(arm)
                with self.assertRaisesRegex(
                    agent_recovery.RecoveryError, "has no resource ID"
                ):
                    self.run_cli()
                self.assert_role_restore_failed(cloud)

    def test_successful_role_put_preserves_deterministic_id_body_and_auth(self):
        manifest = self.recovery_manifest(hosted=True)
        assignment = manifest["identity_role_assignments"]["old-principal"][
            "role_assignments"
        ][0]
        expected_id = (
            f"{assignment['scope']}/providers/Microsoft.Authorization/"
            "roleAssignments/dfc15ec2-8d6f-554e-a1b9-996c497e26fc"
        )
        for status in (200, 201):
            with self.subTest(status=status):
                requests = []

                def arm(request):
                    requests.append(request)
                    self.assertEqual(request.method, "PUT")
                    self.assertEqual(request.url.path, expected_id)
                    self.assertEqual(
                        dict(request.url.params), {"api-version": "2022-04-01"}
                    )
                    self.assertEqual(
                        request.headers["Authorization"], "Bearer sentinel"
                    )
                    self.assertEqual(
                        request.headers["Content-Type"], "application/json"
                    )
                    self.assertEqual(
                        json.loads(request.content),
                        {
                            "properties": {
                                "principalId": "old-principal",
                                "principalType": "ServicePrincipal",
                                "roleDefinitionId": assignment["role_definition_id"],
                                "condition": assignment["condition"],
                                "conditionVersion": assignment["condition_version"],
                                "description": assignment["description"],
                            }
                        },
                    )
                    return agent_recovery.httpx.Response(
                        status, json={"id": expected_id}
                    )

                cloud = self.fake_arm_restore(arm, manifest)
                self.assertEqual(self.run_cli()["state"], "completed")
                self.assertEqual(len(requests), 1)
                cloud.roles.credential.get_token.assert_called_once_with(
                    "https://management.azure.com/.default"
                )
                cloud.agents.enable.assert_called_once()
                report = json.loads(cloud.report.read_text())
                self.assertEqual(
                    report["role_assignments"][0]["result"], expected_id
                )

    def test_role_export_collection_retains_pagination_and_principal_filtering(self):
        existing = self.existing_role_assignment()
        existing["properties"]["description"] = "Restore direct RBAC"
        wrong_principal = copy.deepcopy(existing)
        wrong_principal["properties"]["principalId"] = "different-principal"
        next_link = (
            "https://management.azure.com/subscriptions/test/providers/"
            "Microsoft.Authorization/roleAssignments"
            "?api-version=2022-04-01&$skiptoken=next"
        )
        pages = iter(
            [
                {"value": [wrong_principal], "nextLink": next_link},
                {"value": [existing]},
            ]
        )
        requests = []

        def arm(request):
            requests.append(request)
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.headers["Authorization"], "Bearer sentinel")
            return agent_recovery.httpx.Response(200, json=next(pages))

        roles = self.fake_arm_role_client(arm)
        self.assertEqual(
            roles.list_for_principal("old-principal", ["test"]),
            self.recovery_manifest(hosted=True)["identity_role_assignments"][
                "old-principal"
            ]["role_assignments"],
        )
        self.assertEqual(len(requests), 2)
        self.assertEqual(
            dict(requests[0].url.params),
            {"api-version": "2022-04-01", "$filter": "principalId eq 'old-principal'"},
        )
        self.assertEqual(str(requests[1].url), next_link)

    def test_roles_without_source_principal_fail_before_sdk_operations(self):
        manifest = self.recovery_manifest(hosted=True)
        manifest["agents"][0]["versions"][0]["original_identity_principal_id"] = None
        path = self.directory / "manifest.json"
        path.write_text(json.dumps(manifest))
        with patch.object(
            sys, "argv", ["agent_recovery.py", "validate", "--manifest", str(path)]
        ):
            with self.assertRaisesRegex(
                agent_recovery.RecoveryError, "no source principal"
            ):
                self.run_cli()
        self.credential_guard.assert_not_called()
        self.project_guard.assert_not_called()

    def test_invalid_source_principal_is_explicit(self):
        for principal in ("", " ", [], 3):
            with self.subTest(principal=principal):
                manifest = self.recovery_manifest(hosted=True)
                manifest["agents"][0]["versions"][0][
                    "original_identity_principal_id"
                ] = principal
                with self.assertRaisesRegex(
                    agent_recovery.RecoveryError, "invalid source principal ID"
                ):
                    agent_recovery.validate_manifest(manifest)

    def test_validate_optional_endpoint_uses_restore_normalization_offline(self):
        manifest = self.recovery_manifest()
        manifest["project_endpoint"] += "/"
        path = self.directory / "manifest.json"
        path.write_text(json.dumps(manifest))
        for endpoint in (
            None,
            manifest["project_endpoint"].rstrip("/"),
            manifest["project_endpoint"] + "/",
        ):
            with self.subTest(endpoint=endpoint):
                argv = ["agent_recovery.py", "validate", "--manifest", str(path)]
                if endpoint is not None:
                    argv += ["--project-endpoint", endpoint]
                with patch.object(sys, "argv", argv):
                    self.assertEqual(self.run_cli()["state"], "exported")
        self.credential_guard.assert_not_called()
        self.project_guard.assert_not_called()

    def test_endpoint_mismatch_fails_validate_and_restore_before_sdk_or_output(self):
        manifest = self.recovery_manifest()
        path = self.directory / "manifest.json"
        path.write_text(json.dumps(manifest))
        output = self.directory / "report.json"
        for command in ("validate", "restore"):
            with self.subTest(command=command):
                argv = [
                    "agent_recovery.py", command, "--manifest", str(path),
                    "--project-endpoint", "https://other.test/api/projects/project",
                ]
                if command == "restore":
                    argv += ["--output", str(output)]
                with patch.object(sys, "argv", argv):
                    with self.assertRaisesRegex(
                        agent_recovery.RecoveryError, "different project endpoint"
                    ):
                        self.run_cli()
                self.assertFalse(output.exists())
        self.credential_guard.assert_not_called()
        self.project_guard.assert_not_called()

    def test_restore_explicit_project_change_remains_supported(self):
        cloud = self.fake_restore_cli(
            extra_args=(
                "--project-endpoint", "https://other.test/api/projects/project/",
                "--allow-project-change",
            )
        )
        self.assertEqual(self.run_cli()["state"], "completed")
        self.assertEqual(
            json.loads(cloud.report.read_text())["project_endpoint"],
            "https://other.test/api/projects/project",
        )

    @unittest.skipUnless(os.name == "posix", "POSIX owner-only permissions required")
    def test_private_artifact_is_owner_only_from_creation_under_permissive_umask(self):
        path = self.directory / "manifest.json"
        real_fchmod = os.fchmod
        modes = []

        def check_before_chmod(descriptor, mode):
            modes.append(stat.S_IMODE(os.fstat(descriptor).st_mode))
            self.assertEqual(os.fstat(descriptor).st_size, 0)
            real_fchmod(descriptor, mode)

        previous = os.umask(0)
        try:
            with patch.object(
                agent_recovery.os, "fchmod", side_effect=check_before_chmod
            ):
                agent_recovery.write_private_json(path, {"sensitive": "artifact"})
        finally:
            os.umask(previous)
        self.assertEqual(modes, [0o600])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(set(self.directory.iterdir()), {path})

    def test_private_writer_chmod_failure_never_publishes_or_writes_data(self):
        path = self.directory / "manifest.json"
        path.write_text("old artifact")
        with (
            patch.object(
                agent_recovery.os, "fchmod", side_effect=PermissionError("denied")
            ),
            patch.object(agent_recovery.json, "dump") as dump,
        ):
            with self.assertRaisesRegex(agent_recovery.RecoveryError, "owner-only"):
                agent_recovery.write_private_json(path, {"sensitive": "new artifact"})
        dump.assert_not_called()
        self.assertEqual(path.read_text(), "old artifact")
        self.assertEqual(set(self.directory.iterdir()), {path})

    def test_private_writer_rejects_ineffective_protection(self):
        path = self.directory / "manifest.json"
        with (
            patch.object(
                agent_recovery.os, "fstat",
                return_value=SimpleNamespace(st_mode=0o100644, st_uid=os.geteuid()),
            ),
            patch.object(agent_recovery.json, "dump") as dump,
        ):
            with self.assertRaisesRegex(agent_recovery.RecoveryError, "owner-only"):
                agent_recovery.write_private_json(path, {"sensitive": "artifact"})
        dump.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_private_writer_failure_cleans_partial_file_preserves_destination(self):
        for target, error in (
            ("dump", TypeError("not serializable")),
            ("fsync", OSError("disk failure")),
            ("replace", OSError("replace failure")),
        ):
            with self.subTest(target=target):
                path = self.directory / "manifest.json"
                path.write_text("old artifact")
                owner = (
                    agent_recovery.json if target == "dump"
                    else agent_recovery.os if target == "fsync"
                    else Path
                )
                with patch.object(owner, target, side_effect=error):
                    with self.assertRaises((TypeError, agent_recovery.RecoveryError)):
                        agent_recovery.write_private_json(
                            path, {"sensitive": "artifact"}
                        )
                self.assertEqual(path.read_text(), "old artifact")
                self.assertEqual(set(self.directory.iterdir()), {path})

    def test_private_writer_does_not_follow_preexisting_temporary_symlink(self):
        path = self.directory / "manifest.json"
        target = self.directory / "unrelated.json"
        target.write_text("unchanged")
        temporary = self.directory / ".manifest.json.fixed.tmp"
        temporary.symlink_to(target.name)
        with patch.object(
            agent_recovery.uuid, "uuid4", return_value=SimpleNamespace(hex="fixed")
        ):
            with self.assertRaisesRegex(agent_recovery.RecoveryError, "owner-only"):
                agent_recovery.write_private_json(path, {"sensitive": "artifact"})
        self.assertEqual(target.read_text(), "unchanged")
        self.assertTrue(temporary.is_symlink())
        self.assertFalse(path.exists())

    def test_private_writer_atomically_replaces_existing_destination_with_private_mode(self):
        path = self.directory / "manifest.json"
        path.write_text("old artifact")
        path.chmod(0o644)
        real_replace = Path.replace

        def check_before_replace(temporary, destination):
            self.assertEqual(destination.read_text(), "old artifact")
            self.assertEqual(stat.S_IMODE(temporary.stat().st_mode), 0o600)
            self.assertEqual(json.loads(temporary.read_text()), {"sensitive": "new"})
            return real_replace(temporary, destination)

        with patch.object(Path, "replace", autospec=True, side_effect=check_before_replace):
            agent_recovery.write_private_json(path, {"sensitive": "new"})
        self.assertEqual(json.loads(path.read_text()), {"sensitive": "new"})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(set(self.directory.iterdir()), {path})

    def fake_windows_writer(self):
        self.start_patch(
            patch.object(
                agent_recovery.os,
                "open",
                side_effect=AssertionError("Windows must not use the POSIX writer"),
            )
        )
        self.start_patch(
            patch.object(
                Path,
                "mkdir",
                side_effect=AssertionError("Only the ACL helper may create directories"),
            )
        )
        return SimpleNamespace(
            helper=self.start_patch(
                patch.object(agent_recovery.os.path, "isfile", return_value=True)
            ),
            runtime=self.start_patch(
                patch.object(
                    agent_recovery.shutil,
                    "which",
                    return_value=r"C:\Program Files\PowerShell\7\pwsh.exe",
                )
            ),
            run=self.start_patch(
                patch.object(
                    agent_recovery.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        args=[], returncode=0, stdout=b"", stderr=b""
                    ),
                )
            ),
        )

    def test_windows_private_writer_passes_utf8_json_only_over_stdin(self):
        path = self.directory / "private directory" / "manifest with spaces.json"
        helper = os.path.join(
            os.path.dirname(os.path.abspath(agent_recovery.__file__)),
            "Write-PrivateRecoveryFile.ps1",
        )
        absolute_path = os.path.abspath(path)
        secret = "sensitive-token-'$(not-a-command)'"
        value = {"sensitive": secret, "instructions": "café 日本語\nnext line"}
        writer = self.fake_windows_writer()
        with patch.object(agent_recovery.os, "name", "nt"):
            agent_recovery.write_private_json(path, value)
        writer.helper.assert_called_once_with(helper)
        writer.runtime.assert_called_once_with("pwsh")
        writer.run.assert_called_once_with(
            [
                writer.runtime.return_value,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                helper,
                "-Path",
                absolute_path,
            ],
            input=json.dumps(value, indent=2).encode("utf-8"),
            capture_output=True,
            shell=False,
            check=False,
            timeout=60,
        )
        self.assertNotIn(secret, " ".join(writer.run.call_args.args[0]))
        self.assertIn(b"caf\\u00e9", writer.run.call_args.kwargs["input"])
        self.assertEqual(json.loads(writer.run.call_args.kwargs["input"]), value)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_windows_private_writer_resolves_relative_destination(self):
        path = Path(os.path.relpath(self.directory / "manifest.json"))
        absolute_path = os.path.abspath(path)
        writer = self.fake_windows_writer()
        with patch.object(agent_recovery.os, "name", "nt"):
            agent_recovery.write_private_json(path, {"sensitive": "artifact"})
        self.assertEqual(writer.run.call_args.args[0][-2:], ["-Path", absolute_path])
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_windows_private_writer_requires_sibling_helper(self):
        path = self.directory / "manifest.json"
        writer = self.fake_windows_writer()
        writer.helper.return_value = False
        with patch.object(agent_recovery.os, "name", "nt"):
            with self.assertRaisesRegex(
                agent_recovery.RecoveryError, "Write-PrivateRecoveryFile.ps1 is missing"
            ):
                agent_recovery.write_private_json(path, {"sensitive": "artifact"})
        writer.runtime.assert_not_called()
        writer.run.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_windows_private_writer_requires_powershell_runtime(self):
        path = self.directory / "manifest.json"
        writer = self.fake_windows_writer()
        writer.runtime.return_value = None
        with patch.object(agent_recovery.os, "name", "nt"):
            with self.assertRaisesRegex(
                agent_recovery.RecoveryError, r"PowerShell 7\.3.*pwsh.*required"
            ):
                agent_recovery.write_private_json(path, {"sensitive": "artifact"})
        writer.runtime.assert_called_once_with("pwsh")
        writer.run.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_windows_private_writer_nonzero_exit_does_not_leak_diagnostics(self):
        path = self.directory / "manifest.json"
        secret = "private-child-diagnostic-token"
        writer = self.fake_windows_writer()
        for returncode in (1, 23, -1):
            with self.subTest(returncode=returncode):
                writer.run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=returncode,
                    stdout=secret.encode(), stderr=secret.encode(),
                )
                with patch.object(agent_recovery.os, "name", "nt"):
                    with self.assertRaisesRegex(
                        agent_recovery.RecoveryError, f"exit code {returncode}"
                    ) as failure:
                        agent_recovery.write_private_json(path, {"sensitive": secret})
                self.assertNotIn(
                    secret, "".join(traceback.format_exception(failure.exception))
                )
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_windows_private_writer_timeout_is_safe_and_explicit(self):
        path = self.directory / "manifest.json"
        secret = "private-timeout-diagnostic-token"
        writer = self.fake_windows_writer()
        writer.run.side_effect = subprocess.TimeoutExpired(
            cmd=["pwsh"], timeout=60,
            output=secret.encode(), stderr=secret.encode(),
        )
        with patch.object(agent_recovery.os, "name", "nt"):
            with self.assertRaisesRegex(
                agent_recovery.RecoveryError, "timed out after 60 seconds"
            ) as failure:
                agent_recovery.write_private_json(path, {"sensitive": secret})
        self.assertNotIn(
            secret, "".join(traceback.format_exception(failure.exception))
        )
        self.assertTrue(failure.exception.__suppress_context__)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_windows_private_writer_os_errors_are_safe_and_explicit(self):
        path = self.directory / "manifest.json"
        secret = "private-os-error-diagnostic-token"
        writer = self.fake_windows_writer()
        for error in (
            FileNotFoundError(2, secret),
            PermissionError(13, secret),
            OSError(secret),
        ):
            with self.subTest(error=type(error).__name__):
                writer.run.side_effect = error
                with patch.object(agent_recovery.os, "name", "nt"):
                    with self.assertRaisesRegex(
                        agent_recovery.RecoveryError,
                        f"Couldn't execute Windows ACL writer.*{type(error).__name__}",
                    ) as failure:
                        agent_recovery.write_private_json(path, {"sensitive": secret})
                self.assertNotIn(
                    secret, "".join(traceback.format_exception(failure.exception))
                )
                self.assertTrue(failure.exception.__suppress_context__)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_windows_private_writer_failure_preserves_existing_destination(self):
        path = self.directory / "manifest.json"
        path.write_text("old artifact", encoding="utf-8")
        writer = self.fake_windows_writer()
        writer.run.return_value.returncode = 1
        with patch.object(agent_recovery.os, "name", "nt"):
            with self.assertRaises(agent_recovery.RecoveryError):
                agent_recovery.write_private_json(path, {"sensitive": "new artifact"})
        self.assertEqual(path.read_text(encoding="utf-8"), "old artifact")
        self.assertEqual(set(self.directory.iterdir()), {path})

    def test_private_writer_rejects_unknown_platform_before_creating_artifact(self):
        path = self.directory / "manifest.json"
        writer = self.fake_windows_writer()
        with patch.object(agent_recovery.os, "name", "unknown"):
            with self.assertRaisesRegex(agent_recovery.RecoveryError, "unsupported.*unknown"):
                agent_recovery.write_private_json(path, {"sensitive": "artifact"})
        writer.helper.assert_not_called()
        writer.runtime.assert_not_called()
        writer.run.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
