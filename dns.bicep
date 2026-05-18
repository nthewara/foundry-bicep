// =============================================================================
// dns.bicep
// Private DNS zones for Foundry private endpoints + per-VNet links.
// Scope: resourceGroup.
// =============================================================================

targetScope = 'resourceGroup'

@description('Naming prefix shared across the deployment.')
param prefix string

@description('Short random suffix for uniqueness in link names.')
param randomSuffix string

@description('List of private DNS zone FQDNs to create. Defaults to the six Foundry-private-endpoint zones.')
param privateDnsZones array = [
  'privatelink.cognitiveservices.azure.com'
  'privatelink.openai.azure.com'
  'privatelink.services.ai.azure.com'
  #disable-next-line no-hardcoded-env-urls
  'privatelink.blob.core.windows.net'
  'privatelink.search.windows.net'
  'privatelink.documents.azure.com'
]

@description('Resource ID of the hub VNet to link each zone to.')
param hubVnetId string

@description('Resource ID of the AI app spoke VNet to link each zone to.')
param aiappVnetId string

@description('Resource ID of the VM spoke VNet to link each zone to.')
param vmVnetId string

// -----------------------------------------------------------------------------
// Private DNS zones (global resources — location must be 'global')
// -----------------------------------------------------------------------------

resource zones 'Microsoft.Network/privateDnsZones@2024-06-01' = [for zoneName in privateDnsZones: {
  name: zoneName
  location: 'global'
}]

// -----------------------------------------------------------------------------
// Per-zone VNet links (hub, aiapp, vm). Registration disabled — these are
// resolution-only zones for Private Endpoint A records.
// -----------------------------------------------------------------------------

resource hubLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (zoneName, i) in privateDnsZones: {
  parent: zones[i]
  name: '${prefix}-hub-link-${randomSuffix}'
  location: 'global'
  properties: {
    virtualNetwork: {
      id: hubVnetId
    }
    registrationEnabled: false
  }
}]

resource aiappLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (zoneName, i) in privateDnsZones: {
  parent: zones[i]
  name: '${prefix}-aiapp-link-${randomSuffix}'
  location: 'global'
  properties: {
    virtualNetwork: {
      id: aiappVnetId
    }
    registrationEnabled: false
  }
}]

resource vmLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (zoneName, i) in privateDnsZones: {
  parent: zones[i]
  name: '${prefix}-vm-link-${randomSuffix}'
  location: 'global'
  properties: {
    virtualNetwork: {
      id: vmVnetId
    }
    registrationEnabled: false
  }
}]

// -----------------------------------------------------------------------------
// Outputs
// -----------------------------------------------------------------------------

@description('Map of zone name → resource ID for downstream private-endpoint modules.')
output zoneIds object = toObject(privateDnsZones, zoneName => zoneName, zoneName => resourceId('Microsoft.Network/privateDnsZones', zoneName))
