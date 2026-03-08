"""
Camada de acesso ao CLIP: HTTP client e helpers de navegação.
"""

import re
import httpx
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from fastapi import HTTPException

from config import CLIP_BASE_URL, CURRENT_PERIOD, _unquote, substitute_student_id


# ── Student ID ────────────────────────────────────────────────────────────────
async def detect_student_id(cookie: str) -> str | None:
    """
    Acede à página de perfil do CLIP e extrai o número de aluno de qualquer
    link que contenha '?aluno=XXXXX' ou '&aluno=XXXXX'.
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
    """Faz um GET ao CLIP com o cookie do utilizador e devolve o HTML."""
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


# ── Navegação de período — Testes ─────────────────────────────────────────────
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


# ── Navegação de período — Horário ────────────────────────────────────────────
async def _follow_horario_period(html: str, cookie: str) -> tuple[str, str] | tuple[None, None]:
    """
    A página de horário sem período mostra links de navegação (1º Sem, 2º Sem, …).
    Segue para o período actual ou, em fallback, o mais recente.
    Devolve (html_final, url_final) ou (None, None).
    """
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "tipo_de_per" in href and "per" in href:
            full_url = href if href.startswith("http") else f"{CLIP_BASE_URL}{href}"
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


# ── Navegação de período — Resultados ─────────────────────────────────────────
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
    A página resultados?ano_lectivo=X tem links para outros anos lectivos.
    Navega para o ano correcto baseado em 'N ano' / 'ano passado' na pergunta.
    Devolve (html_do_ano, url) ou (None, None) se não há referência a outro ano.
    """
    q = question.lower()
    year_idx    = _extract_ordinal(r"(\d+|primeiro|segundo|terceiro|quarto|um|dois|tr[êe]s)[ºª°\s-]*ano", q)
    ano_passado = bool(re.search(r"\bano passado\b", q))

    if year_idx is None and not ano_passado:
        return None, None

    soup = BeautifulSoup(html, "html.parser")
    year_urls: dict[int, str] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "resultados" not in href and "ano_lectivo" not in href:
            continue
        m = re.search(r"ano_lectivo=(\d+)", href)
        if not m:
            continue
        year = int(m.group(1))
        full_url = href if href.startswith("http") else f"{CLIP_BASE_URL}{href}"
        year_urls[year] = full_url

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


# ── Fuzzy match de testes ─────────────────────────────────────────────────────
def _match_test_fuzzy(query: str, testes: list[dict]) -> list[dict]:
    """Filtra testes disponíveis cujo nome da cadeira corresponde à query."""
    if not query:
        return testes
    stop = {"de", "da", "do", "dos", "das", "a", "o", "e", "em", "para", "no", "na"}
    words = [w for w in re.split(r"\s+", query.lower()) if w not in stop and len(w) >= 3]
    if not words:
        return testes
    return [t for t in testes if any(w in t["cadeira"].lower() for w in words)]
