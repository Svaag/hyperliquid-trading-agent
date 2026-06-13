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
docker compose logs -f bot
```

Validation:

```bash
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/ready
```

Keep `/metrics` protected with `METRICS_BEARER_TOKEN` before exposing it.
