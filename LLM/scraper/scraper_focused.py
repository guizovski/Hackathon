"""
CLIP Focused Scraper
====================
Começa em /utente/eu/aluno e exclui rotas inúteis para o chatbot.
Sem limite de páginas — apenas deteção de loops e links repetidos.
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
OUTPUT_PATH     = os.getenv("OUTPUT_PATH", "/app/data/sitemap_focused.json")
CLIP_USER       = os.getenv("CLIP_USER", "")
CLIP_PASS       = os.getenv("CLIP_PASS", "")
MAX_PER_PATTERN = int(os.getenv("MAX_PER_PATTERN", "5"))
REQUEST_DELAY   = float(os.getenv("REQUEST_DELAY", "1"))

# ── Rotas a excluir completamente (e todas as suas sub-rotas) ─────────────────
EXCLUDED_PREFIXES = [
    "/utente/eu/aluno/acto_curricular/inscri",
    "/utente/eu/aluno/acto_curricular/candidaturas",
    "/utente/eu/aluno/situa%E7%E3o/plano",
    "/utente/eu/aluno/situacao/plano",
]

IGNORE_PATTERNS = [
    r"logout", r"sair", r"signout",
    r"javascript:", r"mailto:", r"tel:",
    r"\.pdf$", r"\.doc$", r"\.xls$", r"\.zip$",
    r"\.png$", r"\.jpg$", r"\.jpeg$", r"\.gif$", r"\.css$", r"\.js$",
]

ID_PATTERN = re.compile(r"^\d+$|^[a-f0-9]{8,}$")


# ── Anti-loop ─────────────────────────────────────────────────────────────────

def normalize_path(path: str) -> str:
    """Substitui segmentos numéricos/hash por {id} para agrupar padrões."""
    segments = path.strip("/").split("/")
    return "/" + "/".join("{id}" if ID_PATTERN.match(s) else s for s in segments)


def normalize_url_for_dedup(url: str) -> str:
    """
    Normaliza a URL completa (path + query) para detetar duplicados.
    Substitui valores numéricos dos query params por {id}.
    Ex: ?aluno=124344&ano=2024 → ?aluno={id}&ano={id}
    """
    parsed = urlparse(url)
    norm_path = normalize_path(parsed.path)
    if parsed.query:
        params = parse_qs(parsed.query)
        norm_params = "&".join(
            f"{k}={{id}}" if ID_PATTERN.match(v[0]) else f"{k}={v[0]}"
            for k, v in sorted(params.items())
        )
        return f"{norm_path}?{norm_params}"
    return norm_path


def has_repeating_segments(path: str) -> bool:
    """Deteta loops estruturais como /a/b/a/b ou /a/b/c/a/b/c."""
    parts = path.strip("/").split("/")
    n = len(parts)
    for size in range(1, n // 2 + 1):
        for i in range(n - size * 2 + 1):
            if parts[i:i+size] == parts[i+size:i+size*2]:
                return True
    return False


def is_excluded(url: str) -> bool:
    path = urlparse(url).path
    return any(path.startswith(prefix) for prefix in EXCLUDED_PREFIXES)


def should_ignore(url: str) -> bool:
    return any(re.search(p, url, re.IGNORECASE) for p in IGNORE_PATTERNS)


def is_same_domain(url: str) -> bool:
    parsed = urlparse(url)
    base_parsed = urlparse(BASE_URL)
    return (not parsed.netloc) or (parsed.netloc == base_parsed.netloc)


def is_under_aluno(url: str) -> bool:
    path = urlparse(url).path
    return path.startswith("/utente/eu/aluno")


def clean_url(href: str, current_url: str) -> str | None:
    try:
        full = urljoin(current_url, href)
        parsed = urlparse(full)
        result = parsed._replace(fragment="").geturl()
        return result if result.startswith("http") else None
    except Exception:
        return None


# ── Page info ─────────────────────────────────────────────────────────────────

def infer_data_type(url: str, title: str) -> str:
    combined = (url + " " + title).lower()
    mapping = {
        "grades":      ["nota", "aproveitamento", "classif", "grade"],
        "schedule":    ["horario", "horário", "aula", "schedule"],
        "absences":    ["falt", "presença", "absence"],
        "exams":       ["exame", "exam", "época", "epoch"],
        "documents":   ["doc", "ficheiro", "material", "arquivo"],
        "notices":     ["aviso", "notif", "mensagem", "notice"],
        "groups":      ["grupo", "group"],
        "assignments": ["trabalho", "entrega", "assignment"],
        "enrollments": ["inscri", "enroll", "matricul"],
        "statistics":  ["estatistic", "progresso", "ects"],
    }
    for dtype, keywords in mapping.items():
        if any(kw in combined for kw in keywords):
            return dtype
    return "unknown"


def extract_page_info(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    headings = [h.get_text(strip=True) for h in soup.find_all(["h1", "h2", "h3"])[:5]]
    table_headers = []
    for table in soup.find_all("table")[:3]:
        headers = [th.get_text(strip=True) for th in table.find_all("th")[:10]]
        if headers:
            table_headers.append(headers)
    forms = []
    for form in soup.find_all("form"):
        inputs = [
            {"name": inp.get("name", ""), "type": inp.get("type", "text")}
            for inp in form.find_all(["input", "select"])
            if inp.get("name") and inp.get("type") not in ("submit", "button", "hidden")
        ]
        forms.append({
            "action": form.get("action", ""),
            "method": form.get("method", "get").upper(),
            "inputs": inputs
        })
    parsed = urlparse(url)
    return {
        "url": url,
        "path": parsed.path,
        "query_params": list(parse_qs(parsed.query).keys()),
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
        print("[WARN] Credenciais não definidas.")
        return False

    print(f"[INFO] Login como '{CLIP_USER}'...")
    try:
        await page.goto(BASE_URL, timeout=30000, wait_until="domcontentloaded")

        user_selectors = ["input[name='utilizador']", "input[name='user']", "input[name='username']", "input[type='text']"]
        pass_selectors = ["input[name='senha']", "input[name='pass']", "input[name='password']", "input[type='password']"]

        user_field = None
        for sel in user_selectors:
            try:
                user_field = await page.wait_for_selector(sel, timeout=3000)
                if user_field: break
            except PlaywrightTimeout:
                continue

        pass_field = None
        for sel in pass_selectors:
            try:
                pass_field = await page.wait_for_selector(sel, timeout=3000)
                if pass_field: break
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
            print("[ERROR] Login falhou.")
            return False

        print(f"[OK] Login bem sucedido → {current_url}")
        return True

    except Exception as e:
        print(f"[ERROR] Login: {e}")
        return False


# ── Crawler ───────────────────────────────────────────────────────────────────

async def get_aluno_id(page) -> str | None:
    try:
        await page.goto(f"{BASE_URL}/utente/eu/aluno", timeout=20000, wait_until="domcontentloaded")
        params = parse_qs(urlparse(page.url).query)
        if "aluno" in params:
            return params["aluno"][0]
    except Exception:
        pass
    return None


async def crawl(page, start_url: str) -> list[dict]:
    visited_exact = set()       # URLs exatas já visitadas
    visited_normalized = defaultdict(int)  # contagem por padrão normalizado
    queue = [start_url]
    pages_data = []
    skipped_excluded = 0
    skipped_loop = 0
    skipped_pattern = 0

    print(f"[INFO] Crawl focado — sem limite de páginas, max {MAX_PER_PATTERN} por padrão\n")

    while queue:
        url = queue.pop(0)

        # 1. Nunca revisita a mesma URL exata
        if url in visited_exact:
            continue

        # 2. Só mesmo domínio
        if not is_same_domain(url):
            continue

        # 3. Ignora extensões/padrões inúteis
        if should_ignore(url):
            continue

        # 4. Só dentro de /utente/eu/aluno
        if not is_under_aluno(url):
            continue

        # 5. Rotas explicitamente excluídas
        if is_excluded(url):
            visited_exact.add(url)
            skipped_excluded += 1
            continue

        path = urlparse(url).path

        # 6. Deteção de loop estrutural no path
        if has_repeating_segments(path):
            visited_exact.add(url)
            skipped_loop += 1
            print(f"  [LOOP] {path}")
            continue

        # 7. Limite por padrão normalizado (URL completa com params)
        norm = normalize_url_for_dedup(url)
        if visited_normalized[norm] >= MAX_PER_PATTERN:
            skipped_pattern += 1
            visited_exact.add(url)
            continue

        visited_exact.add(url)
        visited_normalized[norm] += 1

        print(f"  [{len(pages_data)+1:04d}] {url}")

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

        info = extract_page_info(url, html)
        pages_data.append(info)

        soup = BeautifulSoup(html, "html.parser")
        new_links = 0
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"].strip()
            if not href:
                continue
            full_url = clean_url(href, url)
            if full_url and full_url not in visited_exact:
                queue.append(full_url)
                new_links += 1

        if new_links:
            print(f"         → {new_links} links (queue: {len(queue)}, excluídos: {skipped_excluded}, loops: {skipped_loop}, padrões: {skipped_pattern})")

    print(f"\n[OK] Crawl terminado: {len(pages_data)} páginas úteis")
    print(f"     Excluídas: {skipped_excluded} | Loops: {skipped_loop} | Padrão repetido: {skipped_pattern}")
    return pages_data


# ── Main ──────────────────────────────────────────────────────────────────────

async def run():
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        logged_in = await do_login(page)
        if not logged_in:
            print("[ERROR] Não foi possível fazer login. A terminar.")
            await browser.close()
            return

        aluno_id = await get_aluno_id(page)
        if aluno_id:
            print(f"[INFO] ID do aluno: {aluno_id}")
            start_url = f"{BASE_URL}/utente/eu/aluno?aluno={aluno_id}"
        else:
            print("[WARN] ID do aluno não detectado.")
            start_url = f"{BASE_URL}/utente/eu/aluno"

        pages = await crawl(page, start_url)
        await browser.close()

    patterns = defaultdict(list)
    for p in pages:
        patterns[p["normalized_path"]].append(p)

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

    routes.sort(key=lambda r: r["occurrences"], reverse=True)

    sitemap = {
        "base_url": BASE_URL,
        "scope": "/utente/eu/aluno",
        "total_pages_crawled": len(pages),
        "total_patterns": len(routes),
        "excluded_prefixes": EXCLUDED_PREFIXES,
        "routes": routes,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(sitemap, f, ensure_ascii=False, indent=2)

    print(f"\n[OK] Sitemap guardado em {OUTPUT_PATH}")
    print(f"[OK] {len(routes)} padrões de rota mapeados")


if __name__ == "__main__":
    asyncio.run(run())
