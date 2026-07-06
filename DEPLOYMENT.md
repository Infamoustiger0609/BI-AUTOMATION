# Prompt2PBI Deployment Guide

## Local Development

1. Copy `.env.dev` to `.env` if you want to run without Docker.
2. Start the stack:

```bash
docker compose -f docker/docker-compose.yml --profile observability --profile proxy up --build
```

3. Open:
   - API: `http://localhost:8000`
   - Swagger docs: `http://localhost:8000/docs`
   - Flower: `http://localhost:5555`

## Single-Server Production

1. Build the image:

```bash
docker build -f docker/Dockerfile -t prompt2pbi:latest .
```

2. Create certificates with Let's Encrypt.
3. Copy `.env.prod` to the server and populate secrets.
4. Start services:

```bash
docker compose -f docker/docker-compose.prod.yml up -d
```

5. For zero-downtime deploys:
   - build the new image
   - start a new stack with a temporary project name
   - health-check `/api/health`
   - switch the reverse proxy upstream
   - remove the old stack after validation

## Kubernetes

Apply manifests in this order:

1. `namespace.yaml`
2. `configmap.yaml`
3. `secret.yaml`
4. `shared-storage-pvc.yaml` (set `storageClassName` to a ReadWriteMany-capable class for your cluster first -- web and worker pods share uploaded files and generated `.pbix` artifacts through this volume)
5. `redis-deployment.yaml`
6. `redis-service.yaml`
7. `web-deployment.yaml`
8. `web-service.yaml`
9. `worker-deployment.yaml`
10. `ingress.yaml`

Job status/progress is stored in Redis (not in-process memory), so it is safe to run multiple `web` replicas and `worker` replicas behind the same `REDIS_URL` -- any pod can serve a status/download request for a job another pod created.

## Troubleshooting

- Check `docker compose logs -f web` for startup issues.
- Verify `/api/health` before routing traffic.
- If downloads fail, confirm the `output/` volume is writable.
- If Celery jobs stall, confirm Redis is healthy and the worker is running.

## Rollback

1. Keep the previous image tagged in your registry.
2. Re-point Docker Compose or Kubernetes to the previous tag.
3. Validate `/api/health`.
4. Roll back the reverse proxy only after the API is healthy.

