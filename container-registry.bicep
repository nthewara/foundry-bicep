// =============================================================================
// container-registry.bicep
// -----------------------------------------------------------------------------
// Optional Azure Container Registry (Premium SKU) with a Private Endpoint in the
// `pe` subnet, wired to the pre-existing `privatelink.azurecr.io` DNS zone, plus
// an AcrPull role assignment for the project managed identity.
//
// Ported from upstream `modules-network-secured/container-registry.bicep`
// (foundry-samples #19, PR #519 "feat: add optional ACR with Private Endpoint").
//
// Repo idiom note: upstream's module creates + links the private DNS zone
// itself. This repo delegates all zone create/link to `dns.bicep`, so this
// module instead CONSUMES the zone ID via `acrDnsZoneId` (same pattern as
// `foundry-private-endpoints.bicep`). Add `privatelink.azurecr.io` to the
// `privateDnsZones` list in main.bicep so the zone exists + is linked to the
// VNets before this PE's DNS zone group is created.
//
// Premium SKU is required for Private Endpoint support.
// Scope: resourceGroup.
// =============================================================================

targetScope = 'resourceGroup'

@description('Name of the Azure Container Registry.')
param acrName string

@description('Azure region for the ACR.')
param location string

@description('Resource ID of the Private Endpoint (`pe`) subnet.')
param peSubnetId string

@description('Suffix used for unique deployment / DNS group names.')
param suffix string

@description('Resource ID of the pre-existing `privatelink.azurecr.io` private DNS zone (created + linked by dns.bicep).')
param acrDnsZoneId string

@description('Optional developer IP CIDR to allowlist for ACR push access (e.g., 203.0.113.0/26 or 10.0.0.0/16). When empty, public access remains disabled.')
param developerIpCidr string = ''

@description('Principal ID of the project managed identity to grant AcrPull role. When empty, no role assignment is created.')
param projectPrincipalId string = ''

// ---- ACR Resource ----
resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: {
    name: 'Premium'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: empty(developerIpCidr) ? 'Disabled' : 'Enabled'
    networkRuleBypassOptions: 'AzureServices'
    networkRuleSet: empty(developerIpCidr) ? null : {
      defaultAction: 'Deny'
      ipRules: [
        {
          action: 'Allow'
          value: developerIpCidr
        }
      ]
    }
  }
}

// ---- Private Endpoint ----
resource acrPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${acrName}-private-endpoint'
  location: location
  properties: {
    subnet: { id: peSubnetId }
    privateLinkServiceConnections: [
      {
        name: '${acrName}-private-link-service-connection'
        properties: {
          privateLinkServiceId: containerRegistry.id
          groupIds: [ 'registry' ]
        }
      }
    ]
  }
}

// ---- DNS Zone Group (zone + vnet links live in dns.bicep) ----
resource acrDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: acrPrivateEndpoint
  name: '${acrName}-dns-group'
  properties: {
    privateDnsZoneConfigs: [
      { name: '${acrName}-dns-config', properties: { privateDnsZoneId: acrDnsZoneId } }
    ]
  }
}

// ---- AcrPull Role Assignment ----
// Grants the project managed identity pull access to the ACR.
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d' // AcrPull built-in role

resource acrPullRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(projectPrincipalId)) {
  name: guid(containerRegistry.id, projectPrincipalId, acrPullRoleId)
  scope: containerRegistry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: projectPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// ---- Outputs ----
@description('Resource ID of the Azure Container Registry.')
output acrId string = containerRegistry.id

@description('Name of the Azure Container Registry.')
output acrName string = containerRegistry.name

@description('Login server URL of the Azure Container Registry.')
output acrLoginServer string = containerRegistry.properties.loginServer

@description('Suffix tag echoed back for use in dependent module names.')
output suffix string = suffix
