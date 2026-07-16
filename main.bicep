// =============================================================================
// main.bicep — top-level orchestrator (subscription scope)
//
// Single-shot deployment that creates the resource group and wires together:
//   networking → firewall → bastion → vm → dns → foundry-dependencies →
//   foundry → foundry-identity (initial project) → foundry-private-endpoints →
//   foundry-roles (pre) → foundry-capability-host → foundry-roles (post) →
//   diagnostics
//
// Bicep computes dependency order automatically from the module input/output
// graph; this file just declares what feeds what.
//
// Deploy:
//   az deployment sub create \
//     --location <region> \
//     --template-file main.bicep \
//     --parameters main.bicepparam
// =============================================================================

targetScope = 'subscription'

// -----------------------------------------------------------------------------
// General parameters
// -----------------------------------------------------------------------------

@description('Azure region for all resources.')
param location string = 'australiaeast'

@description('Naming prefix for all resources (lowercase, alphanumeric).')
@minLength(3)
@maxLength(16)
param prefix string = 'aifoundrybicep'

@description('Override the random suffix used in resource names. Defaults to a deterministic 4-char value derived from the subscription + RG name.')
param randomSuffix string = ''

@description('Name of the resource group to create / use.')
param resourceGroupName string = '${prefix}-${take(uniqueString(subscription().subscriptionId, prefix), 4)}'

@description('Tags applied to the resource group and (where supported) child resources.')
param tags object = {
  lab: 'foundry-bicep'
  managedBy: 'bicep'
  owner: 'nthewara'
}

// -----------------------------------------------------------------------------
// Networking parameters
// -----------------------------------------------------------------------------

@description('CIDR for the hub VNet. Must be a /23 (subnets carved as /26).')
param hubVnetPrefix string = '10.100.0.0/23'

@description('CIDR for the VM spoke VNet.')
param vmVnetPrefix string = '10.10.10.0/23'

@description('CIDR for the AI app spoke VNet. Must be a /23 — agents/mcp live in the second /24.')
param aiappVnetPrefix string = '10.10.20.0/23'

// -----------------------------------------------------------------------------
// Feature toggles
// -----------------------------------------------------------------------------

@description('Provision Azure Firewall (Basic SKU by default). When false, UDRs are skipped and egress is unrestricted.')
param fwProvision bool = true

@description('Azure Firewall SKU tier. Basic is the locked lab default.')
@allowed([ 'Basic', 'Standard', 'Premium' ])
param fwSku string = 'Basic'

@description('Provision Azure Bastion (Basic SKU).')
param bastionProvision bool = true

@description('Create + attach an NSG to the delegated agents/mcp subnets with the blog-recommended minimum outbound rules (AzureActiveDirectory, AzureContainerAppsManagement, AzureContainerRegistry, 100.67.0.0/24). See https://nirmalt.com/posts/securingmicrosoftfoundrywithbyovnet.')
param attachAgentNsg bool = true

@description('When true (default), the Azure Firewall egress is locked to the Foundry-required FQDN allowlist + service-tag/infra network rules (least-privilege). When false, egress falls back to the permissive * -> * rule for troubleshooting.')
param restrictEgress bool = true

@description('Deploy the Windows jump-box VM.')
param vmDeploy bool = true

@description('Provision an optional Azure Container Registry (Premium SKU) with a Private Endpoint in the `pe` subnet, a `privatelink.azurecr.io` DNS zone, and an AcrPull role for the project identity. Synced from upstream sample #19 (PR #519).')
param enableContainerRegistry bool = true

@description('Optional developer IP CIDR to allowlist for ACR push access (e.g., 203.0.113.0/26 or 10.0.0.0/16). When set, ACR public network access is enabled with a deny-all default + this allowlist rule so developers can push images. When empty, public access stays disabled (PE-only).')
param developerIpCidr string = ''

// -----------------------------------------------------------------------------
// VM parameters
// -----------------------------------------------------------------------------

@description('Jump-box VM size. Defaults to D8s_v5 (matches the user lab pref).')
param vmSize string = 'Standard_D8s_v5'

@description('Local admin username for the jump-box VM.')
param adminUsername string = 'azureadmin'

@description('Local admin password for the jump-box VM. Wire from Key Vault in .bicepparam (getSecret).')
@secure()
param adminPassword string

// -----------------------------------------------------------------------------
// DNS parameters
// -----------------------------------------------------------------------------

@description('Private DNS zones to create + link to all three VNets. The default 7-zone set covers everything Foundry needs, including the ACR zone (`privatelink.azurecr.io`) used when enableContainerRegistry=true.')
param privateDnsZones array = [
  'privatelink.cognitiveservices.azure.com'
  'privatelink.openai.azure.com'
  'privatelink.services.ai.azure.com'
  'privatelink.blob.${environment().suffixes.storage}'
  'privatelink.search.windows.net'
  'privatelink.documents.azure.com'
  'privatelink.azurecr.io'
]

// -----------------------------------------------------------------------------
// Foundry parameters
// -----------------------------------------------------------------------------

@description('Logical name component for the AI Services account.')
param aiServicesNameBase string = 'aiservices'

@description('Logical name component for the AI Search service.')
param aiSearchNameBase string = 'aisearch'

@description('Logical name component for the Cosmos DB account.')
param cosmosDBNameBase string = 'cosmosdb'

@description('Logical name component for the dependency Storage account.')
param azureStorageNameBase string = 'foundrystg'

@description('Initial project name.')
param projectName string = 'project'

@description('Project description.')
param projectDescription string = 'A project for the AI Foundry account with network secured deployed Agent'

@description('Project display name.')
param projectDisplayName string = 'network secured agent project'

@description('Capability host name attached to the project.')
param projectCapHostName string = 'caphostproj'

@description('Model deployment: model name.')
param modelName string = 'gpt-4o-mini'

@description('Model deployment: provider.')
param modelFormat string = 'OpenAI'

@description('Model deployment: version.')
param modelVersion string = '2024-07-18'

@description('Model deployment: SKU name.')
param modelSkuName string = 'GlobalStandard'

@description('Model deployment: TPM capacity.')
param modelCapacity int = 30

// -----------------------------------------------------------------------------
// Computed / shared
// -----------------------------------------------------------------------------

var storageBlobZone = 'privatelink.blob.${environment().suffixes.storage}'
var acrDnsZone = 'privatelink.azurecr.io'

var suffix = empty(randomSuffix) ? take(uniqueString(subscription().subscriptionId, resourceGroupName), 4) : randomSuffix

// ACR name: lowercase alphanumeric only (no hyphens allowed in ACR names).
var acrName = toLower('acr${suffix}')

// =============================================================================
// Resource group
// =============================================================================

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

// =============================================================================
// Stage 0 — Agent-subnet NSG (blog-recommended minimum outbound rules).
//           Created before networking so its id can be attached to the
//           delegated agents/mcp subnets in both networking passes.
// =============================================================================

module agentNsg 'nsg.bicep' = if (attachAgentNsg) {
  name: 'agentNsg'
  scope: rg
  params: {
    location: location
    prefix: prefix
    randomSuffix: suffix
  }
}

// =============================================================================
// Stage 1 — Networking (no UDR yet, FW IP not known)
// =============================================================================

module networking 'networking.bicep' = {
  name: 'networking'
  scope: rg
  params: {
    location: location
    prefix: prefix
    randomSuffix: suffix
    hubVnetPrefix: hubVnetPrefix
    vmVnetPrefix: vmVnetPrefix
    aiappVnetPrefix: aiappVnetPrefix
    routeTableNextHopIp: ''
    fwProvision: false // UDRs will be added by `routes` module after FW IP is known
    attachAgentNsg: attachAgentNsg
    #disable-next-line BCP318
    agentNsgId: attachAgentNsg ? agentNsg.outputs.nsgId : ''
  }
}

// =============================================================================
// Stage 2 — Firewall (provides the private IP needed for UDRs)
// =============================================================================

module firewall 'firewall.bicep' = if (fwProvision) {
  name: 'firewall'
  scope: rg
  params: {
    location: location
    prefix: prefix
    randomSuffix: suffix
    fwSku: fwSku
    fwSubnetId: networking.outputs.firewallSubnetId
    fwMgmtSubnetId: networking.outputs.firewallMgmtSubnetId
    restrictEgress: restrictEgress
  }
}

// =============================================================================
// Stage 3 — Re-wire networking with the FW private IP (creates UDRs and
//           re-asserts the spoke subnets with routeTable.id attached).
//           Safe because Bicep idempotently updates the existing VNets.
// =============================================================================

module routes 'networking.bicep' = if (fwProvision) {
  name: 'routes'
  scope: rg
  params: {
    location: location
    prefix: prefix
    randomSuffix: suffix
    hubVnetPrefix: hubVnetPrefix
    vmVnetPrefix: vmVnetPrefix
    aiappVnetPrefix: aiappVnetPrefix
    #disable-next-line BCP318
    routeTableNextHopIp: fwProvision ? firewall.outputs.firewallPrivateIp : ''
    fwProvision: true
    attachAgentNsg: attachAgentNsg
    #disable-next-line BCP318
    agentNsgId: attachAgentNsg ? agentNsg.outputs.nsgId : ''
  }
}

// =============================================================================
// Stage 4 — Bastion (optional)
// =============================================================================

module bastion 'bastion.bicep' = if (bastionProvision) {
  name: 'bastion'
  scope: rg
  params: {
    location: location
    prefix: prefix
    randomSuffix: suffix
    bastionSubnetId: networking.outputs.bastionSubnetId
  }
}

// =============================================================================
// Stage 5 — Jump-box VM (optional)
// =============================================================================

module vm 'vm.bicep' = if (vmDeploy) {
  name: 'vm'
  scope: rg
  params: {
    location: location
    prefix: prefix
    randomSuffix: suffix
    vmSubnetId: networking.outputs.vmSubnetId
    vmSize: vmSize
    adminUsername: adminUsername
    adminPassword: adminPassword
  }
}

// =============================================================================
// Stage 6 — Private DNS zones + VNet links
// =============================================================================

module dns 'dns.bicep' = {
  name: 'dns'
  scope: rg
  params: {
    prefix: prefix
    randomSuffix: suffix
    privateDnsZones: privateDnsZones
    hubVnetId: networking.outputs.hubVnetId
    aiappVnetId: networking.outputs.aiappVnetId
    vmVnetId: networking.outputs.vmVnetId
  }
}

// =============================================================================
// Stage 7 — Foundry dependencies (Cosmos / Storage / AI Search, all private)
// =============================================================================

var aiSearchName = '${aiSearchNameBase}${suffix}'
var cosmosDBName = '${cosmosDBNameBase}${suffix}'
var azureStorageName = toLower('${azureStorageNameBase}${suffix}')
var aiServicesName = '${aiServicesNameBase}${suffix}'

module foundryDeps 'foundry-dependencies.bicep' = {
  name: 'foundryDeps'
  scope: rg
  params: {
    location: location
    aiSearchName: aiSearchName
    azureStorageName: azureStorageName
    cosmosDBName: cosmosDBName
    aiSearchResourceId: ''
    azureStorageAccountResourceId: ''
    cosmosDBResourceId: ''
    aiSearchExists: false
    azureStorageExists: false
    cosmosDBExists: false
  }
}

// =============================================================================
// Stage 8 — Foundry account + model deployment
// =============================================================================

module foundry 'foundry.bicep' = {
  name: 'foundry'
  scope: rg
  params: {
    accountName: aiServicesName
    location: location
    modelName: modelName
    modelFormat: modelFormat
    modelVersion: modelVersion
    modelSkuName: modelSkuName
    modelCapacity: modelCapacity
    agentSubnetId: networking.outputs.agentsSubnetId
    networkInjection: 'true'
  }
  dependsOn: [ foundryDeps ]
}

// =============================================================================
// Stage 9 — Initial project (identity + connections)
// =============================================================================

module projectMod 'project.bicep' = {
  name: 'project'
  scope: rg
  params: {
    accountName: foundry.outputs.accountName
    location: location
    projectName: projectName
    projectDescription: projectDescription
    displayName: projectDisplayName
    aiSearchName: foundryDeps.outputs.aiSearchName
    aiSearchServiceResourceGroupName: foundryDeps.outputs.aiSearchServiceResourceGroupName
    aiSearchServiceSubscriptionId: foundryDeps.outputs.aiSearchServiceSubscriptionId
    cosmosDBName: foundryDeps.outputs.cosmosDBName
    cosmosDBSubscriptionId: foundryDeps.outputs.cosmosDBSubscriptionId
    cosmosDBResourceGroupName: foundryDeps.outputs.cosmosDBResourceGroupName
    azureStorageName: foundryDeps.outputs.azureStorageName
    azureStorageSubscriptionId: foundryDeps.outputs.azureStorageSubscriptionId
    azureStorageResourceGroupName: foundryDeps.outputs.azureStorageResourceGroupName
  }
}

// =============================================================================
// Stage 10 — Private endpoints for AI account + dependencies, attached to PE
//             subnet, wired to the DNS zone IDs from dns.bicep.
// =============================================================================

module foundryPe 'foundry-private-endpoints.bicep' = {
  name: 'foundryPe'
  scope: rg
  params: {
    aiAccountName: foundry.outputs.accountName
    aiSearchName: foundryDeps.outputs.aiSearchName
    storageName: foundryDeps.outputs.azureStorageName
    cosmosDBName: foundryDeps.outputs.cosmosDBName
    vnetName: networking.outputs.aiappVnetName
    peSubnetName: networking.outputs.peSubnetName
    suffix: suffix
    dnsZoneIds: {
      aiServices: dns.outputs.zoneIds['privatelink.services.ai.azure.com']
      openAi: dns.outputs.zoneIds['privatelink.openai.azure.com']
      cognitiveServices: dns.outputs.zoneIds['privatelink.cognitiveservices.azure.com']
      aiSearch: dns.outputs.zoneIds['privatelink.search.windows.net']
      storageBlob: dns.outputs.zoneIds[storageBlobZone]
      cosmosDB: dns.outputs.zoneIds['privatelink.documents.azure.com']
    }
  }
}

// =============================================================================
// Stage 10b — Optional Azure Container Registry (Premium) with Private Endpoint
//             in the `pe` subnet, AcrPull for the project identity. The
//             `privatelink.azurecr.io` zone + VNet links are owned by dns.bicep;
//             this module just consumes the zone ID (repo idiom). Synced from
//             upstream sample #19 (PR #519).
// =============================================================================

module acr 'container-registry.bicep' = if (enableContainerRegistry) {
  name: 'acr'
  scope: rg
  params: {
    acrName: acrName
    location: location
    peSubnetId: networking.outputs.peSubnetId
    suffix: suffix
    acrDnsZoneId: dns.outputs.zoneIds[acrDnsZone]
    developerIpCidr: developerIpCidr
    projectPrincipalId: projectMod.outputs.projectPrincipalId
  }
  dependsOn: [ foundryPe ]
}

// =============================================================================
// Stage 11a — Pre-cap-host role assignments (Search contrib, Storage Blob
//              Data Contributor, Cosmos DB Operator)
// =============================================================================

module rolesPre 'foundry-roles.bicep' = {
  name: 'rolesPre'
  scope: rg
  params: {
    phase: 'pre'
    projectPrincipalId: projectMod.outputs.projectPrincipalId
    aiSearchName: foundryDeps.outputs.aiSearchName
    cosmosDBName: foundryDeps.outputs.cosmosDBName
    storageName: foundryDeps.outputs.azureStorageName
    projectWorkspaceIdGuid: projectMod.outputs.projectWorkspaceIdGuid
  }
  dependsOn: [ foundryPe ]
}

// =============================================================================
// Stage 11b — Capability host (Agents runtime hookup on the project)
// =============================================================================

module capHost 'foundry-capability-host.bicep' = {
  name: 'capHost'
  scope: rg
  params: {
    accountName: foundry.outputs.accountName
    projectName: projectMod.outputs.projectName
    cosmosDBConnection: projectMod.outputs.cosmosDBConnection
    azureStorageConnection: projectMod.outputs.azureStorageConnection
    aiSearchConnection: projectMod.outputs.aiSearchConnection
    projectCapHost: projectCapHostName
  }
  dependsOn: [ rolesPre ]
}

// =============================================================================
// Stage 11c — Post-cap-host role assignments (Storage Blob Data Owner with
//              ABAC condition + Cosmos DB data-plane role 00000000-…-0002)
// =============================================================================

module rolesPost 'foundry-roles.bicep' = {
  name: 'rolesPost'
  scope: rg
  params: {
    phase: 'post'
    projectPrincipalId: projectMod.outputs.projectPrincipalId
    aiSearchName: foundryDeps.outputs.aiSearchName
    cosmosDBName: foundryDeps.outputs.cosmosDBName
    storageName: foundryDeps.outputs.azureStorageName
    projectWorkspaceIdGuid: projectMod.outputs.projectWorkspaceIdGuid
  }
  dependsOn: [ capHost ]
}

// =============================================================================
// Stage 12 — Diagnostics fan-out (LAW + diag storage + diagnosticSettings on
//             every interesting resource).
// =============================================================================

module diagnostics 'diagnostics.bicep' = {
  name: 'diagnostics'
  scope: rg
  params: {
    location: location
    prefix: prefix
    randomSuffix: suffix
    targets: concat(
      [
        { name: 'hubVnet', resourceId: networking.outputs.hubVnetId }
        { name: 'vmVnet', resourceId: networking.outputs.vmVnetId }
        { name: 'aiappVnet', resourceId: networking.outputs.aiappVnetId }
        { name: 'aiSearch', resourceId: foundryDeps.outputs.aiSearchID }
        { name: 'cosmosDB', resourceId: foundryDeps.outputs.cosmosDBId }
        { name: 'storage', resourceId: foundryDeps.outputs.azureStorageId, skipLogs: true }
        { name: 'aiAccount', resourceId: foundry.outputs.accountID }
      ],
      #disable-next-line BCP318
      fwProvision ? [
        #disable-next-line BCP318
        { name: 'firewall', resourceId: firewall.outputs.firewallId }
        #disable-next-line BCP318
        { name: 'firewallPip', resourceId: firewall.outputs.dataPipId }
      ] : [],
      #disable-next-line BCP318
      bastionProvision ? [
        #disable-next-line BCP318
        { name: 'bastion', resourceId: bastion.outputs.bastionId }
        #disable-next-line BCP318
        { name: 'bastionPip', resourceId: bastion.outputs.bastionPipId }
      ] : []
    )
  }
  dependsOn: [ rolesPost, dns ]
}

// =============================================================================
// Outputs
// =============================================================================

output resourceGroupName string = rg.name
output suffix string = suffix
output hubVnetId string = networking.outputs.hubVnetId
output aiappVnetId string = networking.outputs.aiappVnetId
#disable-next-line BCP318
output firewallPrivateIp string = fwProvision ? firewall.outputs.firewallPrivateIp : ''
#disable-next-line BCP318
output bastionName string = bastionProvision ? bastion.outputs.bastionName : ''
#disable-next-line BCP318
output vmPrivateIp string = vmDeploy ? vm.outputs.vmPrivateIp : ''
output aiAccountId string = foundry.outputs.accountID
output aiAccountEndpoint string = foundry.outputs.accountTarget
output projectName string = projectMod.outputs.projectName
output projectId string = projectMod.outputs.projectId
output capabilityHostName string = capHost.outputs.projectCapHost
output lawId string = diagnostics.outputs.lawId
#disable-next-line BCP318
output acrId string = enableContainerRegistry ? acr.outputs.acrId : ''
#disable-next-line BCP318
output acrLoginServer string = enableContainerRegistry ? acr.outputs.acrLoginServer : ''
