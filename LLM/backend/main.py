"""
CLIP Chatbot Backend
====================
Recebe perguntas dos utilizadores, usa o sitemap para decidir
que rotas do CLIP consultar, faz os requests com o cookie do utilizador
e retorna uma resposta interpretada pelo LLM.
"""

import os
import json
import re
import uuid
import base64
import asyncio
import datetime
import httpx

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from urllib.parse import urlparse, parse_qs, unquote, quote

def _unquote(url: str) -> str:
    """Decodifica URLs do CLIP que usam Latin-1 (não UTF-8)."""
    return unquote(url, encoding='latin-1')
from bs4 import BeautifulSoup
import ollama as ollama_client

# ── Config ───────────────────────────────────────────────────────────────────
OLLAMA_URL     = os.getenv("OLLAMA_URL", "http://ollama:11434")
CLIP_BASE_URL  = os.getenv("CLIP_BASE_URL", "https://clip.fct.unl.pt")
SITEMAP_PATH   = os.getenv("SITEMAP_PATH", "/app/data/sitemap.json")
ROUTING_MODEL  = os.getenv("ROUTING_MODEL", os.getenv("LLM_MODEL", "tejo"))
INTERPRET_MODEL = os.getenv("INTERPRET_MODEL", "qwen2.5:7b")

app = FastAPI(title="CLIP Chatbot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Carrega sitemap ───────────────────────────────────────────────────────────
def load_sitemap() -> dict:
    if not os.path.exists(SITEMAP_PATH):
        raise RuntimeError(f"Sitemap não encontrado em {SITEMAP_PATH}.")
    with open(SITEMAP_PATH, encoding="utf-8") as f:
        return json.load(f)

try:
    SITEMAP = load_sitemap()
    total = SITEMAP.get("total_routes") or SITEMAP.get("total_patterns", 0)
    print(f"[OK] Sitemap carregado — {total} rotas disponíveis")
except RuntimeError as e:
    print(f"[WARN] {e}")
    SITEMAP = {"routes": [], "base_url": CLIP_BASE_URL}


# ── Extrai params reais das example_urls do sitemap ───────────────────────────
def extract_real_params_from_sitemap() -> dict:
    """
    Percorre as example_urls do sitemap e extrai valores reais de parâmetros
    como aluno, ano_lectivo, instituição, etc.
    Retorna um dict com os valores mais comuns encontrados.
    """
    params_found = {}
    for route in SITEMAP.get("routes", []):
        example_url = route.get("example_url", "")
        if not example_url:
            continue
        parsed = urlparse(example_url)
        # Query params (ex: ?aluno=124344&ano_lectivo=2025)
        qs = parse_qs(parsed.query)
        for key, values in qs.items():
            if key not in params_found:
                params_found[key] = values[0]
        # Path segments numéricos (ex: /aluno/2025/124344/horario)
        segments = parsed.path.strip("/").split("/")
        for i, seg in enumerate(segments):
            if seg.isdigit() and len(seg) >= 4:
                # Tenta inferir o que é pelo contexto
                prev = segments[i-1] if i > 0 else ""
                if "ano" in prev or len(seg) == 4:
                    params_found.setdefault("ano_lectivo", seg)
                elif len(seg) >= 5:
                    params_found.setdefault("aluno", seg)
    return params_found

REAL_PARAMS = extract_real_params_from_sitemap()
print(f"[OK] Params reais extraídos do sitemap: {REAL_PARAMS}")


# ── Período lectivo actual ────────────────────────────────────────────────────
def get_current_academic_period() -> dict:
    """
    Determina o período lectivo actual com base na data de hoje.
    Calendário escolar português (FCT-UNL):
      Set–Jan  → 1º semestre  (tipo=s, período=1)
      Fev–Jul  → 2º semestre  (tipo=s, período=2)
    O ano_lectivo no CLIP é o ano de FIM do ano escolar (ex: 2025/26 → 2026).
    """
    today = datetime.date.today()
    month = today.month
    year  = today.year
    if month >= 9:           # Set–Dez: início do 1º semestre do próximo ano lectivo
        ano_lectivo = year + 1
        periodo     = 1
    elif month == 1:         # Janeiro: ainda 1º semestre do ano lectivo actual
        ano_lectivo = year
        periodo     = 1
    else:                    # Fev–Jul: 2º semestre
        ano_lectivo = year
        periodo     = 2
    descricao = f"{ano_lectivo - 1}/{str(ano_lectivo)[2:]} — {periodo}º Semestre"
    print(f"[INFO] Período lectivo actual: {descricao} (ano={ano_lectivo}, período={periodo})")
    return {
        "ano_lectivo":             str(ano_lectivo),
        "tipo_de_período_lectivo": "s",
        "período_lectivo":         str(periodo),
        "descricao":               descricao,
    }

CURRENT_PERIOD = get_current_academic_period()

# Student ID baked into the scraped sitemap URLs (used as substitution placeholder)
SITEMAP_STUDENT_ID: str = REAL_PARAMS.get("aluno", "")


# ── Modelos Pydantic ──────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    question: str
    session_cookie: str
    student_id: str | None = None  # optional – auto-detected from session if omitted
    academic_year: str | None = None
    conversation_history: list[dict] = []


class ChatResponse(BaseModel):
    answer: str
    routes_consulted: list[str]
    raw_data_preview: str | None = None
    ics_data: str | None = None  # base64 do ficheiro .ics, quando gerado


class ExportICSRequest(BaseModel):
    session_cookie: str
    student_id: str | None = None


# ── ICS Export ────────────────────────────────────────────────────────────
_ICS_PATTERNS = re.compile(
    r"\b(exporta(r)?|guardar?|gravar?|download|descarreg|adicion|importa(r)?)"
    r".{0,20}(hor[aá]rio|calend[aá]rio|aulas?|schedule)"
    r"|ics|ical|\.ics|ficheiro.{0,10}calend[aá]rio"
    r"|calend[aá]rio.{0,10}(google|apple|outlook|phone)",
    re.IGNORECASE,
)

_ICS_DAY_OFFSET = {
    "Segunda": 0, "Terça": 1, "Quarta": 2,
    "Quinta": 3, "Sexta": 4, "Sábado": 5, "Domingo": 6,
}


def generate_ics(aulas: list[dict]) -> bytes:
    """Gera um ficheiro ICS com o horário semanal recorrente."""
    today = datetime.date.today()
    # Ponto de referência: próxima segunda-feira (ou hoje se for segunda)
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

        # Interpreta hora de início e fim
        def _parse_hm(t: str):
            parts = t.split(":")
            return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0

        h0, m0 = _parse_hm(aula.get("hora", "0:00"))
        h1, m1 = _parse_hm(aula.get("hora_fim") or aula.get("hora", "0:00"))
        # Garante que hora fim > hora início
        if h1 * 60 + m1 <= h0 * 60 + m0:
            h1, m1 = h0 + 1, m0

        dtstart = f"{date_str}T{h0:02d}{m0:02d}00"
        dtend   = f"{date_str}T{h1:02d}{m1:02d}00"

        summary = aula.get("cadeira", "Aula")
        turno = aula.get("turno", "")
        if turno:
            summary += f" ({turno})"
        # Escapa vírgulas e ponto-e-vírgulas no texto (RFC 5545)
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


@app.post("/export/ics")
async def export_ics(req: ExportICSRequest):
    """Gera e devolve um ficheiro ICS com o horário do aluno."""
    student_id = req.student_id
    if not student_id:
        student_id = await detect_student_id(req.session_cookie)
    if not student_id:
        student_id = SITEMAP_STUDENT_ID

    # Encontra a URL do horário no sitemap
    horario_url = None
    for r in SITEMAP.get("routes", []):
        url = r.get("example_url", "")
        if "hor%E1rio" in url or "horário" in _unquote(url):
            horario_url = substitute_student_id(url, student_id) if student_id else url
            break
    if not horario_url:
        raise HTTPException(status_code=404, detail="Rota de horário não encontrada no sitemap.")

    html = await fetch_clip_route(horario_url, req.session_cookie)
    followed_html, followed_url = await _follow_horario_period(html, req.session_cookie)
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


# ── System prompt ─────────────────────────────────────────────────────────────

def build_focused_prompt(question: str, routes: list[dict], student_id: str | None = None) -> str:
    """Constrói um prompt compacto com rotas deduplicas por path."""
    seen_paths: set[str] = set()
    lines = []
    for r in routes:
        url = r.get("example_url", "")
        if not url:
            continue
        if student_id:
            url = substitute_student_id(url, student_id)
        decoded_path = _unquote(urlparse(url).path)
        # Deduplica: um path pode aparecer várias vezes com params de período diferentes
        if decoded_path in seen_paths:
            continue
        seen_paths.add(decoded_path)
        dtype = r.get("data_type", "")
        label = f"[{dtype}] " if dtype and dtype != "unknown" else ""
        lines.append(f"- {label}{decoded_path}\n  URL: {url}")

    routes_text = "\n".join(lines) if lines else "Sem rotas disponíveis."

    return f"""És um assistente do portal académico CLIP da FCT-UNL.
Responde APENAS com JSON, sem texto extra:
{{"routes_to_fetch": ["url_completa"], "reasoning": "breve"}}

Contexto actual: hoje é {datetime.date.today().strftime('%d/%m/%Y')} — {CURRENT_PERIOD['descricao']} (ano_lectivo={CURRENT_PERIOD['ano_lectivo']}, período={CURRENT_PERIOD['período_lectivo']}).
Quando escolheres uma URL, prefere as que correspondem ao período lectivo actual.

Rotas disponíveis para esta pergunta:
{routes_text}

Pergunta: {question}
Escolhe a(s) URL(s) mais relevante(s) exatamente como aparecem acima."""


def parse_horario_clip(html: str) -> list[dict] | None:
    """
    Extrai aulas da página de horário do CLIP.

    O CLIP renderiza cada aula como um <td class="celulaDeCalendario"> com rowspan.
    Dentro de cada célula há um link com parâmetros dia=N e in%EDcio=HHMM que
    identificam o dia e a hora de início — muito mais fiável do que tentar ler
    linhas/colunas da tabela (que têm colspan/rowspan complexos).

    dia=2→Segunda, 3→Terça, 4→Quarta, 5→Quinta, 6→Sexta, 7→Sábado, 8→Domingo
    """
    soup = BeautifulSoup(html, "html.parser")

    DIA_MAP = {
        "2": "Segunda", "3": "Terça", "4": "Quarta",
        "5": "Quinta",  "6": "Sexta", "7": "Sábado", "8": "Domingo",
    }

    aulas = []
    seen = set()  # evita duplicados (mesmo dia+hora+cadeira)

    for td in soup.find_all("td", class_=lambda c: c and "celulaDeCalendario" in c.split()):
        nome_completo = td.get("title", "").strip()

        link = td.find("a", href=True)
        if not link:
            continue
        href = link["href"]

        m_dia = re.search(r"[?&]dia=(\d+)", href)
        m_inicio = re.search(r"in(?:%ED|i)(?:%E7|c)io=(\d+)", href, re.IGNORECASE)
        if not m_inicio:
            m_inicio = re.search(r"in\w+cio=(\d+)", href)

        if not m_dia:
            continue

        dia = DIA_MAP.get(m_dia.group(1), f"Dia{m_dia.group(1)}")

        if m_inicio:
            raw = m_inicio.group(1).zfill(4)  # "800" → "0800"
            hora_inicio_h = int(raw[:-2])
            hora_inicio_m = int(raw[-2:])
        else:
            hora_inicio_h, hora_inicio_m = 0, 0

        # Duração: cada par de linhas HTML = 1 hora → rowspan / 2
        try:
            rowspan = int(td.get("rowspan", 2))
        except (ValueError, TypeError):
            rowspan = 2
        duracao_h = rowspan // 2
        duracao_min = (rowspan % 2) * 30

        hora_fim_h = hora_inicio_h + duracao_h + (hora_inicio_m + duracao_min) // 60
        hora_fim_m = (hora_inicio_m + duracao_min) % 60

        hora_str = f"{hora_inicio_h}:{hora_inicio_m:02d}"
        hora_fim_str = f"{hora_fim_h}:{hora_fim_m:02d}"

        turno = link.get_text(strip=True)

        # Sala: texto no <div> excluindo <b> e <a>
        sala = ""
        div = td.find("div")
        if div:
            parts = []
            for node in div.children:
                if getattr(node, "name", None) is None:
                    t = str(node).strip()
                    if t:
                        parts.append(t)
                elif node.name not in ("b", "a", "br"):
                    t = node.get_text(strip=True)
                    if t:
                        parts.append(t)
            sala = " ".join(parts).strip()

        abrev = td.find("b").get_text(strip=True) if td.find("b") else ""

        key = (dia, hora_str, nome_completo or abrev)
        if key in seen:
            continue
        seen.add(key)

        aulas.append({
            "dia": dia,
            "hora": hora_str,
            "hora_fim": hora_fim_str,
            "hora_sort": hora_inicio_h * 60 + hora_inicio_m,  # para ordenação numérica
            "cadeira": nome_completo or abrev,
            "abrev": abrev,
            "turno": turno,
            "sala": sala,
        })

    return sorted(aulas, key=lambda a: (a["dia"], a["hora_sort"])) if aulas else None


def format_horario(aulas: list[dict], question: str) -> str:
    """Formata as aulas em texto legível, filtrando por dia se mencionado na pergunta."""
    q = question.lower()
    DAY_KEYWORDS = {
        "segunda": "Segunda", "2ª": "Segunda",
        "terça":   "Terça",   "3ª": "Terça",
        "quarta":  "Quarta",  "4ª": "Quarta",
        "quinta":  "Quinta",  "5ª": "Quinta",
        "sexta":   "Sexta",   "6ª": "Sexta",
        "sábado":  "Sábado",
    }
    target_day = next((v for k, v in DAY_KEYWORDS.items() if k in q), None)

    # Agrupa por dia e ordena numericamente pelo campo hora_sort
    by_day: dict[str, list] = {}
    for a in aulas:
        day = a["dia"]
        if target_day and day != target_day:
            continue
        by_day.setdefault(day, []).append(a)

    if not by_day:
        suffix = f" à {target_day}-feira" if target_day and target_day not in ("Sábado", "Domingo") else (f" ao {target_day}" if target_day else "")
        return f"Não encontrei aulas{suffix} no horário."

    DAY_ORDER = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
    lines = []
    for day in DAY_ORDER:
        if day not in by_day:
            continue
        label = f"{day}-feira" if day not in ("Sábado", "Domingo") else day
        lines.append(f"\n{label}:")
        for a in sorted(by_day[day], key=lambda x: x.get("hora_sort", 0)):
            hora_fim = a.get("hora_fim", "")
            intervalo = f"{a['hora']}–{hora_fim}" if hora_fim and hora_fim != a["hora"] else a["hora"]
            sala = f" — {a['sala']}" if a["sala"] else ""
            lines.append(f"  {intervalo}  {a['cadeira']} ({a['turno']}){sala}")
    return "Horário de aulas:\n" + "\n".join(lines)


# ── Parser dedicado para página de inscrição em testes do CLIP ───────────────
_DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4}')


def _parse_testes_table(table) -> list[dict]:
    """Extrai linhas de dados de uma tabela de testes (disponíveis ou inscritos).
    Cada linha válida tem: cadeira | avaliação | data (com dígitos) | sala opcional.
    Rows with checkboxes: [<td><input checkbox>] cadeira | avaliação | data | sala
    """
    entries = []
    for tr in table.find_all("tr", recursive=False):
        # evitar sub-tabelas: só trs directos
        cells = tr.find_all("td")
        # Remove tds que contenham inputs (checkbox / submit)
        tds = [td for td in cells if not td.find("input")]
        if len(tds) < 3:
            continue
        texts = [td.get_text(" ", strip=True) for td in tds]
        cadeira = texts[0].strip()
        # Filtra headers e linhas de navegação: sem dígito na coluna data (índice 2)
        if not cadeira or not _DATE_RE.search(texts[2] if len(texts) > 2 else ""):
            continue
        # Remove marcadores automáticos do CLIP ex: "Sistemas Distribuídos (*)"
        cadeira = cadeira.replace(" (*)", "").strip()
        avaliacao = texts[1] if len(texts) > 1 else ""
        data = texts[2] if len(texts) > 2 else ""
        sala = texts[3] if len(texts) > 3 else ""
        entries.append({"cadeira": cadeira, "avaliacao": avaliacao, "data": data, "sala": sala})
    return entries


def parse_testes_inscricao(html: str) -> dict | None:
    """
    Separa as duas tabelas da página de inscrição em testes:
      - disponíveis: tabela com botão "Inscrever"
      - inscritos:   tabela com botão "Anular"
    Usa find_parent("table") no botão para garantir que apanha a tabela exacta.
    """
    soup = BeautifulSoup(html, "html.parser")

    disponíveis = []
    inscritos = []

    inscrever_btn = soup.find("input", attrs={"type": "submit", "value": re.compile(r"nscrever", re.I)})
    if inscrever_btn:
        tbl = inscrever_btn.find_parent("table")
        if tbl:
            disponíveis = _parse_testes_table(tbl)

    anular_btn = soup.find("input", attrs={"type": "submit", "value": re.compile(r"nular", re.I)})
    if anular_btn:
        tbl = anular_btn.find_parent("table")
        if tbl:
            inscritos = _parse_testes_table(tbl)

    if not disponíveis and not inscritos:
        return None

    return {"disponíveis": disponíveis, "inscritos": inscritos}


def format_testes_inscricao(data: dict) -> str:
    """Formata o resultado de parse_testes_inscricao em texto legível."""
    lines = []

    disponíveis = data.get("disponíveis", [])
    inscritos = data.get("inscritos", [])

    if disponíveis:
        lines.append("Testes disponíveis para inscrição:")
        for t in disponíveis:
            sala = f" | Salas: {t['sala']}" if t["sala"] else ""
            lines.append(f"  • {t['cadeira']} — {t['avaliacao']}: {t['data']}{sala}")
    else:
        lines.append("Não há testes disponíveis para inscrição de momento.")

    if inscritos:
        lines.append("\nInscrições já realizadas:")
        for t in inscritos:
            lines.append(f"  ✓ {t['cadeira']} — {t['avaliacao']}: {t['data']}")

    return "\n".join(lines)


def parse_testes_disponiveis_com_valores(html: str) -> tuple[list[dict], str, str]:
    """
    Extrai os testes disponíveis para inscrição (tabela esquerda).
    Inclui o valor do checkbox de cada teste e o URL de action + nome do botão submit.
    Devolve (testes, action_url, submit_name).
    """
    soup = BeautifulSoup(html, "html.parser")
    testes: list[dict] = []
    action_url = ""
    submit_name = "submit:ep:p1"

    inscrever_btn = soup.find("input", attrs={"type": "submit", "value": re.compile(r"nscrever", re.I)})
    if not inscrever_btn:
        return testes, action_url, submit_name

    submit_name = inscrever_btn.get("name", "submit:ep:p1")
    form = inscrever_btn.find_parent("form")
    if not form:
        return testes, action_url, submit_name

    raw_action = form.get("action", "")
    action_url = raw_action if raw_action.startswith("http") else f"{CLIP_BASE_URL}{raw_action}"

    for tr in form.find_all("tr"):
        checkbox = tr.find("input", attrs={"type": "checkbox"})
        if not checkbox:
            continue
        cells = tr.find_all("td")
        texts = [td.get_text(" ", strip=True) for td in cells]
        # cells[0] = td com checkbox, cells[1..] = cadeira, avaliação, data, salas
        cadeira = texts[1].replace(" (*)", "").strip() if len(texts) > 1 else ""
        if not cadeira:
            continue
        testes.append({
            "cadeira": cadeira,
            "avaliacao": texts[2] if len(texts) > 2 else "",
            "data": texts[3] if len(texts) > 3 else "",
            "salas": texts[4] if len(texts) > 4 else "",
            "value": checkbox.get("value", ""),
        })

    return testes, action_url, submit_name


async def submit_inscricao_teste_clip(
    action_url: str,
    test_value: str,
    submit_name: str,
    student_id: str | None,
    cookie: str,
) -> tuple[bool, str]:
    """
    Submete a inscrição num teste via POST ao CLIP.
    O payload é codificado em Latin-1 (charset do CLIP).
    Devolve (sucesso, html_resposta).
    """
    if student_id:
        action_url = substitute_student_id(action_url, student_id)
    if not action_url.startswith("http"):
        action_url = f"{CLIP_BASE_URL}{action_url}"

    # Codifica o nome do campo e valor em Latin-1 percent-encoded
    # "+teste_para_inscrição" tem +, ç, ã que precisam de codificação correcta
    field_name = "+teste_para_inscrição"
    k = quote(field_name.encode("latin-1", "replace"), safe="")
    v = quote(str(test_value).encode("latin-1", "replace"), safe="")
    btn = quote(submit_name.encode("latin-1", "replace"), safe="")
    payload = f"{k}={v}&{btn}=Inscrever".encode("ascii")

    headers = {
        "Cookie": cookie,
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": action_url,
        "User-Agent": "Mozilla/5.0",
    }
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.post(action_url, content=payload, headers=headers)
        return resp.status_code == 200, resp.text


def _is_numeric(value: str) -> bool:
    try:
        float(value.replace(",", "."))
        return True
    except (ValueError, AttributeError):
        return False


def parse_resultados_clip(html: str) -> list[dict] | None:
    """
    Extrai notas da página /ano_lectivo/resultados do CLIP.
    Cada linha de dados tem 8 colunas:
      [cadeira, a, Esp., Nor., Rec., Ext., Resultado, Créditos ECTS]
    As notas (0-20) estão nas colunas 3 (Normal) e 4 (Recurso).
    'R' = Reprovado no exame (sem nota numérica), 'A' = Admitido.
    Devolve lista de dicts com chaves: secção, cadeira, nota_normal, nota_recurso, resultado, creditos
    """
    soup = BeautifulSoup(html, "html.parser")
    resultado_values = {"Aprovado", "Reprovado", "Não avaliado", "Admitido", "N\u00e3o avaliado"}

    entries = []
    current_section = "Resultados"

    for table in soup.find_all("table"):
        header_cells = table.find("tr")
        if not header_cells:
            continue
        header_text = header_cells.get_text(strip=True)

        # Detecta cabeçalho de secção (ex: "1º Sem", "2º Sem", "2º Tri")
        if any(k in header_text for k in ["Sem", "Tri", "semestre", "trimestre"]):
            current_section = header_text.split("Aprovação")[0].strip()
            continue

        # Procura linhas com 8 células onde a 7ª (índice 6) é um estado de resultado
        for tr in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all(["th", "td"])]
            if len(cells) == 8 and cells[6] in resultado_values:
                nota_nor = cells[3]  # época normal
                nota_rec = cells[4]  # época de recurso
                entries.append({
                    "secção": current_section,
                    "cadeira": cells[0],
                    "nota_normal": nota_nor if _is_numeric(nota_nor) else None,
                    "nota_recurso": nota_rec if _is_numeric(nota_rec) else None,
                    "resultado": cells[6],
                    "creditos_ects": cells[7],
                })

    return entries if entries else None


def format_resultados(entries: list[dict], sem_filter: int | None = None) -> str:
    """Formata os resultados do CLIP em texto legível.
    sem_filter: se não None, mostra apenas a secção correspondente ao nº semestre (1, 2, …).
    """
    if sem_filter is not None:
        # Filtra secções que contenham o número do semestre (ex: '1º Sem', '2º Sem')
        sem_labels = {str(sem_filter), f"{sem_filter}º", f"{sem_filter}º sem", f"{sem_filter}o sem"}
        entries = [
            e for e in entries
            if any(lbl in e["secção"].lower() for lbl in sem_labels)
        ]
    if not entries:
        return f"Não encontrei notas para o {sem_filter}º semestre nesta página."
    lines = ["As tuas notas (escala 0-20):\n"]
    current_sec = None
    for e in entries:
        if e["secção"] != current_sec:
            current_sec = e["secção"]
            lines.append(f"\n{current_sec}:")
        nota = e["nota_recurso"] or e["nota_normal"]
        nota_str = f"{nota}/20" if nota else "sem nota numérica"
        lines.append(f"  - {e['cadeira']}: {nota_str} ({e['resultado']}), {e['creditos_ects']} ECTS")
    return "\n".join(lines)


# ── Parser dedicado para página de resumo académico ─────────────────────────────
def parse_resumo(html: str) -> dict | None:
    """
    Extrai da página situação/resumo:
    - Créditos exigidos/obtidos
    - Média para conclusão do curso
    - Lista de unidades curriculares concluídas (nome, data, créditos, nota)
    """
    soup = BeautifulSoup(html, "html.parser")
    result = {"creditos_exigidos": None, "creditos_obtidos": None, "media": None, "unidades": []}

    # Extrai créditos e média das tabelas de sumário no topo
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for i, tr in enumerate(rows):
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            # Linha com "Exigidos" / "Obtidos"
            if "Exigidos" in cells and "Obtidos" in cells:
                data_row = rows[i + 1] if i + 1 < len(rows) else None
                if data_row:
                    vals = [td.get_text(strip=True) for td in data_row.find_all(["td", "th"])]
                    if len(vals) >= 2:
                        result["creditos_exigidos"] = vals[0].replace(" (ECTS)", "").strip()
                        result["creditos_obtidos"] = vals[1].replace(" (ECTS)", "").strip()
            # Linha com label de média
            for cell in cells:
                if "dia para efeitos" in cell.lower() or "média" in cell.lower():
                    # próxima célula na mesma linha ou linha seguinte
                    idx = cells.index(cell)
                    if idx + 1 < len(cells) and cells[idx + 1]:
                        result["media"] = cells[idx + 1]

    # Extrai tabela de unidades curriculares (colunas: Unidade | Data | Tipo obtenção | Tipo unidade | Créditos | Classificação)
    for table in soup.find_all("table"):
        header = table.find("tr")
        if not header:
            continue
        col_texts = [th.get_text(strip=True).lower() for th in header.find_all(["th", "td"])]
        if "unidade" in col_texts and "classificação" in col_texts:
            cred_idx  = next((i for i, c in enumerate(col_texts) if "créd" in c), None)
            class_idx = next((i for i, c in enumerate(col_texts) if "classif" in c), None)
            date_idx  = next((i for i, c in enumerate(col_texts) if "data" in c), None)
            for tr in table.find_all("tr")[1:]:
                cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                if not cells or not cells[0]:
                    continue
                unidade = cells[0]
                data    = cells[date_idx] if date_idx is not None and date_idx < len(cells) else ""
                cred    = cells[cred_idx] if cred_idx is not None and cred_idx < len(cells) else ""
                nota    = cells[class_idx] if class_idx is not None and class_idx < len(cells) else ""
                if nota or cred:
                    result["unidades"].append({"nome": unidade, "data": data, "creditos": cred, "nota": nota})

    if not result["unidades"] and not result["media"]:
        return None
    return result


def format_resumo(data: dict, question: str) -> str:
    """Formata o resumo académico de forma contextual à pergunta."""
    q = question.lower()
    lines = []

    # Sumário de créditos e média
    if data.get("media"):
        lines.append(f"Média para conclusão do curso: {data['media']}")
    if data.get("creditos_obtidos") and data.get("creditos_exigidos"):
        lines.append(f"Créditos ECTS: {data['creditos_obtidos']} obtidos / {data['creditos_exigidos']} exigidos")

    # Se só perguntou média/créditos, não listar todas as cadeiras
    only_summary = any(w in q for w in ["média", "media", "créditos", "ects", "creditos"]) and \
                   not any(w in q for w in ["notas", "cadeiras", "histórico", "concluí", "tive"])
    if only_summary:
        return "\n".join(lines) if lines else "Não foi possível obter o resumo académico."

    # Lista completa de cadeiras
    if data.get("unidades"):
        lines.append("\nCadeiras concluídas:")
        for u in data["unidades"]:
            nota_str = f"{u['nota']}/20" if u["nota"] and u["nota"].replace(".","").isdigit() else (u["nota"] or "—")
            cred_str = f"  ({u['creditos']} ECTS)" if u["creditos"] else ""
            data_str = f"  [{u['data']}]" if u["data"] else ""
            lines.append(f"  - {u['nome']}: {nota_str}{cred_str}{data_str}")

    return "\n".join(lines) if lines else "Não foi possível obter o resumo académico."


# ── Parser dedicado para página de propinas / dados_para_pagamento ───────────────
def parse_propinas(html: str) -> list[dict] | None:
    """
    Extrai as prestações da página dados_para_pagamento.
    Cada linha válida tem: descrição | montante_prestacao | juros_mora | montante_divida | data_limite.
    Uma prestação está em atraso se juros_mora > 0 OU se a data-limite já passou.
    """
    soup = BeautifulSoup(html, "html.parser")
    today = datetime.date.today()
    entries = []

    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 5:
                continue
            descricao, mont_prest, juros, mont_divida, data_limite = cells[:5]
            # Valida: data_limite tem de ser uma data e mont_divida numérico
            m_data = re.search(r"(\d{4}-\d{2}-\d{2}|\d{2}-\d{2}-\d{4}|\d{2}/\d{2}/\d{4})", data_limite)
            if not m_data:
                continue
            try:
                raw_d = m_data.group(1)
                if "-" in raw_d and len(raw_d) == 10 and raw_d[4] == "-":
                    prazo = datetime.date.fromisoformat(raw_d)
                elif "/" in raw_d:
                    d, mo, y = raw_d.split("/")
                    prazo = datetime.date(int(y), int(mo), int(d))
                else:
                    d, mo, y = raw_d.split("-")
                    prazo = datetime.date(int(y), int(mo), int(d))
            except (ValueError, TypeError):
                continue
            # Montante em dívida
            mont_num = re.sub(r"[^\d,\.]", "", mont_divida).replace(",", ".")
            try:
                valor_divida = float(mont_num) if mont_num else 0.0
            except ValueError:
                continue
            if valor_divida <= 0:
                continue  # já paga
            # Juros
            juros_num = re.sub(r"[^\d,\.]", "", juros).replace(",", ".")
            try:
                valor_juros = float(juros_num) if juros_num else 0.0
            except ValueError:
                valor_juros = 0.0
            em_atraso = valor_juros > 0 or prazo < today
            entries.append({
                "descricao": descricao,
                "montante_prestacao": mont_prest,
                "juros_mora": juros,
                "montante_divida": mont_divida,
                "data_limite": prazo.isoformat(),
                "em_atraso": em_atraso,
            })
    return entries if entries else None


def format_propinas(entries: list[dict], question: str) -> str:
    """Formata as prestações de propinas em texto legível, focado na pergunta."""
    q = question.lower()
    em_atraso    = [e for e in entries if e["em_atraso"]]
    por_pagar    = [e for e in entries if not e["em_atraso"]]

    # Se a pergunta é especificamente sobre atraso
    if any(w in q for w in ["atraso", "atrasad", "juros", "mora"]):
        if em_atraso:
            lines = ["Tens propinas em ATRASO (prazo ultrapassado ou com juros de mora):"]
            for e in em_atraso:
                juros_note = f" | Juros: {e['juros_mora']}€" if e["juros_mora"] and e["juros_mora"] != "0,00" else ""
                lines.append(f"  ⚠️  {e['descricao']}: {e['montante_divida']}€ (prazo: {e['data_limite']}){juros_note}")
            return "\n".join(lines)
        return "Não tens propinas em atraso. ✅"

    # Resposta completa
    lines = []
    if em_atraso:
        lines.append("Propinas em ATRASO ⚠️:")
        for e in em_atraso:
            juros_note = f" (juros: {e['juros_mora']}€)" if e["juros_mora"] and e["juros_mora"] != "0,00" else ""
            lines.append(f"  ⚠️  {e['descricao']}: {e['montante_divida']}€ — prazo: {e['data_limite']}{juros_note}")
    if por_pagar:
        lines.append("\nPropinas por pagar (dentro do prazo):")
        for e in por_pagar:
            lines.append(f"  💳  {e['descricao']}: {e['montante_divida']}€ — prazo: {e['data_limite']}")
    if not lines:
        lines = ["Não há propinas em dívida. \u2705"]
    return "\n".join(lines)


# ── Interpretation prompt ─────────────────────────────────────────────────────
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


def build_interpretation_prompt(question: str, raw_html: str, route: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "meta", "link"]):
        tag.decompose()

    # Extrai tabelas com cabeçalhos emparelhados a cada linha para evitar confusão de colunas
    table_texts = []
    for table in soup.find_all("table"):
        headers = []
        header_row = table.find("tr")
        if header_row:
            headers = [th.get_text(strip=True) for th in header_row.find_all(["th", "td"])]

        rows = []
        for tr in table.find_all("tr")[1:]:  # salta a linha de cabeçalhos
            cells = [td.get_text(strip=True) for td in tr.find_all(["th", "td"])]
            if not cells:
                continue
            if headers and len(headers) == len(cells):
                # Emparelha cabeçalho com valor: "Créditos ECTS: 6 | Nota: 14 | Resultado: Aprovado"
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
        # Complementa com o texto restante fora das tabelas
        for table in soup.find_all("table"):
            table.decompose()
        extra = soup.get_text(separator="\n", strip=True)[:1000]
        clean_text = extra + "\n\n" + structured
    else:
        clean_text = soup.get_text(separator="\n", strip=True)

    truncated = clean_text[:5000] + ("..." if len(clean_text) > 5000 else "")

    # Hints específicos por tipo de rota — começa pelo sitemap, fallback para hardcoded
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
        page_hints = sitemap_struct  # usa o que o sitemap detectou, se houver

    hints_block = f"\nEstrutura da página:\n{page_hints}\n" if page_hints else ""

    return f"""O utilizador perguntou: "{question}"

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


# ── Student ID helpers ───────────────────────────────────────────────────────
def substitute_student_id(url: str, student_id: str) -> str:
    """Replace the sitemap's baked-in student ID with the real one."""
    if SITEMAP_STUDENT_ID and student_id and student_id != SITEMAP_STUDENT_ID:
        return url.replace(f"aluno={SITEMAP_STUDENT_ID}", f"aluno={student_id}")
    return url


async def detect_student_id(cookie: str) -> str | None:
    """
    Fetch the CLIP user page and extract the student number from any link
    that contains '?aluno=XXXXX' or '&aluno=XXXXX'.
    """
    try:
        headers = {"Cookie": cookie, "User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(f"{CLIP_BASE_URL}/utente/eu", headers=headers)
            if resp.status_code != 200:
                return None
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                m = re.search(r"[?&]aluno=(\d+)", a["href"])
                if m:
                    return m.group(1)
    except Exception:
        pass
    return None


# ── Fetch CLIP ────────────────────────────────────────────────────────────────
async def fetch_clip_route(url: str, cookie: str) -> str:
    """Faz um GET ao CLIP com o cookie do utilizador."""
    # Garante URL completa
    if url.startswith("/"):
        url = f"{CLIP_BASE_URL}{url}"

    headers = {"Cookie": cookie, "User-Agent": "Mozilla/5.0"}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            return resp.text
        elif resp.status_code in (401, 403):
            raise HTTPException(status_code=401, detail="Cookie de sessão inválido ou expirado.")
        else:
            return f"[Erro {resp.status_code} ao aceder {url}]"


# ── LLM decide rotas ──────────────────────────────────────────────────────────
_DTYPE_PRIORITY = {"schedule": 0, "grades": 1, "exams": 2, "absences": 3,
                   "enrollments": 4, "documents": 5, "notices": 6, "unknown": 99}


def _pick_best_route_fallback(student_id: str | None) -> list[str]:
    """Fallback: devolve a rota de horário (schedule) mais provável."""
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
    all_routes = SITEMAP.get("routes", [])

    # O system prompt do modelo tejo já contém todas as rotas.
    # Envia apenas a pergunta + contexto do período actual — sem repetir as rotas.
    prompt = (
        f"Contexto: hoje é {datetime.date.today().strftime('%d/%m/%Y')} — "
        f"{CURRENT_PERIOD['descricao']} "
        f"(ano_lectivo={CURRENT_PERIOD['ano_lectivo']}, período={CURRENT_PERIOD['período_lectivo']}).\n"
        f"Pergunta: {question}"
    )
    if student_id:
        prompt += f"\nID do aluno: {student_id}"
    messages = [{"role": "user", "content": prompt}]
    print(f"[INFO] A enviar pergunta ao LLM (system prompt já tem as rotas)")

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
            urls = parsed.get("routes_to_fetch", [])
            # Mapeia cada URL para a versão correcta do sitemap (Latin-1, params reais)
            # Compara caminhos decodificados — ignora encoding e params de período
            sitemap_by_path = {}
            for r in all_routes:
                su = r.get("example_url", "")
                path = _unquote(urlparse(su).path)
                sitemap_by_path[path] = su
            resolved = []
            for u in urls:
                # 1. Correspondência exacta
                if u in sitemap_by_path.values():
                    resolved.append(u)
                    continue
                # 2. Match por path decodificado (tenta latin-1 primeiro, depois UTF-8)
                u_path_latin = _unquote(urlparse(u).path)   # latin-1
                u_path_utf8  = unquote(urlparse(u).path)    # UTF-8
                matched = False
                for u_path in (u_path_latin, u_path_utf8):
                    if u_path in sitemap_by_path:
                        resolved.append(sitemap_by_path[u_path])
                        print(f"[INFO] URL do LLM corrigida: {u} → {sitemap_by_path[u_path]}")
                        matched = True
                        break
                if matched:
                    continue
                # 3. Match parcial por segmento mais específico do path
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
                else:
                    # Aceita mesmo assim se parecer uma URL do CLIP
                    if "clip.fct.unl.pt" in u:
                        resolved.append(u)
            if resolved:
                # Substitui o student_id em todas
                if student_id:
                    resolved = [substitute_student_id(u, student_id) for u in resolved]
                return {"routes_to_fetch": resolved, "reasoning": parsed.get("reasoning", "")}
    except (json.JSONDecodeError, AttributeError):
        pass

    best = _pick_best_route_fallback(student_id)
    print(f"[WARN] LLM falhou a parsear JSON, usando fallback: {best}")
    return {"routes_to_fetch": best, "reasoning": "fallback"}


# ── LLM interpreta HTML ───────────────────────────────────────────────────────
async def llm_interpret(question: str, raw_html: str, route: str) -> str:
    prompt = build_interpretation_prompt(question, raw_html, route)
    client = ollama_client.Client(host=OLLAMA_URL)
    response = await asyncio.to_thread(
        client.chat,
        model=INTERPRET_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return response["message"]["content"].strip()


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

class FetchRequest(BaseModel):
    url: str
    session_cookie: str

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


async def _follow_testes_period(base_url: str, html: str, cookie: str) -> str | None:
    """
    Segue para o período lectivo actual na página de navegação de testes.
    Tenta primeiro o período exacto (ano+semestre actuais), caso contrário o mais recente.
    """
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "tipo_de_per" not in href or "ano_lectivo" not in href:
            continue
        full_url = href if href.startswith("http") else f"{CLIP_BASE_URL}{href}"
        m_ano = re.search(r"ano_lectivo=(\d+)", href)
        m_per = re.search(r"per(?:%ED|i)odo_lectivo=(\d+)", href)
        ano = int(m_ano.group(1)) if m_ano else 0
        per = int(m_per.group(1)) if m_per else 0
        candidates.append((ano, per, full_url))
    if not candidates:
        return None
    # Tenta primeiro o período actual exacto
    cur_ano = int(CURRENT_PERIOD["ano_lectivo"])
    cur_per = int(CURRENT_PERIOD["período_lectivo"])
    for ano, per, url in candidates:
        if ano == cur_ano and per == cur_per:
            try:
                return await fetch_clip_route(url, cookie)
            except Exception:
                break
    # Fallback: mais recente
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    try:
        return await fetch_clip_route(candidates[0][2], cookie)
    except Exception:
        return None


async def _follow_horario_period(html: str, cookie: str) -> tuple[str, str] | tuple[None, None]:
    """
    A página de horário sem período só mostra links de navegação (1º Sem, 2º Sem, …).
    Segue para o período MAIS RECENTE (ex: 2º semestre do ano actual).
    Devolve (html_final, url_final) ou (None, None).
    """
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "tipo_de_per" in href and "per" in href:
            full_url = href if href.startswith("http") else f"{CLIP_BASE_URL}{href}"
            # Extrai período_lectivo para ordenar (maior = mais recente)
            m = re.search(r"per(?:%ED|i)odo_lectivo=(\d+)", href)
            period_num = int(m.group(1)) if m else 0
            m2 = re.search(r"ano_lectivo=(\d+)", href)
            year = int(m2.group(1)) if m2 else 0
            candidates.append((year, period_num, full_url))
    if not candidates:
        return None, None
    # Tenta primeiro o período actual exacto
    cur_ano = int(CURRENT_PERIOD["ano_lectivo"])
    cur_per = int(CURRENT_PERIOD["período_lectivo"])
    for year, period_num, url in candidates:
        if year == cur_ano and period_num == cur_per:
            try:
                result_html = await fetch_clip_route(url, cookie)
                return result_html, url
            except Exception:
                break
    # Fallback: mais recente
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best_url = candidates[0][2]
    try:
        result_html = await fetch_clip_route(best_url, cookie)
        return result_html, best_url
    except Exception:
        return None, None


_NUM_MAP = {
    "um": 1, "uma": 1, "primeiro": 1, "primeira": 1,
    "dois": 2, "duas": 2, "segundo": 2, "segunda": 2,
    "três": 3, "tres": 3, "terceiro": 3, "terceira": 3,
    "quatro": 4, "quarto": 4, "quarta": 4,
    "cinco": 5, "quinto": 5, "quinta": 5,
}


def _extract_ordinal(pattern: str, text: str) -> int | None:
    """Extrai número de uma expressão como '2 semestre' / 'segundo ano'."""
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return None
    raw = m.group(1).rstrip("ºª°").strip()
    if raw.isdigit():
        return int(raw)
    return _NUM_MAP.get(raw.lower())


def _resultados_sem_num(question: str) -> int | None:
    """Extrai o número de semestre pedido na pergunta (1, 2, …) ou None."""
    return _extract_ordinal(
        r"(\d+|primeiro|segundo|terceiro|quarto|um|dois|tr[êe]s)[ºª°\s-]*semestre",
        question.lower(),
    )


async def _follow_resultados_period(
    html: str, cookie: str, question: str
) -> tuple[str, str] | tuple[None, None]:
    """
    A página resultados?ano_lectivo=X contém uma tabela de navegação com links
    para outros anos lectivos (mesma rota, diferente ano_lectivo).
    Cada página cobre um ano lectivo completo (todos os semestres juntos).
    Navega para o ano correcto com base na expressão 'N ano' / 'ano passado'.
    Devolve (html_do_ano, url) ou (None, None) se não há referência a outro ano.
    """
    q = question.lower()
    year_idx    = _extract_ordinal(r"(\d+|primeiro|segundo|terceiro|quarto|um|dois|tr[êe]s)[ºª°\s-]*ano", q)
    ano_passado = bool(re.search(r"\bano passado\b", q))

    # Sem referência a um ano diferente — sem navegação (semestre filtra-se na formatação)
    if year_idx is None and not ano_passado:
        return None, None

    # Colecta links para outros anos lectivos na página de resultados
    soup = BeautifulSoup(html, "html.parser")
    year_urls: dict[int, str] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Links de resultados por ano: contêm 'resultados' e 'ano_lectivo='
        if "resultados" not in href and "ano_lectivo" not in href:
            continue
        m = re.search(r"ano_lectivo=(\d+)", href)
        if not m:
            continue
        year = int(m.group(1))
        full_url = href if href.startswith("http") else f"{CLIP_BASE_URL}{href}"
        year_urls[year] = full_url

    # Inclui também o ano actual da URL em cache (pode não ter link para si próprio)
    print(f"[INFO] Resultados anos disponíveis: {sorted(year_urls.keys())}")
    if not year_urls:
        return None, None

    unique_years = sorted(year_urls.keys())

    if ano_passado and len(unique_years) >= 2:
        target_year = unique_years[-2]
    elif year_idx is not None:
        idx = max(1, min(year_idx, len(unique_years)))
        target_year = unique_years[idx - 1]
    else:
        target_year = unique_years[-1]

    target_url = year_urls[target_year]
    print(f"[INFO] Resultados: navegando para ano_lectivo={target_year} → {target_url}")
    try:
        result_html = await fetch_clip_route(target_url, cookie)
        return result_html, target_url
    except Exception:
        return None, None


# Padrões de pedidos de ACÇÃO (o chatbot só lê — não submete formulários)
_ACTION_PATTERNS = re.compile(
    r"\b(inscreve[- ]me|inscreve me|inscreve-me|cancela[- ]me|anula[- ]me"
    r"|submete|confirma|apaga|elimina|remove|regista[- ]me|matricula[- ]me"
    r"|faz a inscri[çc]|faz o registo|faz a matr[íi]cula"
    r"|clica|carrega no bot[ãa]o|prime o bot[ãa]o)\b",
    re.IGNORECASE,
)

# Rotas de acção — usadas para dar link directo no caso de pedidos de escrita
_ACTION_ROUTE_HINTS = {
    r"test[eo]|avalia": "inscrição/testes_de_avalia",
    r"exame":           "inscrição/exame",
    r"cadeira|escolar|unidade": "inscrição/escolar",
    r"turno":           "inscrição/turnos",
}


def _detect_action_request(question: str, student_id: str | None) -> str | None:
    """
    Se o pedido for uma acção de escrita (inscrever, cancelar, submeter…),
    devolve uma resposta explicando a limitação e dando o link directo.
    Devolve None se não for um pedido de acção.
    """
    if not _ACTION_PATTERNS.search(question):
        return None

    q_lower = question.lower()
    # Encontra a rota mais relevante para dar o link
    target_url = None
    for pattern, route_fragment in _ACTION_ROUTE_HINTS.items():
        if re.search(pattern, q_lower):
            for r in SITEMAP.get("routes", []):
                url = r.get("example_url", "")
                if route_fragment in _unquote(url):
                    target_url = substitute_student_id(url, student_id) if student_id else url
                    break
        if target_url:
            break

    link_note = f"\n\nPodes fazê-lo directamente aqui: {target_url}" if target_url else ""
    return (
        "Não consigo realizar acções no CLIP em teu nome (submeter formulários, "
        "inscrever, cancelar, etc.) — apenas consigo ler e apresentar informação."
        + link_note
    )


_INSCRICAO_TESTE_RE = re.compile(
    r"\b(inscreve[- ]?me|quero inscrever[- ]?me|inscri[\u00e7c][a\u00e3]o no teste"
    r"|faz(?:\s+a)?\s+inscri[\u00e7c][a\u00e3]o(?:\s+n[oa])?\s+teste"
    r"|sim[,.]?\s*inscreve[- ]?me|ok[,.]?\s*inscreve[- ]?me"
    r"|inscreve[- ]?me\s+(?:n[oae]?sse|n[oae]?ste|n[oae]?quele|nisso|em\s+todos|a\s+todos)\b"
    r"|inscreve[- ]?me\s+em\s+todos)",
    re.IGNORECASE,
)

_TODOS_TESTES_RE = re.compile(
    r"\b(em\s+todos|a\s+todos|todos\s+os\s+testes|em\s+tudo)\b",
    re.IGNORECASE,
)

_CONTEXTO_ESTE_RE = re.compile(
    r"\b(n[oae]?sse|n[oae]?ste|n[oae]?quele|nisso|nele|nela)\s+teste\b"
    r"|\binscreve[- ]?me\s+(?:n[oae]?sse|n[oae]?ste|n[oae]?quele|nisso)\b",
    re.IGNORECASE,
)


def _extract_cadeira_from_history(conversation_history: list[dict]) -> str:
    """
    Procura na história da conversa a última mensagem do assistente que lista testes
    e extrai o nome da cadeira (assume que apenas um teste foi listado ou o primeiro).
    """
    for msg in reversed(conversation_history):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        # Mensagens de testes têm o padrão "• Cadeira — ..." ou "Inscrito com sucesso em Cadeira"
        m = re.search(r"[\u2022\u2713]\s+(.+?)\s+\u2014", content)
        if m:
            return m.group(1).strip()
        m = re.search(r"(?:Inscrito|disponível)[^:]*:\s+(.+?)\s+\u2014", content)
        if m:
            return m.group(1).strip()
    return ""


def _detect_inscricao_teste(question: str, conversation_history: list[dict] | None = None) -> str | None:
    """
    Se a pergunta é um pedido de inscrição num teste, devolve:
      - nome da cadeira (string não vazia) se especificado ou inferido do contexto
      - '' (string vazia) se não especificado e não há contexto (mostra todos os disponíveis)
      - '__todos__' se o utilizador quer inscrever-se em todos os testes
      - None se não é um pedido de inscrição de teste
    """
    if not _INSCRICAO_TESTE_RE.search(question):
        return None
    # Se só mencionar exame (sem teste), não é inscrição em teste
    if re.search(r"\bexame\b", question, re.IGNORECASE) and not re.search(r"\bteste\b", question, re.IGNORECASE):
        return None
    # "Inscreve-me em todos os testes"
    if _TODOS_TESTES_RE.search(question):
        return "__todos__"
    # Extrai cadeira: "teste de X" / "no teste de X"
    m = re.search(
        r"\bteste(?:\s+\w+)?\s+(?:de|a|da|do|em)\s+(.+?)(?:\?|$|,|\. |\s+n[ao]\b)",
        question, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    # Fallback: "inscreve-me em/na/no X" (excluindo pronomes contextuais)
    m = re.search(r"\binscreve[- ]?me\s+(?:n[aoe]m?|a|em)\s+(.+?)(?:\?|$|,|\. )", question, re.IGNORECASE)
    if m:
        cadeira = re.sub(r"^(?:teste\s+de\s+)", "", m.group(1).strip(), flags=re.IGNORECASE)
        if not re.match(r"^(esse|este|aquele|isso|todos|tudo)\b", cadeira, re.IGNORECASE):
            return cadeira
    # "nesse/neste/naquele teste" — resolve pelo contexto da conversa
    if _CONTEXTO_ESTE_RE.search(question) or re.search(r"\b(nesse|neste|naquele|nisso)\b", question, re.IGNORECASE):
        if conversation_history:
            cadeira = _extract_cadeira_from_history(conversation_history)
            return cadeira  # pode ser '' se não achou nada — lista tudo
    return ""  # intent detectado mas cadeira não especificada


def _match_test_fuzzy(query: str, testes: list[dict]) -> list[dict]:
    """Filtra testes disponíveis cujo nome da cadeira corresponde à query."""
    if not query:
        return testes
    stop = {"de", "da", "do", "dos", "das", "a", "o", "e", "em", "para", "no", "na"}
    words = [w for w in re.split(r"\s+", query.lower()) if w not in stop and len(w) >= 3]
    if not words:
        return testes
    return [t for t in testes if any(w in t["cadeira"].lower() for w in words)]


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    if not req.session_cookie:
        raise HTTPException(status_code=400, detail="session_cookie é obrigatório.")

    # 0. Resolve student ID – use provided value or auto-detect from session
    student_id = req.student_id
    if not student_id:
        student_id = await detect_student_id(req.session_cookie)
        if student_id:
            print(f"[INFO] Student ID auto-detected: {student_id}")
        else:
            student_id = SITEMAP_STUDENT_ID  # last-resort fallback
            print(f"[WARN] Could not detect student ID, falling back to sitemap default")

    # 1a. Detecta pedido de inscrição em teste (acção suportada directamente)
    inscricao_cadeira = _detect_inscricao_teste(req.question, req.conversation_history)
    if inscricao_cadeira is not None:
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
                        # Reatualiza action_url/submit para próximo POST (página muda após inscrição)
                        _, action_url, submit_name = parse_testes_disponiveis_com_valores(resp_html)
                lines = ["Inscrições submetidas:"]
                lines.extend(resultados)
                return ChatResponse(answer="\n".join(lines), routes_consulted=[testes_route])

            if not matches:
                available = "\n".join(f"  \u2022 {t['cadeira']} \u2014 {t['avaliacao']} ({t['data']})" for t in testes)
                return ChatResponse(
                    answer=f"Não encontrei nenhum teste disponível para '{inscricao_cadeira}'.\n\nTestes disponíveis:\n{available}",
                    routes_consulted=[testes_route],
                )

            if len(matches) > 1 and inscricao_cadeira:
                options = "\n".join(f"  \u2022 {t['cadeira']} \u2014 {t['avaliacao']} ({t['data']})" for t in matches)
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
                        answer=f"Inscrito com sucesso em {test['cadeira']} \u2014 {test['avaliacao']} ({test['data']}).",
                        routes_consulted=[testes_route],
                    )
                else:
                    return ChatResponse(
                        answer=f"Pedido submetido para {test['cadeira']} \u2014 {test['avaliacao']}. Verifica no CLIP para confirmar a inscrição.",
                        routes_consulted=[testes_route],
                    )
            else:
                return ChatResponse(
                    answer=f"Não foi possível submeter a inscrição em {test['cadeira']}. O servidor CLIP não respondeu como esperado.",
                    routes_consulted=[testes_route],
                )
        except HTTPException:
            raise
        except Exception as e:
            return ChatResponse(answer=f"Erro ao tentar inscrever: {e}", routes_consulted=[])

    # 1b. Detecta pedidos de acção não suportados (o chatbot não submete formulários)
    action_response = _detect_action_request(req.question, student_id)
    if action_response:
        return ChatResponse(answer=action_response, routes_consulted=[])

    # 1c. Detecta pedido de exportação ICS
    if _ICS_PATTERNS.search(req.question):
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
            return ChatResponse(
                answer=f"Não consegui gerar o ficheiro ICS: {e.detail}",
                routes_consulted=[],
            )

    # 1c. LLM decide que rotas consultar
    decision = await llm_decide_routes(req.question, req.conversation_history, student_id)
    routes_to_fetch = decision.get("routes_to_fetch", [])
    print(f"[INFO] LLM escolheu rotas: {routes_to_fetch}")
    print(f"[INFO] Reasoning: {decision.get('reasoning', '')}")

    if not routes_to_fetch:
        return ChatResponse(
            answer="Não consegui identificar que secção do CLIP tem essa informação. Podes reformular a pergunta?",
            routes_consulted=[],
        )

    # 2. Vai buscar cada rota ao CLIP com o cookie do utilizador
    fetched_content = {}
    for route in routes_to_fetch[:3]:
        try:
            route = substitute_student_id(route, student_id)
            html = await fetch_clip_route(route, req.session_cookie)
            # Horário sem período — segue automaticamente para o semestre mais recente
            if "hor%E1rio" in route or "horário" in _unquote(route):
                followed_html, followed_url = await _follow_horario_period(html, req.session_cookie)
                if followed_html:
                    html = followed_html
                    route = followed_url
            # Resultados — segue para o ano/semestre pedido na pergunta (se especificado)
            elif "ano_lectivo/resultados" in _unquote(route):
                followed_html, followed_url = await _follow_resultados_period(html, req.session_cookie, req.question)
                if followed_html:
                    html = followed_html
                    route = followed_url
            # Inscrição em testes — segue para o período actual
            elif "inscri" in route and "testes_de_avalia" in route:
                html = await _follow_testes_period(route, html, req.session_cookie) or html
            # Propinas / dados_para_pagamento — injeta parâmetros de período se ausentes
            elif "dados_para_pagamento" in _unquote(route) and "ano_lectivo" not in route:
                sep = "&" if "?" in route else "?"
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

    # 3. Interpreta e responde (usa parser dedicado quando disponível)
    direct_answer = None
    for route, html in fetched_content.items():
        if "ano_lectivo/resultados" in _unquote(route):
            entries = parse_resultados_clip(html)
            if entries:
                sem_filter = _resultados_sem_num(req.question)
                direct_answer = format_resultados(entries, sem_filter)
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
        elif "dados_para_pagamento" in _unquote(route):
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
        answer = await llm_interpret(req.question, combined, ", ".join(fetched_content.keys()))

    first_html = next(iter(fetched_content.values()), "")
    soup = BeautifulSoup(first_html, "html.parser")
    preview = soup.get_text(strip=True)[:200] if first_html else None

    return ChatResponse(
        answer=answer,
        routes_consulted=list(fetched_content.keys()),
        raw_data_preview=preview,
    )
