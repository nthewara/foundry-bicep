/*
  foundry-dependencies.bicep
  --------------------------
  Creates the backend resources used by an Azure AI Foundry account in network
  secured mode:
    - Cosmos DB account (publicNetworkAccess = Disabled)
    - Storage Account (publicNetworkAccess = Disabled, shared key disabled)
    - AI Search service (publicNetworkAccess = disabled)

  Optional existing ARM IDs short-circuit creation (mirrors upstream behavior).

  Ported from upstream `modules-network-secured/standard-dependent-resources.bicep`.
*/

@description('Azure region for deployment.')
param location string

@description('Name of the AI Search service to create when one is not supplied.')
param aiSearchName string

@description('Name of the Storage Account to create when one is not supplied.')
param azureStorageName string

@description('Name of the Cosmos DB account to create when one is not supplied.')
param cosmosDBName string

@description('Full ARM resource ID of an existing AI Search service. If empty, a new one is created.')
param aiSearchResourceId string = ''

@description('Full ARM resource ID of an existing Storage Account. If empty, a new one is created.')
param azureStorageAccountResourceId string = ''

@description('Full ARM resource ID of an existing Cosmos DB account. If empty, a new one is created.')
param cosmosDBResourceId string = ''

@description('True if an existing AI Search service is being passed in.')
param aiSearchExists bool

@description('True if an existing Storage Account is being passed in.')
param azureStorageExists bool

@description('True if an existing Cosmos DB account is being passed in.')
param cosmosDBExists bool

// ---- Cosmos DB ----
var cosmosParts = split(cosmosDBResourceId, '/')

resource existingCosmosDB 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' existing = if (cosmosDBExists) {
  name: cosmosParts[8]
  scope: resourceGroup(cosmosParts[2], cosmosParts[4])
}

var canaryRegions = ['eastus2euap', 'centraluseuap']
var cosmosDbRegion = contains(canaryRegions, location) ? 'westus' : location

resource cosmosDB 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = if (!cosmosDBExists) {
  name: cosmosDBName
  location: cosmosDbRegion
  kind: 'GlobalDocumentDB'
  properties: {
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    disableLocalAuth: true
    enableAutomaticFailover: false
    enableMultipleWriteLocations: false
    publicNetworkAccess: 'Disabled'
    enableFreeTier: false
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    databaseAccountOfferType: 'Standard'
  }
}

// ---- AI Search ----
var acsParts = split(aiSearchResourceId, '/')

resource existingSearchService 'Microsoft.Search/searchServices@2024-06-01-preview' existing = if (aiSearchExists) {
  name: acsParts[8]
  scope: resourceGroup(acsParts[2], acsParts[4])
}

resource aiSearch 'Microsoft.Search/searchServices@2024-06-01-preview' = if (!aiSearchExists) {
  name: aiSearchName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    disableLocalAuth: false
    authOptions: { aadOrApiKey: { aadAuthFailureMode: 'http401WithBearerChallenge' } }
    encryptionWithCmk: {
      enforcement: 'Unspecified'
    }
    hostingMode: 'default'
    partitionCount: 1
    publicNetworkAccess: 'disabled'
    replicaCount: 1
    semanticSearch: 'disabled'
    networkRuleSet: {
      bypass: 'None'
      ipRules: []
    }
  }
  sku: {
    name: 'standard'
  }
}

// ---- Storage ----
var azureStorageParts = split(azureStorageAccountResourceId, '/')

resource existingAzureStorageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' existing = if (azureStorageExists) {
  name: azureStorageParts[8]
  scope: resourceGroup(azureStorageParts[2], azureStorageParts[4])
}

@description('Regions that do not support Standard ZRS storage.')
param noZRSRegions array = ['southindia', 'westus']

@description('Storage SKU override (auto-selected for regions without ZRS support).')
param sku object = contains(noZRSRegions, location) ? { name: 'Standard_GRS' } : { name: 'Standard_ZRS' }

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = if (!azureStorageExists) {
  name: azureStorageName
  location: location
  kind: 'StorageV2'
  sku: sku
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Deny'
      virtualNetworkRules: []
    }
    allowSharedKeyAccess: false
  }
}

// ---- Outputs ----
@description('Name of the AI Search service (new or existing).')
output aiSearchName string = aiSearchExists ? existingSearchService.name : aiSearch.name

@description('ARM resource ID of the AI Search service.')
output aiSearchID string = aiSearchExists ? existingSearchService.id : aiSearch.id

@description('Resource group containing the AI Search service.')
output aiSearchServiceResourceGroupName string = aiSearchExists ? acsParts[4] : resourceGroup().name

@description('Subscription containing the AI Search service.')
output aiSearchServiceSubscriptionId string = aiSearchExists ? acsParts[2] : subscription().subscriptionId

@description('Name of the Storage Account (new or existing).')
output azureStorageName string = azureStorageExists ? existingAzureStorageAccount.name : storage.name

@description('ARM resource ID of the Storage Account.')
output azureStorageId string = azureStorageExists ? existingAzureStorageAccount.id : storage.id

@description('Resource group containing the Storage Account.')
output azureStorageResourceGroupName string = azureStorageExists ? azureStorageParts[4] : resourceGroup().name

@description('Subscription containing the Storage Account.')
output azureStorageSubscriptionId string = azureStorageExists ? azureStorageParts[2] : subscription().subscriptionId

@description('Name of the Cosmos DB account (new or existing).')
output cosmosDBName string = cosmosDBExists ? existingCosmosDB.name : cosmosDB.name

@description('ARM resource ID of the Cosmos DB account.')
output cosmosDBId string = cosmosDBExists ? existingCosmosDB.id : cosmosDB.id

@description('Resource group containing the Cosmos DB account.')
output cosmosDBResourceGroupName string = cosmosDBExists ? cosmosParts[4] : resourceGroup().name

@description('Subscription containing the Cosmos DB account.')
output cosmosDBSubscriptionId string = cosmosDBExists ? cosmosParts[2] : subscription().subscriptionId
