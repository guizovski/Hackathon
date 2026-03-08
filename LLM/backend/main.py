"""
CLIP Chatbot Backend
====================
Ponto de entrada da aplicação FastAPI.
Contém: setup, endpoints e lógica ICS.
Toda a lógica de acesso ao CLIP, parsers e LLM está nos módulos:
  config.py · models.py · parsers.py · clip.py · tejo.py
"""

import uuid
import base64
import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from bs4 import BeautifulSoup

from config import (
    SITEMAP, REAL_PARAMS, CURRENT_PERIOD, SITEMAP_STUDENT_ID,
    CLIP_BASE_URL, substitute_student_id, _unquote,
)
from models import ChatRequest, ChatResponse, ExportICSRequest, FetchRequest
from parsers import (
    parse_horario_clip, format_horario,
    parse_testes_inscricao, format_testes_inscricao,
    parse_testes_disponiveis_com_valores, submit_inscricao_teste_clip,
    parse_resultados_clip, format_resultados,
    parse_resumo, format_resumo,
    parse_propinas, format_propinas,
)
from clip import (
    fetch_clip_route, detect_student_id,
    _follow_testes_period, _follow_horario_period, _follow_resultados_period,
    _resultados_sem_num, _match_test_fuzzy,
)
from tejo import llm_decide_routes, llm_interpret


# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(title="CLIP Chatbot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── ICS ───────────────────────────────────────────────────────────────────────
_ICS_DAY_OFFSET = {
    "Segunda": 0, "Terça": 1, "Quarta": 2,
    "Quinta": 3, "Sexta": 4, "Sábado": 5, "Domingo": 6,
}


def generate_ics(aulas: list[dict]) -> bytes:
    """Gera um ficheiro ICS com o horário semanal recorrente (15 semanas)."""
    today = datetime.date.today()
    days_to_monday = (7 - today.weekday()) % 7 or 7
    anchor_monday = today + datetime.timedelta(days=days_to_monday)
    dtstamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//CLIP Chatbot//CLIP ICS Export//PT",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Horário CLIP",
        "X-WR-TIMEZONE:Europe/Lisbon",
    ]

    for aula in aulas:
        offset = _ICS_DAY_OFFSET.get(aula.get("dia", ""), 0)
        first_day = anchor_monday + datetime.timedelta(days=offset)
        date_str = first_day.strftime("%Y%m%d")

        def _parse_hm(t: str):
            parts = t.split(":")
            return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0

        h0, m0 = _parse_hm(aula.get("hora", "0:00"))
        h1, m1 = _parse_hm(aula.get("hora_fim") or aula.get("hora", "0:00"))
        if h1 * 60 + m1 <= h0 * 60 + m0:
            h1, m1 = h0 + 1, m0

        dtstart = f"{date_str}T{h0:02d}{m0:02d}00"
        dtend   = f"{date_str}T{h1:02d}{m1:02d}00"

        summary  = aula.get("cadeira", "Aula")
        turno    = aula.get("turno", "")
        if turno:
            summary += f" ({turno})"
        summary  = summary.replace(",", "\\,").replace(";", "\\;")
        location = (aula.get("sala") or "").replace(",", "\\,").replace(";", "\\;")

        lines += [
            "BEGIN:VEVENT",
            f"UID:{uuid.uuid4()}@clip.fct.unl.pt",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART;TZID=Europe/Lisbon:{dtstart}",
            f"DTEND;TZID=Europe/Lisbon:{dtend}",
            "RRULE:FREQ=WEEKLY;COUNT=15",
            f"SUMMARY:{summary}",
        ]
        if location:
            lines.append(f"LOCATION:{location}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines).encode("utf-8")


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "routes_loaded": len(SITEMAP.get("routes", [])),
        "params_detected": REAL_PARAMS,
    }


@app.get("/sitemap")
async def get_sitemap():
    return SITEMAP


@app.post("/fetch")
async def fetch_debug(req: FetchRequest):
    """Debug: devolve o texto extraído das tabelas que seria enviado ao LLM."""
    html = await fetch_clip_route(req.url, req.session_cookie)
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "meta", "link"]):
        tag.decompose()

    table_texts = []
    for i, table in enumerate(soup.find_all("table")):
        headers = []
        header_row = table.find("tr")
        if header_row:
            headers = [th.get_text(strip=True) for th in header_row.find_all(["th", "td"])]
        rows = []
        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)
        table_texts.append({"index": i, "headers": headers, "rows": rows[:20]})

    return {"tables": table_texts, "num_tables": len(table_texts)}


@app.post("/export/ics")
async def export_ics(req: ExportICSRequest):
    """Gera e devolve um ficheiro ICS com o horário do aluno."""
    student_id = req.student_id
    if not student_id:
        student_id = await detect_student_id(req.session_cookie)
    if not student_id:
        student_id = SITEMAP_STUDENT_ID

    horario_url = None
    for r in SITEMAP.get("routes", []):
        url = r.get("example_url", "")
        if "hor%E1rio" in url or "horário" in _unquote(url):
            horario_url = substitute_student_id(url, student_id) if student_id else url
            break
    if not horario_url:
        raise HTTPException(status_code=404, detail="Rota de horário não encontrada no sitemap.")

    html = await fetch_clip_route(horario_url, req.session_cookie)
    followed_html, _ = await _follow_horario_period(html, req.session_cookie)
    if followed_html:
        html = followed_html

    aulas = parse_horario_clip(html)
    if not aulas:
        raise HTTPException(status_code=404, detail="Não foi possível extrair o horário.")

    ics_bytes = generate_ics(aulas)
    return Response(
        content=ics_bytes,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=horario_clip.ics"},
    )


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    if not req.session_cookie:
        raise HTTPException(status_code=400, detail="session_cookie é obrigatório.")

    # 0. Resolve student ID
    student_id = req.student_id
    if not student_id:
        student_id = await detect_student_id(req.session_cookie)
        if student_id:
            print(f"[INFO] Student ID auto-detected: {student_id}")
        else:
            student_id = SITEMAP_STUDENT_ID
            print(f"[WARN] Could not detect student ID, falling back to sitemap default")

    # 1. Tejo decide intenção e rotas
    decision = await llm_decide_routes(req.question, req.conversation_history, student_id)
    intent = decision.get("intent", "read")
    print(f"[INFO] Tejo → intent={intent}, rotas={decision.get('routes_to_fetch', [])}")
    print(f"[INFO] Reasoning: {decision.get('reasoning', '')}")

    # ── enroll_test ───────────────────────────────────────────────────────────
    if intent == "enroll_test":
        inscricao_cadeira = decision.get("cadeira", "")
        testes_route = None
        for r in SITEMAP.get("routes", []):
            url = r.get("example_url", "")
            if "testes_de_avalia" in _unquote(url):
                testes_route = substitute_student_id(url, student_id)
                break
        if not testes_route:
            return ChatResponse(answer="Não encontrei a rota de testes no sitemap.", routes_consulted=[])
        try:
            html = await fetch_clip_route(testes_route, req.session_cookie)
            html = await _follow_testes_period(testes_route, html, req.session_cookie) or html
            testes, action_url, submit_name = parse_testes_disponiveis_com_valores(html)

            if not testes:
                return ChatResponse(
                    answer="Não há testes disponíveis para inscrição de momento.",
                    routes_consulted=[testes_route],
                )

            matches = _match_test_fuzzy(inscricao_cadeira if inscricao_cadeira != "__todos__" else "", testes)

            # Inscrição em TODOS os testes disponíveis
            if inscricao_cadeira == "__todos__":
                resultados = []
                for test in testes:
                    ok, resp_html = await submit_inscricao_teste_clip(
                        action_url, test["value"], submit_name, student_id, req.session_cookie
                    )
                    estado = "✓" if ok else "✗"
                    resultados.append(f"  {estado} {test['cadeira']} — {test['avaliacao']} ({test['data']})")
                    if ok:
                        _, action_url, submit_name = parse_testes_disponiveis_com_valores(resp_html)
                lines = ["Inscrições submetidas:"]
                lines.extend(resultados)
                return ChatResponse(answer="\n".join(lines), routes_consulted=[testes_route])

            if not matches:
                available = "\n".join(f"  • {t['cadeira']} — {t['avaliacao']} ({t['data']})" for t in testes)
                return ChatResponse(
                    answer=f"Não encontrei nenhum teste disponível para '{inscricao_cadeira}'.\n\nTestes disponíveis:\n{available}",
                    routes_consulted=[testes_route],
                )
            if len(matches) > 1 and inscricao_cadeira:
                options = "\n".join(f"  • {t['cadeira']} — {t['avaliacao']} ({t['data']})" for t in matches)
                return ChatResponse(
                    answer=f"Há mais do que um teste correspondente. A qual te referes?\n\n{options}",
                    routes_consulted=[testes_route],
                )

            test = matches[0]
            ok, resp_html = await submit_inscricao_teste_clip(
                action_url, test["value"], submit_name, student_id, req.session_cookie
            )
            if ok:
                data = parse_testes_inscricao(resp_html)
                inscritos = data.get("inscritos", []) if data else []
                confirmado = any(test["cadeira"].lower() in i["cadeira"].lower() for i in inscritos)
                if confirmado:
                    return ChatResponse(
                        answer=f"Inscrito com sucesso em {test['cadeira']} — {test['avaliacao']} ({test['data']}).",
                        routes_consulted=[testes_route],
                    )
                return ChatResponse(
                    answer=f"Pedido submetido para {test['cadeira']} — {test['avaliacao']}. Verifica no CLIP para confirmar.",
                    routes_consulted=[testes_route],
                )
            return ChatResponse(
                answer=f"Não foi possível submeter a inscrição em {test['cadeira']}. O servidor CLIP não respondeu como esperado.",
                routes_consulted=[testes_route],
            )
        except HTTPException:
            raise
        except Exception as e:
            return ChatResponse(answer=f"Erro ao tentar inscrever: {e}", routes_consulted=[])

    # ── unsupported_action ────────────────────────────────────────────────────
    elif intent == "unsupported_action":
        return ChatResponse(
            answer="Não consigo realizar acções no CLIP em teu nome (submeter formulários, "
                   "inscrever, cancelar, etc.) — apenas consigo ler e apresentar informação.",
            routes_consulted=[],
        )

    # ── ics_export ────────────────────────────────────────────────────────────
    elif intent == "ics_export":
        export_req = ExportICSRequest(session_cookie=req.session_cookie, student_id=student_id)
        try:
            ics_resp = await export_ics(export_req)
            ics_b64 = base64.b64encode(ics_resp.body).decode()
            return ChatResponse(
                answer="Aqui está o teu horário exportado para formato .ics. "
                       "Podes importá-lo no Google Calendar, Apple Calendar, Outlook ou qualquer app de calendário.",
                routes_consulted=[],
                ics_data=ics_b64,
            )
        except HTTPException as e:
            return ChatResponse(answer=f"Não consegui gerar o ficheiro ICS: {e.detail}", routes_consulted=[])

    # ── read ──────────────────────────────────────────────────────────────────
    routes_to_fetch = decision.get("routes_to_fetch", [])
    if not routes_to_fetch:
        return ChatResponse(
            answer="Não consegui identificar que secção do CLIP tem essa informação. Podes reformular a pergunta?",
            routes_consulted=[],
        )

    # 2. Vai buscar cada rota ao CLIP
    fetched_content = {}
    for route in routes_to_fetch[:3]:
        try:
            route = substitute_student_id(route, student_id)
            html  = await fetch_clip_route(route, req.session_cookie)

            if "hor%E1rio" in route or "horário" in _unquote(route):
                followed_html, followed_url = await _follow_horario_period(html, req.session_cookie)
                if followed_html:
                    html, route = followed_html, followed_url

            elif "ano_lectivo/resultados" in _unquote(route):
                followed_html, followed_url = await _follow_resultados_period(html, req.session_cookie, req.question)
                if followed_html:
                    html, route = followed_html, followed_url

            elif "inscri" in route and "testes_de_avalia" in route:
                html = await _follow_testes_period(route, html, req.session_cookie) or html

            elif "pagamentos_acad" in _unquote(route) and "dados_para_pagamento" in _unquote(route) and "ano_lectivo" not in route:
                sep   = "&" if "?" in route else "?"
                route = (
                    f"{route}{sep}ano_lectivo={CURRENT_PERIOD['ano_lectivo']}"
                    f"&tipo_de_per%EDodo_escolar=a&per%EDodo_escolar=1"
                )
                html = await fetch_clip_route(route, req.session_cookie)

            fetched_content[route] = html
        except HTTPException:
            raise
        except Exception as e:
            fetched_content[route] = f"[Erro ao aceder rota: {e}]"

    # 3. Interpreta e responde (parser dedicado quando disponível, LLM como fallback)
    direct_answer = None
    for route, html in fetched_content.items():
        if "ano_lectivo/resultados" in _unquote(route):
            entries = parse_resultados_clip(html)
            if entries:
                direct_answer = format_resultados(entries, _resultados_sem_num(req.question))
                break
        elif "hor%E1rio" in route or "horário" in _unquote(route):
            aulas = parse_horario_clip(html)
            if aulas:
                direct_answer = format_horario(aulas, req.question)
                break
        elif "testes_de_avalia" in route or "testes_de_avalia" in _unquote(route):
            testes = parse_testes_inscricao(html)
            if testes:
                direct_answer = format_testes_inscricao(testes)
                break
        elif "pagamentos_acad" in _unquote(route):
            prestacoes = parse_propinas(html)
            if prestacoes:
                direct_answer = format_propinas(prestacoes, req.question)
                break
        elif "situa%E7%E3o/resumo" in route or "situação/resumo" in _unquote(route):
            resumo = parse_resumo(html)
            if resumo:
                direct_answer = format_resumo(resumo, req.question)
                break

    if direct_answer:
        answer = direct_answer
    elif len(fetched_content) == 1:
        route, html = next(iter(fetched_content.items()))
        answer = await llm_interpret(req.question, html, route)
    else:
        combined = "\n\n---\n\n".join(f"[Rota: {r}]\n{h}" for r, h in fetched_content.items())
        answer   = await llm_interpret(req.question, combined, ", ".join(fetched_content.keys()))

    first_html = next(iter(fetched_content.values()), "")
    soup       = BeautifulSoup(first_html, "html.parser")
    preview    = soup.get_text(strip=True)[:200] if first_html else None

    return ChatResponse(
        answer=answer,
        routes_consulted=list(fetched_content.keys()),
        raw_data_preview=preview,
    )
