#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()    { echo -e "${GREEN}[✓]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }
error()   { echo -e "${RED}[✗]${NC} $*"; exit 1; }

TUNNEL_PID=""
OLLAMA_PID=""

cleanup() {
  echo ""
  warn "A terminar serviços..."
  [ -n "$TUNNEL_PID" ] && kill "$TUNNEL_PID" 2>/dev/null || true
  docker compose -f "$SCRIPT_DIR/docker-compose.yml" stop backend 2>/dev/null || true
  pkill -x ollama 2>/dev/null || true
  info "Serviços terminados."
}
trap cleanup EXIT INT TERM

# ─── 1. Verificar dependências ──────────────────────────────────────────────
command -v ollama >/dev/null 2>&1 || error "ollama não encontrado. Instala em https://ollama.com"
command -v docker >/dev/null 2>&1 || error "docker não encontrado."

# ─── 2. Iniciar Ollama ──────────────────────────────────────────────────────
if ! pgrep -x ollama >/dev/null 2>&1; then
  info "A iniciar ollama serve..."
  ollama serve &>/tmp/ollama.log &
  OLLAMA_PID=$!
fi

# Aguardar a API do Ollama ficar pronta
echo -n "    A aguardar Ollama"
for i in $(seq 1 15); do
  if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
    echo ""
    info "Ollama pronto"
    break
  fi
  echo -n "."
  sleep 2
  if [ "$i" -eq 15 ]; then
    echo ""
    error "Ollama não respondeu após 30s. Verifica: ollama serve"
  fi
done

# ─── 3. Verificar modelo Tejo ──────────────────────────────────────────────
if ! ollama show tejo >/dev/null 2>&1; then
  error "Modelo 'tejo' não encontrado. Corre:\n  cd $SCRIPT_DIR && python3 generate_modelfile.py && ollama create tejo -f Modelfile"
fi
info "Modelo tejo presente"

# ─── 3b. Verificar sitemap ──────────────────────────────────────────────────
SITEMAP="$SCRIPT_DIR/data/sitemap.json"
if [ ! -f "$SITEMAP" ]; then
  error "data/sitemap.json não encontrado. Corre o scraper primeiro:\n  docker compose --profile scrape run scraper"
fi
info "Sitemap encontrado ($(python3 -c "import json; d=json.load(open('$SITEMAP')); print(len(d.get('routes',[])))" 2>/dev/null || echo '?') rotas)"

# ─── 4. Iniciar Backend (Docker) ────────────────────────────────────────────
info "A iniciar backend Docker (rebuild)..."
docker compose -f "$SCRIPT_DIR/docker-compose.yml" up -d --build backend

# Aguardar backend ficar pronto
echo -n "    A aguardar backend"
for i in $(seq 1 30); do
  if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
    echo ""
    info "Backend pronto em http://localhost:8000"
    break
  fi
  echo -n "."
  sleep 2
  if [ "$i" -eq 30 ]; then
    echo ""
    error "Backend não respondeu após 60s. Verifica: docker logs clip_backend"
  fi
done

echo ""

# ─── 5. Tunnel opcional (cloudflared) ─────────────────────────────────────────
SERVER_URL="http://localhost:8000"
if command -v cloudflared >/dev/null 2>&1; then
  info "cloudflared encontrado — a iniciar tunnel público..."
  rm -f /tmp/cloudflared.log
  cloudflared tunnel --url http://127.0.0.1:8000 --protocol http2 >/tmp/cloudflared.log 2>&1 &
  TUNNEL_PID=$!
  echo -n "    A aguardar URL do tunnel"
  for i in $(seq 1 30); do
    TUNNEL_URL=$(grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' /tmp/cloudflared.log 2>/dev/null | head -1 || true)
    if [ -n "$TUNNEL_URL" ]; then
      SERVER_URL="$TUNNEL_URL"
      break
    fi
    echo -n "."
    sleep 2
  done
  echo ""
fi

echo ""
echo -e "  ┌─────────────────────────────────────────────┐"
echo -e "  │  URL: ${GREEN}${SERVER_URL}${NC}"
echo -e "  │  Configura este URL na extensão Chrome      │"
echo -e "  └─────────────────────────────────────────────┘"
echo ""
info "Tudo a correr. Pressiona Ctrl+C para terminar."

# Manter o script vivo (wait falha se não há jobs em background)
if [ -n "$TUNNEL_PID" ]; then
  wait "$TUNNEL_PID"
elif [ -n "$OLLAMA_PID" ]; then
  wait "$OLLAMA_PID"
else
  sleep infinity
fi
