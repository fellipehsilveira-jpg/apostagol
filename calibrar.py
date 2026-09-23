"""
ApostaGol - Calibracao das probabilidades do painel

O backtest mostrou que o modelo e otimista (quando diz 67%, acontece ~63%). Este
script refaz as previsoes do passado pra TODOS os mercados do painel (gols no jogo,
1o tempo, 2o tempo, resultado e chance dupla), compara com o que aconteceu e aprende
uma correcao por mercado. Nao precisa de odds - so de previsao x resultado.

Protecao contra "decorar o passado": a correcao e aprendida na temporada de escolha
e so e aceita se melhorar o erro (Brier) nas temporadas seguintes, que ela nao viu.
As aceitas sao reajustadas com todo o historico e salvas em calibracao.json, que o
apostagol_dia.py aplica automaticamente no painel.

Como usar (vale rodar de novo de tempos em tempos, ex: uma vez por mes):
    python calibrar.py      (~3 minutos)
"""

import io
import json
import math
import time
from collections import defaultdict
from contextlib import redirect_stdout

import numpy as np

import apostagol_dia as ag
import backtest as bt
from experimentos import ajustar_logistica

MIN_CASOS = 300  # menos que isso na temporada de escolha: nao da pra confiar na correcao


class PrevisorTodosMercados:
    """Mesmo modelo do programa diario, prevendo todos os mercados do painel.
    O Brasileirao nao tem placar de intervalo nas planilhas, entao fica so com o jogo todo."""

    def __init__(self, liga, treino, inicio, xi):
        periodos = ("total",) if liga == "BSA" else ("total", "primeiro_tempo", "segundo_tempo")
        self.modelo = ag.ModeloCompeticao(liga, treino, data_base=inicio, xi=xi, periodos=periodos)
        self.jogos_por_time = self.modelo.jogos_por_time

    def probs(self, casa, fora):
        esperados = self.modelo.gols_esperados(casa, fora)
        saida = {}
        for m in ag.mercados_do_jogo(casa, fora):
            periodo = m["campo"] if m["tipo"] == "gols" else "total"
            if periodo in esperados:
                saida[ag.chave_mercado(m)] = ag.prob_mercado({**m, "esperados": esperados})
        return saida


def aconteceu(chave, ft, ht):
    tipo, campo, linha = chave.split("|")
    casa, fora = ft
    if tipo == "resultado":
        return {"vitoria_casa": casa > fora, "empate": casa == fora, "vitoria_fora": casa < fora,
                "dupla_1x": casa >= fora, "dupla_x2": casa <= fora, "dupla_12": casa != fora}[campo]
    if campo == "total":
        gols = casa + fora
    else:
        if not ht or ht[0] > casa or ht[1] > fora:
            return None  # sem placar de intervalo confiavel
        gols = ht[0] + ht[1] if campo == "primeiro_tempo" else (casa - ht[0]) + (fora - ht[1])
    return gols > float(linha)


def _logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _aplicar(w, probs):
    return 1 / (1 + np.exp(-(w[0] * np.array([_logit(p) for p in probs]) + w[1])))


def brier(probs, reais):
    return float(np.mean((np.array(probs) - np.array(reais)) ** 2))


def main():
    print("=== ApostaGol - calibracao das probabilidades do painel ===\n")
    t = time.time()
    with redirect_stdout(io.StringIO()):
        dados = bt.carregar_dados()
    print("Refazendo as previsoes do passado semana a semana (todos os mercados)...")
    previsoes = bt.gerar_previsoes(dados, ag.XI_PESO_TEMPORAL, mostrar_progresso=False,
                                   fabrica_modelo=PrevisorTodosMercados)
    print(f"  {len(previsoes)} jogos previstos ({time.time() - t:.0f}s)")

    casos = defaultdict(list)  # chave -> [(escolha?, prob, aconteceu)]
    for pv in previsoes:
        p = pv["partida"]
        for chave, prob in pv["probs"].items():
            real = aconteceu(chave, p["ft"], p["ht"])
            if real is not None:
                casos[chave].append((p["temporada"] in bt.TEMPORADAS_OTIMIZACAO, prob, float(real)))

    rotulos = {ag.chave_mercado(m): m["rotulo"] for m in ag.mercados_do_jogo("mandante", "visitante")}
    print("\nNas temporadas de CONFIRMACAO (a correcao nao as viu). 'Zona do painel' = casos em que o")
    print(f"modelo deu pelo menos {ag.PROB_MINIMA_PAINEL*100:.0f}% - e o que aparece pra voce.\n")
    print(f"  {'mercado':38s} {'casos':>6s} | {'zona do painel: modelo -> real -> corrigido':^44s} | "
          f"{'erro antes':>10s} {'depois':>7s}")
    aprovados = {}
    for chave in rotulos:
        lista = casos.get(chave, [])
        escolha = [(pr, r) for e, pr, r in lista if e]
        confirmacao = [(pr, r) for e, pr, r in lista if not e]
        if len(escolha) < MIN_CASOS or len(confirmacao) < MIN_CASOS:
            print(f"  {rotulos[chave]:38s} poucos casos, sem correcao")
            continue
        w = ajustar_logistica(np.array([[_logit(pr)] for pr, _ in escolha]), np.array([r for _, r in escolha]))
        probs_c = [pr for pr, _ in confirmacao]
        reais_c = [r for _, r in confirmacao]
        corrigidas = _aplicar(w, probs_c)
        antes, depois = brier(probs_c, reais_c), brier(corrigidas, reais_c)
        zona = [i for i, pr in enumerate(probs_c) if pr >= ag.PROB_MINIMA_PAINEL]
        if zona:
            z_mod = np.mean([probs_c[i] for i in zona]) * 100
            z_real = np.mean([reais_c[i] for i in zona]) * 100
            z_corr = np.mean([corrigidas[i] for i in zona]) * 100
            txt_zona = f"{z_mod:5.1f}% -> {z_real:5.1f}% -> {z_corr:5.1f}% ({len(zona)} casos)"
        else:
            txt_zona = "-"
        melhorou = depois < antes
        if melhorou:
            todos = [(pr, r) for _, pr, r in lista]
            aprovados[chave] = [float(x) for x in ajustar_logistica(
                np.array([[_logit(pr)] for pr, _ in todos]), np.array([r for _, r in todos]))]
        print(f"  {rotulos[chave]:38s} {len(confirmacao):6d} | {txt_zona:^44s} | {antes:10.4f} {depois:7.4f}"
              f" {'OK' if melhorou else 'mantem original'}")

    with open(ag.ARQUIVO_CALIBRACAO, "w", encoding="utf-8") as f:
        json.dump({"gerado_em": time.strftime("%Y-%m-%d %H:%M"), "mercados": aprovados}, f, indent=1)
    print(f"\n{len(aprovados)} de {len(rotulos)} mercados com correcao aprovada. Salvo em {ag.ARQUIVO_CALIBRACAO}")
    print("O apostagol_dia.py passa a usar essas correcoes automaticamente no painel.")


if __name__ == "__main__":
    main()
