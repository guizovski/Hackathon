"""
Parsers dedicados para cada tipo de página do CLIP.
Cada parse_* extrai dados estruturados; cada format_* produz texto legível.
"""

import re
import datetime
import httpx
from urllib.parse import quote
from bs4 import BeautifulSoup
from fastapi import HTTPException

from config import CLIP_BASE_URL, substitute_student_id

_DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4}')


# ── Horário ───────────────────────────────────────────────────────────────────
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
    seen = set()

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
            raw = m_inicio.group(1).zfill(4)
            hora_inicio_h = int(raw[:-2])
            hora_inicio_m = int(raw[-2:])
        else:
            hora_inicio_h, hora_inicio_m = 0, 0

        try:
            rowspan = int(td.get("rowspan", 2))
        except (ValueError, TypeError):
            rowspan = 2
        duracao_h = rowspan // 2
        duracao_min = (rowspan % 2) * 30

        hora_fim_h = hora_inicio_h + duracao_h + (hora_inicio_m + duracao_min) // 60
        hora_fim_m = (hora_inicio_m + duracao_min) % 60

        hora_str     = f"{hora_inicio_h}:{hora_inicio_m:02d}"
        hora_fim_str = f"{hora_fim_h}:{hora_fim_m:02d}"

        turno = link.get_text(strip=True)

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
            "hora_sort": hora_inicio_h * 60 + hora_inicio_m,
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


# ── Testes de avaliação ───────────────────────────────────────────────────────
def _parse_testes_table(table) -> list[dict]:
    """Extrai linhas de dados de uma tabela de testes (disponíveis ou inscritos)."""
    entries = []
    for tr in table.find_all("tr", recursive=False):
        cells = tr.find_all("td")
        tds = [td for td in cells if not td.find("input")]
        if len(tds) < 3:
            continue
        texts = [td.get_text(" ", strip=True) for td in tds]
        cadeira = texts[0].strip()
        if not cadeira or not _DATE_RE.search(texts[2] if len(texts) > 2 else ""):
            continue
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
    inscritos   = data.get("inscritos", [])

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
    Devolve (testes, action_url, submit_name).
    """
    soup = BeautifulSoup(html, "html.parser")
    testes: list[dict] = []
    action_url  = ""
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
        cadeira = texts[1].replace(" (*)", "").strip() if len(texts) > 1 else ""
        if not cadeira:
            continue
        testes.append({
            "cadeira":   cadeira,
            "avaliacao": texts[2] if len(texts) > 2 else "",
            "data":      texts[3] if len(texts) > 3 else "",
            "salas":     texts[4] if len(texts) > 4 else "",
            "value":     checkbox.get("value", ""),
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


# ── Resultados ────────────────────────────────────────────────────────────────
def _is_numeric(value: str) -> bool:
    try:
        float(value.replace(",", "."))
        return True
    except (ValueError, AttributeError):
        return False


def parse_resultados_clip(html: str) -> list[dict] | None:
    """
    Extrai notas da página /ano_lectivo/resultados do CLIP.
    Colunas: cadeira | a | Esp. | Nor. | Rec. | Ext. | Resultado | Créditos ECTS
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

        if any(k in header_text for k in ["Sem", "Tri", "semestre", "trimestre"]):
            current_section = header_text.split("Aprovação")[0].strip()
            continue

        for tr in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all(["th", "td"])]
            if len(cells) == 8 and cells[6] in resultado_values:
                nota_nor = cells[3]
                nota_rec = cells[4]
                entries.append({
                    "secção":       current_section,
                    "cadeira":      cells[0],
                    "nota_normal":  nota_nor if _is_numeric(nota_nor) else None,
                    "nota_recurso": nota_rec if _is_numeric(nota_rec) else None,
                    "resultado":    cells[6],
                    "creditos_ects": cells[7],
                })

    return entries if entries else None


def format_resultados(entries: list[dict], sem_filter: int | None = None) -> str:
    """Formata os resultados do CLIP em texto legível."""
    if sem_filter is not None:
        sem_labels = {str(sem_filter), f"{sem_filter}º", f"{sem_filter}º sem", f"{sem_filter}o sem"}
        entries = [e for e in entries if any(lbl in e["secção"].lower() for lbl in sem_labels)]
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


# ── Resumo académico ──────────────────────────────────────────────────────────
def parse_resumo(html: str) -> dict | None:
    """Extrai créditos, média e lista de cadeiras concluídas da página situação/resumo."""
    soup = BeautifulSoup(html, "html.parser")
    result = {"creditos_exigidos": None, "creditos_obtidos": None, "media": None, "unidades": []}

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for i, tr in enumerate(rows):
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            if "Exigidos" in cells and "Obtidos" in cells:
                data_row = rows[i + 1] if i + 1 < len(rows) else None
                if data_row:
                    vals = [td.get_text(strip=True) for td in data_row.find_all(["td", "th"])]
                    if len(vals) >= 2:
                        result["creditos_exigidos"] = vals[0].replace(" (ECTS)", "").strip()
                        result["creditos_obtidos"]  = vals[1].replace(" (ECTS)", "").strip()
            for cell in cells:
                if "dia para efeitos" in cell.lower() or "média" in cell.lower():
                    idx = cells.index(cell)
                    if idx + 1 < len(cells) and cells[idx + 1]:
                        result["media"] = cells[idx + 1]

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
                data = cells[date_idx] if date_idx is not None and date_idx < len(cells) else ""
                cred = cells[cred_idx] if cred_idx is not None and cred_idx < len(cells) else ""
                nota = cells[class_idx] if class_idx is not None and class_idx < len(cells) else ""
                if nota or cred:
                    result["unidades"].append({"nome": unidade, "data": data, "creditos": cred, "nota": nota})

    if not result["unidades"] and not result["media"]:
        return None
    return result


def format_resumo(data: dict, question: str) -> str:
    """Formata o resumo académico de forma contextual à pergunta."""
    q = question.lower()
    lines = []

    if data.get("media"):
        lines.append(f"Média para conclusão do curso: {data['media']}")
    if data.get("creditos_obtidos") and data.get("creditos_exigidos"):
        lines.append(f"Créditos ECTS: {data['creditos_obtidos']} obtidos / {data['creditos_exigidos']} exigidos")

    only_summary = any(w in q for w in ["média", "media", "créditos", "ects", "creditos"]) and \
                   not any(w in q for w in ["notas", "cadeiras", "histórico", "concluí", "tive"])
    if only_summary:
        return "\n".join(lines) if lines else "Não foi possível obter o resumo académico."

    if data.get("unidades"):
        lines.append("\nCadeiras concluídas:")
        for u in data["unidades"]:
            nota_str = f"{u['nota']}/20" if u["nota"] and u["nota"].replace(".", "").isdigit() else (u["nota"] or "—")
            cred_str = f"  ({u['creditos']} ECTS)" if u["creditos"] else ""
            data_str = f"  [{u['data']}]" if u["data"] else ""
            lines.append(f"  - {u['nome']}: {nota_str}{cred_str}{data_str}")

    return "\n".join(lines) if lines else "Não foi possível obter o resumo académico."


# ── Propinas ──────────────────────────────────────────────────────────────────
def parse_propinas(html: str) -> list[dict] | None:
    """Extrai as prestações em dívida da página dados_para_pagamento."""
    soup = BeautifulSoup(html, "html.parser")
    today = datetime.date.today()
    entries = []

    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 5:
                continue
            descricao, mont_prest, juros, mont_divida, data_limite = cells[:5]
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
            mont_num = re.sub(r"[^\d,\.]", "", mont_divida).replace(",", ".")
            try:
                valor_divida = float(mont_num) if mont_num else 0.0
            except ValueError:
                continue
            if valor_divida <= 0:
                continue
            juros_num = re.sub(r"[^\d,\.]", "", juros).replace(",", ".")
            try:
                valor_juros = float(juros_num) if juros_num else 0.0
            except ValueError:
                valor_juros = 0.0
            em_atraso = valor_juros > 0 or prazo < today
            entries.append({
                "descricao":          descricao,
                "montante_prestacao": mont_prest,
                "juros_mora":         juros,
                "montante_divida":    mont_divida,
                "data_limite":        prazo.isoformat(),
                "em_atraso":          em_atraso,
            })
    return entries if entries else None


def format_propinas(entries: list[dict], question: str) -> str:
    """Formata as prestações de propinas em texto legível, focado na pergunta."""
    q = question.lower()
    em_atraso = [e for e in entries if e["em_atraso"]]
    por_pagar = [e for e in entries if not e["em_atraso"]]

    if any(w in q for w in ["atraso", "atrasad", "juros", "mora"]):
        if em_atraso:
            lines = ["Tens propinas em ATRASO (prazo ultrapassado ou com juros de mora):"]
            for e in em_atraso:
                juros_note = f" | Juros: {e['juros_mora']}€" if e["juros_mora"] and e["juros_mora"] != "0,00" else ""
                lines.append(f"  ⚠️  {e['descricao']}: {e['montante_divida']}€ (prazo: {e['data_limite']}){juros_note}")
            return "\n".join(lines)
        return "Não tens propinas em atraso. ✅"

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
