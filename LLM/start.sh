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

cleanup() {
  echo ""
  warn "A terminar serviços..."
  [ -n "$TUNNEL_PID" ] && kill "$TUNNEL_PID" 2>/dev/null || true
  docker compose -f "$SCRIPT_DIR/docker-compose.yml" stop backend 2>/dev/null || true
  info "Serviços terminados."
}
trap cleanup EXIT INT TERM

# ─── 1. Verificar dependências ──────────────────────────────────────────────
command -v ollama       >/dev/null 2>&1 || error "ollama não encontrado. Instala em https://ollama.com"
command -v docker       >/dev/null 2>&1 || error "docker não encontrado."
command -v cloudflared  >/dev/null 2>&1 || error "cloudflared não encontrado. Instala em https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"

# ─── 2. Iniciar Ollama ──────────────────────────────────────────────────────
if ! pgrep -x ollama >/dev/null 2>&1; then
  info "A iniciar ollama serve..."
  ollama serve &>/tmp/ollama.log &
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

# ─── 3. Verificar modelos Ollama ────────────────────────────────────────────
for MODEL in tejo qwen2.5:7b; do
  if ! ollama show "$MODEL" >/dev/null 2>&1; then
    error "Modelo '$MODEL' não encontrado. Corre:\n  python3 generate_modelfile.py && ollama create tejo -f Modelfile"
  fi
done
info "Modelos Ollama presentes (tejo, qwen2.5:7b)"

# ─── 4. Iniciar Backend (Docker) ────────────────────────────────────────────
info "A iniciar backend Docker..."
docker compose -f "$SCRIPT_DIR/docker-compose.yml" up -d backend

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

# ─── 5. Iniciar Cloudflare Tunnel ───────────────────────────────────────────
info "A iniciar Cloudflare Tunnel..."
rm -f /tmp/cloudflared.log
cloudflared tunnel --url http://127.0.0.1:8000 --protocol http2 >/tmp/cloudflared.log 2>&1 &
TUNNEL_PID=$!

# Extrair URL do tunnel
echo -n "    A aguardar URL do tunnel"
TUNNEL_URL=""
for i in $(seq 1 30); do
  TUNNEL_URL=$(grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' /tmp/cloudflared.log 2>/dev/null | head -1 || true)
  if [ -n "$TUNNEL_URL" ]; then
    break
  fi
  echo -n "."
  sleep 2
done
echo ""

if [ -n "$TUNNEL_URL" ]; then
  echo ""
  echo -e "  ┌─────────────────────────────────────────────┐"
  echo -e "  │  URL PÚBLICO: ${GREEN}${TUNNEL_URL}${NC}"
  echo -e "  │  Copia este URL para a extensão Chrome      │"
  echo -e "  └─────────────────────────────────────────────┘"
  echo ""
else
  warn "Não foi possível extrair o URL do tunnel. Verifica /tmp/cloudflared.log"
fi

info "Tudo a correr. Pressiona Ctrl+C para terminar."
wait "$TUNNEL_PID"
