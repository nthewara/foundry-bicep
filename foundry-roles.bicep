/*
  foundry-roles.bicep
  -------------------
  Consolidates all role assignments needed by an AI Foundry project's
  managed identity to access its BYO data plane: AI Search, Storage,
  Cosmos DB.

  Ported (with phase gating added) from upstream:
    - modules-network-secured/ai-search-role-assignments.bicep
    - modules-network-secured/azure-storage-account-role-assignment.bicep
    - modules-network-secured/blob-storage-container-role-assignments.bicep
    - modules-network-secured/blob-storage-container-role-assignments-unique.bicep
    - modules-network-secured/cosmos-container-role-assignments.bicep
    - modules-network-secured/cosmosdb-account-role-assignment.bicep

  Phase model (mirrors upstream dependency ordering):
    'pre'  - role assignments needed BEFORE the cap host is created
              (Storage Blob Data Contributor on the SA, Cosmos DB Operator
              on the account, AI Search Index Data Contributor +
              AI Search Service Contributor on the search service).
    'post' - role assignments needed AFTER the cap host is created
              (Storage Blob Data Owner with a workspace-scoped ABAC
              condition, and Cosmos data-plane SQL role 00000000-...-0002
              ["Cosmos DB Built-in Data Contributor"] at the account scope).
    'all'  - apply both sets (use only when ordering doesn't matter).

  IMPORTANT: the Cosmos data-plane role MUST be the built-in
  `00000000-0000-0000-0000-000000000002` (Cosmos DB Built-in Data Contributor).
  Per project MEMORY.md, anything else breaks Foundry agent thread storage.

  Assumes Storage / Cosmos / AI Search live in the same RG as the deployment
  scope of this module. Callers in multi-RG setups invoke this module once
  per target RG.
*/

@description('Which phase of role assignments to apply: pre, post, or all.')
@allowed([
  'pre'
  'post'
  'all'
])
param phase string

@description('Object IDs to apply roles to. Typically the project MI principalId.')
param projectPrincipalId string

@description('Name of the AI Search service in this RG.')
param aiSearchName string

@description('Name of the Cosmos DB account in this RG.')
param cosmosDBName string

@description('Name of the Storage Account in this RG.')
param storageName string

@description('Formatted project workspace GUID (from foundry-identity.bicep output projectWorkspaceIdGuid). Required when phase=post or all.')
param projectWorkspaceIdGuid string = ''

@description('Optional uniqueness salt added to role-assignment guids. Set to a per-project string for multi-project deployments (matches upstream *-unique modules).')
param uniqueSuffix string = ''

var isPre = phase == 'pre' || phase == 'all'
var isPost = phase == 'post' || phase == 'all'

// ---- Existing resources ----
resource searchService 'Microsoft.Search/searchServices@2024-06-01-preview' existing = {
  name: aiSearchName
}

resource cosmosDBAccount 'Microsoft.DocumentDB/databaseAccounts@2024-12-01-preview' existing = {
  name: cosmosDBName
}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageName
}

// ============================================================
// PRE-CAP-HOST ROLE ASSIGNMENTS
// ============================================================

// ---- AI Search: Index Data Contributor + Service Contributor ----
resource searchIndexDataContributorRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  name: '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
  scope: subscription()
}

resource searchIndexDataContributorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (isPre) {
  scope: searchService
  name: guid(projectPrincipalId, searchIndexDataContributorRole.id, searchService.id, uniqueSuffix)
  properties: {
    principalId: projectPrincipalId
    roleDefinitionId: searchIndexDataContributorRole.id
    principalType: 'ServicePrincipal'
  }
}

resource searchServiceContributorRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  name: '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
  scope: subscription()
}

resource searchServiceContributorRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (isPre) {
  scope: searchService
  name: guid(projectPrincipalId, searchServiceContributorRole.id, searchService.id, uniqueSuffix)
  properties: {
    principalId: projectPrincipalId
    roleDefinitionId: searchServiceContributorRole.id
    principalType: 'ServicePrincipal'
  }
}

// ---- Storage account: Blob Data Contributor (account scope) ----
resource storageBlobDataContributor 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  name: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  scope: subscription()
}

resource storageBlobDataContributorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (isPre) {
  scope: storageAccount
  name: guid(projectPrincipalId, storageBlobDataContributor.id, storageAccount.id, uniqueSuffix)
  properties: {
    principalId: projectPrincipalId
    roleDefinitionId: storageBlobDataContributor.id
    principalType: 'ServicePrincipal'
  }
}

// ---- Cosmos DB account: Cosmos DB Operator (control plane) ----
resource cosmosDBOperatorRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  name: '230815da-be43-4aae-9cb4-875f7bd000aa'
  scope: subscription()
}

resource cosmosDBOperatorRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (isPre) {
  scope: cosmosDBAccount
  name: guid(projectPrincipalId, cosmosDBOperatorRole.id, cosmosDBAccount.id, uniqueSuffix)
  properties: {
    principalId: projectPrincipalId
    roleDefinitionId: cosmosDBOperatorRole.id
    principalType: 'ServicePrincipal'
  }
}

// ============================================================
// POST-CAP-HOST ROLE ASSIGNMENTS
// ============================================================

// ---- Storage Blob Data Owner with ABAC condition scoped to workspace containers ----
resource storageBlobDataOwner 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  name: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  scope: subscription()
}

var conditionStr = '((!(ActionMatches{\'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/tags/read\'})  AND  !(ActionMatches{\'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/filter/action\'}) AND  !(ActionMatches{\'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/tags/write\'}) ) OR (@Resource[Microsoft.Storage/storageAccounts/blobServices/containers:name] StringStartsWithIgnoreCase \'${projectWorkspaceIdGuid}\' AND @Resource[Microsoft.Storage/storageAccounts/blobServices/containers:name] StringLikeIgnoreCase \'*-azureml-agent\'))'

resource storageBlobDataOwnerAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (isPost) {
  scope: storageAccount
  name: guid(storageBlobDataOwner.id, storageAccount.id, projectPrincipalId, uniqueSuffix)
  properties: {
    principalId: projectPrincipalId
    roleDefinitionId: storageBlobDataOwner.id
    principalType: 'ServicePrincipal'
    conditionVersion: '2.0'
    condition: conditionStr
  }
}

// ---- Cosmos DB Built-in Data Contributor (SQL data plane) at account scope ----
// MUST be 00000000-0000-0000-0000-000000000002 — see MEMORY.md.
var cosmosDataPlaneRoleDefinitionId = resourceId(
  'Microsoft.DocumentDB/databaseAccounts/sqlRoleDefinitions',
  cosmosDBName,
  '00000000-0000-0000-0000-000000000002'
)
var cosmosAccountScope = '/subscriptions/${subscription().subscriptionId}/resourceGroups/${resourceGroup().name}/providers/Microsoft.DocumentDB/databaseAccounts/${cosmosDBName}'

resource cosmosDataPlaneAssignment 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2022-05-15' = if (isPost) {
  parent: cosmosDBAccount
  name: guid(projectWorkspaceIdGuid, cosmosDBName, cosmosDataPlaneRoleDefinitionId, projectPrincipalId, uniqueSuffix)
  properties: {
    principalId: projectPrincipalId
    roleDefinitionId: cosmosDataPlaneRoleDefinitionId
    scope: cosmosAccountScope
  }
}
