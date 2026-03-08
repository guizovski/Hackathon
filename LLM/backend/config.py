"""
Configuração global partilhada por todos os módulos do backend.
Carregado uma vez no arranque — nunca importar de ficheiros que importem daqui.
"""

import os
import json
import datetime
from urllib.parse import urlparse, parse_qs, unquote


def _unquote(url: str) -> str:
    """Decodifica URLs do CLIP que usam Latin-1 (não UTF-8)."""
    return unquote(url, encoding="latin-1")


# ── Variáveis de ambiente ─────────────────────────────────────────────────────
OLLAMA_URL    = os.getenv("OLLAMA_URL", "http://ollama:11434")
CLIP_BASE_URL = os.getenv("CLIP_BASE_URL", "https://clip.fct.unl.pt")
SITEMAP_PATH  = os.getenv("SITEMAP_PATH", "/app/data/sitemap.json")
ROUTING_MODEL = os.getenv("ROUTING_MODEL", os.getenv("LLM_MODEL", "tejo"))


# ── Sitemap ───────────────────────────────────────────────────────────────────
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
    params_found = {}
    for route in SITEMAP.get("routes", []):
        example_url = route.get("example_url", "")
        if not example_url:
            continue
        parsed = urlparse(example_url)
        qs = parse_qs(parsed.query)
        for key, values in qs.items():
            if key not in params_found:
                params_found[key] = values[0]
        segments = parsed.path.strip("/").split("/")
        for i, seg in enumerate(segments):
            if seg.isdigit() and len(seg) >= 4:
                prev = segments[i - 1] if i > 0 else ""
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
    if month >= 9:
        ano_lectivo = year + 1
        periodo     = 1
    elif month == 1:
        ano_lectivo = year
        periodo     = 1
    else:
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

# Student ID presente nas URLs do sitemap (placeholder de substituição)
SITEMAP_STUDENT_ID: str = REAL_PARAMS.get("aluno", "")


# ── Substituição de student_id ────────────────────────────────────────────────
def substitute_student_id(url: str, student_id: str) -> str:
    """Substitui o student_id do sitemap pelo do utilizador actual."""
    if SITEMAP_STUDENT_ID and student_id and student_id != SITEMAP_STUDENT_ID:
        return url.replace(f"aluno={SITEMAP_STUDENT_ID}", f"aluno={student_id}")
    return url
