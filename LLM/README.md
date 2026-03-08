# CLIP Assistant — Backend

Chatbot para o portal académico CLIP da FCT-UNL. Faz perguntas em linguagem natural e o modelo navega o CLIP com o teu cookie de sessão.

---

## Pré-requisitos

- [Docker + Docker Compose](https://docs.docker.com/get-docker/)
- [Ollama](https://ollama.com) instalado localmente
- Python 3.9+

> Para expor ao exterior instala também o [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) — o `start.sh` usa-o automaticamente se estiver disponível.

Modelos necessários (instalar uma vez — o `setup.sh`/`setup.bat` faz isto automaticamente):
```bash
# 1. Descarrega o modelo base
ollama pull qwen2.5:7b

# 2. Gera o Modelfile com o sitemap embutido
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

```bash
./start.sh
```

O script arranca Ollama, o backend Docker e (se `cloudflared` estiver instalado) um tunnel público. No final mostra o URL a usar na extensão Chrome.

Para terminar: **Ctrl+C**

Para verificar que o backend está a correr:
```bash
curl http://localhost:8000/health
```

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
