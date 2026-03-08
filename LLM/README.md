# CLIP Assistant — Backend

Chatbot para o portal académico CLIP da FCT-UNL. Faz perguntas em linguagem natural e o LLM navega o CLIP com o teu cookie de sessão.

---

## Pré-requisitos

- [Docker + Docker Compose](https://docs.docker.com/get-docker/)
- [Ollama](https://ollama.com) instalado localmente
- [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) (para expor ao exterior)

Modelos necessários (instalar uma vez):
```bash
# 1. Descarrega o modelo base
ollama pull qwen2.5:7b

# 2. Gera o Modelfile com o sitemap embutido
cd /caminho/para/Hackathon/LLM
python3 generate_modelfile.py

# 3. Cria o modelo personalizado no Ollama
ollama create tejo -f Modelfile
```

Verifica se ambos os modelos estão presentes:
```bash
ollama list
# Deve mostrar: tejo:latest  e  qwen2.5:7b
```

---

## Arrancar o servidor

Precisas de **3 terminais** abertos em simultâneo, por esta ordem:

### Terminal 1 — Ollama
```bash
ollama serve
```

### Terminal 2 — Backend
```bash
cd /caminho/para/Hackathon/LLM
docker compose up backend
```

Quando vires `"X routes loaded"` o backend está pronto. Para verificar:
```bash
curl http://localhost:8000/health
```

### Terminal 3 — Túnel público (Cloudflare)
```bash
cloudflared tunnel --url http://127.0.0.1:8000 --protocol http2
```

O terminal mostra o URL público, por exemplo:
```
https://xxxx-xxxx-xxxx.trycloudflare.com
```

Copia esse URL para a extensão Chrome (ícone da extensão → campo do servidor → Guardar).

> ⚠️ O URL muda cada vez que reinicias o túnel. Para URL fixo, cria conta em cloudflare.com e usa `cloudflared tunnel login`.

---

## API

### `POST /chat`

| Campo | Tipo | Obrigatório |
|---|---|---|
| `question` | string | ✅ |
| `session_cookie` | string | ✅ |
| `student_id` | string | ❌ |
| `conversation_history` | list | ❌ |

### `GET /health` — estado do serviço
### `GET /sitemap` — rotas mapeadas do CLIP

---

## Scraper (opcional)

Actualiza o mapa de rotas do CLIP:
```bash
docker compose --profile scrape up scraper
```

Resultado guardado em `data/sitemap.json`.

---

## Segurança

- O cookie de sessão **nunca é guardado** — usado apenas durante o request
- O sitemap contém apenas estrutura (URLs), nunca dados pessoais
