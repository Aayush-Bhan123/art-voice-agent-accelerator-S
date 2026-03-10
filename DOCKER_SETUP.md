# Docker Setup Guide

## Quick Start

### Prerequisites
- Docker and Docker Compose installed
- Backend `.env` file configured in project root
- Frontend `.env.docker` file (created automatically)

### Start All Services

```bash
# Build and start both frontend and backend
docker-compose up --build

# Or run in detached mode
docker-compose up -d --build
```

### Access Services

- **Frontend**: http://localhost:8080
- **Backend API**: http://localhost:8080
- **Backend Health**: http://localhost:8080/api/v1/health

### Stop Services

```bash
docker-compose down

# Remove volumes if needed
docker-compose down -v
```

## Services

### Frontend
- **Port**: 8080
- **Build**: Multi-stage build (Node.js 22 Alpine)
- **Serves**: Production build via `serve`
- **Environment**: Uses `.env.docker` file

### Backend
- **Port**: 8080
- **Build**: Python backend
- **Environment**: Uses root `.env` file

## Network

Both services run on the same Docker network (`art-voice-network`) so they can communicate using service names:
- Frontend → Backend: `http://backend:8080`
- Backend → Frontend: `http://frontend:8080`

## Environment Variables

### Frontend (.env.docker)
```bash
VITE_BACKEND_BASE_URL=http://backend:8080
VITE_WS_BASE_URL=ws://backend:8080
PORT=8080
```

### Backend (.env in root)
Configure your backend environment variables in the root `.env` file.

## Troubleshooting

### Port Conflicts
If port 8080 is already in use:
```yaml
# In docker-compose.yml, change:
ports:
  - "8081:8080"  # Frontend (use 8081 on host)
  - "8082:8080"  # Backend (use 8082 on host)
```

### Rebuild After Code Changes
```bash
# Rebuild specific service
docker-compose build frontend
docker-compose up frontend

# Rebuild all
docker-compose build --no-cache
docker-compose up
```

### View Logs
```bash
# All services
docker-compose logs -f

# Specific service
docker-compose logs -f frontend
docker-compose logs -f backend
```

### Check Service Status
```bash
docker-compose ps
```

### Execute Commands in Container
```bash
# Frontend
docker-compose exec frontend sh

# Backend
docker-compose exec backend bash
```
