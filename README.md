# foundry-bicep

Bicep adaptation of [microsoft-foundry/foundry-samples sample 19, private-network-agent-tools](https://github.com/microsoft-foundry/foundry-samples/tree/main/infrastructure/infrastructure-setup-bicep/19-private-network-agent-tools), retaining this repository's subscription-level deployment, three-VNet hub-spoke topology, Firewall, jump-box and consolidated modules.

**September 2026 sync:** Entra-only Foundry authentication, shared agent tracing/evaluation permissions, and an optional private-CA vault. These updates have offline contract coverage; the historical deployment below does not verify the new resources or preview private-CA API in Azure.

## Quick links
- 📋 [Build plan](./PLAN.md)
- 🔐 [Security review](./SECURITY_REVIEW.md)
- 🔗 [Pinned upstream sample](https://github.com/microsoft-foundry/foundry-samples/tree/e0f4042a0080158d4fe351dfe7c3fb47a9b75a5e/infrastructure/infrastructure-setup-bicep/19-private-network-agent-tools)
- 🔗 Terraform predecessor: <https://github.com/nthewara/foundry>
- [Private-CA trust and recovery tooling](./scripts/private-ca-recovery/README.md)
- [Offline infrastructure contracts](./tests/test_infrastructure_contracts.py)

## Historical build status

| Phase | Scope | Status |
|---|---|---|
| P0 — Scaffold + plan | Repo bootstrap, PLAN.md, README, .gitignore, bicepconfig | ✅ Done |
| P1 — Networking modules | `networking.bicep` hub+spokes, subnets, peerings, UDR re-call pattern | ✅ Done (PR merged) |
| P2 — Firewall + Bastion + jump-box | `firewall.bicep` (Basic SKU + mgmt subnet), `bastion.bicep` (Developer SKU), `vm.bicep` Windows jumpbox w/ KV secret | ✅ Done (PR merged) |
| P3 — Diagnostics | `diagnostics.bicep` fan-out to LAW for every supported resource type | ✅ Done (PR merged) |
| P4 — Foundry core modules | `foundry-dependencies.bicep`, `foundry.bicep`, `foundry-identity.bicep`, `foundry-private-endpoints.bicep`, `foundry-roles.bicep`, `foundry-capability-host.bicep`, `project.bicep`, `add-project.bicep`, `dns.bicep` | ✅ Done (PR merged) |
| P5 — Orchestrator | `main.bicep` wires all modules in correct order, two-pass networking, KV secret pull via `az.getSecret()` | ✅ Done (in main agent) |
| P6 — Tool servers | `tool-servers/` scaffolding | ✅ Done (PR merged) |
| P7 — Ops assets | `dashboards/`, `tests/`, `scripts/createCapHost.sh` (defensive — Bicep now does this natively) | ✅ Done (PR merged) |
| P8 — E2E lab deploy | Deployed to `foundrybicep-1a5d` (australiaeast), 53 resources, both capability hosts Succeeded, project + Storage/Cosmos/AI Search connections wired | ✅ Done — verified Mon 2026-05-18 |

### Deployment iterations

The first deploy surfaced three issues that were caught + fixed by the autonomous iteration loop (file issue → branch → PR → merge → redeploy):

| # | Issue | Fix |
|---|---|---|
| #6 | VM `patchMode: 'AutomaticByOS'` rejected on Hotpatch-compatible image | Switched to `AutomaticByPlatform` (PR #8) |
| #7 | `diagnostics.bicep` passed RG name instead of resourceId for AI Search target | Used `resourceId(...)` helper (PR #8) |
| #9 | `expressionEvaluationOptions.scope='inner'` + `scope: '[parameters(...)]'` on diagnosticSettings → ARM treated literal string as namespace | Removed inner-scope eval, pass full `.id` from parent (PR #10) |
| — | Storage `categoryGroup: 'allLogs'` not supported at account level (only metrics; logs on blob/file/queue/table sub-services) | Added `skipLogs` flag on diag targets (PR #11) |

Iter 4 of the deploy (`foundrybicep-deploy-20260518-061151`) succeeded cleanly in 5m 4s.

## What gets deployed

**Networking (hub-spoke)**
- Three VNets: hub (`10.100.0.0/23`), VM spoke (`10.10.10.0/23`), AI app spoke (`10.10.20.0/23`), with the existing peering and two-pass UDR attachment.
- Private endpoints, agents and MCP subnets remain in the existing topology. Naming an MCP subnet does not deploy tool servers.
- Default routes point to Azure Firewall when `fwProvision=true`; disabling it leaves unrestricted egress.

**Edge + jump host**
- Azure Firewall Basic with management subnet and data/management public IPs.
- Azure Bastion Basic and the Windows jump-box (`Standard_D8s_v5`, `AutomaticByPlatform` patching), individually optional.
- The secure VM password is read from a **pre-existing** Key Vault via `az.getSecret()`, not the new private-CA vault.

**AI Foundry**
- Private AI Services account and initial project, each with a distinct system-assigned managed identity.
- Account local authentication is **disabled**. Existing callers using account/API keys must migrate to Microsoft Entra tokens and appropriate RBAC before redeployment. This is not a switch to user-assigned identities, nor a change to every dependent service's authentication policy.
- Parameterized model deployment and AAD project connections to Storage, Cosmos DB and AI Search.
- Project capability host, with the existing private endpoints -> pre-RBAC -> capability host -> post-RBAC ordering. The platform manages the account host; the template declares the project host.

**Data plane**
- Storage blob, Cosmos DB SQL and AI Search private endpoints and existing project access.
- Optional Premium ACR, enabled by default, private endpoint and project `AcrPull`. Empty `developerIpCidr` keeps public access disabled; a CIDR enables the public endpoint with default-deny IP allowlisting. Push privately from the jump-box or explicitly allow a developer's public egress CIDR.
- Cosmos uses the built-in data contributor role ending `0002` at account scope. Storage retains its workspace-based ABAC condition for selected tag/filter operations; it is not blanket per-container isolation.

**Private DNS:** `dns.bicep` alone owns zones and links to all three VNets. Defaults now create **12 unique zones**: the existing seven, four additional Monitor zones, and `privatelink.vaultcore.azure.net`. Required core/feature zones are unioned with `privateDnsZones`, including when callers pass `[]` or a partial list. No duplicate Blob zone or ACR VNet link is introduced.

### Agent tracing and evaluations

`enableAgentTracing=true` adds workspace-based Application Insights and AMPLS while **reusing the existing diagnostics Log Analytics workspace**, including its 30-day retention. An `AppInsights` connection on the Foundry account is shared to all projects, with the connection string held in connection credentials rather than exposed as a deployment output. Its `ApiKey` connection type configures telemetry and is separate from the account's now-disabled inference key authentication.

Application Insights and LAW public ingestion are disabled. The AMPLS private endpoint is in the existing AI app `pe` subnet, with **all five required DNS mappings**: `privatelink.monitor.azure.com`, `privatelink.oms.opinsights.azure.com`, `privatelink.ods.opinsights.azure.com`, `privatelink.agentsvc.azure-automation.net`, and the centrally owned `privatelink.blob.core.windows.net`. Azure platform diagnostic settings still ingest over Microsoft's private channel.

**Queries deliberately remain publicly reachable with authorization required:** both resources enable public queries, and AMPLS query mode is `Open` (ingestion is `PrivateOnly`). This preserves portal/ARM query experiences and service-side evaluation access; it is not a fully private query topology or query-exfiltration boundary. SDK ingestion must originate from a network with access and DNS resolution to the PE. A single AMPLS serves these connected networks. Attaching other networks/monitoring resources to the same DNS requires planning to avoid global Monitor DNS overrides and unintended ingestion blocking.

The **project** identity receives Log Analytics Reader (`73c42c96-874c-492b-b04d-ab87d138a893`) and Privileged Monitoring Data Reader (`dbc9c667-e97f-4491-aee6-90b9cf960190`) scoped to Application Insights. The latter permits querying sensitive GenAI content; control who can act as the project identity and what telemetry is recorded.

See [Monitor topology/access modes](https://learn.microsoft.com/azure/azure-monitor/fundamentals/private-link-design) and [the five-zone configuration](https://learn.microsoft.com/azure/azure-monitor/fundamentals/private-link-configure#review-and-validate-ampls-configuration).

### Private-CA Key Vault

`enableKeyVault=true` creates a Standard, RBAC-enabled vault unless `existingKeyVaultResourceId` specifies a full vault ARM ID. Optional `keyVaultName` overrides the deterministic new-vault name. Whitespace is trimmed from new optional names/IDs; blank inputs select defaults. An existing vault may be in another RG/subscription **in the same tenant**. Its settings/tags are not overwritten; RBAC is deployed in its own scope, while the `vault` private endpoint and DNS remain in the stack RG.

The **account** identity, not project identities, receives Key Vault Secrets User (`4633458b-17de-408a-b874-0445c86b69e6`). New vaults retain soft-deleted objects for 90 days, with purge protection **off** for this lab. Their public endpoint is **Enabled**, but defaults to deny with no IP/VNet allowlist and the `AzureServices` trusted-service bypass. Keep that exception for Foundry's certificate-retrieval path; a PE alone does not establish that service-side path. Existing vaults must already support RBAC and compatible trusted-service access; the template does not validate or fix their configuration. See [Key Vault network controls](https://learn.microsoft.com/azure/key-vault/general/network-security).

Bicep does **not** upload CA material, register `trustedCertificates`, or recreate hosts. Use the separate [private-CA tooling](./scripts/private-ca-recovery/README.md) from an appropriately connected and authorized host. Recovery workflows support the Windows jump-box with PowerShell 7.3+ (`pwsh` on PATH) and a Windows ACL-enforcing local filesystem such as NTFS, or Linux/macOS with enforced POSIX permissions. Windows artifacts use verified protected current-user ACLs; native Windows validation remains outstanding. Trust registration patches certificate references only, not vault provisioning or permissions. Its account trust API is preview (`2026-07-15-preview` in the pinned sample); availability is not proven by compilation. Recovery recreates supported definitions, **not conversation history, files or vector-store contents**. Account-host recreation is blocked when sibling projects exist.

### Defaults, costs and upgrade behavior

| Parameter | Default | Effect |
|---|---|---|
| `enableAgentTracing` | `true` | App Insights, AMPLS PE, four extra zones, project read roles; existing LAW reused |
| `enableKeyVault` | `true` | New/reference vault, account secret-read role, vault PE and DNS |
| `existingKeyVaultResourceId` | `''` | Create a vault; a full ID instead references an existing vault |
| `keyVaultName` | `''` | Deterministic new name; ignored for an existing vault |
| `enableContainerRegistry` | `true` | Existing Premium ACR feature is retained |
| `developerIpCidr` | `''` | Existing ACR public endpoint remains disabled |
| `fwProvision` / `bastionProvision` / `vmDeploy` | `true` | Existing optional network/jump-host resources |

The new defaults add **two private endpoints**, five DNS zones, Key Vault operations/storage and telemetry ingestion/retention costs. Reusing a vault avoids a second vault, not its PE/DNS or operations costs. LAW is not duplicated. Existing Premium ACR, Firewall, Bastion, VM/disks, Search and Cosmos costs remain. No fixed monthly estimate is implied; usage, SKU, retention and region determine charges.

Set new flags to `false` before a first deployment to omit their resources/roles; tracing disabled still provisions normal diagnostics. Incremental redeployment with a flag switched off **does not delete** previously created resources, connections or role assignments. A disabled tracing flag restores LAW's public-ingestion setting; remove obsolete resources/connections and review network settings explicitly if decommissioning a previously enabled feature.

Existing resource names, main deterministic suffix, unrestricted `location` (`australiaeast` default), model parameters and prior outputs are retained. Storage Owner assignment names already included the project principal locally, avoiding upstream's multi-project collision; their GUID formula is preserved to avoid duplicate-role conflicts on an existing deployment.

## Deploy

```bash
# one-time: copy the example, fill in subId/region/prefix/etc.
cp main.bicepparam.example main.bicepparam
# ...edit main.bicepparam...

az deployment sub create \
  --name foundrybicep-deploy-$(date +%Y%m%d-%H%M%S) \
  --location australiaeast \
  --template-file main.bicep \
  --parameters main.bicepparam
```

Templates are flat (no `modules/` subfolder) to keep diffs against the upstream sample readable.

The deployment identity needs subscription-level RG/deployment permissions, resource creation permissions in target scopes and `Microsoft.Authorization/roleAssignments/write` for the role assignments, including an existing vault or telemetry component in another subscription. An existing vault must have approved PE connectivity; uploads additionally need an appropriate Key Vault data role and network access. No certificates or personal subscription IDs are embedded in examples.

The RG and resources managed by the changed modules merge caller tags with `SecurityControl: Ignore`; that tag is required for this lab's MCAPS policy exception. Existing referenced vaults are not retagged. Do not assume the tag is a portable Azure control or a permanent exemption; tenant policy determines its effect and duration.

### Additional projects

Copy `add-project.bicepparam.example` beside `add-project.bicep`. For a tracing-enabled stack, set `existingAppInsightsResourceId` to main's `appInsightsId` output. This grants the **new project's** telemetry readers in the component's actual subscription/RG and reuses the account-shared connection, without creating another workspace, connection or AMPLS. Leaving it empty skips only telemetry RBAC and is intended for stacks without tracing.

The existing add-project timestamp suffix is unchanged: default repeated invocations create different projects. Supply the same explicit `deploymentTimestamp` to retry a given project deployment. Search, Storage and Cosmos must currently share the Storage RG/subscription because the consolidated data-role module is deployed there; independent data-resource scopes remain unsupported. The additional project is not automatically granted ACR access; assign its identity `AcrPull` on a shared registry if needed.

```bash
cp add-project.bicepparam.example add-project.bicepparam
# Fill in existing resource IDs/names, including appInsightsId for tracing.
az deployment group create -g <deployment-rg> \
  --template-file add-project.bicep --parameters add-project.bicepparam
```

### Outputs and offline checks

All earlier outputs remain. New outputs are `lawName`, `appInsightsId`, `appInsightsName`, `appInsightsAppId`, `amplsId`, `keyVaultName` and `keyVaultResourceId`. Disabled optional features return empty strings; `lawId` and `lawName` remain populated for diagnostics. `appInsightsAppId` is a query identifier, not a connection string.

```bash
# No Azure deployment, secret lookup or SDK/live-agent tests.
python3 -B tests/test_infrastructure_contracts.py -v
# Single-entry-point compile without generated JSON:
az bicep build --file main.bicep --stdout >/dev/null
```

The contract suite compiles every root module, checks compiled resource wiring, scopes, principals, flags, DNS and retained behavior, and compiles both example parameter files using test-only secret replacements. Do not run the separate `test_*_agents_v2.py` integration scripts as offline checks.

## Upstream provenance and applicability

Reviewed against **`microsoft-foundry/foundry-samples` main `e0f4042a0080158d4fe351dfe7c3fb47a9b75a5e` (2026-09-18)**. The latest commit touching `infrastructure/infrastructure-setup-bicep/19-private-network-agent-tools` is **`74b93d65e1b2c1e22d661ef883a747962f47a8ee` (2026-09-15)**. Adopted upstream portions retain the Microsoft copyright and MIT permission notice in [LICENSE.upstream](./LICENSE.upstream). That notice does not establish the license or availability of external container images.

The June authentication, tracing, July evaluation roles and September private-CA changes are adapted to local modules. Local role uniqueness and centralized DNS already address the upstream storage-assignment/ACR duplicate-link bugs. Unlike upstream's four-zone AMPLS group, this implementation attaches all five zones and reuses the existing Blob zone and LAW. New-resource feature flags allow explicitly opting out.

The upstream BYO-VNet/DNS-input redesign is not imported; that work belongs to separate #15. Upstream portal/generated templates and their stale parameter schema are not copied. The existing A2A/OpenAPI/MCP servers, Function source/ARM/docs, demo tests, copied diagrams and legacy capability-host/discovery helpers already match the pinned upstream snapshot byte-for-byte, so no independent updates or diagram regeneration are needed. The ignored Function `local.settings.json` emulator configuration is intentionally not imported. Generated VM JSON also remains unchanged.

Private-CA API behavior is attributed to the pinned upstream implementation: the Microsoft Learn search did not independently establish support for `2026-07-15-preview`, and no GA or live-availability claim is made. Repository checks found no Actions workflows, rulesets or protected-main status checks; the offline checks above are explicitly run, not assumed to be enforced by CI. See [PLAN.md](./PLAN.md#-upstream-sync-log) for commit-level applicability decisions and historical provenance.

## Teardown

```bash
az group delete -n <stack-resource-group> --yes --no-wait
```

Deleting the stack RG does not delete an existing vault in another RG, its account-role assignment there, or soft-deleted vault contents. Review those separately; this command is destructive and is not part of offline validation.
