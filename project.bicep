/*
  project.bicep
  -------------
  Creates the initial Foundry project on an AI Services account, using the
  consolidated identity module under the hood with no connection suffix
  (matches upstream `ai-project-identity.bicep`).

  This is intended to be referenced from `main.bicep` once per stack. For
  additional projects on an already-deployed account, see `add-project.bicep`.
*/

@description('Name of the parent AI Services / Foundry account.')
param accountName string

@description('Azure region for the project.')
param location string

@description('Project name.')
param projectName string

@description('Project description.')
param projectDescription string = 'A project for the AI Foundry account with network secured deployed Agent'

@description('Project display name.')
param displayName string = 'network secured agent project'

@description('AI Search service name to wire as a project connection.')
param aiSearchName string
@description('Resource group containing the AI Search service.')
param aiSearchServiceResourceGroupName string = resourceGroup().name
@description('Subscription containing the AI Search service.')
param aiSearchServiceSubscriptionId string = subscription().subscriptionId

@description('Cosmos DB account name to wire as a project connection.')
param cosmosDBName string
@description('Subscription containing the Cosmos DB account.')
param cosmosDBSubscriptionId string = subscription().subscriptionId
@description('Resource group containing the Cosmos DB account.')
param cosmosDBResourceGroupName string = resourceGroup().name

@description('Storage Account name to wire as a project connection.')
param azureStorageName string
@description('Subscription containing the Storage Account.')
param azureStorageSubscriptionId string = subscription().subscriptionId
@description('Resource group containing the Storage Account.')
param azureStorageResourceGroupName string = resourceGroup().name

module projectIdentity 'foundry-identity.bicep' = {
  name: 'project-${projectName}-identity'
  params: {
    accountName: accountName
    location: location
    projectName: projectName
    projectDescription: projectDescription
    displayName: displayName
    aiSearchName: aiSearchName
    aiSearchServiceResourceGroupName: aiSearchServiceResourceGroupName
    aiSearchServiceSubscriptionId: aiSearchServiceSubscriptionId
    cosmosDBName: cosmosDBName
    cosmosDBSubscriptionId: cosmosDBSubscriptionId
    cosmosDBResourceGroupName: cosmosDBResourceGroupName
    azureStorageName: azureStorageName
    azureStorageSubscriptionId: azureStorageSubscriptionId
    azureStorageResourceGroupName: azureStorageResourceGroupName
    uniqueConnectionSuffix: ''
  }
}

output projectName string = projectIdentity.outputs.projectName
output projectId string = projectIdentity.outputs.projectId
output projectPrincipalId string = projectIdentity.outputs.projectPrincipalId
output projectWorkspaceId string = projectIdentity.outputs.projectWorkspaceId
output projectWorkspaceIdGuid string = projectIdentity.outputs.projectWorkspaceIdGuid
output cosmosDBConnection string = projectIdentity.outputs.cosmosDBConnection
output azureStorageConnection string = projectIdentity.outputs.azureStorageConnection
output aiSearchConnection string = projectIdentity.outputs.aiSearchConnection
