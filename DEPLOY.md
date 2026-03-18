# Deploying with deploy-app.sh

Quick-deploy the app to Azure Container Apps without re-provisioning infrastructure.

## Prerequisites

- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) (`az`)
- [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd) (`azd`)
- An active `azd` environment (run `azd env select <name>` if not set)
- Infrastructure already provisioned via `azd up` or `azd provision`

## Usage

```bash
# Deploy everything (backend + frontend)
./devops/scripts/azd/deploy-app.sh

# Backend only
./devops/scripts/azd/deploy-app.sh --server

# Frontend only
./devops/scripts/azd/deploy-app.sh --client

# Explicit all
./devops/scripts/azd/deploy-app.sh --all
```

## What It Does

| Step | Description |
|------|-------------|
| 1. **Authenticate** | Ensures both `azd` and `az` CLI sessions are active |
| 2. **Discover** | Finds container app names and ACR endpoint from the resource group |
| 3. **Deploy** | Runs `azd deploy` for the selected service(s) |
| 4. **Set env vars** | Injects runtime env vars not managed by App Configuration |

## Environment Variables

The script reads these from your shell or `azd env`:

| Variable | Purpose | Default |
|----------|---------|---------|
| `AZURE_RESOURCE_GROUP` | Target resource group | `rg-artagent-iafg` |
| `AZURE_SUBSCRIPTION_ID` | Target subscription | *(set in script)* |
| `AZURE_VOICELIVE_API_KEY` | VoiceLive API key (set on container app) | — |
| `AGENT_SCENARIO` | Industry scenario (`insurance`, `banking`, etc.) | — |

To persist values across deploys:

```bash
azd env set AZURE_VOICELIVE_API_KEY <your-key>
azd env set AGENT_SCENARIO insurance
```

## After Deploying

```bash
# Stream live logs
azd monitor --live

# Check container app status
az containerapp show -n <app-name> -g <resource-group>

# View console logs
az containerapp logs show -n <app-name> -g <resource-group> --type console
```

## First-Time Setup

If infrastructure isn't provisioned yet, use `azd up` instead — see [docs/getting-started/quickstart.md](docs/getting-started/quickstart.md).
