/*
  foundry-identity.bicep
  ----------------------
  Creates an Azure AI Foundry project (sub-resource of the AI Services account)
  with a system-assigned managed identity and BYO connections to Cosmos DB,
  Storage and AI Search.

  Consolidates three upstream files:
    - modules-network-secured/ai-project-identity.bicep
    - modules-network-secured/ai-project-identity-unique.bicep
    - modules-network-secured/format-project-workspace-id.bicep

  When `uniqueConnectionSuffix` is empty the connection names match the
  resource names (matches the non-unique upstream path used by the initial
  project). When non-empty (e.g. `-${projectName}`) connection names are
  suffixed so multiple projects can co-exist on one account (matches the
  unique upstream path used by add-project).

  The account-MI side of identity wiring lives in `foundry.bicep` (the
  account creates its own SystemAssigned identity there). Role assignments
  for the project MI live in `foundry-roles.bicep`.
*/

@description('Name of the parent AI Services / Foundry account.')
param accountName string

@description('Azure region for the project.')
param location string

@description('Project name.')
param projectName string

@description('Project description.')
param projectDescription string

@description('Project display name.')
param displayName string

@description('AI Search service name to wire as a connection.')
param aiSearchName string
@description('Resource group containing the AI Search service.')
param aiSearchServiceResourceGroupName string
@description('Subscription containing the AI Search service.')
param aiSearchServiceSubscriptionId string

@description('Cosmos DB account name to wire as a connection.')
param cosmosDBName string
@description('Subscription containing the Cosmos DB account.')
param cosmosDBSubscriptionId string
@description('Resource group containing the Cosmos DB account.')
param cosmosDBResourceGroupName string

@description('Storage Account name to wire as a connection.')
param azureStorageName string
@description('Subscription containing the Storage Account.')
param azureStorageSubscriptionId string
@description('Resource group containing the Storage Account.')
param azureStorageResourceGroupName string

@description('Optional suffix appended to project connection names. Empty = use resource names verbatim (matches upstream ai-project-identity.bicep). Non-empty = matches upstream ai-project-identity-unique.bicep.')
param uniqueConnectionSuffix string = ''

resource searchService 'Microsoft.Search/searchServices@2024-06-01-preview' existing = {
  name: aiSearchName
  scope: resourceGroup(aiSearchServiceSubscriptionId, aiSearchServiceResourceGroupName)
}

resource cosmosDBAccount 'Microsoft.DocumentDB/databaseAccounts@2024-12-01-preview' existing = {
  name: cosmosDBName
  scope: resourceGroup(cosmosDBSubscriptionId, cosmosDBResourceGroupName)
}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: azureStorageName
  scope: resourceGroup(azureStorageSubscriptionId, azureStorageResourceGroupName)
}

resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: accountName
  scope: resourceGroup()
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' = {
  parent: account
  name: projectName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    description: projectDescription
    displayName: displayName
  }

  resource project_connection_cosmosdb_account 'connections@2025-04-01-preview' = {
    name: '${cosmosDBName}${uniqueConnectionSuffix}'
    properties: {
      category: 'CosmosDB'
      target: cosmosDBAccount.properties.documentEndpoint
      authType: 'AAD'
      metadata: {
        ApiType: 'Azure'
        ResourceId: cosmosDBAccount.id
        location: cosmosDBAccount.location
      }
    }
  }

  resource project_connection_azure_storage 'connections@2025-04-01-preview' = {
    name: '${azureStorageName}${uniqueConnectionSuffix}'
    properties: {
      category: 'AzureStorageAccount'
      target: storageAccount.properties.primaryEndpoints.blob
      authType: 'AAD'
      metadata: {
        ApiType: 'Azure'
        ResourceId: storageAccount.id
        location: storageAccount.location
      }
    }
  }

  resource project_connection_azureai_search 'connections@2025-04-01-preview' = {
    name: '${aiSearchName}${uniqueConnectionSuffix}'
    properties: {
      category: 'CognitiveSearch'
      target: 'https://${aiSearchName}.search.windows.net'
      authType: 'AAD'
      metadata: {
        ApiType: 'Azure'
        ResourceId: searchService.id
        location: searchService.location
      }
    }
  }
}

// ---- Workspace ID formatter (port of format-project-workspace-id.bicep) ----
// `internalId` is real on the runtime resource but absent from the current
// Bicep type definition for projects@2025-04-01-preview. Suppress BCP053.
#disable-next-line BCP053
var rawWorkspaceId = project.properties.internalId
var wsPart1 = substring(rawWorkspaceId, 0, 8)
var wsPart2 = substring(rawWorkspaceId, 8, 4)
var wsPart3 = substring(rawWorkspaceId, 12, 4)
var wsPart4 = substring(rawWorkspaceId, 16, 4)
var wsPart5 = substring(rawWorkspaceId, 20, 12)
var formattedWorkspaceGuid = '${wsPart1}-${wsPart2}-${wsPart3}-${wsPart4}-${wsPart5}'

@description('Project resource name.')
output projectName string = project.name

@description('Project ARM resource ID.')
output projectId string = project.id

@description('Project system-assigned MI principal ID.')
output projectPrincipalId string = project.identity.principalId

@description('Raw project internal/workspace id (no dashes).')
#disable-next-line BCP053
output projectWorkspaceId string = project.properties.internalId

@description('Project workspace id reformatted as a GUID (8-4-4-4-12).')
output projectWorkspaceIdGuid string = formattedWorkspaceGuid

@description('Cosmos DB connection name used on this project.')
output cosmosDBConnection string = '${cosmosDBName}${uniqueConnectionSuffix}'

@description('Storage connection name used on this project.')
output azureStorageConnection string = '${azureStorageName}${uniqueConnectionSuffix}'

@description('AI Search connection name used on this project.')
output aiSearchConnection string = '${aiSearchName}${uniqueConnectionSuffix}'
