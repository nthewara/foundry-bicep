"""Offline end-user PowerShell flows. External Azure/SDK operations are fakes."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
ACCOUNT = (
    "/subscriptions/sub/resourceGroups/rg/providers/"
    "Microsoft.CognitiveServices/accounts/account"
)
PROJECT = ACCOUNT + "/projects/project"
ENDPOINT = "https://account.services.ai.azure.com/api/projects/project"
HARNESS = r"""
$ErrorActionPreference = "Stop"
$global:mockDeleted = @{}
$global:mockTrust = $null
function Log-Call([string]$Kind, [object]$Arguments) {
    $line = @{kind=$Kind; arguments=@($Arguments)} | ConvertTo-Json -Compress -Depth 20
    [IO.File]::AppendAllText($env:RECOVERY_TEST_LOG, "$line`n")
}
function Start-Sleep { }
function az {
    $a = @($args)
    Log-Call "az" $a
    $global:LASTEXITCODE = 0
    if ($a[0] -eq "account") { return "{}" }
    if ($a[0] -ne "rest") { throw "Unexpected Azure CLI command: $a" }
    $method = $a[[Array]::IndexOf($a, "--method") + 1]
    $url = $a[[Array]::IndexOf($a, "--url") + 1]
    if ($env:RECOVERY_TEST_FAIL_GET -eq "1" -and $method -eq "get") {
        $global:LASTEXITCODE = 1
        return "AuthorizationFailed"
    }
    if ($method -eq "delete") {
        $global:mockDeleted[$url] = $true
        return "{}"
    }
    if ($method -eq "put") {
        $global:mockDeleted[$url] = $false
        return "{}"
    }
    if ($method -eq "patch") {
        $body = $a[[Array]::IndexOf($a, "--body") + 1].Substring(1)
        $global:mockTrust = (Get-Content -LiteralPath $body -Raw | ConvertFrom-Json).properties.trustedCertificates
        return "{}"
    }
    if ($method -ne "get") { throw "Unexpected ARM method: $method" }
    if ($url -match "/projects\?") {
        return (@{
            value=@(@{id=$env:RECOVERY_TEST_PROJECT; name="project"})
            nextLink="https://management.azure.com/mock-page-2"
        } | ConvertTo-Json -Depth 10)
    }
    if ($url -match "/mock-page-2") {
        $items = @()
        if ($env:RECOVERY_TEST_SIBLING -eq "1") {
            $items = @(@{id="$($env:RECOVERY_TEST_ACCOUNT)/projects/other"; name="other"})
        }
        return (@{value=$items} | ConvertTo-Json -Depth 10)
    }
    if ($url -match "/connections\?") { return '{"value":[]}' }
    if ($url -match "/capabilityHosts\?") {
        return '{"value":[{"name":"host","properties":{"capabilityHostKind":"Agents","provisioningState":"Succeeded"}}]}'
    }
    if ($url -match "/capabilityHosts/") {
        if ($global:mockDeleted[$url]) {
            $global:LASTEXITCODE = 1
            return "ResourceNotFound"
        }
        return '{"name":"host","properties":{"capabilityHostKind":"Agents","provisioningState":"Succeeded"}}'
    }
    if ($url -match "/accounts/account\?") {
        if ($null -eq $global:mockTrust) {
            $global:mockTrust = @(@{
                keyVaultId="/subscriptions/sub/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/other"
                certificates=@(@{name="preserved"; version="abc"})
            })
        }
        return (@{properties=@{trustedCertificates=$global:mockTrust}} | ConvertTo-Json -Depth 10)
    }
    throw "Unexpected ARM URL: $url"
}
function Invoke-FakePython {
    $a = @($args)
    Log-Call "python" $a
    $global:LASTEXITCODE = 0
    if ($a[0] -eq "-c") { return }
    if ($a[1] -eq "validate") {
        # Real local CLI validation, with network connects prohibited.
        $guard = 'import runpy,socket,sys; socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError("Network forbidden")); sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name="__main__")'
        & $env:RECOVERY_TEST_PYTHON -c $guard @a
        return
    }
    if ($a[1] -eq "export") {
        $output = $a[[Array]::IndexOf($a, "--output") + 1]
        [IO.File]::Copy($env:RECOVERY_TEST_AGENT_TEMPLATE, $output)
        return
    }
    if ($a[1] -eq "restore") {
        $output = $a[[Array]::IndexOf($a, "--output") + 1]
        [IO.File]::WriteAllText($output, '{"state":"completed"}')
        return
    }
    throw "Unexpected Python command: $a"
}
$parameters = Get-Content -LiteralPath $env:RECOVERY_TEST_PARAMETERS -Raw | ConvertFrom-Json -AsHashtable
& $env:RECOVERY_TEST_SCRIPT @parameters
"""


class PowerShellRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("pwsh"):
            raise RuntimeError("PowerShell 7 (pwsh) is required for the offline CLI tests.")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.recovery = self.base / "backup"
        self.recovery.mkdir(mode=0o700)
        self.manifest = {
            "schema_version": 1,
            "state": "exported",
            "project_endpoint": ENDPOINT,
            "agents": [],
            "identity_role_assignments": {},
        }
        self.capability = {
            "schemaVersion": 1,
            "accountResourceId": ACCOUNT,
            "projectResourceId": PROJECT,
            "projectEndpoint": ENDPOINT,
            "capabilityHostApiVersion": "2025-06-01",
            "projectConnectionApiVersion": "2025-04-01-preview",
            "projectCapabilityHost": {
                "name": "host",
                "properties": {"capabilityHostKind": "Agents"},
            },
            "accountCapabilityHost": {
                "name": "host",
                "properties": {"capabilityHostKind": "Agents"},
            },
        }

    def run_script(self, mode="Recreate", *, trust=False, sibling=False, fresh=False,
                   fail_get=False, **overrides):
        if not fresh:
            (self.recovery / "agents.json").write_text(json.dumps(self.manifest))
            (self.recovery / "capability-hosts.json").write_text(json.dumps(self.capability))
        template = self.base / "template.json"
        template.write_text(json.dumps(self.manifest))
        harness = self.base / "harness.ps1"
        harness.write_text(HARNESS)
        parameters = {
            "SubscriptionId": "sub",
            "ResourceGroup": "rg",
            "AccountName": "account",
            "Confirm": False,
        }
        if trust:
            parameters.update({
                "KeyVaultResourceId": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/selected",
                "CertificateReference": ["root=abc123"],
                "OutputPath": str(self.base / "trust.json"),
            })
        else:
            parameters.update({
                "Mode": mode,
                "ProjectName": "project",
                "ProjectEndpoint": ENDPOINT,
                "RecoveryDirectory": str(self.recovery),
                "PythonExecutable": "Invoke-FakePython",
                "AcknowledgeDataLoss": True,
            })
        parameters.update(overrides)
        parameter_file = self.base / "parameters.json"
        parameter_file.write_text(json.dumps(parameters))
        log = self.base / "calls.jsonl"
        env = {
            **os.environ,
            "RECOVERY_TEST_LOG": str(log),
            "RECOVERY_TEST_PROJECT": PROJECT,
            "RECOVERY_TEST_ACCOUNT": ACCOUNT,
            "RECOVERY_TEST_SCRIPT": str(ROOT / (
                "Set-PrivateCaTrust.ps1" if trust else "Recreate-CapabilityHostAndAgents.ps1"
            )),
            "RECOVERY_TEST_PARAMETERS": str(parameter_file),
            "RECOVERY_TEST_PYTHON": sys.executable,
            "RECOVERY_TEST_AGENT_TEMPLATE": str(template),
            "RECOVERY_TEST_SIBLING": "1" if sibling else "0",
            "RECOVERY_TEST_FAIL_GET": "1" if fail_get else "0",
        }
        result = subprocess.run(
            ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(harness)],
            text=True, capture_output=True, env=env, timeout=45,
        )
        self.calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        self.mutations = [
            call for call in self.calls if (
                call["kind"] == "az" and "--method" in call["arguments"]
                and call["arguments"][call["arguments"].index("--method") + 1] in {"put", "delete", "patch"}
            ) or (
                call["kind"] == "python" and len(call["arguments"]) > 1
                and call["arguments"][1] == "restore" and "--dry-run" not in call["arguments"]
            )
        ]
        self.output = result.stdout + result.stderr
        return result

    def test_restore_whatif_forwards_dry_run_without_mutations(self):
        result = self.run_script(mode="RestoreAgents", WhatIf=True)
        self.assertEqual(result.returncode, 0, self.output)
        self.assertEqual(self.mutations, [])
        self.assertTrue(any("--dry-run" in call["arguments"] for call in self.calls))

    def test_restore_explicit_confirm_cannot_bypass_prompt(self):
        result = self.run_script(mode="RestoreAgents", Confirm=True)
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertEqual(self.mutations, [])

    def test_restore_confirm_false_can_replay(self):
        result = self.run_script(mode="RestoreAgents")
        self.assertEqual(result.returncode, 0, self.output)
        self.assertEqual(len(self.mutations), 1)

    def test_recreate_whatif_cannot_mutate(self):
        result = self.run_script(WhatIf=True)
        self.assertEqual(result.returncode, 0, self.output)
        self.assertEqual(self.mutations, [])

    def test_recreate_explicit_confirm_cannot_bypass_prompt(self):
        result = self.run_script(Confirm=True)
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertEqual(self.mutations, [])

    def test_recreate_without_acknowledgement_cannot_mutate(self):
        result = self.run_script(AcknowledgeDataLoss=False)
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertEqual(self.mutations, [])

    def test_mixed_manifest_rejected_before_host_deletion(self):
        self.manifest["project_endpoint"] = ENDPOINT + "-wrong"
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertEqual(self.mutations, [])
        self.assertIn("different project endpoint", self.output)

    def test_validate_rejects_mixed_manifests(self):
        self.manifest["project_endpoint"] = ENDPOINT + "-wrong"
        result = self.run_script(mode="Validate")
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertEqual(self.mutations, [])

    def test_restore_rejects_wrong_capability_manifest(self):
        self.capability["projectEndpoint"] = ENDPOINT + "-wrong"
        result = self.run_script(mode="RestoreAgents")
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertEqual(self.mutations, [])

    def test_account_recreation_refuses_sibling_on_later_page(self):
        result = self.run_script(RecreateAccountCapabilityHost=True, sibling=True)
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertEqual(self.mutations, [])
        self.assertIn("other project", self.output.lower())
        self.assertTrue(any("https://management.azure.com/mock-page-2" in call["arguments"] for call in self.calls))

    def test_account_recreation_single_project_all_pages(self):
        result = self.run_script(RecreateAccountCapabilityHost=True)
        self.assertEqual(result.returncode, 0, self.output)
        self.assertEqual(len(self.mutations), 5)
        self.assertTrue(any("https://management.azure.com/mock-page-2" in call["arguments"] for call in self.calls))

    def test_failed_preflight_does_not_mutate(self):
        result = self.run_script(fail_get=True)
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertEqual(self.mutations, [])

    def test_fresh_export_artifacts_are_private(self):
        result = self.run_script(mode="Export", fresh=True)
        self.assertEqual(result.returncode, 0, self.output)
        self.assertEqual(self.mutations, [])
        self.assertEqual((self.recovery / "capability-hosts.json").stat().st_mode & 0o777, 0o600)

    def test_fresh_recreate_whatif_can_export_without_mutating(self):
        self.recovery.rmdir()
        result = self.run_script(fresh=True, WhatIf=True)
        self.assertEqual(result.returncode, 0, self.output)
        self.assertEqual(self.mutations, [])
        self.assertTrue((self.recovery / "capability-hosts.json").exists())
        self.assertEqual(self.recovery.stat().st_mode & 0o777, 0o700)

    def test_fresh_account_export_refuses_other_projects(self):
        result = self.run_script(
            mode="Export", fresh=True, RecreateAccountCapabilityHost=True, sibling=True
        )
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertEqual(self.mutations, [])
        self.assertFalse((self.recovery / "capability-hosts.json").exists())

    def test_insecure_recovery_directory_fails_before_mutation(self):
        self.recovery.chmod(0o755)
        result = self.run_script(mode="Export", fresh=True)
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertEqual(self.mutations, [])
        self.assertFalse((self.recovery / "capability-hosts.json").exists())

    def test_registration_whatif_does_not_patch(self):
        result = self.run_script(trust=True, WhatIf=True)
        self.assertEqual(result.returncode, 0, self.output)
        self.assertEqual(self.mutations, [])
        self.assertFalse((self.base / "trust.json").exists())

    def test_registration_confirm_and_reference_preservation(self):
        result = self.run_script(trust=True)
        self.assertEqual(result.returncode, 0, self.output)
        self.assertEqual(len(self.mutations), 1)
        report = json.loads((self.base / "trust.json").read_text())
        self.assertEqual(len(report["trustedCertificates"]), 2)

    def test_registration_explicit_confirm_cannot_bypass_prompt(self):
        result = self.run_script(trust=True, Confirm=True)
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertEqual(self.mutations, [])

    def test_all_powershell_files_parse(self):
        code = (
            "$failed = $false; Get-ChildItem -LiteralPath $env:RECOVERY_PARSE_ROOT -Filter '*.ps1' | "
            "ForEach-Object { $e=$null; $t=$null; "
            "[System.Management.Automation.Language.Parser]::ParseFile($_.FullName,[ref]$t,[ref]$e) | Out-Null; "
            "if ($e.Count) { $e | Out-String | Write-Error; $failed=$true } }; "
            "if ($failed) { exit 1 }"
        )
        result = subprocess.run(
            ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", code],
            capture_output=True, text=True,
            env={**os.environ, "RECOVERY_PARSE_ROOT": str(ROOT)}, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def run_private_writer(self, code):
        result = subprocess.run(
            [
                "pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                "$ErrorActionPreference='Stop'; "
                ". (Join-Path $env:RECOVERY_WRITER_ROOT 'Private-RecoveryFiles.ps1'); "
                + code,
            ],
            capture_output=True, text=True, timeout=20,
            env={
                **os.environ,
                "RECOVERY_WRITER_ROOT": str(ROOT),
                "RECOVERY_WRITER_PATH": str(self.recovery / "protected.json"),
            },
        )
        self.output = result.stdout + result.stderr
        return result

    def test_windows_platform_selects_acl_writer_before_content(self):
        result = self.run_private_writer(r"""
function Test-RecoveryWindows { return $true }
function Initialize-WindowsRecoveryDirectory { param($Path); Write-Host "WINDOWS-DIRECTORY" }
function New-WindowsRecoveryFile {
    param($Path)
    Write-Host "WINDOWS-FILE"
    return [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
}
Write-JsonFile -Path $env:RECOVERY_WRITER_PATH -Value @{value="synthetic"}
""")
        self.assertEqual(result.returncode, 0, self.output)
        self.assertIn("WINDOWS-DIRECTORY", self.output)
        self.assertIn("WINDOWS-FILE", self.output)
        self.assertEqual(json.loads((self.recovery / "protected.json").read_text()), {"value": "synthetic"})

    def test_windows_directory_acl_failure_prevents_file_creation(self):
        result = self.run_private_writer(r"""
function Test-RecoveryWindows { return $true }
function Initialize-WindowsRecoveryDirectory { param($Path); throw "Directory ACL denied" }
function New-WindowsRecoveryFile { throw "Must not create a file" }
Write-JsonFile -Path $env:RECOVERY_WRITER_PATH -Value @{value="synthetic"}
""")
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertIn("Directory ACL denied", self.output)
        self.assertFalse((self.recovery / "protected.json").exists())
        self.assertEqual(list(self.recovery.iterdir()), [])

    def test_windows_file_acl_failure_preserves_existing_output(self):
        output = self.recovery / "protected.json"
        output.write_text('{"existing":true}')
        result = self.run_private_writer(r"""
function Test-RecoveryWindows { return $true }
function Initialize-WindowsRecoveryDirectory { param($Path) }
function New-WindowsRecoveryFile {
    param($Path)
    [IO.File]::WriteAllText($Path, "")
    throw "File ACL verification failed"
}
Write-JsonFile -Path $env:RECOVERY_WRITER_PATH -Value @{value="synthetic"}
""")
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertIn("File ACL verification failed", self.output)
        self.assertEqual(json.loads(output.read_text()), {"existing": True})
        self.assertEqual(list(self.recovery.iterdir()), [output])

    def test_windows_acl_policy_rejects_inheritance_other_users_and_missing_rights(self):
        setup = r"""
function Get-RecoveryWindowsSid { return [pscustomobject]@{Value="current-user-sid"} }
$global:mockOwner = [pscustomobject]@{Value="current-user-sid"}
$rule = [pscustomobject]@{
    IdentityReference=[pscustomobject]@{Value="current-user-sid"}
    AccessControlType=[Security.AccessControl.AccessControlType]::Allow
    FileSystemRights=[Security.AccessControl.FileSystemRights]::FullControl
    IsInherited=$false
    PropagationFlags=[Security.AccessControl.PropagationFlags]::None
}
$global:mockRules = @($rule)
$security = [pscustomobject]@{AreAccessRulesProtected=$true}
$security | Add-Member ScriptMethod GetOwner { param($Type); return $global:mockOwner }
$security | Add-Member ScriptMethod GetAccessRules { param($Explicit,$Inherited,$Type); return $global:mockRules }
"""
        variants = {
            "valid": "",
            "inheritance": "$security.AreAccessRulesProtected=$false",
            "owner": '$global:mockOwner.Value="other-user-sid"',
            "grantee": '$rule.IdentityReference.Value="other-user-sid"',
            "inherited": "$rule.IsInherited=$true",
            "missing": "$global:mockRules=@()",
            "extra": "$global:mockRules=@($rule,$rule)",
            "read-only": "$rule.FileSystemRights=[Security.AccessControl.FileSystemRights]::Read",
            "deny": "$rule.AccessControlType=[Security.AccessControl.AccessControlType]::Deny",
            "inherit-only": "$rule.PropagationFlags=[Security.AccessControl.PropagationFlags]::InheritOnly",
        }
        for name, modification in variants.items():
            with self.subTest(name=name):
                result = self.run_private_writer(
                    setup + "\n" + modification + "\nAssert-PrivateWindowsSecurity -Security $security"
                )
                if name == "valid":
                    self.assertEqual(result.returncode, 0, self.output)
                else:
                    self.assertNotEqual(result.returncode, 0, self.output)
                    self.assertIn("Recovery ACL", self.output)

    def test_private_writer_stdin_entrypoint_preserves_json_without_output(self):
        output = self.recovery / "file with spaces.json"
        value = {"synthetic-auth": "test-value", "array": [1, {"nested": None}]}
        result = subprocess.run(
            [
                "pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
                str(ROOT / "Write-PrivateRecoveryFile.ps1"), "-Path", str(output),
            ],
            input=json.dumps(value), capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(json.loads(output.read_text()), value)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
