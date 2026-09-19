# foundry-bicep — Build Plan

> Bicep port of [microsoft-foundry/foundry-samples sample 19, `19-private-network-agent-tools`](https://github.com/microsoft-foundry/foundry-samples/tree/main/infrastructure/infrastructure-setup-bicep/19-private-network-agent-tools), wrapped in the same hub-spoke + Firewall + jump-box + diagnostics structure as [`nthewara/foundry`](https://github.com/nthewara/foundry) (which is Terraform).

**Status:** ✅ Plan approved 2026-05-18. Decisions locked (see bottom). Ready to start P1.

### Locked decisions
1. **Firewall SKU** → `Basic` (cheaper for lab; we already have hardening notes for Basic-SKU + `AzureFirewallManagementSubnet`)
2. **Bastion** → on by default
3. **DNS zones** → local to the lab RG (no shared-RG flag)
4. **Tool servers** → all 4 ported day-1 (a2a, mcp-http, openapi, azure-function)
5. **`add-project`** → standalone top-level template (`add-project.bicep` + sanitised `.bicepparam.example`). Lets you add another project to an existing Foundry account post-deploy without re-running the full main template.
6. **Lab tracker** → `foundrybicep-<rand>` when P8 lands
7. **Layout** → **flat files at repo root**, not a `modules/` tree. One `.bicep` file per concern (mirrors `nthewara/foundry`'s flat `.tf` layout — easier to diff & port). Sub-files for `tool-servers/`, `scripts/`, `tests/`, `diagrams/`, `dashboards/` only.

---

## 🎯 Goals

1. **Same shape as `nthewara/foundry`** — RG layout, hub-spoke VNet topology, Firewall + UDR egress, Windows jump-box, optional Bastion, full diagnostics to LAW + storage, sanitised `*.bicepparam.example` files.
2. **Same Foundry capability as upstream sample #19** — private AI Services account with network injection into a private VNet, Cosmos DB + AI Search + Storage on private endpoints, capability host, project, model deployment, role assignments.
3. **Bicep-native** — modules, `.bicepparam`, no Terraform, no ARM JSON authoring (only generated `main.json` artifacts if useful).
4. **Pluggable tool servers** — keep upstream's `a2a-server`, `mcp-http-server`, `openapi-server`, `azure-function-server` (toggleable).

---

## 🧱 Architecture (target)

```
                            INTERNET
                                │
                                ▼
┌──────────────────────────────── HUB VNET (10.100.0.0/23) ─────────────────────────────┐
│  AzureFirewallSubnet  AzureBastionSubnet  AzureFirewallMgmtSubnet  GatewaySubnet      │
│       │                    │                                                          │
│  ┌────▼──────┐        ┌────▼─────┐                                                    │
│  │ Azure FW  │        │ Bastion  │ (optional)                                         │
│  │ + Policy  │        └──────────┘                                                    │
│  └───────────┘                                                                        │
└──────────────────┬─────────────────────────────────┬──────────────────────────────────┘
                   │ peering                         │ peering
   ┌───────────────▼─────────┐         ┌─────────────▼──────────────────────────────────┐
   │ SPOKE 1 — VM (10.10.10) │         │ SPOKE 2 — AI APP (10.10.20.0/23)              │
   │  ┌─────────────────┐    │         │  ┌──────────┐  ┌──────────┐  ┌──────────┐    │
   │  │ Win jump-box    │    │         │  │ pe       │  │ agents   │  │ mcp      │    │
   │  └─────────────────┘    │         │  │ /26      │  │ /26 deleg│  │ /26 deleg│    │
   └─────────────────────────┘         │  │ PEs ×N   │  │ App env  │  │ ACA      │    │
                                       │  └──────────┘  └──────────┘  └──────────┘    │
                                       └────────────────────────────────────────────────┘
                                                            │
                                       Private DNS zones (linked to spoke + hub)
                                       AI Foundry account (PNA disabled, networkInjections)
                                       Cosmos DB / Storage / AI Search (private)
                                       Capability host + project + model deployment
```

### Deltas vs. upstream sample #19
| Area | Upstream | This repo |
|---|---|---|
| VNet | single new VNet OR existing | **Hub + 2 spokes** (mirrors `nthewara/foundry`) |
| Egress | direct | **Azure Firewall + UDR** on agent/pe/mcp subnets |
| Mgmt access | (none) | **Windows jump-box** + optional **Bastion** |
| Observability | (none) | **LAW + storage** diagnostics on every resource (VNets, NSGs, PIPs, KV, ACR, FW, AIS, Cosmos, Search, Storage) |
| MCP subnet | yes | yes (kept) |
| Tool servers | yes | yes (a2a, mcp, openapi, function) — flag-gated |
| Secrets | inline params | **Key Vault reference** in `.bicepparam` for `vmAdminPassword`, etc. |
| Naming | `uniqueString` | `prefix-<role>-<random>` to match nthewara/foundry style |

---

## 📁 Repo layout (flat, mirrors `nthewara/foundry`)

```
foundry-bicep/
├── README.md                          # docs (architecture, deploy, troubleshoot, costs)
├── PLAN.md                            # this file
├── SECURITY_REVIEW.md                 # ported from nthewara/foundry
├── .gitignore                         # blocks real *.bicepparam, *.tfvars, backend.hcl
│
├── main.bicep                         # top-level orchestrator (subscription scope)
├── main.bicepparam.example            # sanitised params, committed
│
├── networking.bicep                   # hub VNet + 2 spokes + subnets + peering + UDRs
├── firewall.bicep                     # AzFW Basic + policy + PIPs + LAW + rule groups
├── bastion.bicep                      # Bastion (default on)
├── vm.bicep                           # Windows jump-box (NIC + VM, KV-referenced pwd)
├── dns.bicep                          # 6 private DNS zones + VNet links
│
├── foundry.bicep                      # AI Services account (PNA disabled, networkInjections)
├── foundry-dependencies.bicep         # Cosmos + Storage + AI Search (private)
├── foundry-identity.bicep             # account + project MIs, format workspace id
├── foundry-capability-host.bicep      # capability host hookup
├── foundry-roles.bicep                # all role assignments (AI Search / Storage / Cosmos / blob containers)
├── foundry-private-endpoints.bicep    # PEs for AIS, Cosmos, Storage, AI Search + DNS A records
│
├── project.bicep                      # initial project (called from main.bicep)
├── add-project.bicep                  # ★ STANDALONE: adds another project to an existing Foundry account
├── add-project.bicepparam.example     # sanitised params for add-project
│
├── diagnostics.bicep                  # LAW + diag storage + all diag settings (one place, like Terraform)
├── outputs.bicep (or in main.bicep)   # final outputs (endpoints, IDs)
│
├── tool-servers/                      # ported as-is from upstream
│   ├── a2a-server/
│   ├── mcp-http-server/
│   ├── openapi-server/
│   └── azure-function-server/         # incl. deploy-function.bicep
├── scripts/
│   ├── createCapHost.sh
│   ├── deleteCapHost.sh
│   └── get-existing-resources.ps1
├── tests/                             # 7 test_*_agents_v2.py + TESTING-GUIDE.md
├── diagrams/                          # regenerated for our hub-spoke layout
└── dashboards/
    ├── dashboard.json
    └── workbook.json
```

### How Bicep "modules" work with flat files
In Bicep, `module foo 'networking.bicep' = { … }` references any peer `.bicep` file — there is no folder requirement. The layout above gives us the same one-file-per-concern feel as the Terraform repo while staying Bicep-native. `main.bicep` will reference each peer file once and pass through params.

### About `add-project.bicep`
Upstream sample #19 ships a small companion template that targets an **already-deployed** Foundry account and adds another project (with its own capability host, identity, role assignments). It's useful when you want a second project on the same account without redeploying everything, e.g. dev vs. prod isolation on shared infra. We keep it standalone so you can run `az deployment group create -f add-project.bicep -p add-project.bicepparam` after the main stack is up.

---

## 🔧 Parameter surface (`main.bicep`)

Mirrors `nthewara/foundry` variables + upstream Foundry params:

```bicep
// General
param subscriptionId string                  // for tag/scope only
param location string = 'australiaeast'
param prefix string = 'aifoundrybicep'

// VNet config
param hubVnetPrefix string = '10.100.0.0/23'
param vmVnetPrefix string = '10.10.10.0/23'
param aiappVnetPrefix string = '10.10.20.0/23'

// Feature toggles
param fwProvision bool = true
param fwSku string = 'Standard'
param vmDeploy bool = true
param bastionProvision bool = false

// Foundry / model
param aiServicesName string = 'aiservices'
param projectName string = 'project'
param modelName string = 'gpt-4o-mini'
param modelFormat string = 'OpenAI'
param modelVersion string = '2024-07-18'
param modelSkuName string = 'GlobalStandard'
param modelCapacity int = 30

// Secrets via Key Vault
@secure() param vmAdminPassword string       // resolved from KV in .bicepparam
param vmAdminUsername string = 'azureadmin'

// DNS zones (defaults match upstream + nthewara/foundry merged set)
param privateDnsZones array = [
  'privatelink.cognitiveservices.azure.com'
  'privatelink.openai.azure.com'
  'privatelink.services.ai.azure.com'
  'privatelink.blob.core.windows.net'
  'privatelink.search.windows.net'
  'privatelink.documents.azure.com'
]
```

`main.bicepparam` will be **gitignored**; only `main.bicepparam.example` committed.

---

## 🛠 Deploy flow (target UX)

```bash
# 1. Real params live outside repo
~/workspace/tfvars/foundry-bicep.bicepparam     # gitignored sibling

# 2. Subscription-scope deploy (creates RG + everything)
az deployment sub create \
  --location australiaeast \
  --template-file main.bicep \
  --parameters ~/workspace/tfvars/foundry-bicep.bicepparam

# 3. After main deploy → capability host (existing upstream script)
./scripts/createCapHost.sh <rg> <aiservicesAccountName> <projectName>

# 4. Add another project later
az deployment group create \
  -g <rg> \
  --template-file add-project.bicep \
  --parameters ~/workspace/tfvars/foundry-bicep-project2.bicepparam
```

---

## 📦 Phased build (suggested PRs)

| Phase | Scope | Branch | Approx LOC |
|---|---|---|---|
| **P0** | Repo skeleton, README, PLAN, .gitignore, `main.bicepparam.example` (placeholders only) | `chore/scaffold` | ~150 |
| **P1** | Networking modules: hub VNet, 2 spokes, peering, UDR | `feat/networking` | ~400 |
| **P2** | Firewall + Bastion + Jump-box modules | `feat/edge-and-mgmt` | ~350 |
| **P3** | Diagnostics module (LAW + storage + reusable diag-setting) | `feat/diagnostics` | ~250 |
| **P4** | Port upstream Foundry `modules-network-secured/` into `modules/foundry/`, wire to spoke-aiapp | `feat/foundry-core` | ~800 |
| **P5** | Top-level `main.bicep` orchestration + `add-project.bicep` | `feat/orchestrator` | ~250 |
| **P6** | Port tool servers (a2a, mcp, openapi, function) as-is | `feat/tool-servers` | unchanged copy |
| **P7** | Port scripts, tests, diagrams, dashboards | `feat/ops-assets` | unchanged copy |
| **P8** | E2E deploy in lab sub, fix bugs, lock in `lab-tracker` entry | `lab/foundrybicep-v1` | n/a |

Each phase = 1 PR, reviewable in isolation. P1–P3 are pure-infra and can run before any Foundry bits land.

---

## ✅ Acceptance criteria

- `bicep build main.bicep` succeeds with **zero warnings** in strict mode.
- `az deployment sub validate` passes against the lab sub.
- End-to-end deploy in `australiaeast` produces:
  - Working hub-spoke with peering + UDRs pointing at Firewall
  - Reachable jump-box via Bastion (when enabled) or via FW DNAT
  - AI Foundry account with `publicNetworkAccess = Disabled` + `networkInjections`
  - All 4 dependent resources (AIS, Cosmos, Storage, AI Search) on private endpoints, no public access
  - All 6 private DNS zones linked to spoke-aiapp + hub
  - Diagnostics enabled on every resource with logs flowing to LAW
  - At least one chat completion works from inside jump-box
  - All 7 upstream `test_*_agents_v2.py` tests pass
- Lab tracker updated with RG name, region, ~$/day cost estimate, deallocate/restore commands.

---

## ✅ Decisions (locked 2026-05-18)

All questions answered above under "Locked decisions". Plan approved → proceeding to P1 (networking.bicep).

---

## 🔗 References

- Upstream sample: <https://github.com/microsoft-foundry/foundry-samples/tree/main/infrastructure/infrastructure-setup-bicep/19-private-network-agent-tools>
- Terraform predecessor: <https://github.com/nthewara/foundry>
- Foundry BYO VNet docs: <https://learn.microsoft.com/azure/ai-foundry/how-to/configure-private-link>

---

## 🔄 Upstream sync log

### 2026-06-16 — sync from upstream #19 (last upstream change 2026-06-12)

Synced the **optional Azure Container Registry with Private Endpoint** feature from upstream microsoft-foundry/foundry-samples#519 (`feat: add optional ACR with Private Endpoint`), adapted to this repo's modular hub-spoke idiom:

- **New module `container-registry.bicep`** — Premium ACR + PE in the `pe` subnet + AcrPull role for the project identity. Unlike upstream (which creates/links the DNS zone inside the module), this repo delegates zone create/link to `dns.bicep`, so the module consumes `acrDnsZoneId` (same pattern as `foundry-private-endpoints.bicep`).
- **`main.bicep`** — new `enableContainerRegistry` (default `true`) + `developerIpCidr` params; `privatelink.azurecr.io` added to the default `privateDnsZones` (now 7 zones); ACR module wired as Stage 10b after PEs + project; `acrId`/`acrLoginServer` outputs.
- **`main.bicepparam.example`** + README updated to document the new params/resource.

**Upstream changes intentionally NOT ported** (N/A to this repo's idiom):
- *Private DNS zone defaults fix (microsoft-foundry/foundry-samples#762)* — fixed an upstream bug where supplying `existingDnsZones` replaced (rather than merged with) the required-zone map. This repo's `dns.bicep` uses an explicit array + per-VNet links, so that bug class doesn't exist here.
- *Deterministic `uniqueSuffix` (timestamp → `uniqueString(resourceGroup().id)`)* — this repo already uses a deterministic suffix (`uniqueString(subscription().subscriptionId, resourceGroupName)`, overridable via `randomSuffix`).
- *`canadacentral` added to allowed locations* — this repo's `location` param is a free-form string (no `@allowed` list), so no change needed. Default region stays `australiaeast` (preserved customization).

### 2026-09-19 - pinned upstream infrastructure sync

**Source:** [microsoft-foundry/foundry-samples at `e0f4042a0080158d4fe351dfe7c3fb47a9b75a5e`](https://github.com/microsoft-foundry/foundry-samples/tree/e0f4042a0080158d4fe351dfe7c3fb47a9b75a5e/infrastructure/infrastructure-setup-bicep/19-private-network-agent-tools), main committed 2026-09-18. The latest sample commit is [`74b93d65e1b2c1e22d661ef883a747962f47a8ee`](https://github.com/microsoft-foundry/foundry-samples/commit/74b93d65e1b2c1e22d661ef883a747962f47a8ee), committed 2026-09-15. The snapshot and upstream LICENSE were verified against that pinned tree. Microsoft copyright/permission is retained in `LICENSE.upstream`.

**Local baseline:** `e421adb` includes the June 16 merge of implementation `0348f4b`. The preceding sync log supplied a June 12 date but no immutable upstream SHA. The recovered path history has 13 commits under the current name and six under two predecessor names, March 11 through September 15. This entry establishes definitive provenance without retrospectively guessing the old sync's exact source revision.

| Upstream change | Local applicability |
|---|---|
| June 17 `85f28656af55574f0f9d1eaf25af4281684727b4`: disable account local auth | Applied in `foundry.bicep`; README explicitly warns existing key-auth consumers to migrate |
| June 23 `16fcedc3ed1c7ccadcf73a7e58aea3b48413673b`: per-project Storage Owner GUID | Local GUID already includes project principal and optional suffix. Preserved it and workspace ABAC rather than introducing a different GUID for an existing identical role/scope/principal |
| June 23 `aba282e7e98be16e747676c36ae639cc98e66eaf`: tracing/AMPLS and duplicate ACR DNS-link fix | Integrated App Insights, shared account connection and AMPLS into `diagnostics.bicep`, reusing its LAW; centralized DNS already avoids duplicate ACR links |
| July 21 `e47d01180329f46a5e0198e19e3eae5b3d22bf72`: evaluation readers | Added isolated `telemetry` phase to `foundry-roles.bicep`, wired to initial AND additional project identities at App Insights scope |
| September 15 `74b93d65e1b2c1e22d661ef883a747962f47a8ee`: private-CA vault and operations | One scoped `key-vault.bicep` module creates/references the vault and grants ACCOUNT identity Secrets User; existing PE module consumes its ID; recovery tooling is documented separately |

**Deliberate adaptations and preserved contracts**

- Main remains subscription-scoped with the same deterministic naming, RG, three VNets, two-pass routes, Basic Firewall/Bastion, Windows D8s_v5 default, secure password, Australia East default, unrestricted location and model parameters. No topology/diagram regeneration or BYO-VNet changes; separate #15 owns that work.
- ACR remains default-enabled Premium, PE-backed, with project AcrPull and optional developer CIDR. Existing outputs, Cosmos built-in role `00000000-0000-0000-0000-000000000002`, workspace Storage ABAC, deployment ordering and diagnostics fixes are retained.
- `enableAgentTracing` and `enableKeyVault` default to true, but can be disabled for new stacks. Disabled flags in incremental deployment do not delete existing resources. README describes the authentication change, new charges and decommissioning caveats.
- Required central DNS zones are merged with caller overrides. AMPLS attaches FIVE mappings, including the existing Blob zone. Upstream's four-zone omission is not copied; the default is 12 unique zones with three VNet links each.
- Tracing uses private App Insights/LAW ingestion but explicitly retains authorized public queries and AMPLS `Open` query mode. LAW diagnostic settings use the Azure platform private channel. See [Monitor topology/access guidance](https://learn.microsoft.com/azure/azure-monitor/fundamentals/private-link-design) and [five-zone requirements](https://learn.microsoft.com/azure/azure-monitor/fundamentals/private-link-configure#review-and-validate-ampls-configuration).
- Vault creation preserves upstream's `Enabled` public endpoint, default deny plus trusted-service bypass, 90-day soft delete and lab purge protection setting. Existing cross-subscription/RG vaults are referenced without settings/tag changes. New optional inputs are trimmed; tags merge caller values with the required `SecurityControl: Ignore`.
- `add-project` reuses the account-shared tracing connection and accepts `existingAppInsightsResourceId` for project evaluation roles in the component's actual scope. Existing timestamp naming and shared data-RG limitation are documented, not refactored.
- No upstream portal JSON, stale parameter schema, generated VM/Function artifacts, or network-input overhaul is imported. All 23 tracked tool-server/demo files in the pinned snapshot match locally byte-for-byte. The coordinating comparison against the untouched baseline also confirmed identical copied diagrams and `createCapHost.sh`, `deleteCapHost.sh` and `get-existing-resources.ps1`. These assets remain unchanged: there are no independent tool/demo updates or diagram-regeneration changes to adopt. The only absent demo file is the ignored Function `local.settings.json` developer configuration (upstream uses the local storage emulator), intentionally not imported. Shared upstream preflight/cleanup links do not imply importing those out-of-snapshot tools.
- Bicep does not upload certificates or set `trustedCertificates`. The private-CA `2026-07-15-preview` API is attributed to the pinned upstream source. The broad Microsoft Learn search did not independently establish feature/API support; neither GA status nor live availability is claimed. Recovery does not restore conversations/file/vector contents; account-host recreation must not affect sibling projects.
- Repository inspection found no Actions workflows, rulesets or protected-main status checks. The offline validation below was run explicitly; this sync does not add unrelated CI or branch-protection configuration.

**Offline validation:** Baseline 16/16 root templates compiled with Bicep 0.45.15 and no compiler/linter warnings (CLI upgrade notice only). Post-change 17/17 compile via `az bicep build --file ... --stdout`. `python3 -B tests/test_infrastructure_contracts.py -v` passes 32 deterministic tests, including both parameter examples with test-only `az.getSecret` replacement, new/existing vault scopes, feature combinations, five-zone DNS, roles, naming and retained behavior. No live deployment, validation, what-if, host recovery, secret resolution or `test_*_agents_v2.py` execution is part of this sync.
