// =============================================================================
// diagnostics.bicep
// Log Analytics Workspace + diagnostic storage account + diagnosticSettings
// fan-out across an arbitrary list of target resources.
//
// Uses categoryGroup: 'allLogs' to dodge per-type log-category enumeration
// (matches the Terraform repo's dynamic-block behaviour, cleaner here).
// 'AllMetrics' is the universal metrics category.
//
// Fan-out trick: diagnosticSettings is an extension resource and Bicep `scope:`
// only accepts resource symbols, not arbitrary IDs. We use per-target nested
// ARM deployments so we can target any resourceId string regardless of type.
//
// Scope: resourceGroup.
// =============================================================================

targetScope = 'resourceGroup'

// -----------------------------------------------------------------------------
// Parameters
// -----------------------------------------------------------------------------

@description('Azure region for LAW + diag storage.')
param location string

@description('Naming prefix shared across the deployment.')
param prefix string

@description('Short random suffix for uniqueness.')
param randomSuffix string

@description('Tags applied to monitoring resources.')
param tags object = {}
var commonTags = union(tags, { SecurityControl: 'Ignore' })

@description('Add private agent trace ingestion, sharing this module Log Analytics workspace.')
param enableAgentTracing bool = false

@description('Foundry account receiving the shared Application Insights connection. Required when tracing is enabled.')
param aiAccountName string = ''

@description('Private endpoint subnet ID. Required when tracing is enabled.')
param peSubnetId string = ''

@description('Five centrally managed AMPLS private DNS zone IDs, including the existing Blob zone.')
param monitorDnsZoneIds array = []

@description('LAW data retention in days.')
@minValue(30)
@maxValue(730)
param retentionDays int = 30

@description('Targets to enable diagnostics on. Each item: { name: string, resourceId: string }. `name` is used in the diagnosticSettings child name so it must be unique and ARM-safe.')
param targets array = []

// -----------------------------------------------------------------------------
// Log Analytics Workspace
// -----------------------------------------------------------------------------

var lawName = '${prefix}-law-${randomSuffix}'

resource law 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: lawName
  location: location
  tags: commonTags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: retentionDays
    publicNetworkAccessForIngestion: enableAgentTracing ? 'Disabled' : 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

// -----------------------------------------------------------------------------
// Diagnostic storage account
// Storage names must be 3–24 lowercase alphanumeric. Strip everything else and
// truncate to 24 chars.
// -----------------------------------------------------------------------------

var prefixSanitised = toLower(replace(replace(replace(replace(prefix, '-', ''), '_', ''), '.', ''), ' ', ''))
var suffixSanitised = toLower(replace(replace(replace(replace(randomSuffix, '-', ''), '_', ''), '.', ''), ' ', ''))
var diagBase = '${prefixSanitised}diag${suffixSanitised}'
var diagStorageName = length(diagBase) > 24 ? substring(diagBase, 0, 24) : diagBase

resource diagStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: diagStorageName
  location: location
  tags: commonTags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
  }
}

// -----------------------------------------------------------------------------
// Diagnostic settings fan-out via per-target nested deployments.
// Each nested deployment carries a tiny ARM template that declares a single
// Microsoft.Insights/diagnosticSettings resource scoped to target.resourceId.
//
// Lint disabled: per-target nested deployments are the cleanest way to attach
// an extension resource (diagnosticSettings) against an arbitrary resourceId
// string when the target type is unknown. A standalone module file would also
// work but this phase intentionally keeps the module surface to three files.
// -----------------------------------------------------------------------------

// NOTE: We intentionally use expressionEvaluationOptions.scope = 'outer' so
// that target.resourceId / target.name (loop-bound values) and law.id /
// diagStorage.id are evaluated in the OUTER template context and baked into
// the inner template as literals. ARM does not evaluate parameter()
// expressions inside a resource's `scope` property at inner-template
// validation time, which causes InvalidResourceNamespace when scope is
// '[parameters(...)]'. Baking the resourceId in as a literal sidesteps that.
#disable-next-line no-deployments-resources
resource diagFanOut 'Microsoft.Resources/deployments@2022-09-01' = [for target in targets: {
  name: 'diag-${target.name}'
  properties: {
    mode: 'Incremental'
    expressionEvaluationOptions: {
      scope: 'outer'
    }
    template: {
      '$schema': 'https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#'
      contentVersion: '1.0.0.0'
      resources: [
        {
          type: 'Microsoft.Insights/diagnosticSettings'
          apiVersion: '2021-05-01-preview'
          scope: target.resourceId
          name: '${target.name}-diag'
          properties: {
            workspaceId: law.id
            storageAccountId: diagStorage.id
            logs: contains(target, 'skipLogs') && target.skipLogs ? [] : [
              {
                categoryGroup: 'allLogs'
                enabled: true
              }
            ]
            metrics: [
              {
                category: 'AllMetrics'
                enabled: true
              }
            ]
          }
        }
      ]
    }
  }
}]

// Agent tracing shares the diagnostics workspace; queries stay Entra-authorized
// over public endpoints so service-side evaluations do not require VNet access.
resource aiAccount 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: aiAccountName
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = if (enableAgentTracing) {
  name: '${prefix}-appi-${randomSuffix}'
  location: location
  tags: commonTags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: law.id
    publicNetworkAccessForIngestion: 'Disabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

resource appInsightsConnection 'Microsoft.CognitiveServices/accounts/connections@2025-04-01-preview' = if (enableAgentTracing) {
  parent: aiAccount
  name: '${aiAccountName}-appinsights'
  properties: {
    category: 'AppInsights'
    target: appInsights!.id
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: {
      key: appInsights!.properties.ConnectionString
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: appInsights!.id
    }
  }
}

resource ampls 'Microsoft.Insights/privateLinkScopes@2021-07-01-preview' = if (enableAgentTracing) {
  name: '${prefix}-ampls-${randomSuffix}'
  location: 'global'
  tags: commonTags
  properties: {
    accessModeSettings: {
      ingestionAccessMode: 'PrivateOnly'
      queryAccessMode: 'Open'
    }
  }
}

resource scopedAppInsights 'Microsoft.Insights/privateLinkScopes/scopedResources@2021-07-01-preview' = if (enableAgentTracing) {
  parent: ampls
  name: 'appinsights'
  properties: {
    linkedResourceId: appInsights!.id
  }
}

resource scopedLaw 'Microsoft.Insights/privateLinkScopes/scopedResources@2021-07-01-preview' = if (enableAgentTracing) {
  parent: ampls
  name: 'law'
  properties: {
    linkedResourceId: law.id
  }
}

resource monitorPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = if (enableAgentTracing) {
  name: '${prefix}-ampls-${randomSuffix}-pe'
  location: location
  tags: commonTags
  properties: {
    subnet: { id: peSubnetId }
    privateLinkServiceConnections: [
      {
        name: 'azuremonitor'
        properties: {
          privateLinkServiceId: ampls!.id
          groupIds: ['azuremonitor']
        }
      }
    ]
  }
  dependsOn: [scopedAppInsights, scopedLaw]
}

resource monitorDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = if (enableAgentTracing) {
  parent: monitorPrivateEndpoint
  name: 'azuremonitor'
  properties: {
    privateDnsZoneConfigs: [for zoneId in monitorDnsZoneIds: {
      name: replace(last(split(zoneId, '/')), '.', '-')
      properties: { privateDnsZoneId: zoneId }
    }]
  }
}

// -----------------------------------------------------------------------------
// Outputs
// -----------------------------------------------------------------------------

output lawId string = law.id
output lawName string = law.name
output lawCustomerId string = law.properties.customerId
output diagStorageId string = diagStorage.id
output diagStorageName string = diagStorage.name
output appInsightsId string = enableAgentTracing ? appInsights!.id : ''
output appInsightsName string = enableAgentTracing ? appInsights!.name : ''
output appInsightsAppId string = enableAgentTracing ? appInsights!.properties.AppId : ''
output amplsId string = enableAgentTracing ? ampls!.id : ''
