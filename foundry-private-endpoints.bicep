/*
  foundry-private-endpoints.bicep
  -------------------------------
  Creates private endpoints in the `pe` subnet for the Foundry-stack resources:
    - AI Foundry / AI Services account (groupId: account)
    - AI Search                          (groupId: searchService)
    - Storage Account (blob)             (groupId: blob)
    - Cosmos DB                          (groupId: Sql)
    - Microsoft Fabric workspace         (optional, groupId: Fabric)

  Each PE gets a `privateDnsZoneGroup` linked to the matching pre-existing
  private DNS zones (zones and vnet links are owned by `dns.bicep` in this
  repo — this module just consumes the zone IDs via `dnsZoneIds`).

  Ported from upstream `modules-network-secured/private-endpoint-and-dns.bicep`
  with the zone create/link paths removed (delegated to dns.bicep).
*/

@description('Name of the AI Foundry / Cognitive Services account.')
param aiAccountName string
@description('Name of the AI Search service.')
param aiSearchName string
@description('Name of the Storage Account.')
param storageName string
@description('Name of the Cosmos DB account.')
param cosmosDBName string

@description('Optional full ARM resource ID of a Microsoft Fabric workspace to wire in via private endpoint. Leave empty to skip.')
param fabricWorkspaceResourceId string = ''

@description('Name of the existing VNet that owns the PE subnet.')
param vnetName string
@description('Name of the PE subnet inside the VNet.')
param peSubnetName string
@description('Subscription containing the VNet.')
param vnetSubscriptionId string = subscription().subscriptionId
@description('Resource group containing the VNet.')
param vnetResourceGroupName string = resourceGroup().name

@description('Suffix used for unique deployment / DNS group names.')
param suffix string

@description('Subscription containing the Storage Account.')
param storageAccountSubscriptionId string = subscription().subscriptionId
@description('Resource group containing the Storage Account.')
param storageAccountResourceGroupName string = resourceGroup().name

@description('Subscription containing the AI Search service.')
param aiSearchSubscriptionId string = subscription().subscriptionId
@description('Resource group containing the AI Search service.')
param aiSearchResourceGroupName string = resourceGroup().name

@description('Subscription containing the Cosmos DB account.')
param cosmosDBSubscriptionId string = subscription().subscriptionId
@description('Resource group containing the Cosmos DB account.')
param cosmosDBResourceGroupName string = resourceGroup().name

@description('Pre-existing private DNS zone IDs keyed by service. Produced by dns.bicep. Required keys: aiServices, openAi, cognitiveServices, aiSearch, storageBlob, cosmosDB. Optional: fabric.')
param dnsZoneIds object

// ---- Resource references ----
resource aiAccount 'Microsoft.CognitiveServices/accounts@2023-05-01' existing = {
  name: aiAccountName
  scope: resourceGroup()
}

resource aiSearch 'Microsoft.Search/searchServices@2023-11-01' existing = {
  name: aiSearchName
  scope: resourceGroup(aiSearchSubscriptionId, aiSearchResourceGroupName)
}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageName
  scope: resourceGroup(storageAccountSubscriptionId, storageAccountResourceGroupName)
}

resource cosmosDBAccount 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' existing = {
  name: cosmosDBName
  scope: resourceGroup(cosmosDBSubscriptionId, cosmosDBResourceGroupName)
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {
  name: vnetName
  scope: resourceGroup(vnetSubscriptionId, vnetResourceGroupName)
}
resource peSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: peSubnetName
}

// ---- Fabric (optional) ----
var fabricPassedIn = fabricWorkspaceResourceId != ''
var fabricParts = split(fabricWorkspaceResourceId, '/')
var fabricWorkspaceName = fabricPassedIn ? last(fabricParts) : ''

// ---- AI Services Private Endpoint ----
resource aiAccountPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${aiAccountName}-private-endpoint'
  location: resourceGroup().location
  properties: {
    subnet: { id: peSubnet.id }
    privateLinkServiceConnections: [
      {
        name: '${aiAccountName}-private-link-service-connection'
        properties: {
          privateLinkServiceId: aiAccount.id
          groupIds: ['account']
        }
      }
    ]
  }
}

// ---- AI Search Private Endpoint ----
resource aiSearchPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${aiSearchName}-private-endpoint'
  location: resourceGroup().location
  properties: {
    subnet: { id: peSubnet.id }
    privateLinkServiceConnections: [
      {
        name: '${aiSearchName}-private-link-service-connection'
        properties: {
          privateLinkServiceId: aiSearch.id
          groupIds: ['searchService']
        }
      }
    ]
  }
}

// ---- Storage (blob) Private Endpoint ----
resource storagePrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${storageName}-private-endpoint'
  location: resourceGroup().location
  properties: {
    subnet: { id: peSubnet.id }
    privateLinkServiceConnections: [
      {
        name: '${storageName}-private-link-service-connection'
        properties: {
          privateLinkServiceId: storageAccount.id
          groupIds: ['blob']
        }
      }
    ]
  }
}

// ---- Cosmos DB Private Endpoint ----
resource cosmosDBPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${cosmosDBName}-private-endpoint'
  location: resourceGroup().location
  properties: {
    subnet: { id: peSubnet.id }
    privateLinkServiceConnections: [
      {
        name: '${cosmosDBName}-private-link-service-connection'
        properties: {
          privateLinkServiceId: cosmosDBAccount.id
          groupIds: ['Sql']
        }
      }
    ]
  }
}

// ---- Fabric Private Endpoint (optional) ----
resource fabricPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = if (fabricPassedIn) {
  name: '${fabricWorkspaceName}-fabric-private-endpoint'
  location: resourceGroup().location
  properties: {
    subnet: { id: peSubnet.id }
    privateLinkServiceConnections: [
      {
        name: '${fabricWorkspaceName}-private-link-service-connection'
        properties: {
          privateLinkServiceId: fabricWorkspaceResourceId
          groupIds: ['Fabric']
        }
      }
    ]
  }
}

// ---- DNS Zone Groups (zones + links live in dns.bicep) ----
resource aiServicesDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: aiAccountPrivateEndpoint
  name: '${aiAccountName}-dns-group'
  properties: {
    privateDnsZoneConfigs: [
      { name: '${aiAccountName}-dns-aiserv-config', properties: { privateDnsZoneId: dnsZoneIds.aiServices } }
      { name: '${aiAccountName}-dns-openai-config', properties: { privateDnsZoneId: dnsZoneIds.openAi } }
      { name: '${aiAccountName}-dns-cogserv-config', properties: { privateDnsZoneId: dnsZoneIds.cognitiveServices } }
    ]
  }
}

resource aiSearchDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: aiSearchPrivateEndpoint
  name: '${aiSearchName}-dns-group'
  properties: {
    privateDnsZoneConfigs: [
      { name: '${aiSearchName}-dns-config', properties: { privateDnsZoneId: dnsZoneIds.aiSearch } }
    ]
  }
}

resource storageDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: storagePrivateEndpoint
  name: '${storageName}-dns-group'
  properties: {
    privateDnsZoneConfigs: [
      { name: '${storageName}-dns-config', properties: { privateDnsZoneId: dnsZoneIds.storageBlob } }
    ]
  }
}

resource cosmosDBDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: cosmosDBPrivateEndpoint
  name: '${cosmosDBName}-dns-group'
  properties: {
    privateDnsZoneConfigs: [
      { name: '${cosmosDBName}-dns-config', properties: { privateDnsZoneId: dnsZoneIds.cosmosDB } }
    ]
  }
}

resource fabricDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = if (fabricPassedIn) {
  parent: fabricPrivateEndpoint
  name: '${fabricWorkspaceName}-dns-group'
  properties: {
    privateDnsZoneConfigs: [
      { name: '${fabricWorkspaceName}-dns-config', properties: { privateDnsZoneId: dnsZoneIds.?fabric ?? '' } }
    ]
  }
}

@description('Suffix tag echoed back for use in dependent module names.')
output suffix string = suffix
