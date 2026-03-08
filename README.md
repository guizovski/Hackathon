# CLIP REvived by Team10

Assistente académico para o portal CLIP da FCT-UNL. Faz perguntas em linguagem natural — horários, notas, propinas, testes — e o modelo navega o CLIP com o teu cookie de sessão.

# Descrição do problema e da solução:

Muitos estudantes que utilizam o CLIP partilham sempre o mesmo pensamento: o CLIP é muito lento, com múltiplas subárvores e por vezes difícil de navegar, como podemos observar com a nossa recolha de dados. Com a quantidade de vezes que um aluno precisa de aceder ao CLIP ao longo do ano, estas demoras de resposta acumulam-se.

# A solução:

O nosso chatbot torna o CLIP mais prático e eficiente, pois não é necessário saltar de subpágina em subpágina — subpáginas que por sua vez demoram tempo a carregar —, tornando o processo de navegação muito mais rápido e eficaz. Não só a navegação: caso apenas necessitemos de informação, a LLM também a pode disponibilizar diretamente, sem ser necessário navegar pelo website.

Ao invés de mudarmos o CLIP — um sistema antiquado e com muitos problemas, o que tornaria a sua modificação e transição para uma alternativa melhor bastante complicada —, construímos por cima dele, tornando-o o mais prático possível sem alterar a sua estrutura base.

---

## Índice

1. [Requisitos de sistema](#1-requisitos-de-sistema)
2. [Setup do Backend](#2-setup-do-backend)
3. [Arrancar o servidor](#3-arrancar-o-servidor)
4. [Instalar a extensão Chrome](#4-instalar-a-extensão-chrome)
5. [Usar o assistente](#5-usar-o-assistente)
6. [Windows — notas especiais](#6-windows--notas-especiais)

---

## 1. Requisitos de sistema

| Componente | Versão mínima | Para quê |
|---|---|---|
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | 24+ | Correr o backend |
| [Ollama](https://ollama.com) | qualquer | Correr o modelo LLM |
| Python 3 | 3.9+ | Gerar o Modelfile do modelo |
| RAM | ≥ 8 GB | O modelo `qwen2.5:7b` precisa de ~5 GB |

> Para expor o servidor ao exterior (outro dispositivo ou demo), instala também o [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/).

---

## 2. Setup do Backend

### macOS / Linux — automático

```bash
chmod +x setup.sh
./setup.sh
```

O script instala automaticamente Docker, Ollama e Python (se necessário), descarrega o modelo base e cria o modelo `tejo`.

---

### Windows — automático

```bat
setup.bat
```

O script instala Ollama e Python via `winget`, descarrega o modelo base e cria o modelo `tejo`.

> **Docker Desktop no Windows** não pode ser instalado automaticamente — requer WSL2. O script mostra as instruções passo a passo.

---

### Setup manual (qualquer sistema)

```bash
# 1. Descarregar modelo base
ollama pull qwen2.5:7b

# 2. Gerar Modelfile com o sitemap do CLIP embutido
cd LLM
python3 generate_modelfile.py

# 3. Criar modelo personalizado
ollama create tejo -f Modelfile

# Verificar
ollama list
# Deve mostrar: tejo:latest   qwen2.5:7b
```

---

## 3. Arrancar o servidor

### macOS / Linux — um único comando

```bash
cd LLM
./start.sh
```

O script arranca tudo automaticamente e mostra o URL público no final:

```
  +---------------------------------------------+
  |  Backend: http://localhost:8000             |
  |  Configura este URL na extensão Chrome      |
  +---------------------------------------------+
```

**Ctrl+C** para terminar todos os serviços.

---

### Windows — com start.bat

```bat
cd LLM
start.bat
```

---

> Para expor ao exterior (outro dispositivo), instala o [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) e corre:
> ```bat
> cloudflared tunnel --url http://127.0.0.1:8000 --protocol http2
> ```

---

## 4. Instalar a extensão Chrome

1. Abre o Chrome e vai a `chrome://extensions`
2. Ativa o **Modo de programador** (canto superior direito)
3. Clica em **Carregar sem compressão**
4. Seleciona a pasta `Extension/`
5. A extensão aparece na barra do Chrome com o ícone 🎓

**Configurar o URL do servidor:**
1. Clica no ícone 🎓 na barra do Chrome
2. No campo **URL do Servidor Tejo**, cola o URL mostrado pelo `start.sh`/`start.bat` (ex: `http://localhost:8000`)
3. Clica em **Guardar**

---

## 5. Usar o assistente

1. Vai ao [CLIP FCT-UNL](https://clip.fct.unl.pt) e faz login
2. O assistente aparece automaticamente no canto inferior direito
3. Navega para a tua página de aluno para que o assistente aceda ao teu contexto
4. Usa os botões de sugestão ou escreve livremente

**Exemplos de perguntas:**
- "Qual é o meu horário esta semana?"
- "Tenho propinas em atraso?"
- "Quais são as minhas notas?"
- "Tenho testes para me inscrever?"
- "Exportar horário para .ics"

---

## 6. Windows — notas especiais

### Docker Desktop
O Docker Desktop no Windows requer **WSL2** (Windows Subsystem for Linux):

1. Abre o PowerShell como administrador:
   ```powershell
   wsl --install
   ```
2. Reinicia o computador
3. Descarrega e instala o [Docker Desktop](https://www.docker.com/products/docker-desktop/)
4. No Docker Desktop → Settings → General → ativa "Use the WSL 2 based engine"

### Firewall
Se o Docker ou o cloudflared forem bloqueados pelo Windows Defender Firewall, clica em **Permitir acesso** quando pedido.

---

## Estrutura do projeto

```
Hackathon/
├── Extension/          # Extensão Chrome
│   ├── manifest.json
│   ├── content.js      # UI + lógica principal
│   ├── background.js   # Service worker (cookies)
│   └── popup.html/js   # Configurações
└── LLM/                # Backend
    ├── setup.sh        # Setup automático (macOS/Linux)
    ├── setup.bat       # Setup automático (Windows)
    ├── start.sh        # Arrancar servidor (macOS/Linux)
    ├── generate_modelfile.py
    ├── Modelfile
    ├── docker-compose.yml
    ├── backend/        # FastAPI
    └── data/
        └── sitemap.json
```
