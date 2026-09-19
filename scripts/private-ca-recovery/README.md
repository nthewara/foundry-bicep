# Private CA capability-host and agent recovery

Use these scripts when you add or rotate `trustedCertificates` on an existing
Foundry account and need the project runtime to load the updated CA trust.

Adapted from the seven recovery files in
[`microsoft-foundry/foundry-samples`](https://github.com/microsoft-foundry/foundry-samples/tree/e0f4042a0080158d4fe351dfe7c3fb47a9b75a5e/infrastructure/infrastructure-setup-bicep/19-private-network-agent-tools/scripts/private-ca-recovery),
upstream main `e0f4042a0080158d4fe351dfe7c3fb47a9b75a5e`, including sample update
`74b93d65e1b2c1e22d661ef883a747962f47a8ee` (September 15, 2026).
This adaptation adds confirmation, cross-manifest preflight, single-project
account guards, required-RBAC readiness checks, and protected artifact writes.
No cloud deployment or destructive recovery is certified by the offline tests.

> [!CAUTION]
> Deleting a project capability host is a destructive reset. Existing agent,
> conversation, file, and vector-store state can become permanently orphaned.
> Export the recovery manifest first, keep normal source-controlled deployment
> assets, and use this workflow only when capability-host recreation is required.

## What the scripts preserve

| Surface | Recovery source | Behavior |
| --- | --- | --- |
| Capability host | ARM resource | Saves the writable host properties and verifies every referenced project connection before deletion. |
| Prompt Agent | Cosmos-backed Foundry Agent API | Exports the materialized Prompt Agent definition through the supported API before deletion, then creates a new version. The script never reads or edits Cosmos DB containers directly. |
| Hosted Agent | Existing container image | Exports the container image, protocol, compute, telemetry, environment, metadata, and endpoint configuration, then creates a new version from that image. |
| Agent endpoint | Foundry Agent API | Preserves the endpoint/card configuration, remaps routed versions, and restores the enabled or disabled state. |
| Agent identity | Azure RBAC | Captures direct role assignments in the selected subscriptions and reapplies them to each new version identity before endpoint routing is restored. |

The default export retains every version definition but restores only the latest
version and any version referenced by endpoint routing. Use
`-RestoreAllAgentVersions` when version history is required.

Container-based Hosted Agents are supported. If a selected Hosted Agent was
deployed from source code instead of `container_configuration.image`, export
stops before any destructive action. Redeploy that agent from its original
source or agent manifest.

## Reproducibility and customer portability

The workflow is customer-neutral: it contains no fixed tenant, subscription,
region, resource name, network, or certificate assumptions. Every environment
value is supplied through parameters or discovered from the selected Foundry
account and project, so customers can run the same export, trust update,
validation, recreation, and resume steps in their own environment.

Reproducibility safeguards include:

- exact top-level Python dependency pins in `requirements.txt`;
- versioned ARM and Foundry API usage recorded in the recovery artifacts;
- one protected recovery directory per snapshot, with no silent overwrite;
- definition hashes and pre-destructive validation;
- deterministic version ordering and one-to-one version replay, including safe
  reuse after a partial retry;
- immutable container image digests as the recommended Hosted Agent source.

The scripts are usable by any customer whose subscription, region, and Foundry
account support the referenced features. They don't bypass service availability:
`trustedCertificates` and Hosted Agents can be preview or allowlist-gated, and
private networking, Key Vault RBAC, regional support, quotas, and agent API
availability must already be satisfied for that customer.

For non-default layouts, use the provided parameters instead of changing the
scripts: `-ProjectEndpoint` overrides endpoint discovery,
`-ProjectCapabilityHostName` and `-AccountCapabilityHostName` disambiguate
multiple hosts, the API-version parameters support an explicitly approved
service version, and `-RoleAssignmentSubscriptionId` accepts an array for
cross-subscription dependencies.

## Prerequisites

- PowerShell 7.3 or later (`pwsh`) on `PATH`. The repository's Windows jump-box
  is a supported administration host; use PowerShell 7, not the built-in Windows
  PowerShell 5.1. On Windows, use a filesystem enforcing Windows ACLs, such as
  local NTFS. Linux and macOS require a filesystem enforcing POSIX permissions.
  Script execution must be permitted by your organization's execution policy;
  the tooling does not bypass that policy.
- Azure CLI authenticated to the tenant that contains the Foundry resource.
- Python 3.10 or later.
- Run from your own private-network environment with access to the project and
  ARM endpoints, such as a VPN, ExpressRoute-connected machine, or VNet jump
  host. No sample environment or credentials are included.
- Permissions to read/write capability hosts and agent versions.
- The Foundry account identity has **Key Vault Secrets User** on the vault that
  stores the public CA PEM secrets.
- **Role Based Access Control Administrator**, **User Access Administrator**, or
  **Owner** when agent identity role assignments are copied.

On the Windows jump-box, open `pwsh` and install the pinned Python dependencies.
Set `$bootstrapPython` to your installed interpreter name or full path. Use
`"python3"` instead of `"python"` when required on Linux or macOS:

```powershell
cd ./scripts/private-ca-recovery
$bootstrapPython = "python"
& $bootstrapPython -m venv .venv
$python = if ($IsWindows) { ".\.venv\Scripts\python.exe" } else { "./.venv/bin/python" }
& $python -m pip install -r requirements.txt
```

Run the following commands in the same PowerShell session from this directory.
`-PythonExecutable` accepts another interpreter path if you manage Python
environments separately.

## 1. Export before deletion

This step is non-destructive:

```powershell
$recovery = "./.foundry-recovery/private-ca-rotation-001"

.\Recreate-CapabilityHostAndAgents.ps1 `
  -Mode Export `
  -SubscriptionId "<subscription-id>" `
  -ResourceGroup "<resource-group>" `
  -AccountName "<foundry-account>" `
  -ProjectName "<project>" `
  -RecoveryDirectory $recovery `
  -PythonExecutable $python
```

The export fails safely if it finds a selected agent kind it can't replay. To
inventory unsupported agents without backing them up, add
`-SkipUnsupportedAgents`; don't proceed with deletion until those agents have a
separate source-controlled deployment path. Recreate still refuses to delete
the host while any selected version is unsupported.

Export mode doesn't overwrite existing manifests. Use a new recovery directory
for each snapshot so a failed or stale export can't be mixed with another run.

Validate the saved artifacts without changing Azure resources:

```powershell
.\Recreate-CapabilityHostAndAgents.ps1 `
  -Mode Validate `
  -SubscriptionId "<subscription-id>" `
  -ResourceGroup "<resource-group>" `
  -AccountName "<foundry-account>" `
  -ProjectName "<project>" `
  -RecoveryDirectory $recovery `
  -PythonExecutable $python
```

The recovery directory contains:

- `capability-hosts.json` - preserved host properties and connection names.
- `agents.json` - agent definitions, routing, identities, and role assignments.

`agents.json` can contain Prompt Agent tool/authentication configuration and
Hosted Agent environment-variable values. Keep the directory protected and
don't commit it. Store durable non-secret configuration in source control and
credentials in the normal secure configuration store. The included `.gitignore`
excludes the default `.foundry-recovery` directory and the known backup/report
filenames anywhere under this tooling directory. Output outside this directory
is not covered by this scoped ignore file.

On Windows, new recovery directories and files have an explicit current-user
full-control ACL with inheritance disabled and inherited entries removed.
The current user is the owner and sole grantee. The ACL is supplied at creation
and verified before file content is written. Existing directories must already
meet that policy; inherited/shared ACLs and directory junctions are rejected
instead of silently changing a potentially shared directory. Use a new,
dedicated recovery directory on the jump-box, not the repository root.

On Linux/macOS, new PowerShell recovery directories are owner-only (`0700`);
existing directories accessible to other users are refused. JSON files and
temporary request bodies are created with `0600` permissions before writing.
Reports are atomically replaced on both platforms. Permission/ACL creation or
verification failures are explicit errors, with no insecure fallback.

Python uses the same `Write-PrivateRecoveryFile.ps1` writer on Windows through
`pwsh`, passing JSON through standard input rather than command-line arguments
or an unprotected temporary file. Missing PowerShell/helper files and writer
failures stop recovery. No additional Python dependency is required. The POSIX
Python path continues to use exclusive `0600` file creation and verified chmod.
Restrict access to copied backups too. These controls are not encryption and do
not protect against the same OS user, filesystem administrators, or insecure
remote storage.

## 2. Register the private CA references

Upload each public root or intermediate CA PEM as a separate, versioned Key
Vault secret. Then register the exact versions while preserving existing
certificate references:

```powershell
.\Set-PrivateCaTrust.ps1 `
  -SubscriptionId "<subscription-id>" `
  -ResourceGroup "<resource-group>" `
  -AccountName "<foundry-account>" `
  -KeyVaultResourceId "<key-vault-resource-id>" `
  -CertificateReference @(
    "private-root-ca-pem=<secret-version>",
    "private-intermediate-ca-pem=<secret-version>"
  )
```

The script never reads or prints certificate secret values. It updates only the
account's `trustedCertificates` property and verifies the stored references.
It does not upload certificates, provision Key Vault, configure network access,
grant permissions, rotate private keys, or prove runtime TLS trust. Key Vault
and identity permissions in the root Bicep deployment are separate concerns.
The default account API `2026-07-15-preview` comes from upstream and has **not
been independently proven available** in a live subscription here. Confirm
service/region/preview support in your environment before use, or supply an
explicitly approved `-AccountApiVersion`. Registration is a read/merge/write
without ETag concurrency protection; coordinate trust changes with other
operators.

## 3. Recreate the host and redeploy agents

Review both manifests, then run:

```powershell
.\Recreate-CapabilityHostAndAgents.ps1 `
  -Mode Recreate `
  -SubscriptionId "<subscription-id>" `
  -ResourceGroup "<resource-group>" `
  -AccountName "<foundry-account>" `
  -ProjectName "<project>" `
  -RecoveryDirectory $recovery `
  -PythonExecutable $python `
  -AcknowledgeDataLoss
```

Before deleting anything, the workflow checks both manifests against the
requested account/project endpoint, validates definition hashes, confirms that
all selected versions are recoverable, and rechecks every saved project
connection name. It does not validate connection credentials. It then:

1. Deletes the project capability host and waits for deletion.
2. Recreates it with the same Cosmos DB, Storage, AI Search, subnet, and other
   connection references.
3. Reuses an already-present identical version or creates a new agent version.
4. Reapplies captured RBAC to each new agent-version identity.
5. Restores endpoint routing with old-to-new version mapping.
6. Restores the original enabled or disabled state. Each recovered agent is
   disabled as soon as the service permits it and stays disabled while versions,
   RBAC, and routing are incomplete. Temporary disable conflicts are retried
   during readiness polling. A failed disable can leave the endpoint serving;
   an error report with `disable-pending` requires operator attention.

The account capability host normally doesn't need recreation for a
`trustedCertificates` change. If your recovery procedure explicitly requires
it, add `-RecreateAccountCapabilityHost`; the script deletes project scope
first and recreates account scope before project scope. This option is supported
only when the account contains **exactly the selected project and no other
projects**, even if other projects appear unused. All pages of the account's
project collection are checked during preflight and again after confirmation.
Listing failure also blocks recreation. `-ProjectApiVersion` controls that list
API (default `2025-06-01`). This is not multi-project backup/recovery.
Stop concurrent project creation and configuration changes during recovery;
the checks cannot lock the Azure account against other operators.

If host recreation succeeds but agent replay fails, don't repeat the destructive
step. Resume from the existing manifest:

```powershell
.\Recreate-CapabilityHostAndAgents.ps1 `
  -Mode RestoreAgents `
  -SubscriptionId "<subscription-id>" `
  -ResourceGroup "<resource-group>" `
  -AccountName "<foundry-account>" `
  -ProjectName "<project>" `
  -RecoveryDirectory $recovery `
  -PythonExecutable $python
```

## Confirmation and dry runs

`Recreate` requires `-AcknowledgeDataLoss` and a `ShouldProcess` confirmation.
One approval covers the complete host deletion/recreation and agent/RBAC/routing
replay sequence. There are no mid-sequence prompts that could leave a deleted
host waiting for a second approval. `RestoreAgents` separately confirms agent,
RBAC, routing, and enabled-state changes. Both have high confirmation impact.
Use `-Confirm:$false` only for a deliberately approved noninteractive run;
`-Confirm` forces a prompt and fails safely in a noninteractive host.
`Set-PrivateCaTrust` confirms its account trust-reference PATCH when requested
with `-Confirm` or required by `$ConfirmPreference`.

`-WhatIf` never changes cloud resources:

- `Recreate -WhatIf` performs read-only discovery/preflight and can create a
  local export, but neither deletes hosts nor replays agents. It still requires
  `-AcknowledgeDataLoss` to reach the operation preview.
- `RestoreAgents -WhatIf` validates both manifests, then forwards Python
  `--dry-run`. It reads live agent definitions and writes a local planned
  restore report, but does not create/disable/enable agents, update routing,
  or create RBAC assignments.
- `Set-PrivateCaTrust -WhatIf` reads existing references and previews the PATCH
  without applying it or writing a completed trust report.
- `Export` and `Validate` are cloud-read-only modes, not offline modes.

Private output directories and local exports/planning reports may still be
created under `-WhatIf`. Direct Python `restore --dry-run` has the same agent
read/planning semantics but no PowerShell confirmation prompt. A dry run does
not certify future permissions, identity readiness, image pulls, or tool calls.

## Recovery boundaries

- A destructive capability-host reset isn't a Cosmos DB restore. Reusing the
  same Cosmos DB connection doesn't make orphaned agent or thread records
  reachable through a supported API.
- Prompt Agent definitions are captured before deletion through Foundry, then
  replayed as new versions. Keep canonical definitions, non-secret tool setup,
  and knowledge files in source control, with credentials in the normal secure
  configuration store.
- Hosted Agents are replayed from the recorded container image. Prefer immutable
  image digests over mutable tags. Image reachability, pull credentials, model
  availability, and successful tool execution are not certified.
- Conversations, threads, files, vector-store contents, and source-deployed
  Hosted Agent application code are not backed up or restored.
- File and vector-store IDs can refer to state that a reset or dependency loss
  orphaned. The export reports these references; restore the source files and
  knowledge configuration separately.
- New agent versions can receive new IDs and managed identities. Use the restore
  report to update clients and verify role assignments.
- Role assignments are captured only in `-RoleAssignmentSubscriptionId`
  subscriptions. The current Foundry subscription is included by default; pass
  an array for cross-subscription dependencies such as ACR. Only selected direct
  Azure RBAC assignments are replayed, not all effective permissions or external
  identity grants. Old assignments are not removed.
- If replay receives `409 RoleAssignmentExists`, it verifies the existing grants
  across every lookup page before treating the assignment as restored. Principal,
  scope, and role-definition IDs must match exactly (case-insensitively);
  condition and condition version must also match. A grant at a parent/child
  scope or with different conditions is not equivalent. Verification requires
  permission to read role assignments; failed lookups, unmatched grants, and
  other conflicts remain errors. An equivalent assignment can retain its
  existing UUID instead of the recovery tool's deterministic UUID.
- Missing or unknown agent readiness is never treated as ready. Polling is
  bounded by `-TimeoutMinutes` (Python `--version-timeout-seconds`).
  Recorded required roles require a new principal before replay can complete.
  Missing principal IDs or failed RBAC writes produce errors and leave replay
  incomplete, rather than silently claiming recovery.
- `-SkipAgentRoleAssignments` is an explicit opt-out, not evidence that recorded
  roles were restored. Review skipped/planned role entries and the restore
  report. IAM propagation and permissions outside the selected scopes still
  need operator verification.
- Failure writes an error report where possible and does not roll back prior
  cloud mutations. An agent disabled during incomplete replay remains disabled;
  fix the cause and use `RestoreAgents` rather than deleting hosts again.

## Offline checks

With the pinned dependencies installed and `pwsh` on `PATH`, run this regression
suite on a Linux development host using the `$python` interpreter selected above:

```powershell
& $python -m unittest discover -s . -p 'test_*.py' -v
& $python -m py_compile agent_recovery.py test_agent_recovery.py test_powershell_recovery.py
& $python -m pip check
```

The tests use fake SDK clients and mocked external commands in actual PowerShell
processes. No live host deletion, trust registration, agent mutation, or RBAC
write is performed. The PowerShell tests exercise both successful and failing
CLI flows, confirmation gates, multi-page project discovery, protected output,
and cross-manifest validation.

Windows platform selection, ACL-policy verification, directory/file ACL failures,
and Python-to-PowerShell invocation failures are covered by Linux-hosted mocks.
The standard .NET
[`FileSystemAclExtensions.Create`](https://learn.microsoft.com/dotnet/api/system.io.filesystemaclextensions.create)
APIs supply Windows security descriptors at creation. **Native Windows/NTFS ACL
enforcement has not been executed or verified in this Linux environment.**
The POSIX-specific regression harness is not a substitute for a native Windows
smoke test before operational recovery on the jump-box.

See:

- [Capability hosts](https://learn.microsoft.com/azure/foundry/agents/concepts/capability-hosts)
- [Foundry Agent Service resource and data loss recovery](https://learn.microsoft.com/azure/foundry/how-to/agent-service-operator-disaster-recovery)
- [Manage Hosted Agents](https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-agent)
