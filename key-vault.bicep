// Adapted from microsoft-foundry/foundry-samples sample 19. See LICENSE.upstream.
// This module runs in the vault's RG/subscription; private endpoints remain in the stack RG.
targetScope = 'resourceGroup'

@description('Vault name in this deployment scope.')
@minLength(3)
@maxLength(24)
param keyVaultName string

@description('Create a vault. False references an existing RBAC vault without changing its configuration.')
param createVault bool = true

@description('Region for a new vault.')
param location string

@description('Foundry ACCOUNT system-assigned principal, not a project principal.')
param accountPrincipalId string

@description('Tags applied to a new vault.')
param tags object = {}
var commonTags = union(tags, { SecurityControl: 'Ignore' })

resource vault 'Microsoft.KeyVault/vaults@2024-11-01' = if (createVault) {
  name: keyVaultName
  location: location
  tags: commonTags
  properties: {
    tenantId: tenant().tenantId
    sku: { family: 'A', name: 'standard' }
    accessPolicies: []
    enableRbacAuthorization: true
    enablePurgeProtection: false
    softDeleteRetentionInDays: 90
    // Foundry's trusted-service certificate retrieval needs this exception.
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Deny'
      ipRules: []
      virtualNetworkRules: []
    }
  }
}

resource vaultReference 'Microsoft.KeyVault/vaults@2024-11-01' existing = {
  name: keyVaultName
}

var secretsUserRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')

resource secretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vaultReference
  name: guid(vaultReference.id, accountPrincipalId, secretsUserRoleId)
  properties: {
    principalId: accountPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: secretsUserRoleId
  }
  dependsOn: [vault]
}

output keyVaultName string = vaultReference.name
output keyVaultResourceId string = vaultReference.id
