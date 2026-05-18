/*
  add-project.bicep
  -----------------
  Standalone top-level template that, given an already-deployed AI Foundry
  account + its BYO Cosmos / Storage / AI Search resources, adds another
  project (with its own MI, connections, role assignments, and capability
  host) to that account.

  Ported with minimal changes from upstream
  `infrastructure-setup-bicep/19-private-network-agents-tools-setup/add-project.bicep`.

  Differences from upstream:
    - Uses our consolidated modules (`foundry-identity.bicep`,
      `foundry-roles.bicep`, `foundry-capability-host.bicep`) instead of the
      upstream split-up files.
    - Connection names are suffixed with the per-project unique suffix to
      avoid collisions on the shared account.

  Run with:
    az deployment group create -g <rg> \
      --template-file add-project.bicep \
      --parameters add-project.bicepparam
*/

@description('Azure region for the project resources.')
param location string = resourceGroup().location

@description('Name of the existing AI Foundry / AI Services account.')
param existingAccountName string

@description('Resource group containing the AI Services account.')
param accountResourceGroupName string = resourceGroup().name

@description('Subscription containing the AI Services account.')
param accountSubscriptionId string = subscription().subscriptionId

@description('Base name for the new project. A unique suffix is appended.')
param projectName string

@description('Description for the new project.')
param projectDescription string = 'Additional AI Foundry project with network secured deployed Agent'

@description('Display name for the new project.')
param displayName string

@description('Name for the project capability host.')
param projectCapHost string = 'caphostproj'

// Existing shared resources
@description('Name of the existing AI Search service.')
param existingAiSearchName string
@description('Resource group containing the AI Search service.')
param aiSearchResourceGroupName string
@description('Subscription containing the AI Search service.')
param aiSearchSubscriptionId string

@description('Name of the existing Storage Account.')
param existingStorageName string
@description('Resource group containing the Storage Account.')
param storageResourceGroupName string
@description('Subscription containing the Storage Account.')
param storageSubscriptionId string

@description('Name of the existing Cosmos DB account.')
param existingCosmosDBName string
@description('Resource group containing the Cosmos DB account.')
param cosmosDBResourceGroupName string
@description('Subscription containing the Cosmos DB account.')
param cosmosDBSubscriptionId string

// Build a short, unique suffix for this project
@description('Deployment timestamp used to derive a unique suffix. Leave default.')
param deploymentTimestamp string = utcNow('yyyyMMddHHmmss')
var uniqueSuffix = substring(uniqueString('${resourceGroup().id}-${deploymentTimestamp}'), 0, 4)
var finalProjectName = toLower('${projectName}${uniqueSuffix}')

// Create the project + connections (uses unique connection-suffix path)
module aiProject 'foundry-identity.bicep' = {
  name: 'ai-${finalProjectName}-${uniqueSuffix}-deployment'
  scope: resourceGroup(accountSubscriptionId, accountResourceGroupName)
  params: {
    accountName: existingAccountName
    location: location
    projectName: finalProjectName
    projectDescription: projectDescription
    displayName: displayName
    aiSearchName: existingAiSearchName
    aiSearchServiceResourceGroupName: aiSearchResourceGroupName
    aiSearchServiceSubscriptionId: aiSearchSubscriptionId
    cosmosDBName: existingCosmosDBName
    cosmosDBSubscriptionId: cosmosDBSubscriptionId
    cosmosDBResourceGroupName: cosmosDBResourceGroupName
    azureStorageName: existingStorageName
    azureStorageSubscriptionId: storageSubscriptionId
    azureStorageResourceGroupName: storageResourceGroupName
    uniqueConnectionSuffix: '-${finalProjectName}'
  }
}

// Pre-cap-host role assignments (account-scoped — Search Index/Service
// Contributor, Storage Blob Data Contributor, Cosmos DB Operator).
module preRoles 'foundry-roles.bicep' = {
  name: 'pre-roles-${uniqueSuffix}-deployment'
  scope: resourceGroup(storageSubscriptionId, storageResourceGroupName)
  params: {
    phase: 'pre'
    projectPrincipalId: aiProject.outputs.projectPrincipalId
    aiSearchName: existingAiSearchName
    cosmosDBName: existingCosmosDBName
    storageName: existingStorageName
    uniqueSuffix: uniqueSuffix
  }
}

// Capability host on the new project (must run after pre roles)
module addProjectCapabilityHost 'foundry-capability-host.bicep' = {
  name: 'capabilityHost-configuration-${uniqueSuffix}-deployment'
  scope: resourceGroup(accountSubscriptionId, accountResourceGroupName)
  params: {
    accountName: existingAccountName
    projectName: aiProject.outputs.projectName
    cosmosDBConnection: aiProject.outputs.cosmosDBConnection
    azureStorageConnection: aiProject.outputs.azureStorageConnection
    aiSearchConnection: aiProject.outputs.aiSearchConnection
    projectCapHost: projectCapHost
  }
  dependsOn: [
    preRoles
  ]
}

// Post-cap-host role assignments (Storage Blob Data Owner w/ ABAC,
// Cosmos data-plane built-in Data Contributor at account scope).
module postRoles 'foundry-roles.bicep' = {
  name: 'post-roles-${uniqueSuffix}-deployment'
  scope: resourceGroup(storageSubscriptionId, storageResourceGroupName)
  params: {
    phase: 'post'
    projectPrincipalId: aiProject.outputs.projectPrincipalId
    aiSearchName: existingAiSearchName
    cosmosDBName: existingCosmosDBName
    storageName: existingStorageName
    projectWorkspaceIdGuid: aiProject.outputs.projectWorkspaceIdGuid
    uniqueSuffix: uniqueSuffix
  }
  dependsOn: [
    addProjectCapabilityHost
  ]
}

@description('Final unique project name created.')
output projectName string = aiProject.outputs.projectName

@description('Project MI principal ID.')
output projectPrincipalId string = aiProject.outputs.projectPrincipalId

@description('Raw project workspace ID.')
output projectWorkspaceId string = aiProject.outputs.projectWorkspaceId

@description('Capability host name on the new project.')
output capabilityHostName string = addProjectCapabilityHost.outputs.projectCapHost
