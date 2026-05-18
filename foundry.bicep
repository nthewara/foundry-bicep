/*
  foundry.bicep
  -------------
  Azure AI Foundry (Cognitive Services / AIServices) account with:
    - Public network access DISABLED
    - System-assigned managed identity
    - networkInjections pinned to the agents subnet (Data Proxy private VNet integration)
    - Model deployment as a sub-resource

  Ported from upstream `modules-network-secured/ai-account-identity.bicep` (the
  account + model portion). The MI on the account is implicit via SystemAssigned
  here; project-level identities live in `foundry-identity.bicep`.

  NOTE: API version 2025-04-01-preview is currently the only version that
  supports `allowProjectManagement` + `networkInjections`. Bicep will warn
  about the preview API — this is expected, see PR body.
*/

@description('Name of the AI Foundry / Cognitive Services account.')
param accountName string

@description('Azure region for the account.')
param location string

@description('Name of the model to deploy (e.g. gpt-4o-mini).')
param modelName string

@description('Provider of the model (e.g. OpenAI).')
param modelFormat string

@description('Model version (e.g. 2024-07-18).')
param modelVersion string

@description('SKU name for the model deployment (e.g. GlobalStandard).')
param modelSkuName string

@description('Tokens-per-minute capacity for the model deployment.')
param modelCapacity int

@description('Full ARM resource ID of the agents subnet (used for networkInjections).')
param agentSubnetId string

@description('Set to "true" to inject the account into the agents subnet via Data Proxy. Use "false" to skip injection.')
param networkInjection string = 'true'

#disable-next-line BCP036
resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' = {
  name: accountName
  location: location
  sku: {
    name: 'S0'
  }
  kind: 'AIServices'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: accountName
    networkAcls: {
      defaultAction: 'Deny'
      virtualNetworkRules: []
      ipRules: []
      bypass: 'AzureServices'
    }
    publicNetworkAccess: 'Disabled'
    networkInjections: ((networkInjection == 'true')
      ? [
          {
            scenario: 'agent'
            subnetArmId: agentSubnetId
            useMicrosoftManagedNetwork: false
          }
        ]
      : null)
    disableLocalAuth: false
  }
}

#disable-next-line BCP081
resource modelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-04-01-preview' = {
  parent: account
  name: modelName
  sku: {
    capacity: modelCapacity
    name: modelSkuName
  }
  properties: {
    model: {
      name: modelName
      format: modelFormat
      version: modelVersion
    }
  }
}

@description('Name of the deployed AI Foundry account.')
output accountName string = account.name

@description('Full ARM resource ID of the AI Foundry account.')
output accountID string = account.id

@description('Endpoint URI for the AI Foundry account.')
output accountTarget string = account.properties.endpoint

@description('System-assigned managed identity principal ID of the account.')
output accountPrincipalId string = account.identity.principalId
