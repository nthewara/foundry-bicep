@description('Azure region for all resources.')
param location string

@description('Naming prefix used for all resources (e.g. aifoundrybicep).')
param prefix string

@description('Random suffix appended to resource names for uniqueness.')
param randomSuffix string

@description('Azure Firewall SKU tier. Basic is the locked default for this lab (cheaper, but REQUIRES a management subnet + management PIP).')
@allowed([
  'Basic'
  'Standard'
  'Premium'
])
param fwSku string = 'Basic'

@description('Resource ID of the AzureFirewallSubnet (data-plane).')
param fwSubnetId string

@description('Resource ID of the AzureFirewallManagementSubnet. REQUIRED when fwSku == Basic.')
param fwMgmtSubnetId string

@description('When true (default), egress is locked to the Foundry-required FQDN allowlist (application rules) plus service-tag / infra network rules (least-privilege). When false, egress falls back to the permissive `* -> *` application rule for troubleshooting.')
param restrictEgress bool = true

// ---------------------------------------------------------------------------
// Egress FQDN allowlist
// ---------------------------------------------------------------------------

// Curated Foundry-required FQDN allowlist. When restrictEgress is false we fall
// back to a single wildcard so the firewall behaves like the old permissive
// rule (useful for troubleshooting).
var appRuleFqdns = restrictEgress ? [
  '*.identity.azure.net'
  #disable-next-line no-hardcoded-env-urls
  'login.microsoftonline.com'
  '*.login.microsoft.com'
  'mcr.microsoft.com'
  '*.data.mcr.microsoft.com'
  '*.cdn.mscr.io'
  '*.azurecr.io'
  #disable-next-line no-hardcoded-env-urls
  '*.blob.core.windows.net'
  '*.azure-automation.net'
  #disable-next-line no-hardcoded-env-urls
  'management.azure.com'
  '*.azurecontainerapps.io'
  '*.cognitiveservices.azure.com'
  '*.openai.azure.com'
  '*.services.ai.azure.com'
  '*.search.windows.net'
  '*.documents.azure.com'
  'packages.microsoft.com'
  '*.ubuntu.com'
  'archive.ubuntu.com'
  'security.ubuntu.com'
] : [
  '*'
]

// ---------------------------------------------------------------------------
// Public IPs
// ---------------------------------------------------------------------------

resource fwDataPip 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: '${prefix}-pip-azfw-${randomSuffix}'
  location: location
  sku: {
    name: 'Standard'
    tier: 'Regional'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

// Basic SKU MUST have a management subnet + management PIP. We always create
// the mgmt PIP here so the firewall resource can wire it in conditionally; if
// you ever move to Standard/Premium you can stop creating it or just leave it
// unused (cheap). We keep it always-on to match the Basic-SKU default.
resource fwMgmtPip 'Microsoft.Network/publicIPAddresses@2024-05-01' = if (fwSku == 'Basic') {
  name: '${prefix}-pip-azfw-mgmt-${randomSuffix}'
  location: location
  sku: {
    name: 'Standard'
    tier: 'Regional'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

// ---------------------------------------------------------------------------
// Firewall Policy + Rule Collection Group
// ---------------------------------------------------------------------------

resource fwPolicy 'Microsoft.Network/firewallPolicies@2024-05-01' = {
  name: '${prefix}-afwp-${randomSuffix}'
  location: location
  properties: {
    sku: {
      tier: fwSku
    }
  }
}

resource fwRuleCollectionGroup 'Microsoft.Network/firewallPolicies/ruleCollectionGroups@2024-05-01' = {
  parent: fwPolicy
  name: 'DefaultRuleCollectionGroup'
  properties: {
    priority: 200
    ruleCollections: [
      {
        ruleCollectionType: 'FirewallPolicyFilterRuleCollection'
        name: 'AllowWebTraffic'
        priority: 100
        action: {
          type: 'Allow'
        }
        rules: [
          {
            ruleType: 'ApplicationRule'
            name: 'AllowHttpHttps'
            sourceAddresses: [
              '*'
            ]
            targetFqdns: appRuleFqdns
            protocols: [
              {
                protocolType: 'Http'
                port: 80
              }
              {
                protocolType: 'Https'
                port: 443
              }
            ]
          }
        ]
      }
      {
        ruleCollectionType: 'FirewallPolicyFilterRuleCollection'
        name: 'AllowFoundryInfra'
        priority: 300
        action: {
          type: 'Allow'
        }
        rules: [
          {
            ruleType: 'NetworkRule'
            name: 'Allow-Entra-443'
            ipProtocols: [ 'TCP' ]
            sourceAddresses: [ '*' ]
            destinationAddresses: [ 'AzureActiveDirectory' ]
            destinationPorts: [ '443' ]
          }
          {
            ruleType: 'NetworkRule'
            name: 'Allow-Foundry-Infra-100-67'
            ipProtocols: [ 'Any' ]
            sourceAddresses: [ '*' ]
            destinationAddresses: [ '100.67.0.0/24' ]
            destinationPorts: [ '*' ]
          }
        ]
      }
      {
        ruleCollectionType: 'FirewallPolicyFilterRuleCollection'
        name: 'AllowRFC1918'
        priority: 200
        action: {
          type: 'Allow'
        }
        rules: [
          {
            ruleType: 'NetworkRule'
            name: 'AllowRFC1918-10to10'
            ipProtocols: [ 'Any' ]
            sourceAddresses: [ '10.0.0.0/8' ]
            destinationAddresses: [ '10.0.0.0/8' ]
            destinationPorts: [ '*' ]
          }
          {
            ruleType: 'NetworkRule'
            name: 'AllowRFC1918-10to172'
            ipProtocols: [ 'Any' ]
            sourceAddresses: [ '10.0.0.0/8' ]
            destinationAddresses: [ '172.16.0.0/12' ]
            destinationPorts: [ '*' ]
          }
          {
            ruleType: 'NetworkRule'
            name: 'AllowRFC1918-10to192'
            ipProtocols: [ 'Any' ]
            sourceAddresses: [ '10.0.0.0/8' ]
            destinationAddresses: [ '192.168.0.0/16' ]
            destinationPorts: [ '*' ]
          }
          {
            ruleType: 'NetworkRule'
            name: 'AllowRFC1918-172to10'
            ipProtocols: [ 'Any' ]
            sourceAddresses: [ '172.16.0.0/12' ]
            destinationAddresses: [ '10.0.0.0/8' ]
            destinationPorts: [ '*' ]
          }
          {
            ruleType: 'NetworkRule'
            name: 'AllowRFC1918-172to172'
            ipProtocols: [ 'Any' ]
            sourceAddresses: [ '172.16.0.0/12' ]
            destinationAddresses: [ '172.16.0.0/12' ]
            destinationPorts: [ '*' ]
          }
          {
            ruleType: 'NetworkRule'
            name: 'AllowRFC1918-172to192'
            ipProtocols: [ 'Any' ]
            sourceAddresses: [ '172.16.0.0/12' ]
            destinationAddresses: [ '192.168.0.0/16' ]
            destinationPorts: [ '*' ]
          }
          {
            ruleType: 'NetworkRule'
            name: 'AllowRFC1918-192to10'
            ipProtocols: [ 'Any' ]
            sourceAddresses: [ '192.168.0.0/16' ]
            destinationAddresses: [ '10.0.0.0/8' ]
            destinationPorts: [ '*' ]
          }
          {
            ruleType: 'NetworkRule'
            name: 'AllowRFC1918-192to172'
            ipProtocols: [ 'Any' ]
            sourceAddresses: [ '192.168.0.0/16' ]
            destinationAddresses: [ '172.16.0.0/12' ]
            destinationPorts: [ '*' ]
          }
          {
            ruleType: 'NetworkRule'
            name: 'AllowRFC1918-192to192'
            ipProtocols: [ 'Any' ]
            sourceAddresses: [ '192.168.0.0/16' ]
            destinationAddresses: [ '192.168.0.0/16' ]
            destinationPorts: [ '*' ]
          }
        ]
      }
      // Placeholder: add a DNAT rule collection here later if we need
      // inbound RDP to the jump-box via the firewall public IP.
      // {
      //   ruleCollectionType: 'FirewallPolicyNatRuleCollection'
      //   name: 'DnatRules'
      //   priority: 300
      //   action: { type: 'Dnat' }
      //   rules: []
      // }
    ]
  }
}

// ---------------------------------------------------------------------------
// Azure Firewall
// ---------------------------------------------------------------------------

resource firewall 'Microsoft.Network/azureFirewalls@2024-05-01' = {
  name: '${prefix}-azfw-${randomSuffix}'
  location: location
  properties: {
    sku: {
      name: 'AZFW_VNet'
      tier: fwSku
    }
    firewallPolicy: {
      id: fwPolicy.id
    }
    ipConfigurations: [
      {
        name: '${prefix}-azfw-ipconfig'
        properties: {
          subnet: {
            id: fwSubnetId
          }
          publicIPAddress: {
            id: fwDataPip.id
          }
        }
      }
    ]
    managementIpConfiguration: fwSku == 'Basic' ? {
      name: '${prefix}-azfw-mgmt-ipconfig'
      properties: {
        subnet: {
          id: fwMgmtSubnetId
        }
        publicIPAddress: {
          id: fwMgmtPip.id
        }
      }
    } : null
  }
  dependsOn: [
    fwRuleCollectionGroup
  ]
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Resource ID of the Azure Firewall.')
output firewallId string = firewall.id

@description('Name of the Azure Firewall.')
output firewallName string = firewall.name

@description('Private IP address of the firewall data-plane ipConfiguration (next-hop for UDRs).')
output firewallPrivateIp string = firewall.properties.ipConfigurations[0].properties.privateIPAddress

@description('Resource ID of the firewall policy.')
output firewallPolicyId string = fwPolicy.id

@description('Resource ID of the data-plane public IP.')
output dataPipId string = fwDataPip.id

@description('Resource ID of the management public IP (empty when fwSku != Basic).')
output mgmtPipId string = fwSku == 'Basic' ? fwMgmtPip.id : ''
