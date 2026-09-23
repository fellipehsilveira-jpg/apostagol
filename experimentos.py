"""
ApostaGol - Experimentos de modelo (so backtest, nao mexe no programa do dia a dia)

Compara variantes do modelo nas mesmas temporadas e com as mesmas regras do backtest:
    1. Outros modelos de gols da penaltyblog (e Dixon-Coles com 3 temporadas de historico)
    2. "Stacking": combina a probabilidade da Bet365 (sem margem) com a do modelo numa
       regressao logistica ajustada so com jogos anteriores. O peso que ela da ao modelo
       responde: o nosso modelo acrescenta alguma informacao que a Bet365 ainda nao tem?

Criterio de aprovacao (definido ANTES de rodar), nas temporadas de confirmacao:
    ROI > 0, CLV medio >= 0 e pelo menos 200 apostas.

Como usar:
    python experimentos.py      (~10 minutos)
"""

import io
import math
import time
from collections import defaultdict
from contextlib import redirect_stdout

import numpy as np
from scipy.optimize import minimize

import apostagol_dia as ag
import backtest as bt

FAIXA_COMBINADA = (1.20, 1.45)
MIN_EXEMPLOS_STACKING = 1000
MIN_APOSTAS_APROVACAO = 200
REGULARIZACAO = 1e-4

VARIANTES_MODELO = [
    ("Dixon-Coles 2 temporadas (atual)", bt.PrevisorDixonColes, 2),
    ("Dixon-Coles 3 temporadas", bt.PrevisorDixonColes, 3),
    ("Poisson bivariado", bt.fabrica_penaltyblog(ag.pb.models.BivariatePoissonGoalModel), 2),
    ("Binomial negativa", bt.fabrica_penaltyblog(ag.pb.models.NegativeBinomialGoalModel), 2),
    ("Poisson inflado de zeros", bt.fabrica_penaltyblog(ag.pb.models.ZeroInflatedPoissonGoalsModel), 2),
]
# WeibullCopulaGoalsModel ficou de fora: ~1.8s por ajuste x ~900 ajustes = ~27 min


# ---------- Stacking (Bet365 + modelo) ----------

def _logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def ajustar_logistica(X, y):
    """Regressao logistica com um pouco de regularizacao. Retorna [pesos..., intercepto]."""
    def perda(w):
        z = X @ w[:-1] + w[-1]
        return np.mean(np.logaddexp(0, z) - y * z) + REGULARIZACAO * np.sum(w[:-1] ** 2)

    inicio = np.zeros(X.shape[1] + 1)
    inicio[0] = 1.0  # comeca confiando no mercado
    return minimize(perda, inicio, method="BFGS").x


def empilhar(previsoes, usar_modelo=True):
    """Troca a probabilidade do modelo pela combinada Bet365 + modelo. Os coeficientes
    de cada mes sao ajustados so com jogos de meses anteriores (sem olhar o futuro)."""
    exemplos = defaultdict(list)  # mercado -> [(data, x_mercado, x_modelo, acertou, indice, mercado)]
    for i, prev in enumerate(previsoes):
        p = prev["partida"]
        for m in bt.MERCADOS:
            if m in p["justas"]:
                exemplos[m].append((p["data"], _logit(p["justas"][m]), _logit(prev["probs"][m]),
                                    bt.ganhou_mercado(m, p["ft"]), i))

    novas_probs = defaultdict(dict)
    corte_por_indice = {}
    pesos_finais = {}
    for m, lista in exemplos.items():
        lista.sort()
        meses = sorted({e[0][:7] for e in lista})
        for mes in meses:
            inicio_mes = f"{mes}-01"
            treino = [e for e in lista if e[0] < inicio_mes]
            if len(treino) < MIN_EXEMPLOS_STACKING:
                continue
            colunas = [1, 2] if usar_modelo else [1]
            X = np.array([[e[c] for c in colunas] for e in treino])
            y = np.array([e[3] for e in treino], dtype=float)
            w = ajustar_logistica(X, y)
            pesos_finais[m] = w
            for e in lista:
                if e[0][:7] == mes:
                    x = np.array([e[c] for c in colunas])
                    novas_probs[e[4]][m] = 1 / (1 + math.exp(-(x @ w[:-1] + w[-1])))
                    corte_por_indice[e[4]] = max(t[0] for t in treino)

    saida = []
    for i, prev in enumerate(previsoes):
        probs = novas_probs.get(i)
        if not probs or not all(m in probs for m in ("H", "D", "A")):
            continue
        soma = probs["H"] + probs["D"] + probs["A"]
        nova = {m: probs[m] / soma for m in ("H", "D", "A")}
        nova["O25"] = probs.get("O25", prev["probs"]["O25"])
        saida.append({**prev, "probs": nova, "treino_ate": corte_por_indice[i]})
    return saida, pesos_finais


# ---------- Avaliacao ----------

def avaliar(previsoes, temporadas):
    prevs = [pv for pv in previsoes if pv["partida"]["temporada"] in temporadas]
    brier = {"1x2": [0.0, 0.0, 0], "o25": [0.0, 0.0, 0]}
    calib = [0, 0.0, 0]
    for pv in prevs:
        p = pv["partida"]
        if all(k in p["justas"] for k in ("H", "D", "A")):
            for m in ("H", "D", "A"):
                real = bt.ganhou_mercado(m, p["ft"])
                brier["1x2"][0] += (pv["probs"][m] - real) ** 2
                brier["1x2"][1] += (p["justas"][m] - real) ** 2
            brier["1x2"][2] += 1
        if "O25" in p["justas"]:
            real = bt.ganhou_mercado("O25", p["ft"])
            brier["o25"][0] += (pv["probs"]["O25"] - real) ** 2
            brier["o25"][1] += (p["justas"]["O25"] - real) ** 2
            brier["o25"][2] += 1
        for m in bt.MERCADOS:
            if m in p["justas"] and 0.6 <= pv["probs"][m] < 0.8:
                calib[0] += 1
                calib[1] += pv["probs"][m]
                calib[2] += bt.ganhou_mercado(m, p["ft"])
    dif = {k: (b[0] - b[1]) / b[2] if b[2] else None for k, b in brier.items()}
    return {
        "jogos": len(prevs), "brier_dif": dif,
        "calib": (calib[1] / calib[0], calib[2] / calib[0]) if calib[0] else None,
        "regras": bt.resumo(bt.simular(prevs)["apostas"]),
        "faixa": bt.resumo(bt.simular(prevs, faixa_odd=FAIXA_COMBINADA)["apostas"]),
    }


def aprovado(r):
    return bool(r and r["n"] >= MIN_APOSTAS_APROVACAO and r["roi_plano"] > 0
                and r["clv"] is not None and r["clv"] >= 0)


def _fmt_resumo(r):
    if not r:
        return f"{0:5d} {'-':>7s} {'-':>7s}"
    return f"{r['n']:5d} {r['roi_plano']*100:+6.1f}% {bt._pct(r['clv']):>7s}"


def imprimir_bloco(titulo, resultados, chave):
    print(f"\n{titulo}")
    print(f"  {'variante':36s} {'Brier-Bet365':>13s} {'modelo 60-80%':>14s} | {'REGRAS ATUAIS':^21s} | "
          f"{'ODDS 1.20-1.45':^21s}")
    print(f"  {'':36s} {'1X2':>6s} {'O2.5':>6s} {'disse->real':>14s} | {'n':>5s} {'ROI':>7s} {'CLV':>7s} | "
          f"{'n':>5s} {'ROI':>7s} {'CLV':>7s}")
    for nome, res in resultados:
        r = res[chave]
        d = r["brier_dif"]
        cal = f"{r['calib'][0]*100:.1f}->{r['calib'][1]*100:.1f}" if r["calib"] else "-"
        b1 = f"{d['1x2']*1000:+6.1f}" if d["1x2"] is not None else "     -"
        b2 = f"{d['o25']*1000:+6.1f}" if d["o25"] is not None else "     -"
        print(f"  {nome:36s} {b1} {b2} {cal:>14s} | {_fmt_resumo(r['regras'])} | {_fmt_resumo(r['faixa'])}")


# ---------- Programa principal ----------

def main():
    print("=== ApostaGol - experimentos de modelo (backtest) ===\n")
    with redirect_stdout(io.StringIO()):
        dados = bt.carregar_dados()
    todas = set()
    for partidas in dados.values():
        temps = sorted({p["temporada"] for p in partidas})
        todas |= set(temps[1:])
    escolha = bt.TEMPORADAS_OTIMIZACAO
    confirmacao = todas - escolha

    previsoes_por_variante = {}
    resultados = []
    for nome, fabrica, n_temp in VARIANTES_MODELO:
        t = time.time()
        prevs = bt.gerar_previsoes(dados, ag.XI_PESO_TEMPORAL, mostrar_progresso=False,
                                   fabrica_modelo=fabrica, n_temporadas=n_temp)
        previsoes_por_variante[nome] = prevs
        resultados.append((nome, {"escolha": avaliar(prevs, escolha), "confirmacao": avaliar(prevs, confirmacao)}))
        print(f"  {nome}: {len(prevs)} jogos previstos ({time.time() - t:.0f}s)")

    # melhor modelo escolhido SO pela temporada de escolha (menor Brier somado)
    def brier_escolha(item):
        d = item[1]["escolha"]["brier_dif"]
        return (d["1x2"] or 0) + (d["o25"] or 0)
    melhor_nome = min(resultados, key=brier_escolha)[0]
    print(f"\nMelhor modelo na temporada de escolha (menor erro): {melhor_nome}")

    atual = previsoes_por_variante[VARIANTES_MODELO[0][0]]
    pesos_relatorio = {}
    empilhamentos = [("Controle: so Bet365 recalibrada", atual, False),
                     ("Stacking Bet365 + Dixon-Coles atual", atual, True)]
    if melhor_nome != VARIANTES_MODELO[0][0]:
        empilhamentos.append((f"Stacking Bet365 + {melhor_nome}", previsoes_por_variante[melhor_nome], True))
    for nome, base, usar_modelo in empilhamentos:
        prevs, pesos = empilhar(base, usar_modelo)
        violacoes = sum(pv["treino_ate"] >= pv["partida"]["data"] for pv in prevs)
        pesos_relatorio[nome] = pesos
        resultados.append((nome, {"escolha": avaliar(prevs, escolha), "confirmacao": avaliar(prevs, confirmacao)}))
        print(f"  {nome}: {len(prevs)} jogos | checagem anti-futuro: "
              f"{'OK' if not violacoes else f'FALHOU em {violacoes}'}")

    print("\nComo ler: 'Brier-Bet365' = erro do modelo MENOS erro da Bet365, x1000 (negativo = modelo")
    print("melhor que a casa). 'modelo 60-80%' = quanto o modelo disse em media nessa faixa -> quanto")
    print("aconteceu de verdade. ROI = retorno apostando sempre o mesmo valor. CLV > 0 = bateu o fechamento.")
    imprimir_bloco(f"TEMPORADA DE ESCOLHA {sorted(escolha)}", resultados, "escolha")
    imprimir_bloco(f"TEMPORADAS DE CONFIRMACAO {sorted(confirmacao)} (as que valem pro veredito)",
                   resultados, "confirmacao")

    print("\nQUANTO O STACKING CONFIA EM CADA FONTE (ultimo ajuste; peso 0 = ignora)")
    for nome, pesos in pesos_relatorio.items():
        partes = []
        for m, w in pesos.items():
            txt = f"{m}: Bet365 {w[0]:.2f}"
            if len(w) > 2:
                txt += f" / modelo {w[1]:+.2f}"
            partes.append(txt)
        print(f"  {nome}: " + " | ".join(partes))

    print(f"\nVEREDITO (confirmacao: ROI > 0, CLV >= 0 e pelo menos {MIN_APOSTAS_APROVACAO} apostas)")
    algum = False
    for nome, res in resultados:
        if nome.startswith("Controle"):
            continue
        for chave, rotulo in (("regras", "regras atuais"), ("faixa", "odds 1.20-1.45")):
            if aprovado(res["confirmacao"][chave]):
                algum = True
                print(f"  APROVADO: {nome} ({rotulo})")
    if not algum:
        print("  Nenhuma variante passou. O programa do dia a dia NAO deve mudar com base nesses testes.")


if __name__ == "__main__":
    main()
