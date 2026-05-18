@description('Azure region for the Bastion resources.')
param location string

@description('Naming prefix used for all resources (e.g. aifoundrybicep).')
param prefix string

@description('Random suffix appended to resource names for uniqueness.')
param randomSuffix string

@description('Resource ID of the AzureBastionSubnet (must be exactly named AzureBastionSubnet).')
param bastionSubnetId string

@description('Bastion SKU. Basic supports the default port range and standard features used by this lab.')
@allowed([
  'Basic'
  'Standard'
  'Premium'
  'Developer'
])
param bastionSku string = 'Basic'

resource bastionPip 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: '${prefix}-pip-bastion-${randomSuffix}'
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

resource bastion 'Microsoft.Network/bastionHosts@2024-05-01' = {
  name: '${prefix}-bastion-${randomSuffix}'
  location: location
  sku: {
    name: bastionSku
  }
  properties: {
    ipConfigurations: [
      {
        name: 'bastionIpConfig'
        properties: {
          subnet: {
            id: bastionSubnetId
          }
          publicIPAddress: {
            id: bastionPip.id
          }
        }
      }
    ]
  }
}

@description('Resource ID of the Bastion host.')
output bastionId string = bastion.id

@description('Name of the Bastion host.')
output bastionName string = bastion.name

@description('Resource ID of the Bastion public IP.')
output bastionPipId string = bastionPip.id
