#!/bin/bash
# ============================================================================
# 🚀 App Deploy Script (azd deploy)
# ============================================================================
# Deploys the application to Azure Container Apps using azd deploy.
# Discovers container app names from the resource group, then deploys
# server, client, or both based on flags.
#
# Usage:
#   ./devops/scripts/azd/deploy-app.sh              # Deploy all services
#   ./devops/scripts/azd/deploy-app.sh --server      # Deploy backend only
#   ./devops/scripts/azd/deploy-app.sh --client      # Deploy frontend only
#   ./devops/scripts/azd/deploy-app.sh --all         # Deploy all services (explicit)
#
# Environment variables (optional overrides):
#   AZURE_RESOURCE_GROUP   — Target resource group (default: rg-artagent-iafg)
#   AZURE_SUBSCRIPTION_ID  — Target subscription (default: b2fa8dfc-5194-4f95-b774-1f0c919bc3e7)
# ============================================================================

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$SCRIPT_DIR/../../.."
readonly DEFAULT_RESOURCE_GROUP="rg-artagent-iafg"
readonly DEFAULT_SUBSCRIPTION_ID="b2fa8dfc-5194-4f95-b774-1f0c919bc3e7"

# ============================================================================
# Logging (matches project conventions from preprovision.sh)
# ============================================================================

BLUE=$'\033[0;34m'
GREEN=$'\033[0;32m'
GREEN_BOLD=$'\033[1;32m'
YELLOW=$'\033[1;33m'
RED=$'\033[0;31m'
CYAN=$'\033[0;36m'
DIM=$'\033[2m'
NC=$'\033[0m'

log()           { printf '│ %s%s%s\n' "$DIM" "$*" "$NC"; }
info()          { printf '│ %s%s%s\n' "$BLUE" "$*" "$NC"; }
success()       { printf '│ %s✔%s %s\n' "$GREEN" "$NC" "$*"; }
phase_success() { printf '│ %s✔ %s%s\n' "$GREEN_BOLD" "$*" "$NC"; }
warn()          { printf '│ %s⚠%s  %s\n' "$YELLOW" "$NC" "$*"; }
fail()          { printf '│ %s✖%s %s\n' "$RED" "$NC" "$*" >&2; }

header() {
    echo ""
    echo "╭─────────────────────────────────────────────────────────────"
    echo "│ ${CYAN}$*${NC}"
    echo "├─────────────────────────────────────────────────────────────"
}

footer() {
    echo "╰─────────────────────────────────────────────────────────────"
    echo ""
}

# ============================================================================
# Parse arguments
# ============================================================================

DEPLOY_SERVER=false
DEPLOY_CLIENT=false

parse_args() {
    # Default: deploy all if no flags specified
    if [[ $# -eq 0 ]]; then
        DEPLOY_SERVER=true
        DEPLOY_CLIENT=true
        return
    fi

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --server)  DEPLOY_SERVER=true ;;
            --client)  DEPLOY_CLIENT=true ;;
            --all)     DEPLOY_SERVER=true; DEPLOY_CLIENT=true ;;
            -h|--help)
                echo "Usage: $0 [--server] [--client] [--all]"
                echo ""
                echo "Flags:"
                echo "  --server   Deploy backend (rtaudio-server) only"
                echo "  --client   Deploy frontend (rtaudio-client) only"
                echo "  --all      Deploy both server and client (default)"
                echo "  -h|--help  Show this help"
                exit 0
                ;;
            *)
                fail "Unknown argument: $1"
                echo "Run '$0 --help' for usage."
                exit 1
                ;;
        esac
        shift
    done
}

# ============================================================================
# Step 1: Authenticate (azd auth login)
# ============================================================================

step_authenticate() {
    header "🔐 Step 1: Authenticate with Azure"

    # Check if already authenticated
    if azd auth login --check-status &>/dev/null 2>&1; then
        success "Already authenticated with azd"
    else
        info "Running azd auth login..."
        if ! azd auth login; then
            fail "azd auth login failed. Cannot proceed."
            footer
            exit 1
        fi
        success "azd auth login succeeded"
    fi

    # Also ensure az CLI is logged in (azd deploy uses it under the hood)
    if az account show &>/dev/null 2>&1; then
        local sub_name sub_id
        sub_name=$(az account show --query name -o tsv)
        sub_id=$(az account show --query id -o tsv)
        success "Azure CLI: $sub_name ($sub_id)"
    else
        info "Running az login..."
        if ! az login; then
            fail "az login failed. Cannot proceed."
            footer
            exit 1
        fi
        success "az login succeeded"
    fi

    # Set the target subscription
    local target_sub="${AZURE_SUBSCRIPTION_ID:-$DEFAULT_SUBSCRIPTION_ID}"
    info "Setting subscription to $target_sub"
    if ! az account set --subscription "$target_sub"; then
        fail "Failed to set subscription $target_sub"
        footer
        exit 1
    fi
    success "Subscription set: $(az account show --query name -o tsv) ($target_sub)"

    phase_success "Authentication OK"
    footer
}

# ============================================================================
# Step 2: Verify azd environment and discover container apps
# ============================================================================

step_discover() {
    header "🔍 Step 2: Discover container app names"

    # Get active azd environment
    local azd_env
    azd_env=$(azd env list --output json 2>/dev/null | jq -r '.[] | select(.IsDefault==true) | .Name' 2>/dev/null || echo "")

    if [[ -z "$azd_env" ]]; then
        fail "No active azd environment found."
        fail "Run: azd env new <name>  OR  azd env select <name>"
        footer
        exit 1
    fi
    success "Active azd environment: $azd_env"

    # Get resource group: env var > azd env > default
    local rg
    rg="${AZURE_RESOURCE_GROUP:-$(azd env get-value AZURE_RESOURCE_GROUP 2>/dev/null | head -n1 || echo "")}"
    rg="${rg:-$DEFAULT_RESOURCE_GROUP}"

    if [[ -z "$rg" ]]; then
        fail "Cannot determine resource group."
        fail "Set AZURE_RESOURCE_GROUP or ensure azd env has it configured."
        footer
        exit 1
    fi
    success "Resource group: $rg"

    # Discover ACR endpoint
    info "Querying container registry in resource group..."
    local acr_login_server
    acr_login_server=$(az acr list \
        --resource-group "$rg" \
        --query "[0].loginServer" \
        -o tsv 2>/dev/null || echo "")

    if [[ -n "$acr_login_server" ]]; then
        success "Container registry: $acr_login_server"
        # Set in azd env so azd deploy can find it
        azd env set AZURE_CONTAINER_REGISTRY_ENDPOINT "$acr_login_server" 2>/dev/null || true
        success "AZURE_CONTAINER_REGISTRY_ENDPOINT set in azd env"
    else
        fail "No container registry found in resource group $rg"
        footer
        exit 1
    fi

    # Discover container apps by azd-service-name tag
    info "Querying container apps in resource group..."

    # Get server container app name
    SERVER_CONTAINER_APP=$(az containerapp list \
        --resource-group "$rg" \
        --query "[?tags.\"azd-service-name\"=='rtaudio-server'].name | [0]" \
        -o tsv 2>/dev/null || echo "")

    # Get client container app name
    CLIENT_CONTAINER_APP=$(az containerapp list \
        --resource-group "$rg" \
        --query "[?tags.\"azd-service-name\"=='rtaudio-client'].name | [0]" \
        -o tsv 2>/dev/null || echo "")

    if [[ -n "$SERVER_CONTAINER_APP" ]]; then
        success "Server container app: $SERVER_CONTAINER_APP"
    else
        warn "Server container app not found (tag: azd-service-name=rtaudio-server)"
    fi

    if [[ -n "$CLIENT_CONTAINER_APP" ]]; then
        success "Client container app: $CLIENT_CONTAINER_APP"
    else
        warn "Client container app not found (tag: azd-service-name=rtaudio-client)"
    fi

    # Validate requested targets exist
    if [[ "$DEPLOY_SERVER" == "true" && -z "$SERVER_CONTAINER_APP" ]]; then
        fail "Cannot deploy server — container app not found in resource group $rg"
        footer
        exit 1
    fi
    if [[ "$DEPLOY_CLIENT" == "true" && -z "$CLIENT_CONTAINER_APP" ]]; then
        fail "Cannot deploy client — container app not found in resource group $rg"
        footer
        exit 1
    fi

    phase_success "Container apps discovered"
    footer
}

# ============================================================================
# Step 3: Deploy using azd deploy
# ============================================================================

step_deploy() {
    header "🚀 Step 3: Deploy"

    # Build the deploy target summary
    local targets=()
    [[ "$DEPLOY_SERVER" == "true" ]] && targets+=("rtaudio-server → $SERVER_CONTAINER_APP")
    [[ "$DEPLOY_CLIENT" == "true" ]] && targets+=("rtaudio-client → $CLIENT_CONTAINER_APP")

    info "Deploy targets:"
    for t in "${targets[@]}"; do
        log "  • $t"
    done
    echo "│"

    # Deploy server
    if [[ "$DEPLOY_SERVER" == "true" ]]; then
        info "Deploying rtaudio-server..."
        if azd deploy rtaudio-server; then
            success "rtaudio-server deployed"
        else
            fail "rtaudio-server deployment failed"
            footer
            exit 1
        fi
    fi

    # Deploy client
    if [[ "$DEPLOY_CLIENT" == "true" ]]; then
        info "Deploying rtaudio-client..."
        if azd deploy rtaudio-client; then
            success "rtaudio-client deployed"
        else
            fail "rtaudio-client deployment failed"
            footer
            exit 1
        fi
    fi

    phase_success "Deployment complete"
    footer
}

# ============================================================================
# Step 4: Set env vars not managed by App Configuration
# ============================================================================

step_set_env_vars() {
    header "🔧 Step 4: Set container app environment variables"

    if [[ "$DEPLOY_SERVER" != "true" ]]; then
        info "Skipping — server not deployed"
        footer
        return
    fi

    local rg="${AZURE_RESOURCE_GROUP:-$(azd env get-value AZURE_RESOURCE_GROUP 2>/dev/null | head -n1 || echo "")}"
    rg="${rg:-$DEFAULT_RESOURCE_GROUP}"

    # Collect env vars to set (only non-empty values)
    local env_vars=()

    # VoiceLive API key (secret — not stored in App Configuration)
    local vl_api_key="${AZURE_VOICELIVE_API_KEY:-$(azd env get-value AZURE_VOICELIVE_API_KEY 2>/dev/null | head -n1 || echo "")}"
    if [[ -n "$vl_api_key" ]]; then
        env_vars+=("AZURE_VOICELIVE_API_KEY=$vl_api_key")
        info "AZURE_VOICELIVE_API_KEY: set"
    else
        warn "AZURE_VOICELIVE_API_KEY not found in env or azd env — skipping"
    fi

    # Default scenario (which industry scenario to use)
    local scenario="${AGENT_SCENARIO:-$(azd env get-value AGENT_SCENARIO 2>/dev/null | head -n1 || echo "")}"
    if [[ -n "$scenario" ]]; then
        env_vars+=("AGENT_SCENARIO=$scenario")
        info "AGENT_SCENARIO: $scenario"
    fi

    if [[ ${#env_vars[@]} -eq 0 ]]; then
        info "No additional env vars to set"
        footer
        return
    fi

    info "Updating $SERVER_CONTAINER_APP..."
    if az containerapp update \
        --name "$SERVER_CONTAINER_APP" \
        --resource-group "$rg" \
        --set-env-vars "${env_vars[@]}" \
        --output none; then
        success "Environment variables updated on $SERVER_CONTAINER_APP"
    else
        warn "Failed to update env vars (non-fatal — deploy still succeeded)"
    fi

    phase_success "Environment variables configured"
    footer
}

# ============================================================================
# Summary
# ============================================================================

print_summary() {
    header "📋 Deployment Summary"

    if [[ "$DEPLOY_SERVER" == "true" ]]; then
        success "Server: $SERVER_CONTAINER_APP ✔"
    fi
    if [[ "$DEPLOY_CLIENT" == "true" ]]; then
        success "Client: $CLIENT_CONTAINER_APP ✔"
    fi

    echo "│"
    info "Useful commands:"
    log "  azd monitor --live         Stream logs"
    log "  az containerapp show -n <name> -g <rg>   Check status"
    log "  az containerapp logs show -n <name> -g <rg> --type console   View logs"

    footer
}

# ============================================================================
# Main
# ============================================================================

main() {
    parse_args "$@"

    cd "$PROJECT_ROOT"

    header "🚀 ART Voice Agent — App Deploy"
    local deploy_what="all services"
    if [[ "$DEPLOY_SERVER" == "true" && "$DEPLOY_CLIENT" == "false" ]]; then
        deploy_what="server only"
    elif [[ "$DEPLOY_SERVER" == "false" && "$DEPLOY_CLIENT" == "true" ]]; then
        deploy_what="client only"
    fi
    info "Target: $deploy_what"
    footer

    step_authenticate
    step_discover
    step_deploy
    step_set_env_vars
    print_summary
}

main "$@"
