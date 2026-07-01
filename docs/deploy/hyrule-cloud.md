# Hyrule Cloud Deployment Notes

Target: fresh Hyrule.host Customer VM.

Recommended VM:

- OS: Debian 13
- Size: md
- Duration: 30 days
- Domain mode: auto (`*.deploy.hyrule.host`)
- Open ports: 22, 80, 443

After provisioning:

```bash
ssh root@<hostname>.deploy.hyrule.host
apt-get update
apt-get install -y ca-certificates curl git docker.io docker-compose-plugin
systemctl enable --now docker

git clone <repo-url> /opt/hyperliquid-trading-agent
cd /opt/hyperliquid-trading-agent
cp .env.example .env
$EDITOR .env

docker compose up -d --build
docker compose logs -f api
```

Validation:

```bash
curl http://127.0.0.1:8081/health
curl http://127.0.0.1:8081/ready
curl http://127.0.0.1:8081/runtime/status
```

Only the `api` service should be exposed publicly, preferably behind TLS/auth. Service-role workers (`newswire`, `world-model`, `trader`, `agent`) expose no dashboard ports. See [service-role-runtime.md](service-role-runtime.md) for role boundaries, command-intent polling, and compatibility aliases. Keep `/metrics` protected with `METRICS_BEARER_TOKEN` before exposing it.
