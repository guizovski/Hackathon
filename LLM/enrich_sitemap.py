"""
enrich_sitemap.py
=================
Visita cada rota do sitemap com o cookie do utilizador e analisa a
estrutura da página HTML — sem guardar dados pessoais.

Adiciona/actualiza a chave `page_structure` em cada entrada do sitemap:
  {
    "is_navigation": bool,         # página de selecção de período/semestre
    "tables": [                    # uma entrada por tabela encontrada
      {
        "columns": [...],          # cabeçalhos das colunas (texto dos <th>/<td> da 1ª linha)
        "row_count_approx": N,     # nº de linhas de dados (estimativa)
        "has_checkbox": bool,      # tabela com checkboxes (inscrição)
        "submit_button": "text"    # texto do botão submit dentro da tabela, se existir
      }
    ],
    "key_classes": [...],          # classes CSS com significado semântico detectadas
    "link_patterns": [...],        # parâmetros encontrados nos hrefs (ex: "dia=N", "início=HHMM")
    "forms": [                     # formulários (acção + campos, sem valores)
      {"action": "...", "fields": ["campo1", "campo2"]}
    ],
    "description": "..."           # resumo gerado automaticamente
  }

Uso:
  python3 enrich_sitemap.py --cookie "clip_session=XYZ" [--sitemap data/sitemap.json] [--dry-run]
"""

import argparse
import asyncio
import json
import re
import sys
from urllib.parse import unquote, urlparse

import httpx
from bs4 import BeautifulSoup

CLIP_BASE_URL = "https://clip.fct.unl.pt"

# Classes CSS do CLIP com significado estrutural
SEMANTIC_CLASSES = {
    "celulaDeCalendario",   # célula de aula no horário
    "calendário",
    "resultados",
    "linhaImpar", "linhaPar",   # linhas alternadas de tabelas de dados
    "error", "aviso",
    "formulário", "form",
}

# Padrões nos hrefs que indicam o esquema dos dados (ex: dia=2, início=800)
HREF_PARAM_PATTERNS = [
    r"\bdia=\d+",
    r"\bin(?:%ED|i)(?:%E7|c)io=\d+",   # início= (com ou sem encoding)
    r"\bano_lectivo=\d+",
    r"\bper(?:%ED|i)odo_lectivo=\d+",
    r"\btipo_de_per",
    r"\baluno=\d+",
    r"\bunidade_curricular=",
    r"\bturma=",
]


def _unquote(s: str) -> str:
    return unquote(s, encoding="latin-1")


async def fetch(url: str, cookie: str, client: httpx.AsyncClient) -> str | None:
    try:
        resp = await client.get(
            url,
            headers={"Cookie": cookie, "User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.text
    except Exception as e:
        print(f"  [WARN] fetch failed for {url}: {e}", file=sys.stderr)
    return None


async def fetch_following_period(url: str, cookie: str, client: httpx.AsyncClient) -> tuple:
    """Faz fetch e, se a página for de navegação por períodos, segue para o mais recente."""
    html = await fetch(url, cookie, client)
    if not html:
        return None, url

    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "tipo_de_per" not in href:
            continue
        full_url = href if href.startswith("http") else f"{CLIP_BASE_URL}{href}"
        m_ano = re.search(r"ano_lectivo=(\d+)", href)
        m_per = re.search(r"per(?:%ED|i)odo_lectivo=(\d+)", href)
        ano = int(m_ano.group(1)) if m_ano else 0
        per = int(m_per.group(1)) if m_per else 0
        candidates.append((ano, per, full_url))

    if not candidates:
        return html, url  # já é página de dados

    # Segue para o período mais recente
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best_url = candidates[0][2]
    print(f"  → navega para período: {_unquote(best_url.split('?')[0].replace(CLIP_BASE_URL, ''))}")
    followed = await fetch(best_url, cookie, client)
    return followed or html, best_url


def analyse_structure(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    # ── 1. Página de navegação? ───────────────────────────────────────────────
    nav_links = [
        a["href"] for a in soup.find_all("a", href=True)
        if "tipo_de_per" in a["href"] or (
            "ano_lectivo" in a["href"] and
            re.search(r"(sem|tri|per)", a["href"], re.I)
        )
    ]
    is_navigation = len(nav_links) >= 2

    # ── 2. Tabelas ────────────────────────────────────────────────────────────
    tables_info = []
    for table in soup.find_all("table"):
        # Cabeçalhos: primeira linha com <th> ou <td>
        header_row = table.find("tr")
        if not header_row:
            continue
        columns = [
            cell.get_text(" ", strip=True)
            for cell in header_row.find_all(["th", "td"])
            if cell.get_text(strip=True)
        ]
        if not columns:
            continue

        data_rows = [
            tr for tr in table.find_all("tr")[1:]
            if tr.find_all("td")
        ]
        has_checkbox = bool(table.find("input", {"type": "checkbox"}))
        submit_btn = table.find("input", {"type": "submit"})
        submit_text = submit_btn.get("value", "").strip() if submit_btn else None

        tables_info.append({
            "columns": columns,
            "row_count_approx": len(data_rows),
            "has_checkbox": has_checkbox,
            **({"submit_button": submit_text} if submit_text else {}),
        })

    # ── 3. Classes CSS com significado ────────────────────────────────────────
    all_classes: set[str] = set()
    for tag in soup.find_all(True):
        for cls in (tag.get("class") or []):
            if cls in SEMANTIC_CLASSES:
                all_classes.add(cls)
    # Detecta celulaDeCalendario mesmo que não esteja na lista acima
    for tag in soup.find_all(class_=True):
        for cls in tag["class"]:
            if "calendario" in cls.lower() or "calendario" in cls.lower():
                all_classes.add(cls)
            if "celula" in cls.lower():
                all_classes.add(cls)

    # ── 4. Padrões nos hrefs ──────────────────────────────────────────────────
    all_hrefs = " ".join(a.get("href", "") for a in soup.find_all("a", href=True))
    matched_patterns = [
        pat for pat in HREF_PARAM_PATTERNS
        if re.search(pat, all_hrefs, re.IGNORECASE)
    ]

    # ── 5. Formulários ────────────────────────────────────────────────────────
    forms_info = []
    for form in soup.find_all("form"):
        action = _unquote(form.get("action", ""))
        # Ignora o formulário de atalhos (sempre presente)
        if "atalhos" in action:
            continue
        fields = [
            inp.get("name", "")
            for inp in form.find_all(["input", "select", "textarea"])
            if inp.get("name") and inp.get("type") not in ("hidden", "submit", "button")
        ]
        if fields or action:
            forms_info.append({"action": action, "fields": fields})

    # ── 6. Descrição automática ───────────────────────────────────────────────
    desc_parts = []
    if is_navigation:
        desc_parts.append("Página de navegação por períodos lectivos.")
    if "celulaDeCalendario" in all_classes:
        desc_parts.append(
            "Grelha de horário: células <td class='celulaDeCalendario'> com "
            "parâmetros dia=N (2=Seg…6=Sex) e início=HHMM no href; "
            "rowspan/2 = duração em horas."
        )
    for t in tables_info:
        btn = t.get("submit_button", "")
        cols = ", ".join(t["columns"][:6])
        row_note = f"{t['row_count_approx']} linha(s)" if t["row_count_approx"] else "sem linhas"
        cb = " (com checkboxes)" if t["has_checkbox"] else ""
        btn_note = f" [botão: {btn}]" if btn else ""
        desc_parts.append(f"Tabela{cb}{btn_note}: {cols} — {row_note}.")
    if forms_info:
        for f in forms_info:
            fields_str = ", ".join(f["fields"]) if f["fields"] else "(sem campos visíveis)"
            desc_parts.append(f"Formulário ({f['action']}): {fields_str}.")

    description = " ".join(desc_parts) if desc_parts else "Estrutura não reconhecida."

    return {
        "is_navigation": is_navigation,
        "tables": tables_info,
        "key_classes": sorted(all_classes),
        "link_patterns": matched_patterns,
        "forms": forms_info,
        "description": description,
    }


async def main():
    parser = argparse.ArgumentParser(description="Enrich CLIP sitemap with page structure info.")
    parser.add_argument("--cookie", required=True, help="Valor do cookie de sessão (ex: 'clip_session=abc')")
    parser.add_argument("--sitemap", default="data/sitemap.json", help="Caminho para o sitemap JSON")
    parser.add_argument("--dry-run", action="store_true", help="Não guarda alterações, só imprime")
    parser.add_argument("--only-unknown", action="store_true",
                        help="Só processa rotas sem page_structure")
    args = parser.parse_args()

    with open(args.sitemap, encoding="utf-8") as f:
        sitemap = json.load(f)

    routes = sitemap.get("routes", [])
    print(f"[INFO] {len(routes)} rotas no sitemap.")

    async with httpx.AsyncClient() as client:
        for i, route in enumerate(routes):
            url = route.get("example_url", "")
            if not url:
                continue
            if args.only_unknown and route.get("page_structure"):
                continue

            print(f"[{i+1}/{len(routes)}] {_unquote(urlparse(url).path)}")
            html, final_url = await fetch_following_period(url, args.cookie, client)
            if not html:
                print("  → sem resposta, a saltar")
                continue

            structure = analyse_structure(html, final_url)
            # Guarda qual foi o URL real da página de dados (após seguir período)
            if final_url != url:
                structure["data_url_example"] = final_url
            route["page_structure"] = structure
            print(f"  → {structure['description'][:120]}")

            # Pequena pausa para não sobrecarregar o servidor
            await asyncio.sleep(0.5)

    if args.dry_run:
        print("\n[DRY RUN] Não foram guardadas alterações.")
        return

    with open(args.sitemap, "w", encoding="utf-8") as f:
        json.dump(sitemap, f, ensure_ascii=False, indent=2)
    print(f"\n[OK] Sitemap actualizado em {args.sitemap}")


if __name__ == "__main__":
    asyncio.run(main())
