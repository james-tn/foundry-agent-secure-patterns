using '../../vendor/foundry-samples/infrastructure/infrastructure-setup-bicep/19-private-network-agent-tools/main.bicep'

param location = 'eastus2'
param aiServices = 'agentpocsec'
param firstProjectName = 'internalapipoc'
param projectDescription = 'Private-network POC for internal API access'
param displayName = 'Private Internal API POC'

param modelName = 'gpt-4o-mini'
param modelFormat = 'OpenAI'
param modelVersion = '2024-07-18'
param modelSkuName = 'GlobalStandard'
param modelCapacity = 1

param vnetName = 'vnet-agentpoc-sec'
param agentSubnetName = 'agent-subnet'
param peSubnetName = 'pe-subnet'
param mcpSubnetName = 'mcp-subnet'
param vnetAddressPrefix = '172.20.0.0/16'
param agentSubnetPrefix = '172.20.0.0/24'
param peSubnetPrefix = '172.20.1.0/24'
param mcpSubnetPrefix = '172.20.2.0/24'

// Optional: reuse an existing AI Search service (e.g. when the target region
// has no Standard Search capacity). Leave empty to create one.
param existingAiSearchResourceId = readEnvironmentVariable('EXISTING_AI_SEARCH_ID', '')

param enableContainerRegistry = true
param developerIpCidr = ''
