param location string = 'eastus2'
param poolName string = 'controlled-python-pool'
param environmentId string
param identityId string
param image string

resource sessionPool 'Microsoft.App/sessionPools@2025-10-02-preview' = {
  name: poolName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    environmentId: environmentId
    poolManagementType: 'Dynamic'
    containerType: 'CustomContainer'
    scaleConfiguration: {
      maxConcurrentSessions: 5
      readySessionInstances: 2
    }
    dynamicPoolConfiguration: {
      lifecycleConfiguration: {
        cooldownPeriodInSeconds: 600
        lifecycleType: 'Timed'
      }
    }
    customContainerTemplate: {
      containers: [
        {
          name: 'jupyterpython'
          image: image
          env: [
            {
              name: 'SYS_RUNTIME_SANDBOX'
              value: 'AzureContainerApps-DynamicSessions'
            }
            {
              name: 'AZURE_CODE_EXEC_ENV'
              value: 'AzureContainerApps-DynamicSessions-Py3.12'
            }
            {
              name: 'AZURECONTAINERAPPS_SESSIONS_SANDBOX_VERSION'
              value: '7758'
            }
            {
              name: 'JUPYTER_TOKEN'
              value: 'AzureContainerApps-DynamicSessions'
            }
          ]
          resources: {
            cpu: 1
            memory: '2Gi'
          }
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: 6000
              }
              failureThreshold: 4
            }
            {
              type: 'Startup'
              httpGet: {
                path: '/health'
                port: 6000
              }
              failureThreshold: 30
              periodSeconds: 2
            }
          ]
        }
      ]
      ingress: {
        targetPort: 6000
      }
      registryCredentials: {
        server: split(image, '/')[0]
        identity: identityId
      }
    }
    mcpServerSettings: {
      #disable-next-line BCP089
      isMCPServerEnabled: true
    }
    sessionNetworkConfiguration: {
      status: 'EgressDisabled'
    }
  }
}

output poolId string = sessionPool.id
