"""
ApostaGol - Gera o site do celular (roda sem perguntas)

Calcula o painel de hoje e de amanha (horario de Brasilia) com todas as competicoes
que tiverem jogo e gera a pasta site/ com uma pagina unica que funciona no celular
(e pode ser adicionada a tela inicial). Na nuvem, o GitHub Actions roda isto toda
manha e publica a pasta site/ no GitHub Pages.

Como usar:
    python gerar_site.py               (hoje e amanha)
    python gerar_site.py 2026-10-10    (a partir de outra data - bom pra testar)
"""

import json
import os
import sys
from datetime import date, datetime, timedelta

from PIL import Image, ImageDraw, ImageFont

import apostagol_dia as ag

PASTA_SITE = os.path.join(ag.PASTA, "site")
TEMPLATE = os.path.join(ag.PASTA, "web", "painel.html")
MARCADOR_DADOS = "/*__DADOS__*/null"
COR_TEMA = "#0f5132"


def cenario_para_json(c):
    """So o necessario pra pagina mostrar o cenario, refazer as contas da 'Conferir odd'
    e conferir o resultado depois (minhas apostas)."""
    periodo = c["campo"] if c["tipo"] == "gols" else "total"
    lam_casa, lam_fora, rho = c["esperados"][periodo]
    return {
        "id": f"{c['jogo']['id']}-{ag.chave_mercado(c)}",
        "jogo_id": c["jogo"]["id"],
        "liga": c["competicao"],
        "data": ag.data_brasil(c["jogo"]).date().isoformat(),
        "hora": ag.data_brasil(c["jogo"]).strftime("%H:%M"),
        "inicio": c["jogo"]["utcDate"],  # a pagina esconde o cenario quando o jogo ja comecou
        "casa": c["casa_nome"], "fora": c["fora_nome"],
        "rotulo": c["rotulo"], "tipo": c["tipo"], "campo": c["campo"], "linha": c["linha"],
        "prob": round(c["prob"], 6),
        "lam_casa": lam_casa, "lam_fora": lam_fora, "rho": rho,
        "calib": ag.CALIBRACAO.get(ag.chave_mercado(c)),
    }


def times_brasileiros(cliente, hoje):
    """IDs dos times do Brasileirao (temporada atual e anterior), pra achar os brasileiros
    em outras competicoes (ex: Libertadores). Usa o cache em disco do historico."""
    ids = set()
    for ano in (hoje.year, hoje.year - 1):
        for p in ag.carregar_temporada(cliente, "BSA", ano):
            ids.update((p["casa"], p["fora"]))
    return ids


def faixas_json(candidatos):
    return [{"nome": nome, "min": odd_min, "max": odd_max, "itens": [cenario_para_json(c) for c in itens]}
            for nome, odd_min, odd_max, itens in ag.montar_painel(candidatos)]


def painel_do_dia(cliente, data_ref, brasileiros):
    print(f"\n=== Painel de {data_ref.isoformat()} ===")
    jogos = ag.buscar_jogos_do_dia(cliente, data_ref, incluir_em_andamento=True)
    codigos = [codigo for codigo in ag.COMPETICOES if codigo in jogos]
    faixas, faixas_brasil, modelos = [], [], {}
    if codigos:
        todos = ag.calcular_candidatos(cliente, jogos, codigos)
        # gols esperados de cada jogo (inclusive os que ja comecaram): a aba Ao vivo recalcula com o placar
        for c in todos:
            modelos.setdefault(str(c["jogo"]["id"]), {
                "jogo_id": c["jogo"]["id"], "liga": c["competicao"], "casa": c["casa_nome"], "fora": c["fora_nome"],
                "inicio": c["jogo"]["utcDate"], "hora": ag.data_brasil(c["jogo"]).strftime("%H:%M"),
                "esp": {periodo: list(v) for periodo, v in c["esperados"].items()},
            })
        candidatos = [c for c in todos if c["jogo"].get("status") in ("SCHEDULED", "TIMED")]
        faixas = faixas_json(candidatos)
        do_brasil = [c for c in candidatos if c["competicao"] == "BSA"
                     or c["jogo"]["homeTeam"]["id"] in brasileiros or c["jogo"]["awayTeam"]["id"] in brasileiros]
        if do_brasil:
            faixas_brasil = faixas_json(do_brasil)
    return {"data": data_ref.isoformat(), "jogos": sum(len(v) for v in jogos.values()),
            "ligas": codigos, "faixas": faixas, "faixas_brasil": faixas_brasil, "modelos": list(modelos.values())}


def resultados_recentes(cliente, hoje):
    """Placar dos jogos dos ultimos dias, pra pagina conferir as apostas salvas.
    {id do jogo: {"s": status, "ft": [casa, fora], "ht": [casa, fora]}}"""
    saida = {}
    # a API aceita no maximo 10 dias por consulta: 8 pra tras + hoje + amanha
    for m in ag.buscar_partidas(cliente, hoje - timedelta(days=8), hoje + timedelta(days=1)):
        if m.get("status") in ("SCHEDULED", "TIMED"):
            continue
        placar = m.get("score") or {}
        ft = placar.get("regularTime") or placar.get("fullTime") or {}
        ht = placar.get("halfTime") or {}
        saida[str(m["id"])] = {
            "s": m["status"],
            "ft": [ft["home"], ft["away"]] if ft.get("home") is not None else None,
            "ht": [ht["home"], ht["away"]] if ht.get("home") is not None else None,
        }
    return saida


def gerar_icone(tamanho, caminho):
    img = Image.new("RGB", (tamanho, tamanho), COR_TEMA)
    d = ImageDraw.Draw(img)
    m = tamanho // 6
    d.ellipse([m, m, tamanho - m, tamanho - m], fill="#ffffff")
    try:
        fonte = ImageFont.load_default(size=tamanho // 3)
    except TypeError:  # Pillow antigo
        fonte = ImageFont.load_default()
    d.text((tamanho / 2, tamanho / 2), "AG", fill=COR_TEMA, font=fonte, anchor="mm")
    img.save(caminho)


def main():
    if os.environ.get("CI") and not os.environ.get("FOOTBALL_DATA_TOKEN"):
        sys.exit("Faltou o Secret FOOTBALL_DATA_TOKEN no GitHub (Settings -> Secrets and variables -> Actions).")
    hoje = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else datetime.now(ag.FUSO_BRASIL).date()
    cliente = ag.ClienteAPI(ag.carregar_api_key())

    brasileiros = times_brasileiros(cliente, hoje)
    dias = [painel_do_dia(cliente, hoje, brasileiros), painel_do_dia(cliente, hoje + timedelta(days=1), brasileiros)]
    proximas = {}
    if not any(d["faixas"] for d in dias):
        proximas = ag.proximas_datas(cliente, hoje)

    dados = {
        "gerado_em": datetime.now(ag.FUSO_BRASIL).strftime("%d/%m/%Y %H:%M"),
        "dias": dias,
        "proximas": proximas,
        "resultados": resultados_recentes(cliente, hoje),
        "ligas": {codigo: info[0] for codigo, info in ag.COMPETICOES.items()},
        "calibrado": bool(ag.CALIBRACAO),
        "config": {
            "odd_minima": ag.ODD_MINIMA, "odd_maxima": ag.ODD_MAXIMA, "max_gols": ag.MAX_GOLS_MODELO,
            "alerta_edge": ag.ALERTA_EDGE_ALTO, "fator_forma": ag.FATOR_FORMA, "fator_desfalque": 0.85,
            "kelly_fracao": 0.25, "kelly_teto": 0.02,
        },
    }

    os.makedirs(PASTA_SITE, exist_ok=True)
    with open(TEMPLATE, "r", encoding="utf-8") as f:
        html = f.read()
    # "</" escapado pra um nome de time nunca fechar a tag <script> sem querer
    html = html.replace(MARCADOR_DADOS, json.dumps(dados, ensure_ascii=False).replace("</", "<\\/"))
    with open(os.path.join(PASTA_SITE, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(PASTA_SITE, "painel.json"), "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False)
    with open(os.path.join(PASTA_SITE, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"name": "ApostaGol", "short_name": "ApostaGol", "start_url": ".", "display": "standalone",
                   "background_color": COR_TEMA, "theme_color": COR_TEMA, "lang": "pt-BR",
                   "icons": [{"src": "icone-192.png", "sizes": "192x192", "type": "image/png"},
                             {"src": "icone-512.png", "sizes": "512x512", "type": "image/png"}]}, f)
    for tamanho in (192, 512):
        gerar_icone(tamanho, os.path.join(PASTA_SITE, f"icone-{tamanho}.png"))
    gerar_icone(180, os.path.join(PASTA_SITE, "apple-touch-icon.png"))

    total = sum(len(fx["itens"]) for d in dias for fx in d["faixas"])
    print(f"\nSite gerado em {PASTA_SITE} ({total} cenarios em {len(dias)} dias).")


if __name__ == "__main__":
    main()
