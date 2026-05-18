// =============================================================================
// networking.bicep
// Hub + 2-spoke VNets, subnets, peerings, and route table.
// Mirrors the Terraform predecessor (nthewara/foundry/networking.tf) layout and
// matches the upstream Foundry sample subnet model (agents + mcp delegated to
// Microsoft.App/environments).
//
// Scope: resourceGroup (called from main.bicep as a module).
// =============================================================================

targetScope = 'resourceGroup'

// -----------------------------------------------------------------------------
// Parameters
// -----------------------------------------------------------------------------

@description('Azure region for all resources.')
param location string

@description('Naming prefix shared across the deployment (e.g. aifoundrybicep).')
param prefix string

@description('Short random suffix appended to resource names for uniqueness.')
param randomSuffix string

@description('CIDR for the hub VNet. Must be a /23 (subnets are carved as /26).')
param hubVnetPrefix string = '10.100.0.0/23'

@description('CIDR for the VM spoke VNet.')
param vmVnetPrefix string = '10.10.10.0/23'

@description('CIDR for the AI app spoke VNet. Must be a /23 — agents/mcp live in the second /24.')
param aiappVnetPrefix string = '10.10.20.0/23'

@description('Private IP of the Azure Firewall to use as the next hop for the spoke UDR. Pass empty string to skip UDR creation (e.g. before the firewall exists).')
param routeTableNextHopIp string = ''

@description('Whether the firewall is being provisioned. UDRs are only created when this is true AND routeTableNextHopIp is non-empty.')
param fwProvision bool = true

// -----------------------------------------------------------------------------
// Variables (subnet math — mirrors locals.tf in the Terraform repo)
// -----------------------------------------------------------------------------

var hubOctets = split(split(hubVnetPrefix, '/')[0], '.')
var vmOctets = split(split(vmVnetPrefix, '/')[0], '.')
var aiappOctets = split(split(aiappVnetPrefix, '/')[0], '.')

var hubBase = '${hubOctets[0]}.${hubOctets[1]}.${hubOctets[2]}'
var vmBase = '${vmOctets[0]}.${vmOctets[1]}.${vmOctets[2]}'
var aiappBase = '${aiappOctets[0]}.${aiappOctets[1]}.${aiappOctets[2]}'

// agents/mcp live in the +1 third octet (so /23 fits both /24s)
var aiappThirdOctetPlus1 = int(aiappOctets[2]) + 1
var aiappPlus1Base = '${aiappOctets[0]}.${aiappOctets[1]}.${aiappThirdOctetPlus1}'

var createRouteTable = fwProvision && !empty(routeTableNextHopIp)

// -----------------------------------------------------------------------------
// Route Table (must be declared before subnets that reference it)
// -----------------------------------------------------------------------------

resource routeTable 'Microsoft.Network/routeTables@2024-01-01' = if (createRouteTable) {
  name: '${prefix}-rt-spokes-${randomSuffix}'
  location: location
  properties: {
    disableBgpRoutePropagation: false
    routes: [
      {
        name: 'default-to-firewall'
        properties: {
          addressPrefix: '0.0.0.0/0'
          nextHopType: 'VirtualAppliance'
          nextHopIpAddress: routeTableNextHopIp
        }
      }
    ]
  }
}

// -----------------------------------------------------------------------------
// Hub VNet — Azure Firewall, Bastion, Gateway, FW Management
// -----------------------------------------------------------------------------

var hubVnetName = '${prefix}-vnet-hub-${randomSuffix}'

resource hubVnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: hubVnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        hubVnetPrefix
      ]
    }
    subnets: [
      {
        name: 'AzureFirewallSubnet'
        properties: {
          addressPrefix: '${hubBase}.0/26'
        }
      }
      {
        name: 'AzureBastionSubnet'
        properties: {
          addressPrefix: '${hubBase}.64/26'
        }
      }
      {
        name: 'GatewaySubnet'
        properties: {
          addressPrefix: '${hubBase}.128/26'
        }
      }
      {
        name: 'AzureFirewallManagementSubnet'
        properties: {
          addressPrefix: '${hubBase}.192/26'
        }
      }
    ]
  }
}

// -----------------------------------------------------------------------------
// VM Spoke
// -----------------------------------------------------------------------------

var vmVnetName = '${prefix}-vnet-vm-${randomSuffix}'

resource vmVnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: vmVnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        vmVnetPrefix
      ]
    }
    subnets: [
      {
        name: 'VM'
        properties: {
          addressPrefix: '${vmBase}.0/26'
          routeTable: createRouteTable ? {
            id: routeTable.id
          } : null
        }
      }
    ]
  }
}

// -----------------------------------------------------------------------------
// AI App Spoke
// pe       → first /24, .0/26
// agents   → +1 /24, .0/26 (delegated to Microsoft.App/environments)
// mcp      → +1 /24, .64/26 (delegated to Microsoft.App/environments)
// -----------------------------------------------------------------------------

var aiappVnetName = '${prefix}-vnet-aiapp-${randomSuffix}'

resource aiappVnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: aiappVnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        aiappVnetPrefix
      ]
    }
    subnets: [
      {
        name: 'pe'
        properties: {
          addressPrefix: '${aiappBase}.0/26'
          routeTable: createRouteTable ? {
            id: routeTable.id
          } : null
        }
      }
      {
        name: 'agents'
        properties: {
          addressPrefix: '${aiappPlus1Base}.0/26'
          delegations: [
            {
              name: 'Microsoft.App.environments'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: 'mcp'
        properties: {
          addressPrefix: '${aiappPlus1Base}.64/26'
          delegations: [
            {
              name: 'Microsoft.App.environments'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
    ]
  }
}

// -----------------------------------------------------------------------------
// Peerings (bidirectional)
// -----------------------------------------------------------------------------

resource hubToVm 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2024-01-01' = {
  parent: hubVnet
  name: '${prefix}-hub-to-vm'
  properties: {
    remoteVirtualNetwork: {
      id: vmVnet.id
    }
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: true
    allowGatewayTransit: false
    useRemoteGateways: false
  }
}

resource vmToHub 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2024-01-01' = {
  parent: vmVnet
  name: '${prefix}-vm-to-hub'
  properties: {
    remoteVirtualNetwork: {
      id: hubVnet.id
    }
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: true
    allowGatewayTransit: false
    useRemoteGateways: false
  }
}

resource hubToAiapp 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2024-01-01' = {
  parent: hubVnet
  name: '${prefix}-hub-to-aiapp'
  properties: {
    remoteVirtualNetwork: {
      id: aiappVnet.id
    }
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: true
    allowGatewayTransit: false
    useRemoteGateways: false
  }
}

resource aiappToHub 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2024-01-01' = {
  parent: aiappVnet
  name: '${prefix}-aiapp-to-hub'
  properties: {
    remoteVirtualNetwork: {
      id: hubVnet.id
    }
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: true
    allowGatewayTransit: false
    useRemoteGateways: false
  }
}

// -----------------------------------------------------------------------------
// Outputs
// -----------------------------------------------------------------------------

output hubVnetId string = hubVnet.id
output hubVnetName string = hubVnet.name
output vmVnetId string = vmVnet.id
output vmVnetName string = vmVnet.name
output aiappVnetId string = aiappVnet.id
output aiappVnetName string = aiappVnet.name

output firewallSubnetId string = '${hubVnet.id}/subnets/AzureFirewallSubnet'
output firewallSubnetName string = 'AzureFirewallSubnet'
output bastionSubnetId string = '${hubVnet.id}/subnets/AzureBastionSubnet'
output bastionSubnetName string = 'AzureBastionSubnet'
output gatewaySubnetId string = '${hubVnet.id}/subnets/GatewaySubnet'
output gatewaySubnetName string = 'GatewaySubnet'
output firewallMgmtSubnetId string = '${hubVnet.id}/subnets/AzureFirewallManagementSubnet'
output firewallMgmtSubnetName string = 'AzureFirewallManagementSubnet'

output vmSubnetId string = '${vmVnet.id}/subnets/VM'
output vmSubnetName string = 'VM'

output peSubnetId string = '${aiappVnet.id}/subnets/pe'
output peSubnetName string = 'pe'
output agentsSubnetId string = '${aiappVnet.id}/subnets/agents'
output agentsSubnetName string = 'agents'
output mcpSubnetId string = '${aiappVnet.id}/subnets/mcp'
output mcpSubnetName string = 'mcp'

output routeTableId string = createRouteTable ? routeTable.id : ''
