#!/usr/bin/env bash
# setup.sh — instala todos os requisitos do CLIP Assistant Backend
# Suporta: macOS, Ubuntu/Debian, Fedora/RHEL, Arch
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${GREEN}[✓]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }
error()   { echo -e "${RED}[✗]${NC} $*"; exit 1; }
step()    { echo -e "\n${BLUE}▶ $*${NC}"; }

OS="$(uname -s)"
ARCH="$(uname -m)"

require_sudo() {
  if [[ $EUID -ne 0 ]] && ! sudo -n true 2>/dev/null; then
    warn "Este script precisa de sudo para instalar pacotes."
    sudo true || error "Permissões sudo necessárias."
  fi
}

# ─── Detectar distro Linux ───────────────────────────────────────────────────
detect_linux_distro() {
  if command -v apt-get &>/dev/null; then echo "debian"
  elif command -v dnf &>/dev/null;   then echo "fedora"
  elif command -v pacman &>/dev/null; then echo "arch"
  else echo "unknown"; fi
}

# ─── 1. Docker ───────────────────────────────────────────────────────────────
install_docker() {
  step "Verificar Docker"
  if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
    info "Docker já instalado ($(docker --version | cut -d' ' -f3 | tr -d ','))"
    return
  fi

  if [[ "$OS" == "Darwin" ]]; then
    if command -v brew &>/dev/null; then
      warn "A instalar Docker Desktop via Homebrew..."
      brew install --cask docker
      warn "Abre o Docker Desktop manualmente e aguarda até estar a correr, depois volta a correr este script."
      open -a Docker 2>/dev/null || true
      echo -n "    A aguardar Docker daemon"
      for i in $(seq 1 30); do
        docker info &>/dev/null 2>&1 && break
        echo -n "."; sleep 3
      done
      echo ""
      docker info &>/dev/null 2>&1 || error "Docker não está a correr. Abre o Docker Desktop e volta a correr o script."
      info "Docker pronto"
    else
      error "Instala o Docker Desktop manualmente: https://www.docker.com/products/docker-desktop/\nDepois volta a correr este script."
    fi

  elif [[ "$OS" == "Linux" ]]; then
    require_sudo
    DISTRO=$(detect_linux_distro)
    if [[ "$DISTRO" == "debian" ]]; then
      warn "A instalar Docker (apt)..."
      sudo apt-get update -qq
      sudo apt-get install -y ca-certificates curl gnupg lsb-release
      sudo install -m 0755 -d /etc/apt/keyrings
      curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
      sudo chmod a+r /etc/apt/keyrings/docker.gpg
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | \
        sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
      sudo apt-get update -qq
      sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
      sudo usermod -aG docker "$USER"
      sudo systemctl enable --now docker
    elif [[ "$DISTRO" == "fedora" ]]; then
      warn "A instalar Docker (dnf)..."
      sudo dnf -y install dnf-plugins-core
      sudo dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo
      sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
      sudo usermod -aG docker "$USER"
      sudo systemctl enable --now docker
    elif [[ "$DISTRO" == "arch" ]]; then
      warn "A instalar Docker (pacman)..."
      sudo pacman -Sy --noconfirm docker docker-compose
      sudo usermod -aG docker "$USER"
      sudo systemctl enable --now docker
    else
      error "Distro não suportada. Instala o Docker manualmente: https://docs.docker.com/engine/install/"
    fi
    info "Docker instalado"
  fi
}

# ─── 2. Ollama ───────────────────────────────────────────────────────────────
install_ollama() {
  step "Verificar Ollama"
  if command -v ollama &>/dev/null; then
    info "Ollama já instalado ($(ollama --version 2>/dev/null | head -1))"
    return
  fi

  warn "A instalar Ollama..."
  if [[ "$OS" == "Darwin" ]]; then
    if command -v brew &>/dev/null; then
      brew install ollama
    else
      curl -fsSL https://ollama.com/install.sh | sh
    fi
  elif [[ "$OS" == "Linux" ]]; then
    curl -fsSL https://ollama.com/install.sh | sh
  fi
  info "Ollama instalado"
}

# ─── 3. Python 3 (para generate_modelfile.py) ───────────────────────────────
check_python() {
  step "Verificar Python 3"
  if command -v python3 &>/dev/null; then
    info "Python $(python3 --version 2>&1 | cut -d' ' -f2) disponível"
    return
  fi
  warn "Python 3 não encontrado."
  if [[ "$OS" == "Darwin" ]] && command -v brew &>/dev/null; then
    brew install python3
  elif [[ "$OS" == "Linux" ]]; then
    DISTRO=$(detect_linux_distro)
    [[ "$DISTRO" == "debian" ]] && sudo apt-get install -y python3
    [[ "$DISTRO" == "fedora" ]] && sudo dnf install -y python3
    [[ "$DISTRO" == "arch" ]]   && sudo pacman -Sy --noconfirm python
  fi
  info "Python 3 instalado"
}

# ─── 5. Criar modelo Tejo ────────────────────────────────────────────────────
setup_tejo_model() {
  step "Configurar modelo Tejo"

  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  LLM_DIR="$SCRIPT_DIR/LLM"

  # Arrancar Ollama temporariamente se não estiver a correr
  OLLAMA_STARTED=false
  if ! pgrep -x ollama &>/dev/null; then
    ollama serve &>/tmp/ollama_setup.log &
    OLLAMA_STARTED=true
    echo -n "    A aguardar Ollama"
    for i in $(seq 1 15); do
      curl -sf http://localhost:11434/api/tags &>/dev/null && break
      echo -n "."; sleep 2
    done
    echo ""
  fi

  # Descarregar modelo base se necessário
  if ! ollama show qwen2.5:7b &>/dev/null 2>&1; then
    warn "A descarregar qwen2.5:7b (~4.7 GB, pode demorar)..."
    ollama pull qwen2.5:7b
  else
    info "qwen2.5:7b já presente"
  fi

  # Gerar Modelfile e (re)criar modelo tejo — sempre, para garantir que está actualizado
  warn "A gerar Modelfile e criar modelo tejo..."
  cd "$LLM_DIR"
  python3 generate_modelfile.py
  ollama create tejo -f Modelfile
  info "Modelo tejo criado/actualizado"

  # Verificar sitemap
  if [ ! -f "$LLM_DIR/data/sitemap.json" ]; then
    warn "data/sitemap.json não encontrado — o backend não vai funcionar sem ele."
    warn "Depois do setup, corre: docker compose --profile scrape run scraper"
  fi

  # Parar Ollama se foi arrancado por este script
  if $OLLAMA_STARTED; then
    pkill -x ollama 2>/dev/null || true
  fi
}

# ─── Main ─────────────────────────────────────────────────────────────────────
echo -e "${GREEN}"
echo "  ╔════════════════════════════════════════╗"
echo "  ║   CLIP Assistant — Setup              ║"
echo "  ╚════════════════════════════════════════╝"
echo -e "${NC}"

[[ "$OS" == "Windows_NT" ]] && error "No Windows usa setup.bat em vez deste script."

install_docker
install_ollama
check_python
setup_tejo_model

echo ""
echo -e "${GREEN}✅  Setup concluído!${NC}"
echo ""
echo "  Para arrancar o servidor:"
echo -e "  ${YELLOW}cd LLM && ./start.sh${NC}"
echo ""
