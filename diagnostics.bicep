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
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: retentionDays
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

#disable-next-line no-deployments-resources
resource diagFanOut 'Microsoft.Resources/deployments@2022-09-01' = [for target in targets: {
  name: 'diag-${target.name}'
  properties: {
    mode: 'Incremental'
    expressionEvaluationOptions: {
      scope: 'inner'
    }
    parameters: {
      settingName: {
        value: '${target.name}-diag'
      }
      targetResourceId: {
        value: target.resourceId
      }
      workspaceId: {
        value: law.id
      }
      storageAccountId: {
        value: diagStorage.id
      }
    }
    template: {
      '$schema': 'https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#'
      contentVersion: '1.0.0.0'
      parameters: {
        settingName: {
          type: 'string'
        }
        targetResourceId: {
          type: 'string'
        }
        workspaceId: {
          type: 'string'
        }
        storageAccountId: {
          type: 'string'
        }
      }
      resources: [
        {
          type: 'Microsoft.Insights/diagnosticSettings'
          apiVersion: '2021-05-01-preview'
          scope: '[parameters(\'targetResourceId\')]'
          name: '[parameters(\'settingName\')]'
          properties: {
            workspaceId: '[parameters(\'workspaceId\')]'
            storageAccountId: '[parameters(\'storageAccountId\')]'
            logs: [
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

// -----------------------------------------------------------------------------
// Outputs
// -----------------------------------------------------------------------------

output lawId string = law.id
output lawCustomerId string = law.properties.customerId
output diagStorageId string = diagStorage.id
output diagStorageName string = diagStorage.name
