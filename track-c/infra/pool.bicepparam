using './pool.bicep'

param location = 'eastus'
param poolName = 'controlled-python-pool'
param environmentId = readEnvironmentVariable('ACA_ENVIRONMENT_ID')
param identityId = readEnvironmentVariable('POOL_IDENTITY_ID')
param image = readEnvironmentVariable('POOL_IMAGE')
