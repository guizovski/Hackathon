"""
Lógica de interacção com o LLM Tejo:
  - build_interpretation_prompt: constrói o prompt de interpretação HTML
  - llm_decide_routes: decide intenção + rotas a consultar
  - llm_interpret: interpreta HTML e responde à pergunta
"""

import asyncio
import re
import json
import datetime
from urllib.parse import urlparse, unquote

import ollama as ollama_client
from bs4 import BeautifulSoup

from config import (
    OLLAMA_URL, ROUTING_MODEL, SITEMAP, CURRENT_PERIOD,
    substitute_student_id, _unquote,
)


# ── Sitemap structure lookup ──────────────────────────────────────────────────
def _get_sitemap_structure(route: str) -> str:
    """Procura a page_structure do sitemap para uma dada URL/rota."""
    route_decoded = _unquote(route)
    for r in SITEMAP.get("routes", []):
        pattern = _unquote(r.get("pattern", ""))
        url = _unquote(r.get("example_url", ""))
        if pattern and (pattern in route_decoded or route_decoded in url):
            desc = (r.get("page_structure") or {}).get("description", "")
            if desc and "não reconhecida" not in desc and "Guilherme" not in desc:
                return desc
    return ""


# ── Interpretation prompt ─────────────────────────────────────────────────────
def build_interpretation_prompt(question: str, raw_html: str, route: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "meta", "link"]):
        tag.decompose()

    # Extrai tabelas com cabeçalhos emparelhados para evitar confusão de colunas
    table_texts = []
    for table in soup.find_all("table"):
        headers = []
        header_row = table.find("tr")
        if header_row:
            headers = [th.get_text(strip=True) for th in header_row.find_all(["th", "td"])]

        rows = []
        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all(["th", "td"])]
            if not cells:
                continue
            if headers and len(headers) == len(cells):
                row_str = " | ".join(f"{h}: {c}" for h, c in zip(headers, cells))
            else:
                row_str = " | ".join(cells)
            rows.append(row_str)

        if headers:
            table_texts.append("Cabeçalhos: " + " | ".join(headers) + "\n" + "\n".join(rows))
        elif rows:
            table_texts.append("\n".join(rows))

    if table_texts:
        structured = "\n\n[TABELA]\n" + "\n\n[TABELA]\n".join(table_texts)
        for table in soup.find_all("table"):
            table.decompose()
        extra = soup.get_text(separator="\n", strip=True)[:1000]
        clean_text = extra + "\n\n" + structured
    else:
        clean_text = soup.get_text(separator="\n", strip=True)

    truncated = clean_text[:5000] + ("..." if len(clean_text) > 5000 else "")

    route_decoded = _unquote(route)
    sitemap_struct = _get_sitemap_structure(route)

    if "horário" in route_decoded or "hor%E1rio" in route:
        page_hints = sitemap_struct or """Formato da página de horário:
- Cada aula é uma célula <td class="celulaDeCalendario"> com atributo title=Nome completo da cadeira.
- O link dentro da célula tem parâmetros: dia=N (2=Segunda…6=Sexta), início=HHMM (ex: 800=8h00).
- O rowspan da célula indica a duração: rowspan/2 = horas (ex: rowspan=4 → 2h).
- Dentro da célula: <b>Abreviatura</b>, link com turno (ex: tp.1, p.3), texto da sala."""
    elif "resultados" in route_decoded:
        page_hints = sitemap_struct or """Formato da página de resultados:
- Tabela com colunas: Cadeira | (col vazia) | Esp. | Nor. | Rec. | Ext. | Resultado | Créditos ECTS
- As colunas Nor./Rec./Ext. são as notas (0-20) por época. Créditos ECTS é a última coluna — NÃO é nota.
- Resultado pode ser: Aprovado, Reprovado, Não avaliado, Admitido."""
    elif "testes_de_avalia" in route_decoded:
        page_hints = sitemap_struct or """Formato da página de inscrição em testes:
- Duas tabelas lado a lado: esquerda=disponíveis para inscrição, direita=já inscritos.
- Colunas: Unidade curricular | Avaliação | Data (YYYY-MM-DD HH:MM) | Sala(s)
- Botão "Inscrever" na tabela da esquerda; botão "Anular" na da direita."""
    elif "presenças" in route_decoded or "presen" in route_decoded:
        page_hints = sitemap_struct or """Formato da página de presenças/faltas:
- Tabela com colunas: Cadeira | Aulas dadas | Faltas | % Presenças
- O limite de faltas a partir do qual o aluno perde a frequência é normalmente 1/3 das aulas."""
    elif "propinas" in route_decoded:
        page_hints = sitemap_struct or """Formato da página de propinas:
- Lista de prestações com: descrição | montante | data limite | estado (pago/em dívida)."""
    elif "situação/progressão" in route_decoded:
        page_hints = sitemap_struct or """Formato da página de progressão académica:
- Mostra percentagem de créditos ECTS concluídos face ao total do plano.
- NÃO contém notas — os valores percentuais NÃO devem ser apresentados como notas."""
    elif "calendário" in route_decoded:
        page_hints = sitemap_struct or """Formato da página de calendário de avaliação:
- Tabela com: Cadeira | Tipo de avaliação | Data | Hora | Sala(s) | Observações."""
    else:
        page_hints = sitemap_struct

    hints_block = f"\nEstrutura da página:\n{page_hints}\n" if page_hints else ""

    return f"""ATENÇÃO: Responde APENAS em prosa, em português. NÃO respondas com JSON. NÃO uses os campos "intent", "routes_to_fetch" nem nenhum formato JSON. Apenas texto corrido.

O utilizador perguntou: "{question}"

Contexto actual: hoje é {datetime.date.today().strftime('%d/%m/%Y')} — {CURRENT_PERIOD['descricao']}.

Fui buscar: {route}
{hints_block}
Conteúdo obtido:
---
{truncated}
---

Contexto importante sobre o sistema académico português:
- As notas são valores entre 0 e 20 (inteiros ou com uma casa decimal). Créditos ECTS (6, 7.5, 10…) NÃO são notas.
- Nunca confundas créditos ECTS com notas. Nunca apresentes percentagens de progressão como notas.
- Se a página for de NAVEGAÇÃO (só tem links de períodos/semestres sem dados concretos), explica quais as opções disponíveis e pede ao utilizador para especificar o período.

REGRAS DE RESPOSTA OBRIGATÓRIAS:
- Responde SEMPRE em português europeu (pt-PT). Usa "tu/tens/podes/faz" — NUNCA "você/tem/pode/efetue".
- Sê directo e conciso: apresenta os dados relevantes sem introduções, recomendações ou conclusões desnecessárias.
- Não uses frases como "De acordo com os dados fornecidos" ou "Recomendo que".
- Apresenta os dados exactamente como estão na página."""


# ── LLM routing ───────────────────────────────────────────────────────────────
_DTYPE_PRIORITY = {
    "schedule": 0, "grades": 1, "exams": 2, "absences": 3,
    "enrollments": 4, "documents": 5, "notices": 6, "unknown": 99,
}


def _pick_best_route_fallback(student_id: str | None) -> list[str]:
    """Fallback: devolve a rota de schedule mais provável do sitemap."""
    routes = SITEMAP.get("routes", [])
    sorted_routes = sorted(routes, key=lambda r: _DTYPE_PRIORITY.get(r.get("data_type", "unknown"), 99))
    for r in sorted_routes[:1]:
        url = r.get("example_url", "")
        if student_id:
            url = substitute_student_id(url, student_id)
        if url:
            return [url]
    return []


async def llm_decide_routes(question: str, history: list[dict], student_id: str | None = None) -> dict:
    """
    Envia a pergunta ao Tejo e obtém intenção + rotas a consultar.
    Devolve dict com 'intent', 'routes_to_fetch', 'cadeira' e 'reasoning'.
    """
    all_routes = SITEMAP.get("routes", [])

    prompt = (
        f"Contexto: hoje é {datetime.date.today().strftime('%d/%m/%Y')} — "
        f"{CURRENT_PERIOD['descricao']} "
        f"(ano_lectivo={CURRENT_PERIOD['ano_lectivo']}, período={CURRENT_PERIOD['período_lectivo']}).\n"
        f"Pergunta: {question}"
    )
    if student_id:
        prompt += f"\nID do aluno: {student_id}"
    messages = list(history) + [{"role": "user", "content": prompt}]
    print(f"[INFO] A enviar pergunta ao Tejo (intent + rotas)")

    client = ollama_client.Client(host=OLLAMA_URL)
    response = await asyncio.to_thread(client.chat, model=ROUTING_MODEL, messages=messages)
    raw = response["message"]["content"].strip()
    print(f"[LLM] Resposta bruta: {raw[:300]}")

    try:
        json_str = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if not json_str:
            json_str = re.search(r"\{.*\}", raw, re.DOTALL)
        if json_str:
            parsed = json.loads(json_str.group(1) if json_str.lastindex else json_str.group())
            intent  = parsed.get("intent", "read")
            cadeira = parsed.get("cadeira", "")
            if intent in ("ics_export", "enroll_test", "unsupported_action"):
                return {"intent": intent, "cadeira": cadeira, "routes_to_fetch": [], "reasoning": parsed.get("reasoning", "")}
            urls = parsed.get("routes_to_fetch", [])

            # Mapeia cada URL para a versão correcta do sitemap (Latin-1 encoded, params reais)
            sitemap_by_path = {}
            for r in all_routes:
                su   = r.get("example_url", "")
                path = _unquote(urlparse(su).path)
                sitemap_by_path[path] = su

            resolved = []
            for u in urls:
                if u in sitemap_by_path.values():
                    resolved.append(u)
                    continue
                u_path_latin = _unquote(urlparse(u).path)
                u_path_utf8  = unquote(urlparse(u).path)
                matched = False
                for u_path in (u_path_latin, u_path_utf8):
                    if u_path in sitemap_by_path:
                        resolved.append(sitemap_by_path[u_path])
                        print(f"[INFO] URL do LLM corrigida: {u} → {sitemap_by_path[u_path]}")
                        matched = True
                        break
                if matched:
                    continue
                # Match parcial por segmento mais específico do path
                u_path = u_path_latin
                best_match = None
                best_len = 0
                for sp, su in sitemap_by_path.items():
                    if u_path in sp or sp in u_path:
                        if len(sp) > best_len:
                            best_len = len(sp)
                            best_match = su
                if best_match:
                    resolved.append(best_match)
                    print(f"[INFO] URL do LLM mapeada por path parcial: {u} → {best_match}")
                elif "clip.fct.unl.pt" in u:
                    resolved.append(u)
                else:
                    # Último recurso: o LLM pode ter devolvido um rótulo (ex: "💶 Propinas")
                    # em vez de uma URL — tenta fazer match por keywords contra os paths do sitemap
                    label_clean = re.sub(r"[^\w\s]", "", u, flags=re.UNICODE).lower().strip()
                    label_words = [w for w in label_clean.split() if len(w) >= 3]
                    if label_words:
                        # Hints explícitos: forçam preferência por um path específico
                        _LABEL_PATH_HINTS = {
                            "propina":   "dados_para_pagamento",
                            "pagamento": "dados_para_pagamento",
                            "horario":   "hor%E1rio",
                            "horário":   "hor%E1rio",
                            "nota":      "resultados",
                            "resultado": "resultados",
                            "falta":     "presen%E7as",
                            "presenca":  "presen%E7as",
                            "resumo":    "resumo",
                            "progressao": "progress%E3o",
                        }
                        forced_hint = next(
                            (hint for kw, hint in _LABEL_PATH_HINTS.items() if kw in label_clean),
                            None,
                        )
                        lbl_best = None
                        lbl_best_score = 0
                        for sp, su in sitemap_by_path.items():
                            # Boost se o path corresponde ao hint explícito
                            hint_bonus = 10 if forced_hint and forced_hint in su else 0
                            score = sum(1 for w in label_words if w in sp.lower()) + hint_bonus
                            if score > lbl_best_score:
                                lbl_best_score = score
                                lbl_best = su
                        if lbl_best:
                            resolved.append(lbl_best)
                            print(f"[INFO] URL do LLM mapeada por label '{u}' → {lbl_best}")

            if resolved:
                if student_id:
                    resolved = [substitute_student_id(u, student_id) for u in resolved]
                return {"intent": intent, "cadeira": cadeira, "routes_to_fetch": resolved, "reasoning": parsed.get("reasoning", "")}
    except (json.JSONDecodeError, AttributeError):
        pass

    best = _pick_best_route_fallback(student_id)
    print(f"[WARN] Tejo falhou a parsear JSON, usando fallback: {best}")
    return {"intent": "read", "cadeira": "", "routes_to_fetch": best, "reasoning": "fallback"}


# ── LLM interpreta HTML ───────────────────────────────────────────────────────
async def llm_interpret(question: str, raw_html: str, route: str) -> str:
    """Envia HTML da página ao Tejo para que interprete e responda à pergunta."""
    prompt = build_interpretation_prompt(question, raw_html, route)
    client = ollama_client.Client(host=OLLAMA_URL)
    response = await asyncio.to_thread(
        client.chat,
        model=ROUTING_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response["message"]["content"].strip()

    # Detecção de segurança: se o Tejo respondeu com JSON de routing em vez de prosa,
    # extrai o campo 'reasoning' que normalmente contém a resposta real.
    if raw.startswith("{") and '"intent"' in raw:
        try:
            parsed = json.loads(re.search(r"\{.*\}", raw, re.DOTALL).group())
            reasoning = parsed.get("reasoning", "")
            if reasoning and len(reasoning) > 20:
                print(f"[WARN] llm_interpret devolveu JSON de routing — a usar 'reasoning' como resposta")
                return reasoning
        except Exception:
            pass
        # Se não conseguimos extrair reasoning útil, tenta novamente com instrução mais forte
        print(f"[WARN] llm_interpret devolveu JSON — a retentar com prompt reforçado")
        retry_prompt = (
            f"Responde em português, em frases simples, SEM JSON:\n\n{prompt}"
        )
        response2 = await asyncio.to_thread(
            client.chat,
            model=ROUTING_MODEL,
            messages=[{"role": "user", "content": retry_prompt}],
        )
        return response2["message"]["content"].strip()

    return raw
