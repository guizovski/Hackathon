"""
Gera um Modelfile para Ollama com o sitemap do CLIP embutido no system prompt.
Uso: python3 generate_modelfile.py [--base qwen2.5:7b] [--output Modelfile]
"""
import json
import re
import argparse
from urllib.parse import unquote, urlparse

SITEMAP_PATH       = "data/sitemap.json"
TRAINING_DATA_PATH = "training_data.json"

DTYPE_LABEL = {
    "schedule":    "horário de aulas",
    "grades":      "notas/classificações",
    "exams":       "exames e testes",
    "absences":    "faltas/presenças",
    "enrollments": "inscrições",
    "documents":   "documentos académicos",
    "notices":     "avisos",
}

# Número de exemplos por categoria nos few-shots
MAX_EXAMPLES_PER_GROUP = 3


def _find_url_for_pattern(sitemap: dict, pattern: str) -> str | None:
    """Encontra a URL do sitemap cujo path decodificado contém o padrão."""
    if not pattern or pattern in ("__action__", None):
        return None
    pattern_dec = unquote(pattern, encoding="utf-8")
    for r in sitemap.get("routes", []):
        url = r.get("example_url", "")
        path_dec = unquote(urlparse(url).path, encoding="latin-1")
        if pattern_dec in path_dec or pattern in path_dec:
            return url
    return None


def load_few_shot(sitemap: dict) -> list[tuple[str, str]]:
    """
    Carrega exemplos de treino do training_data.json.
    Cada exemplo é (pergunta, url_exacta_do_sitemap) — URLs reais, não padrões.
    Grupos com route_pattern=None geram exemplos de recusa.
    """
    try:
        with open(TRAINING_DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []

    examples = []
    for ts in data.get("training_sets", []):
        pattern = ts.get("route_pattern")
        questions = ts.get("questions", [])

        if pattern is None:
            for q in questions[:2]:
                examples.append((q, "__action__"))
        else:
            # Resolve o padrão para a URL exacta do sitemap
            url = ts.get("example_url") or _find_url_for_pattern(sitemap, pattern)
            if not url:
                # fallback: usa o padrão tal qual (menos ideal)
                url = pattern
            for q in questions[:MAX_EXAMPLES_PER_GROUP]:
                examples.append((q, url))
    return examples


def build_routes_reference(sitemap: dict) -> str:
    """Lista compacta: path decodificado [tipo] → URL completa (numa linha)."""
    lines = []
    seen = set()
    for r in sitemap.get("routes", []):
        url = r.get("example_url", "")
        if not url:
            continue
        path = unquote(urlparse(url).path, encoding="latin-1")
        if path in seen:
            continue
        seen.add(path)
        dtype = r.get("data_type", "unknown")
        label = DTYPE_LABEL.get(dtype, "")
        tag = f" [{label}]" if label else ""
        lines.append(f"  {path}{tag} → {url}")
    return "\n".join(lines)


def build_specific_rules(sitemap: dict) -> str:
    """Gera regras específicas por categoria a partir do training_data."""
    try:
        with open(TRAINING_DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return ""

    rules = []
    rule_n = 6
    for ts in data.get("training_sets", []):
        pattern = ts.get("route_pattern")
        if not pattern or pattern == "__action__":
            continue
        url = ts.get("example_url") or _find_url_for_pattern(sitemap, pattern)
        if not url:
            continue
        desc = ts.get("description", "")
        # Gera uma regra explícita: "Se X → usa URL Y"
        rules.append(f"{rule_n}. {desc} → usa EXACTAMENTE: {url}")
        rule_n += 1
    # Regra de desambiguação: resumo vs resultados
    rules.append(
        f"{rule_n}. IMPORTANTE — disambiguação de rotas de notas: "
        f"'quais são as minhas notas', 'que notas tive', 'histórico', 'média', 'créditos ECTS', 'cadeiras concluídas' "
        f"→ SEMPRE usa a rota de resumo académico (situação/resumo). "
        f"'notas deste semestre', 'resultados do ano lectivo actual', 'aprovado/reprovado em X' "
        f"→ usa a rota de resultados (ano_lectivo/resultados)."
    )
    return "\n".join(rules)


def build_few_shot_section(sitemap: dict) -> str:
    examples = load_few_shot(sitemap)
    lines = []
    for pergunta, url in examples:
        lines.append(f'  P: "{pergunta}"')
        if url == "__action__":
            lines.append('  R: {"intent": "unsupported_action", "routes_to_fetch": [], "cadeira": "", "reasoning": "acao nao suportada"}')
        else:
            lines.append(f'  R: {{"intent": "read", "routes_to_fetch": ["{url}"], "cadeira": "", "reasoning": "..."}}')
        lines.append("")
    if not examples:
        lines.append("  (sem exemplos)")
    return "\n".join(lines)


def _few_shot_messages(sitemap: dict) -> str:
    """Gera blocos MESSAGE user/assistant para few-shot no Modelfile."""
    examples = load_few_shot(sitemap)
    lines = []
    for pergunta, url in examples:
        if url == "__action__":
            answer = '{"intent": "unsupported_action", "routes_to_fetch": [], "cadeira": "", "reasoning": "acao nao suportada"}'
        else:
            answer = f'{{"intent": "read", "routes_to_fetch": ["{url}"], "cadeira": "", "reasoning": "..."}}'
        lines.append(f'MESSAGE user "{pergunta}"')
        lines.append(f'MESSAGE assistant "{answer}"')
        lines.append("")
    return "\n".join(lines)


def generate_modelfile(sitemap: dict, base_model: str) -> str:
    specific_rules = build_specific_rules(sitemap)

    system_prompt = f"""És um assistente especializado no portal académico CLIP da FCT-UNL.
O teu objetivo é: dada uma pergunta do utilizador, identificar a intenção e devolver um JSON com as URL(s) do CLIP a consultar.

RESPONDE SEMPRE E APENAS com JSON neste formato exato (sem texto extra, sem markdown):
{{"intent": "read", "routes_to_fetch": ["url_completa"], "cadeira": "", "reasoning": "uma linha"}}

INTENTS POSSÍVEIS:
- "read"               → pergunta de leitura de dados (padrão)
- "ics_export"         → exportar horário (.ics / Google Calendar / Outlook / Apple Calendar / importar horário)
                         routes_to_fetch deve ser []
- "enroll_test"        → inscrever-se num teste ("inscreve-me", "quero inscrever-me no teste de X")
                         routes_to_fetch deve ser []
                         cadeira: nome da cadeira mencionada, "" se não especificado, "__todos__" se quer todos
- "unsupported_action" → acção de escrita não suportada (submeter, cancelar, apagar, matricular, etc.)
                         routes_to_fetch deve ser []

REGRAS CRÍTICAS:
1. Usa as URLs das regras abaixo EXACTAMENTE como aparecem — encoding Latin-1 (%E9, %E7%E3o, %ED, etc).
2. NUNCA inventes URLs nem parâmetros — apenas usa as que estão listadas abaixo.
3. NUNCA uses encoding UTF-8 (%C3%A9, %C3%A7, etc) — o CLIP só aceita Latin-1.
4. Responde APENAS com o JSON. Nenhum texto antes ou depois.
5. Se a pergunta não corresponder a nenhuma regra abaixo, devolve {{"intent": "read", "routes_to_fetch": [], "cadeira": "", "reasoning": "nao sei"}}.
6. Responde SEMPRE em português europeu (pt-PT): "tu/tens/podes" — NUNCA "você/tem/pode".
7. CRÍTICO: routes_to_fetch deve conter APENAS strings que comecem exactamente com "https://clip.fct.unl.pt". NUNCA coloques rótulos, emojis, nomes de páginas, botões ou qualquer texto livre em routes_to_fetch. Se colocares algo que não seja uma URL https:// completa, a resposta é inválida.
{specific_rules}"""

    escaped = system_prompt.replace('"""', '\\"\\"\\"')

    few_shot = build_few_shot_section(sitemap)

    return f'''FROM {base_model}

PARAMETER temperature 0.05
PARAMETER top_p 0.9
PARAMETER num_ctx 16384

SYSTEM """
{escaped}
"""

# ── Exemplos few-shot ─────────────────────────────────────────────────────────
# Formato: MESSAGE user "pergunta" / MESSAGE assistant "{{json}}"
{_few_shot_messages(sitemap)}
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="qwen2.5:7b")
    parser.add_argument("--output", default="Modelfile")
    args = parser.parse_args()

    print(f"[INFO] A ler sitemap de {SITEMAP_PATH}...")
    with open(SITEMAP_PATH, encoding="utf-8") as f:
        sitemap = json.load(f)

    total = len(sitemap.get("routes", []))
    print(f"[INFO] {total} rotas encontradas")

    modelfile = generate_modelfile(sitemap, args.base)

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(modelfile)

    print(f"[OK] Modelfile gerado em '{args.output}'")
    print(f"[NEXT] ollama create tejo -f {args.output}")


if __name__ == "__main__":
    main()
