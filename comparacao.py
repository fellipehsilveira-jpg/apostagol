"""
ApostaGol - Metodo de comparacao de casas (backtest, sem modelo de previsao)

Em vez de prever o jogo, usa uma casa "afiada" (ou a media do mercado) como
probabilidade justa e aposta na Bet365 quando ela paga MAIS do que essa referencia.
E a abordagem dos projetos do GitHub que mostraram lucro (value-bet-model,
BeatTheBookie/Kaunitz). Aqui confirmamos ou derrubamos isso com os nossos dados.

Estrategias:
    A. Ancora Pinnacle        - justa = Pinnacle pre-jogo sem margem, aposta na Bet365
    B. Ancora Betfair Exchange - justa = Betfair Exchange sem margem, aposta na Bet365
    C. Consenso do mercado    - justa = media das casas sem margem, aposta na Bet365
    D. Pinnacle + melhor preco - como A, mas apostando na maior odd do mercado

Regras de stake: Kelly 25% limitado a 2% da banca, apostas simples.
Criterio de aprovacao (definido antes): nas temporadas de confirmacao, ROI > 0,
CLV >= 0 e pelo menos 200 apostas.

Como usar:
    python comparacao.py       (~2 minutos)
"""

import io
import math
from collections import defaultdict
from contextlib import redirect_stdout

import apostagol_dia as ag
import backtest as bt

MERCADOS = {"H": "vitoria casa", "D": "empate", "A": "vitoria fora", "O25": "mais de 2.5", "U25": "menos de 2.5"}
METODOS = ["shin", "power", "multiplicative"]
LIMIARES = [0.0, 0.01, 0.02, 0.03, 0.04, 0.06]
MIN_APOSTAS = 200
FAIXAS_ODD = [(1.0, 1.20, "abaixo de 1.20"), (1.20, 1.45, "1.20 a 1.45"), (1.45, 1.70, "1.45 a 1.70"),
              (1.70, 2.20, "1.70 a 2.20"), (2.20, 4.0, "2.20 a 4.00"), (4.0, 1000, "acima de 4.00")]

# nome, casa de referencia, casa onde aposta, temporadas de escolha, temporadas de confirmacao
# (a Pinnacle so aparece nas planilhas ate metade de 2025/26; a Betfair so a partir de 2024/25)
ESTRATEGIAS = [
    ("A. Ancora Pinnacle", "PS", "B365", {"2324", "2425"}, {"2526"}),
    ("B. Ancora Betfair Exchange", "BFE", "B365", {"2425"}, {"2526", "2627"}),
    ("C. Consenso (media do mercado)", "Avg", "B365", {"2324", "2425"}, {"2526", "2627"}),
    ("D. Pinnacle + melhor preco", "PS", "Max", {"2324", "2425"}, {"2526"}),
    # versoes realistas: so as casas em que o usuario teria conta (bwin no lugar da Betano, que nao
    # existe nas planilhas) e a ancora Betfair, que continua disponivel em 2026/27
    ("E. Pinnacle + melhor de 2 contas", "PS", ("B365", "BW"), {"2324", "2425"}, {"2526"}),
    ("F. Betfair + melhor preco", "BFE", "Max", {"2425"}, {"2526", "2627"}),
    ("G. Betfair + melhor de 2 contas", "BFE", ("B365", "BW"), {"2425"}, {"2526", "2627"}),
]
CONTROLES = [("Controle: Bet365 contra ela mesma", "B365", "B365"),
             ("Controle: Pinnacle contra ela mesma", "PS", "PS")]


def ganhou(mercado, ft):
    if mercado == "U25":
        return ft[0] + ft[1] < 3
    return bt.ganhou_mercado(mercado, ft)


_cache_justas = {}


def justas(p, casa, momento, metodo):
    """Probabilidades sem margem de uma casa num jogo (com cache, e o passo mais lento)."""
    chave = (id(p), casa, momento, metodo)
    if chave not in _cache_justas:
        odds = (p["casas"].get(momento) or {}).get(casa)
        _cache_justas[chave] = bt.probs_justas_mercados(odds, metodo) if odds else {}
    return _cache_justas[chave]


def gerar_apostas(partidas, referencia, casa_aposta, metodo, limiar, temporadas, faixa=None):
    """Decide SO com odds pre-jogo; o fechamento entra apenas pra medir o CLV."""
    apostas = []
    for p in partidas:
        if p["temporada"] not in temporadas:
            continue
        pre = p["casas"]["pre"]
        ref = justas(p, referencia, "pre", metodo)
        for m in MERCADOS:
            # casa_aposta pode ser uma casa ou varias (aposta na que pagar mais)
            contas = casa_aposta if isinstance(casa_aposta, tuple) else (casa_aposta,)
            ofertas = [o for o in ((pre.get(c) or {}).get(m) for c in contas) if o]
            odd, prob = (max(ofertas) if ofertas else None), ref.get(m)
            if odd is None or prob is None:
                continue
            if faixa and not faixa[0] <= odd < faixa[1]:
                continue
            ev = odd * prob - 1
            if ev <= limiar:
                continue
            fracao = ag.kelly_fracionario(prob, odd)
            if fracao <= 0:
                continue
            clv = None
            if p["casas"]["fech"]:
                fech = justas(p, "PS", "fech", metodo).get(m) or justas(p, "B365", "fech", metodo).get(m)
                clv = odd * fech - 1 if fech else None
            apostas.append({"data": p["data"], "liga": p["liga"], "temporada": p["temporada"],
                            "jogo": f"{p['casa']} x {p['fora']}", "mercado": m, "odd": odd, "prob": prob,
                            "ev": ev, "fracao": fracao, "clv": clv, "ganhou": ganhou(m, p["ft"])})
    return bt.aplicar_banca(apostas)


def _linha(r, ic=None):
    if not r:
        return "    0 apostas"
    txt = f"{r['n']:5d} apostas | ROI {r['roi_plano']*100:+5.1f}%"
    if ic:
        txt += f" [{ic['roi'][0]*100:+.1f}% a {ic['roi'][1]*100:+.1f}%]"
    txt += f" | CLV {bt._pct(r['clv'])}"
    if ic and ic["clv"]:
        txt += f" [{ic['clv'][0]*100:+.1f}% a {ic['clv'][1]*100:+.1f}%]"
    return txt


def aprovado(r):
    return bool(r and r["n"] >= MIN_APOSTAS and r["roi_plano"] > 0 and r["clv"] is not None and r["clv"] >= 0)


# ---------- Teste dos metodos de remover a margem ----------

def testar_metodos_devig(europa):
    print("\n" + "=" * 78)
    print("1) QUAL METODO DE TIRAR A MARGEM ACERTA MAIS? (log loss: quanto MENOR, melhor)")
    print("=" * 78)
    resultados = {}
    for casa in ("B365", "PS"):
        for grupo, chaves in (("1X2", ["H", "D", "A"]), ("mais/menos 2.5", ["O25", "U25"])):
            linha = []
            for metodo in METODOS:
                perda, n = 0.0, 0
                for p in europa:
                    j = justas(p, casa, "pre", metodo)
                    if not all(k in j for k in chaves):
                        continue
                    certo = next(k for k in chaves if ganhou(k, p["ft"]))
                    perda -= math.log(max(j[certo], 1e-9))
                    n += 1
                resultados[(casa, grupo, metodo)] = perda / n if n else None
                linha.append(f"{metodo} {perda / n:.5f}" if n else f"{metodo} -")
            nome = "Bet365" if casa == "B365" else "Pinnacle"
            print(f"  {nome:9s} {grupo:15s} ({n} jogos): " + " | ".join(linha))
    return resultados


# ---------- Programa principal ----------

def main():
    print("=== ApostaGol - metodo de comparacao de casas (backtest) ===")
    with redirect_stdout(io.StringIO()):
        dados = bt.carregar_dados()
    europa = [p for liga, ps in dados.items() if liga != "BSA" for p in ps]
    brasil = dados["BSA"]

    devig = testar_metodos_devig(europa)

    print("\n" + "=" * 78)
    print("2) CONTROLES (tem que dar ZERO apostas; se nao der, ha bug)")
    print("=" * 78)
    todas_temp = {p["temporada"] for p in europa}
    for nome, ref, casa in CONTROLES:
        n = len(gerar_apostas(europa, ref, casa, "shin", 0.0, todas_temp)["apostas"])
        print(f"  {nome}: {n} apostas {'(OK)' if n == 0 else '(PROBLEMA!)'}")
    print("  Anti-futuro: as decisoes usam so odds pre-jogo; o fechamento so mede o CLV.")

    print("\n" + "=" * 78)
    print("3) ESTRATEGIAS - escolhe metodo/limiar na temporada de escolha, confirma nas seguintes")
    print("=" * 78)
    print("Como ler: ROI [intervalo de 95%]. Se o intervalo atravessa o zero, pode ser sorte.")
    print("CLV medido contra o fechamento da Pinnacle (ou da Bet365 quando nao houver Pinnacle).")
    finais = []
    for nome, ref, casa, escolha, confirmacao in ESTRATEGIAS:
        grade = []
        for metodo in METODOS:
            for limiar in LIMIARES:
                r = bt.resumo(gerar_apostas(europa, ref, casa, metodo, limiar, escolha)["apostas"])
                grade.append((metodo, limiar, r))
        validas = [g for g in grade if g[2] and g[2]["n"] >= MIN_APOSTAS]
        print(f"\n{nome}  (escolha {sorted(escolha)} -> confirmacao {sorted(confirmacao)})")
        if not validas:
            print("  Poucas apostas na escolha pra decidir.")
            continue
        metodo, limiar, _ = max(validas, key=lambda g: g[2]["roi_plano"])
        print(f"  {'limiar':>7s} | {'escolha (' + metodo + ')':^34s} | confirmacao")
        for lim in LIMIARES:
            r_esc = next(g[2] for g in grade if g[0] == metodo and g[1] == lim)
            sim_conf = gerar_apostas(europa, ref, casa, metodo, lim, confirmacao)
            r_conf = bt.resumo(sim_conf["apostas"])
            marca = "  <- escolhido" if lim == limiar else ""
            esc_txt = f"{r_esc['n']:5d} apostas ROI {r_esc['roi_plano']*100:+5.1f}%" if r_esc else "0 apostas"
            print(f"  {lim*100:6.0f}% | {esc_txt:34s} | {_linha(r_conf)}{marca}")
        sim = gerar_apostas(europa, ref, casa, metodo, limiar, confirmacao)
        r = bt.resumo(sim["apostas"])
        ic = bt.intervalo_confianca(sim["apostas"])
        print(f"  CONFIRMACAO do escolhido: {_linha(r, ic)}")
        if r:
            print(f"  Banca R$ {bt.BANCA_INICIAL:.0f} -> R$ {sim['banca_final']:.2f} (Kelly 25%, teto 2%) | "
                  f"maior queda {sim['maior_queda']*100:.1f}% | {'APROVADA' if aprovado(r) else 'reprovada'}")
        finais.append((nome, ref, casa, metodo, limiar, confirmacao, sim, r))

    print("\n" + "=" * 78)
    print("4) DETALHE DAS ESTRATEGIAS NA CONFIRMACAO")
    print("=" * 78)
    for nome, ref, casa, metodo, limiar, confirmacao, sim, r in finais:
        if not r:
            continue
        print(f"\n{nome} ({metodo}, limiar {limiar*100:.0f}%)")
        bt.imprimir_tabela("  por faixa de odd", bt.agrupar(
            sim["apostas"], lambda a: next(n for lo, hi, n in FAIXAS_ODD if lo <= a["odd"] < hi),
            [n for _, _, n in FAIXAS_ODD]))
        bt.imprimir_tabela("  por mercado", bt.agrupar(sim["apostas"], lambda a: MERCADOS[a["mercado"]],
                                                       list(MERCADOS.values())))
        bt.imprimir_tabela("  por liga", bt.agrupar(sim["apostas"], lambda a: a["liga"]))
        bt.imprimir_tabela("  por temporada", bt.agrupar(sim["apostas"], lambda a: a["temporada"]))

    print("\n" + "=" * 78)
    print("5) BRASILEIRAO (so ha odd de FECHAMENTO: Bet365 fechamento x referencia fechamento, sem CLV)")
    print("=" * 78)
    for nome, ref, casa, metodo, limiar, *_ in finais:
        if casa != "B365":
            continue  # a planilha do Brasil nao tem bwin, e a "maior odd" inclui casas fora do Brasil
        for rotulo, temps in (("2023-2024", {"2023", "2024"}), ("2025-2026", {"2025", "2026"})):
            sim = gerar_apostas(brasil, ref, casa, metodo, limiar, temps)
            r = bt.resumo(sim["apostas"])
            print(f"  {nome:34s} {rotulo}: {_linha(r, bt.intervalo_confianca(sim['apostas']))}")

    print("\n" + "=" * 78)
    print("VEREDITO (confirmacao: ROI > 0, CLV >= 0, pelo menos 200 apostas)")
    print("=" * 78)
    aprovadas = [f for f in finais if aprovado(f[7])]
    for nome, ref, casa, metodo, limiar, confirmacao, sim, r in aprovadas:
        print(f"  APROVADA: {nome} (metodo {metodo}, limiar {limiar*100:.0f}%)")
    if not aprovadas:
        print("  Nenhuma estrategia aprovada.")
    melhor = min(METODOS, key=lambda m: devig[("B365", "1X2", m)] or 9)
    print(f"\nMetodo de tirar margem com menor erro na Bet365 (1X2): {melhor} "
          f"(o programa usa hoje: {ag.METODO_DEVIG})")


if __name__ == "__main__":
    main()
