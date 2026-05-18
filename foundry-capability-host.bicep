/*
  foundry-capability-host.bicep
  -----------------------------
  Creates the project-level capability host (Agents kind) on an AI Foundry
  project, wiring it to BYO Cosmos / Storage / AI Search connections that
  were created by `foundry-identity.bicep`.

  Ported directly from upstream
  `modules-network-secured/add-project-capability-host.bicep`.

  Note: upstream also ships a post-deploy `scripts/createCapHost.sh` helper
  used when ARM can't yet enable the cap host in one shot (cap-host enable
  is sometimes done out-of-band). This Bicep declares the cap-host resource
  itself; the script lives in `scripts/` and is owned by a different agent.
*/

@description('Cosmos DB connection name on the project (output of foundry-identity.bicep).')
param cosmosDBConnection string

@description('Storage connection name on the project (output of foundry-identity.bicep).')
param azureStorageConnection string

@description('AI Search connection name on the project (output of foundry-identity.bicep).')
param aiSearchConnection string

@description('Name of the project.')
param projectName string

@description('Name of the parent AI Foundry / Cognitive Services account.')
param accountName string

@description('Name of the capability host to create (e.g. caphostproj).')
param projectCapHost string

var threadConnections = ['${cosmosDBConnection}']
var storageConnections = ['${azureStorageConnection}']
var vectorStoreConnections = ['${aiSearchConnection}']

resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: accountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' existing = {
  name: projectName
  parent: account
}

resource projectCapabilityHost 'Microsoft.CognitiveServices/accounts/projects/capabilityHosts@2025-04-01-preview' = {
  name: projectCapHost
  parent: project
  properties: {
    // Bicep type definition for this preview API doesn't yet expose
    // `capabilityHostKind` even though it's required at runtime. Suppress.
    #disable-next-line BCP037
    capabilityHostKind: 'Agents'
    vectorStoreConnections: vectorStoreConnections
    storageConnections: storageConnections
    threadStorageConnections: threadConnections
  }
}

@description('Name of the deployed capability host.')
output projectCapHost string = projectCapabilityHost.name
