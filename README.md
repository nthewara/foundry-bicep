# foundry-bicep

Bicep port of [microsoft-foundry/foundry-samples #19 — private-network-agents-tools-setup](https://github.com/microsoft-foundry/foundry-samples/tree/main/infrastructure/infrastructure-setup-bicep/19-private-network-agents-tools-setup), wrapped in the hub-spoke + Firewall + jump-box + full-diagnostics structure from [`nthewara/foundry`](https://github.com/nthewara/foundry) (Terraform).

✅ **Build complete — deployed end-to-end, all phases merged.** See the status table below and [PLAN.md](./PLAN.md) for the build history.

## Quick links
- 📋 [Build plan](./PLAN.md)
- 🔐 [Security review](./SECURITY_REVIEW.md)
- 🔗 Upstream Bicep sample: <https://github.com/microsoft-foundry/foundry-samples/tree/main/infrastructure/infrastructure-setup-bicep/19-private-network-agents-tools-setup>
- 🔗 Terraform predecessor: <https://github.com/nthewara/foundry>

## Status

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
- 2× VNet: `fbicep-vnet-hub` (10.100.0.0/23), `fbicep-vnet-aiapp` (10.100.2.0/23) peered bidirectionally
- Subnets per VNet for Firewall, Bastion, jumpbox, private endpoints, agent runtime
- UDRs pointing default route to AzFW private IP

### 🔐 BYO-VNET egress hardening (NSG + Azure Firewall)

Based on [Securing Microsoft Foundry with Bring Your Own VNET](https://nirmalt.com/posts/securingmicrosoftfoundrywithbyovnet), the delegated agent subnets and the firewall egress are locked down to a least-privilege model.

**NSG on the delegated `agents` + `mcp` subnets** (`nsg.bicep`, attached via `attachAgentNsg=true`) — minimum required outbound rules:

| Priority | Rule | Destination | Port |
|---|---|---|---|
| 100 | Allow-Entra-443 | `AzureActiveDirectory` (service tag) | 443/TCP |
| 110 | Allow-ACA-Mgmt-443 | `AzureContainerAppsManagement` (service tag) | 443/TCP |
| 120 | Allow-ACR-443 | `AzureContainerRegistry` (service tag) | 443/TCP |
| 130 | Allow-Foundry-Infra | `100.67.0.0/24` (agent infra comms) | any |

**Azure Firewall egress** (`firewall.bicep`, controlled by `restrictEgress=true`) — replaces the old permissive `* → *` application rule:
- **Network rules** (`AllowFoundryInfra`): `AzureActiveDirectory` service tag on 443, and `100.67.0.0/24` on any port. Existing RFC1918 east-west rules preserved.
- **Application rules**: curated Foundry FQDN allowlist (`mcr.microsoft.com`, `*.data.mcr.microsoft.com`, `*.azurecr.io`, `*.azurecontainerapps.io`, `*.cognitiveservices.azure.com`, `*.openai.azure.com`, `*.services.ai.azure.com`, `*.search.windows.net`, `*.documents.azure.com`, `login.microsoftonline.com`, `management.azure.com`, `*.blob.core.windows.net`, Ubuntu/package repos, etc.).
- Set `restrictEgress=false` to fall back to `* → *` for troubleshooting.

All Foundry PaaS resources (Cosmos DB, AI Search, Storage, Foundry account) remain **public network access disabled** — private-endpoint only, as the blog requires.

**Edge + jump host**
- `fbicep-azfw-1a5d` — Azure Firewall Basic SKU + management subnet + 2× PIP (data + mgmt)
- `fbicep-bastion-1a5d` — Bastion Developer SKU (no public IP, no NSG dance)
- `fbicep-winvm1` — Windows Server 2022 Hotpatch jumpbox, admin password pulled from KV via `az.getSecret()`

**AI Foundry**
- `aiservices1a5d` — Cognitive Services AI Foundry account with private endpoints across all sub-services
- `aiservices1a5d/project` — Foundry project
- `aiservices1a5d@aml_aiagentservice` — account-level capability host
- `caphostproj` — project-level capability host with Storage thread storage + Cosmos vector storage + AI Search connections

**Data plane**
- `foundrystg1a5d` — Storage account (blob private endpoint)
- `cosmosdb1a5d` — Cosmos DB SQL (private endpoint, both control + data plane RBAC)
- `aisearch1a5d` — AI Search (private endpoint)
- `acr<suffix>` — *(optional)* Azure Container Registry, Premium SKU, private endpoint in the `pe` subnet, `privatelink.azurecr.io` zone, AcrPull granted to the project identity. Toggled by `enableContainerRegistry` (default `true`); set `developerIpCidr` to allowlist a push source. Synced from upstream sample #19 (PR #519).

**Private DNS** — 7 zones for blob/cosmos/search/cognitiveservices/openai/aml + `privatelink.azurecr.io` (ACR), linked to all three VNets.

**Diagnostics** — every resource that supports it fans out to a single LAW.

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

> **Note on the 7-day tag bypass:** `main.bicepparam.example` includes `SecurityControl=Ignore` on the RG and resources to bypass tenant policy that auto-disables `publicNetworkAccess` on storage. This bypass expires after 7 days — for longer-lived labs, re-apply the tag or harden code to tolerate the flip.

## Teardown

```bash
az group delete -n foundrybicep-1a5d --yes --no-wait
```
