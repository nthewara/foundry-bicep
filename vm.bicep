@description('Azure region for the VM resources.')
param location string

@description('Naming prefix used for all resources (e.g. aifoundrybicep).')
param prefix string

@description('Random suffix appended to resource names for uniqueness.')
param randomSuffix string

@description('Resource ID of the subnet the jump-box NIC will attach to.')
param vmSubnetId string

@description('VM size. Default is Standard_D8s_v5 (8 vCPU / 32 GB RAM) — capable of running VS2026 + AI workloads.')
param vmSize string = 'Standard_D8s_v5'

@description('Local administrator username.')
param adminUsername string = 'azureadmin'

@description('Local administrator password. Wire this from Key Vault in .bicepparam.')
@secure()
param adminPassword string

@description('OS disk SKU. Premium_LRS by default for snappy jump-box experience.')
@allowed([
  'Standard_LRS'
  'StandardSSD_LRS'
  'Premium_LRS'
  'PremiumV2_LRS'
  'UltraSSD_LRS'
])
param osDiskType string = 'Premium_LRS'

resource nic 'Microsoft.Network/networkInterfaces@2024-05-01' = {
  name: '${prefix}-nic-winvm-${randomSuffix}'
  location: location
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig'
        properties: {
          subnet: {
            id: vmSubnetId
          }
          privateIPAllocationMethod: 'Dynamic'
        }
      }
    ]
  }
}

resource vm 'Microsoft.Compute/virtualMachines@2024-07-01' = {
  name: '${prefix}-winvm1'
  location: location
  properties: {
    hardwareProfile: {
      vmSize: vmSize
    }
    osProfile: {
      computerName: 'jumpbox1'
      adminUsername: adminUsername
      adminPassword: adminPassword
      windowsConfiguration: {
        provisionVMAgent: true
        enableAutomaticUpdates: true
        patchSettings: {
          patchMode: 'AutomaticByPlatform'
        }
      }
    }
    storageProfile: {
      imageReference: {
        publisher: 'MicrosoftWindowsServer'
        offer: 'WindowsServer'
        sku: '2022-datacenter-azure-edition-hotpatch'
        version: 'latest'
      }
      osDisk: {
        name: '${prefix}-osdisk-winvm-${randomSuffix}'
        createOption: 'FromImage'
        caching: 'ReadWrite'
        managedDisk: {
          storageAccountType: osDiskType
        }
      }
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: nic.id
        }
      ]
    }
    licenseType: 'Windows_Server'
    diagnosticsProfile: {
      bootDiagnostics: {
        enabled: true
      }
    }
  }
}

@description('Resource ID of the jump-box VM.')
output vmId string = vm.id

@description('Name of the jump-box VM.')
output vmName string = vm.name

@description('Private IP address of the jump-box NIC.')
output vmPrivateIp string = nic.properties.ipConfigurations[0].properties.privateIPAddress
