# Deploying to a new environment

Quick-deploy of the entire infrastructure and application using azd utility


| Step | Description |
|------|-------------|
| 1. Create Environment |
| 2. Set up Environment variables |
| 3. Run azd |


| Variable | Purpose | Default |
|----------|---------|---------|
| `AZURE_RESOURCE_GROUP` | Target resource group | `rg-artagent-iafg` |
| `AZURE_SUBSCRIPTION_ID` | Target subscription | *(set in script)* |
| `AZURE_VOICELIVE_API_KEY` | VoiceLive API key (set on container app) | — |
| `AGENT_SCENARIO` | Industry scenario (`insurance`, `banking`, etc.) | — |
| `azd env set AZURE_ENV_NAME="dev2"`|
| `azd env set AZURE_LOCATION=canadaeast`|
| `azd env set TF_VAR_location=canadaeast`|
| `azd env set RS_CONTAINER_NAME="tfstate2"`|

| `azd env set RS_RESOURCE_GROUP="rg-tfstate-dev-2333"`|
| `azd env set RS_STORAGE_ACCOUNT="tfstatedev2334"`|
| `azd env set TF_VAR_deployed_by="xxx"`|
| `azd env set TF_VAR_voice_live_location="eastus2"`|
| `azd env set AZURE_RESOURCE_GROUP="rg-artagentce-dev2"`|

| `azd up`