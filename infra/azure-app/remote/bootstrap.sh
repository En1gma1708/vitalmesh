#!/usr/bin/env bash
# Install and start the whole VitalMesh demo on THIS VM (D99). Run by deploy.ps1 over SSH; safe to
# run again (it updates the code, rewrites the settings and restarts the services).
#
#   bootstrap.sh <git-ref> <public-name>      (the shared password arrives on stdin)
#
# Expects, uploaded by deploy.ps1 into /home/vmadmin/upload:
#   models.env       KEY=value lines for the model providers (never printed, never in git)
#   notice.txt       the one-line banner shown on every page
#   corpus.sql.gz    the guideline corpus tables (public-domain documents, local embeddings)
#   units/*.service  the systemd units
# Nothing here prints a secret.
set -euo pipefail

REF="${1:?git ref}"
FQDN="${2:?public name}"
read -r SHARED_PASSWORD
[ -n "$SHARED_PASSWORD" ] || { echo "no shared password on stdin" >&2; exit 2; }

UP=/home/vmadmin/upload
ROOT=/opt/vitalmesh
APP=$ROOT/app
ENVF=$ROOT/env
REPO=https://github.com/En1gma1708/vitalmesh.git
CADDY_IMAGE=caddy:2.8.4

step() { printf '\n== %s\n' "$*"; }
# The SSH user joins the docker group at first boot but this session predates it (first real run,
# "permission denied ... docker.sock"): every docker call goes through sudo. Stdin is passed through.
docker() { sudo docker "$@"; }

step "wait for first-boot setup (docker, uv)"
for _ in $(seq 1 120); do [ -f /var/lib/cloud/instance/vitalmesh-ready ] && break; sleep 5; done
[ -f /var/lib/cloud/instance/vitalmesh-ready ] || { echo "cloud-init did not finish" >&2; exit 3; }

step "code at $REF"
if [ -d "$APP/.git" ]; then
  git -C "$APP" fetch --quiet origin "$REF"
else
  git clone --quiet "$REPO" "$APP"
  git -C "$APP" fetch --quiet origin "$REF"
fi
git -C "$APP" checkout --quiet --detach FETCH_HEAD
git -C "$APP" log -1 --format='commit %h %s'

step "python environment"
cd "$APP"
uv sync --frozen --quiet
.venv/bin/python scripts/gen_proto.py >/dev/null

step "settings (secrets stay in a 600 file)"
umask 077
[ -f "$ROOT/auth.secret" ] || python3 -c 'import secrets;print("VITALMESH_AUTH_SECRET="+secrets.token_urlsafe(48))' > "$ROOT/auth.secret"
{
  cat "$ROOT/auth.secret"
  cat "$UP/models.env"
  echo "VITALMESH_REQUIRE_VERIFIED=0"
  echo "VITALMESH_BASE_URL=https://$FQDN"
  echo "VITALMESH_RUN_DEADLINE_S=240"
  echo "VITALMESH_NOTICE=$(head -n1 "$UP/notice.txt")"
} > "$ENVF"
umask 022
# Langfuse keys are deliberately NOT set: a shared demo must not send visitors' text to a tracing service

step "units"
sudo cp "$UP"/units/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vitalmesh-infra.service

step "guideline corpus"
docker exec vitalmesh-postgres psql -q -U vitalmesh -d vitalmesh -c "CREATE EXTENSION IF NOT EXISTS vector" >/dev/null
ROWS=$(docker exec vitalmesh-postgres psql -U vitalmesh -d vitalmesh -tAc "select count(*) from documents" 2>/dev/null || echo "")
if [ "$ROWS" = "" ] || [ "${ROWS:-0}" -eq 0 ] 2>/dev/null; then
  gunzip -c "$UP/corpus.sql.gz" | docker exec -i vitalmesh-postgres psql -q -U vitalmesh -d vitalmesh -v ON_ERROR_STOP=1 >/dev/null
fi
docker exec vitalmesh-postgres psql -U vitalmesh -d vitalmesh -tAc \
  "select 'documents=' || (select count(*) from documents) || ' chunks=' || (select count(*) from chunks)"

step "topics (once) and embedding model download (once)"
if [ ! -f "$ROOT/topics.done" ]; then .venv/bin/python scripts/reset_topics.py | tail -1; touch "$ROOT/topics.done"; fi
.venv/bin/python -c "from vitalmesh_retrieval.embed import LocalEmbedder; print(len(LocalEmbedder().embed_query('warm up')), 'dimensions')"

step "application services"
sudo systemctl enable vitalmesh-processor vitalmesh-agents vitalmesh-api >/dev/null 2>&1
sudo systemctl restart vitalmesh-processor vitalmesh-agents vitalmesh-api

step "web front: HTTPS for $FQDN and a shared password"
HASH=$(printf '%s' "$SHARED_PASSWORD" | docker run --rm -i "$CADDY_IMAGE" sh -c 'read -r p; caddy hash-password --plaintext "$p"')
cat > "$ROOT/Caddyfile" <<EOF
$FQDN {
	encode zstd gzip
	basic_auth {
		reviewer $HASH
	}
	header -Server
	reverse_proxy 127.0.0.1:8000 {
		flush_interval -1
	}
}
EOF
docker rm -f vitalmesh-caddy >/dev/null 2>&1 || true
docker run -d --name vitalmesh-caddy --restart unless-stopped --network host \
  -v "$ROOT/Caddyfile:/etc/caddy/Caddyfile:ro" -v vitalmesh-caddy-data:/data "$CADDY_IMAGE" >/dev/null

step "check"
for _ in $(seq 1 90); do
  CODE=$(printf 'user = "reviewer:%s"\n' "$SHARED_PASSWORD" | curl -s -o /dev/null -w '%{http_code}' -K - \
    --resolve "$FQDN:443:127.0.0.1" "https://$FQDN/healthz" || true)
  [ "$CODE" = "200" ] && break
  sleep 5
done
echo "https healthz with the shared password: $CODE"
for u in infra processor agents api; do printf '%-10s %s\n' "$u" "$(systemctl is-active vitalmesh-$u)"; done
[ "$CODE" = "200" ] || { echo "the site did not come up; see: journalctl -u vitalmesh-api, docker logs vitalmesh-caddy" >&2; exit 4; }
echo "BOOTSTRAP OK"
