// =============================================================================
// nsg.bicep
// Network Security Group for the Foundry-delegated `agents` subnet (BYO-VNET).
//
// Implements the minimum-required OUTBOUND rules prescribed in:
//   https://nirmalt.com/posts/securingmicrosoftfoundrywithbyovnet
//
// Allow-only ruleset — Azure default rules handle everything else (including
// the default outbound deny to Internet after the allows). No explicit denies.
//
// Scope: resourceGroup (called from main.bicep as a module).
// =============================================================================

targetScope = 'resourceGroup'

// -----------------------------------------------------------------------------
// Parameters
// -----------------------------------------------------------------------------

@description('Azure region for the NSG.')
param location string

@description('Naming prefix shared across the deployment (e.g. aifoundrybicep).')
param prefix string

@description('Short random suffix appended to resource names for uniqueness.')
param randomSuffix string

// -----------------------------------------------------------------------------
// Network Security Group
// -----------------------------------------------------------------------------

resource nsg 'Microsoft.Network/networkSecurityGroups@2024-01-01' = {
  name: '${prefix}-nsg-agents-${randomSuffix}'
  location: location
  properties: {
    securityRules: [
      {
        name: 'Allow-Entra-443'
        properties: {
          priority: 100
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'AzureActiveDirectory'
          destinationPortRange: '443'
        }
      }
      {
        name: 'Allow-ACA-Mgmt-443'
        properties: {
          priority: 110
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'AzureContainerAppsManagement'
          destinationPortRange: '443'
        }
      }
      {
        name: 'Allow-ACR-443'
        properties: {
          priority: 120
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'AzureContainerRegistry'
          destinationPortRange: '443'
        }
      }
      {
        name: 'Allow-Foundry-Infra'
        properties: {
          priority: 130
          direction: 'Outbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '100.67.0.0/24'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

// -----------------------------------------------------------------------------
// Outputs
// -----------------------------------------------------------------------------

output nsgId string = nsg.id
output nsgName string = nsg.name
