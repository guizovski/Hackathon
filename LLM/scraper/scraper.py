"""
CLIP Structure Scraper
======================
Faz login no CLIP e mapeia TODA a estrutura do site de forma recursiva.

Anti-loop sem limite de profundidade:
  1. Set global de URLs visitadas (nunca revisita a mesma URL)
  2. Normalização de paths (agrupa /aluno/123 e /aluno/456 como /aluno/{id})
  3. Limite de variações por padrão normalizado (evita explodir em listas enormes)
  4. Deteção de segmentos repetidos no path (A/B/A/B = loop)
  5. Limite total de páginas como safety net
"""

import os
import json
import asyncio
import re
from urllib.parse import urljoin, urlparse, parse_qs
from collections import defaultdict
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL        = os.getenv("CLIP_BASE_URL", "https://clip.fct.unl.pt")
OUTPUT_PATH     = os.getenv("OUTPUT_PATH", "/app/data/sitemap.json")
CLIP_USER       = os.getenv("CLIP_USER", "")
CLIP_PASS       = os.getenv("CLIP_PASS", "")
MAX_PAGES       = int(os.getenv("MAX_PAGES", "2000"))     # safety net total
MAX_PER_PATTERN = int(os.getenv("MAX_PER_PATTERN", "5"))  # max URLs por padrão normalizado
REQUEST_DELAY   = float(os.getenv("REQUEST_DELAY", "1"))  # segundos entre requests

# Padrões a ignorar completamente
IGNORE_PATTERNS = [
    r"logout", r"sair", r"signout",
    r"javascript:", r"mailto:", r"tel:",
    r"\.pdf$", r"\.doc$", r"\.xls$", r"\.zip$",
    r"\.png$", r"\.jpg$", r"\.jpeg$", r"\.gif$", r"\.css$", r"\.js$",
]

# Segmentos que são IDs numéricos ou hashes
ID_PATTERN = re.compile(r"^\d+$|^[a-f0-9]{8,}$")


# ── Anti-loop helpers ─────────────────────────────────────────────────────────

def normalize_path(path: str) -> str:
    """
    Substitui segmentos que parecem IDs por {id}.
    /utente/eu/aluno/2024/12345/horario → /utente/eu/aluno/{id}/{id}/horario
    Permite agrupar URLs com mesma estrutura mas IDs diferentes.
    """
    segments = path.strip("/").split("/")
    normalized = []
    for seg in segments:
        if ID_PATTERN.match(seg):
            normalized.append("{id}")
        else:
            normalized.append(seg)
    return "/" + "/".join(normalized)


def has_repeating_segments(path: str) -> bool:
    """
    Deteta loops no path como /a/b/c/a/b/c ou /a/b/a/b.
    Se qualquer subsequência de segmentos se repete consecutivamente, é loop.
    """
    parts = path.strip("/").split("/")
    n = len(parts)
    for size in range(1, n // 2 + 1):
        for i in range(n - size * 2 + 1):
            if parts[i:i+size] == parts[i+size:i+size*2]:
                return True
    return False


def should_ignore(url: str) -> bool:
    for pattern in IGNORE_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return True
    return False


def is_same_domain(url: str) -> bool:
    parsed = urlparse(url)
    base_parsed = urlparse(BASE_URL)
    return (not parsed.netloc) or (parsed.netloc == base_parsed.netloc)


def clean_url(href: str, current_url: str) -> str | None:
    try:
        full = urljoin(current_url, href)
        parsed = urlparse(full)
        # Remove fragmento (#...)
        clean = parsed._replace(fragment="")
        result = clean.geturl()
        if not result.startswith("http"):
            return None
        return result
    except Exception:
        return None


# ── Page info extractor ───────────────────────────────────────────────────────

def infer_data_type(url: str, title: str) -> str:
    combined = (url + " " + title).lower()
    mapping = {
        "grades":       ["nota", "aproveitamento", "classif", "grade"],
        "schedule":     ["horario", "horário", "aula", "schedule"],
        "absences":     ["falt", "presença", "absence"],
        "exams":        ["exame", "exam", "época", "epoch"],
        "documents":    ["doc", "ficheiro", "material", "arquivo"],
        "notices":      ["aviso", "notif", "mensagem", "notice"],
        "groups":       ["grupo", "group"],
        "assignments":  ["trabalho", "entrega", "assignment"],
        "enrollments":  ["inscri", "enroll", "matricul"],
        "statistics":   ["estatistic", "progresso", "ects"],
        "navigation":   ["menu", "início", "home", "principal"],
    }
    for dtype, keywords in mapping.items():
        if any(kw in combined for kw in keywords):
            return dtype
    return "unknown"


def extract_page_info(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    # Título
    title = soup.title.get_text(strip=True) if soup.title else ""

    # Headings principais (perceber o propósito da página)
    headings = [h.get_text(strip=True) for h in soup.find_all(["h1", "h2", "h3"])[:5]]

    # Headers de tabelas (o CLIP usa muito tabelas para dados)
    table_headers = []
    for table in soup.find_all("table")[:3]:
        headers = [th.get_text(strip=True) for th in table.find_all("th")[:10]]
        if headers:
            table_headers.append(headers)

    # Forms (perceber que inputs existem e para onde submetem)
    forms = []
    for form in soup.find_all("form"):
        action = form.get("action", "")
        method = form.get("method", "get").upper()
        inputs = [
            {"name": inp.get("name", ""), "type": inp.get("type", "text")}
            for inp in form.find_all(["input", "select"])
            if inp.get("name") and inp.get("type") not in ("submit", "button", "hidden")
        ]
        forms.append({"action": action, "method": method, "inputs": inputs})

    # Query params da URL
    parsed = urlparse(url)
    query_params = list(parse_qs(parsed.query).keys())

    return {
        "url": url,
        "path": parsed.path,
        "query_params": query_params,
        "title": title,
        "headings": headings,
        "table_headers": table_headers,
        "forms": forms,
        "data_type": infer_data_type(url, title),
        "normalized_path": normalize_path(parsed.path),
    }


# ── Login ─────────────────────────────────────────────────────────────────────

async def do_login(page) -> bool:
    if not CLIP_USER or not CLIP_PASS:
        print("[WARN] CLIP_USER ou CLIP_PASS não definidos.")
        return False

    print(f"[INFO] A fazer login como '{CLIP_USER}'...")
    try:
        await page.goto(BASE_URL, timeout=30000, wait_until="domcontentloaded")

        # Tenta vários seletores possíveis para os campos de login
        user_selectors = [
            "input[name='utilizador']", "input[name='user']",
            "input[name='username']",   "input[type='text']"
        ]
        pass_selectors = [
            "input[name='senha']",    "input[name='pass']",
            "input[name='password']", "input[type='password']"
        ]

        user_field = None
        for sel in user_selectors:
            try:
                user_field = await page.wait_for_selector(sel, timeout=3000)
                if user_field:
                    break
            except PlaywrightTimeout:
                continue

        pass_field = None
        for sel in pass_selectors:
            try:
                pass_field = await page.wait_for_selector(sel, timeout=3000)
                if pass_field:
                    break
            except PlaywrightTimeout:
                continue

        if not user_field or not pass_field:
            print("[ERROR] Campos de login não encontrados.")
            return False

        await user_field.fill(CLIP_USER)
        await pass_field.fill(CLIP_PASS)
        await pass_field.press("Enter")
        await page.wait_for_load_state("domcontentloaded", timeout=15000)

        current_url = page.url
        if "login" in current_url.lower() or "erro" in current_url.lower():
            print("[ERROR] Login falhou — verifica as credenciais.")
            return False

        print(f"[OK] Login bem sucedido → {current_url}")
        return True

    except Exception as e:
        print(f"[ERROR] Erro durante login: {e}")
        return False


# ── Crawler ───────────────────────────────────────────────────────────────────

async def crawl(page, start_url: str) -> list[dict]:
    """
    BFS sem limite de profundidade.
    Anti-loop por:
      - visited: URLs exatas já processadas
      - pattern_count: limite por padrão normalizado de path
      - has_repeating_segments: deteção de ciclos estruturais no path
    """
    visited = set()
    pattern_count = defaultdict(int)
    queue = [start_url]
    pages_data = []

    print(f"[INFO] Crawl iniciado: max {MAX_PAGES} páginas, max {MAX_PER_PATTERN} por padrão\n")

    while queue and len(visited) < MAX_PAGES:
        url = queue.pop(0)

        if url in visited:
            continue
        if not is_same_domain(url):
            continue
        if should_ignore(url):
            continue

        path = urlparse(url).path

        # Deteção de loop estrutural no path
        if has_repeating_segments(path):
            print(f"  [LOOP] {path}")
            continue

        # Limite por padrão normalizado
        norm = normalize_path(path)
        if pattern_count[norm] >= MAX_PER_PATTERN:
            continue

        visited.add(url)
        pattern_count[norm] += 1

        print(f"  [{len(visited):04d}] {url}")

        # Vai buscar a página
        try:
            await page.goto(url, timeout=20000, wait_until="domcontentloaded")
            await asyncio.sleep(REQUEST_DELAY)
            html = await page.content()
        except PlaywrightTimeout:
            print(f"  [TIMEOUT] {url}")
            continue
        except Exception as e:
            print(f"  [ERROR] {url} → {e}")
            continue

        # Extrai info e guarda
        info = extract_page_info(url, html)
        pages_data.append(info)

        # Descobre novos links
        soup = BeautifulSoup(html, "html.parser")
        new_links = 0
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"].strip()
            if not href:
                continue
            full_url = clean_url(href, url)
            if full_url and full_url not in visited:
                queue.append(full_url)
                new_links += 1

        if new_links:
            print(f"         → {new_links} links encontrados (queue: {len(queue)})")

    print(f"\n[OK] Crawl terminado: {len(pages_data)} páginas, {len(pattern_count)} padrões únicos")
    return pages_data


# ── Main ──────────────────────────────────────────────────────────────────────

async def run():
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        logged_in = await do_login(page)
        if not logged_in:
            print("[WARN] A continuar sem autenticação (só páginas públicas)")

        start_url = page.url if logged_in else BASE_URL
        pages = await crawl(page, start_url)

        await browser.close()

    # Agrupa por padrão normalizado
    patterns = defaultdict(list)
    for p in pages:
        patterns[p["normalized_path"]].append(p)

    # Constrói sitemap — usa a primeira ocorrência como representante do padrão
    routes = []
    for norm_path, examples in patterns.items():
        rep = examples[0]
        routes.append({
            "pattern": norm_path,
            "example_url": rep["url"],
            "title": rep["title"],
            "headings": rep["headings"],
            "table_headers": rep["table_headers"],
            "forms": rep["forms"],
            "query_params": rep["query_params"],
            "data_type": rep["data_type"],
            "occurrences": len(examples),
        })

    # Ordena por ocorrências (rotas mais comuns primeiro)
    routes.sort(key=lambda r: r["occurrences"], reverse=True)

    sitemap = {
        "base_url": BASE_URL,
        "total_pages_crawled": len(pages),
        "total_patterns": len(routes),
        "routes": routes,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(sitemap, f, ensure_ascii=False, indent=2)

    print(f"\n[OK] Sitemap guardado em {OUTPUT_PATH}")
    print(f"[OK] {len(routes)} padrões de rota mapeados")


if __name__ == "__main__":
    asyncio.run(run())
