"""
ApostaGol - Backtest (testa o nosso metodo no passado)

Refaz as temporadas passadas semana a semana, como se voce estivesse usando o
apostagol_dia.py naquela epoca:
    1. ajusta o NOSSO modelo (Dixon-Coles) so com os jogos que ja tinham acontecido;
    2. calcula a probabilidade de cada mercado;
    3. compara com as odds REAIS que a Bet365 oferecia (sem a margem, metodo Shin);
    4. aplica as NOSSAS regras: probabilidade >= 58.8%, odd >= 1.70, edge > 0,
       stake Kelly 25% limitada a 2% da banca;
    5. confere o resultado real e atualiza a banca.

Fonte das odds historicas: https://www.football-data.co.uk/ (planilhas gratuitas).
Mercados testaveis (os unicos com odd historica gratuita): vitoria casa, empate,
vitoria fora e mais de 2.5 gols (so 1X2 no Brasileirao). Gols 1.5/3.5, 1o tempo,
2o tempo e chance dupla NAO sao testados.

Como usar:
    python backtest.py            (pergunta o modo)
    python backtest.py rapido     (so o resultado com as regras atuais)
    python backtest.py completo   (resultado + teste de variacoes/otimizacao)
"""

import csv
import io
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

import requests

import apostagol_dia as ag

PASTA_CACHE_BT = os.path.join(ag.PASTA, "cache_backtest")
ARQUIVO_SAIDA = os.path.join(ag.PASTA, "backtest_apostas.csv")
URL_EUROPA = "https://www.football-data.co.uk/mmz4281/{temporada}/{divisao}.csv"
URL_BRASIL = "https://www.football-data.co.uk/new/BRA.csv"

# nosso codigo de competicao -> codigo da divisao no football-data.co.uk
LIGAS_EUROPA = {"PL": "E0", "ELC": "E1", "PD": "SP1", "SA": "I1", "BL1": "D1", "FL1": "F1",
                "DED": "N1", "PPL": "P1"}
TEMPORADAS_EUROPA = ["2324", "2425", "2526", "2627"]  # a primeira so serve de historico pro treino
TEMPORADAS_BRASIL = ["2023", "2024", "2025", "2026"]
TEMPORADAS_OTIMIZACAO = {"2425", "2024"}  # usadas pra escolher parametros; as seguintes validam

BANCA_INICIAL = 1000.0
CACHE_HORAS_TEMPORADA_ATUAL = 12
MIN_JOGOS_TREINO = 30  # mesmo corte do apostagol_dia.py

MERCADOS = {"H": "vitoria casa", "D": "empate", "A": "vitoria fora", "O25": "mais de 2.5 gols"}
FAIXAS_EXPERIENCIA = [(10, "5 a 9 jogos"), (20, "10 a 19 jogos"), (10 ** 9, "20+ jogos")]
FAIXA_POR_ESTRELAS ={1: "negativo", 2: "0-3 p.p.", 3: "3-7 p.p.", 4: "7-12 p.p.", 5: "12-18 p.p.", 6: "18+ p.p."}

# variacoes testadas no modo completo
XIS_TESTE = [0.0, 0.0018, 0.0035]
EDGES_MIN_TESTE = [0.0, 0.02, 0.04, 0.06]
TETOS_EDGE_TESTE = [None, 0.15]
MIN_APOSTAS_OTIMIZACAO = 100


# ---------- Download e leitura dos dados historicos ----------

def baixar(url, nome_arquivo, temporada_atual):
    """Baixa um CSV (uma vez so). A temporada em andamento e baixada de novo a cada 12h."""
    caminho = os.path.join(PASTA_CACHE_BT, nome_arquivo)
    if os.path.exists(caminho):
        idade_h = (time.time() - os.path.getmtime(caminho)) / 3600
        if not temporada_atual or idade_h < CACHE_HORAS_TEMPORADA_ATUAL:
            with open(caminho, "rb") as f:
                return f.read()
    resp = requests.get(url, timeout=60)
    if resp.status_code != 200:
        print(f"  (nao consegui baixar {url} - HTTP {resp.status_code}, seguindo sem ele)")
        return None
    os.makedirs(PASTA_CACHE_BT, exist_ok=True)
    with open(caminho, "wb") as f:
        f.write(resp.content)
    return resp.content


def _linhas_csv(conteudo):
    try:
        texto = conteudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = conteudo.decode("latin-1")
    return list(csv.DictReader(io.StringIO(texto)))


def _odd(linha, coluna):
    try:
        valor = float(linha.get(coluna) or "")
    except ValueError:
        return None
    return valor if valor > 1.0 else None


def _inteiro(texto):
    try:
        return int(float(texto))
    except (TypeError, ValueError):
        return None


def _data(texto):
    for formato in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(texto.strip(), formato).date()
        except (ValueError, AttributeError):
            continue
    return None


def _probs_justas(odds, chaves, metodo=None):
    if any(odds.get(k) is None for k in chaves):
        return {}
    try:
        r = ag.pb.implied.calculate_implied([odds[k] for k in chaves], method=metodo or ag.METODO_DEVIG)
    except (ValueError, ZeroDivisionError):
        return {}
    return dict(zip(chaves, r.probabilities))


def _completar_partida(p):
    """Probabilidades justas (sem margem) da Bet365 na odd de aposta e no fechamento."""
    p["justas"] = {**_probs_justas(p["odds"], ["H", "D", "A"]), **_probs_justas(p["odds"], ["O25", "U25"])}
    fech = p["fechamento"] or {}
    p["justas_fech"] = {**_probs_justas(fech, ["H", "D", "A"]), **_probs_justas(fech, ["O25", "U25"])}
    return p


def probs_justas_mercados(odds, metodo=None):
    """Probabilidades sem margem do 1X2 e do mais/menos 2.5 de uma casa."""
    odds = odds or {}
    return {**_probs_justas(odds, ["H", "D", "A"], metodo), **_probs_justas(odds, ["O25", "U25"], metodo)}


# casa -> (colunas pre-jogo, colunas de fechamento) nas planilhas europeias
COLUNAS_CASAS = {
    "B365": ("B365H B365D B365A B365>2.5 B365<2.5", "B365CH B365CD B365CA B365C>2.5 B365C<2.5"),
    "PS": ("PSH PSD PSA P>2.5 P<2.5", "PSCH PSCD PSCA PC>2.5 PC<2.5"),
    "Avg": ("AvgH AvgD AvgA Avg>2.5 Avg<2.5", "AvgCH AvgCD AvgCA AvgC>2.5 AvgC<2.5"),
    "Max": ("MaxH MaxD MaxA Max>2.5 Max<2.5", "MaxCH MaxCD MaxCA MaxC>2.5 MaxC<2.5"),
    "BFE": ("BFEH BFED BFEA BFE>2.5 BFE<2.5", "BFECH BFECD BFECA BFEC>2.5 BFEC<2.5"),
    "BW": ("BWH BWD BWA BW>2.5 BW<2.5", "BWCH BWCD BWCA BWC>2.5 BWC<2.5"),  # bwin: so tem 1X2
}
CHAVES_MERCADOS = ["H", "D", "A", "O25", "U25"]


def _odds_casas(linha, indice):
    """{casa: {mercado: odd}} de todas as casas (indice 0 = pre-jogo, 1 = fechamento)."""
    return {casa: dict(zip(CHAVES_MERCADOS, (_odd(linha, c) for c in colunas[indice].split())))
            for casa, colunas in COLUNAS_CASAS.items()}


def carregar_europa(liga, divisao):
    partidas = []
    for i, temporada in enumerate(TEMPORADAS_EUROPA):
        conteudo = baixar(URL_EUROPA.format(temporada=temporada, divisao=divisao), f"{divisao}_{temporada}.csv",
                          temporada_atual=(i == len(TEMPORADAS_EUROPA) - 1))
        if not conteudo:
            continue
        for linha in _linhas_csv(conteudo):
            d = _data(linha.get("Date"))
            fthg, ftag = _inteiro(linha.get("FTHG")), _inteiro(linha.get("FTAG"))
            if not d or fthg is None or ftag is None or not linha.get("HomeTeam"):
                continue
            hthg, htag = _inteiro(linha.get("HTHG")), _inteiro(linha.get("HTAG"))
            partidas.append(_completar_partida({
                "liga": liga, "temporada": temporada, "data": d.isoformat(),
                "casa": linha["HomeTeam"].strip(), "fora": linha["AwayTeam"].strip(),
                "ft": [fthg, ftag], "ht": [hthg, htag] if hthg is not None and htag is not None else None,
                "odds": {"H": _odd(linha, "B365H"), "D": _odd(linha, "B365D"), "A": _odd(linha, "B365A"),
                         "O25": _odd(linha, "B365>2.5"), "U25": _odd(linha, "B365<2.5")},
                "fechamento": {"H": _odd(linha, "B365CH"), "D": _odd(linha, "B365CD"), "A": _odd(linha, "B365CA"),
                               "O25": _odd(linha, "B365C>2.5"), "U25": _odd(linha, "B365C<2.5")},
                "casas": {"pre": _odds_casas(linha, 0), "fech": _odds_casas(linha, 1)},
            }))
    return partidas


def carregar_brasil():
    """A planilha do Brasil so tem odd de FECHAMENTO do 1X2 - ela vira a odd de aposta
    (mais exigente que a odd de abertura) e nao da pra medir CLV."""
    conteudo = baixar(URL_BRASIL, "BRA.csv", temporada_atual=True)
    if not conteudo:
        return []
    partidas = []
    for linha in _linhas_csv(conteudo):
        if linha.get("League") != "Serie A" or linha.get("Season") not in TEMPORADAS_BRASIL:
            continue
        d = _data(linha.get("Date"))
        hg, ag_ = _inteiro(linha.get("HG")), _inteiro(linha.get("AG"))
        if not d or hg is None or ag_ is None:
            continue
        partidas.append(_completar_partida({
            "liga": "BSA", "temporada": linha["Season"], "data": d.isoformat(),
            "casa": linha["Home"].strip(), "fora": linha["Away"].strip(), "ft": [hg, ag_], "ht": None,
            "odds": {"H": _odd(linha, "B365CH"), "D": _odd(linha, "B365CD"), "A": _odd(linha, "B365CA")},
            "fechamento": None,
            # so fechamento: ele faz o papel de pre-jogo e nao existe CLV
            "casas": {"pre": {casa: {m: _odd(linha, f"{casa}C{m}") for m in ("H", "D", "A")}
                              for casa in ("B365", "PS", "Avg", "Max", "BFE")}, "fech": None},
        }))
    return partidas


def carregar_dados():
    print("Carregando historico com odds da Bet365 (football-data.co.uk)...")
    dados = {"BSA": carregar_brasil()}
    for liga, divisao in LIGAS_EUROPA.items():
        dados[liga] = carregar_europa(liga, divisao)
    for liga, partidas in dados.items():
        print(f"  {liga:4s} {len(partidas):5d} jogos")
    return dados


# ---------- Previsoes semana a semana (sem olhar o futuro) ----------

def _prob(esperados, tipo, campo, linha=None):
    return ag.prob_mercado({"tipo": tipo, "campo": campo, "linha": linha, "esperados": esperados})


def probs_da_grade(grade):
    return {"H": grade.home_win, "D": grade.draw, "A": grade.away_win, "O25": grade.total_goals("over", 2.5)}


class PrevisorDixonColes:
    """O modelo do apostagol_dia.py, exatamente como ele e usado no dia a dia."""

    def __init__(self, liga, treino, inicio, xi):
        self.modelo = ag.ModeloCompeticao(liga, treino, data_base=inicio, xi=xi, periodos=("total",))
        self.jogos_por_time = self.modelo.jogos_por_time

    def probs(self, casa, fora):
        e = self.modelo.gols_esperados(casa, fora)
        return {"H": _prob(e, "resultado", "vitoria_casa"), "D": _prob(e, "resultado", "empate"),
                "A": _prob(e, "resultado", "vitoria_fora"), "O25": _prob(e, "gols", "total", 2.5)}


def fabrica_penaltyblog(classe):
    """Cria um previsor com qualquer modelo de gols da penaltyblog (mesma interface)."""
    class PrevisorPenaltyblog:
        def __init__(self, liga, treino, inicio, xi):
            pesos = ag.pb.models.dixon_coles_weights(ag.pd.to_datetime([p["data"] for p in treino]), xi,
                                                     base_date=ag.pd.Timestamp(inicio))
            self.modelo = classe([p["ft"][0] for p in treino], [p["ft"][1] for p in treino],
                                 [p["casa"] for p in treino], [p["fora"] for p in treino], weights=pesos)
            self.modelo.fit()
            self.jogos_por_time = defaultdict(int)
            for p in treino:
                self.jogos_por_time[p["casa"]] += 1
                self.jogos_por_time[p["fora"]] += 1

        def probs(self, casa, fora):
            return probs_da_grade(self.modelo.predict(casa, fora, max_goals=ag.MAX_GOLS_MODELO + 1))

    return PrevisorPenaltyblog


def gerar_previsoes(dados, xi, mostrar_progresso=True, fabrica_modelo=PrevisorDixonColes, n_temporadas=2):
    """Para cada semana, ajusta o modelo com os jogos das ultimas n_temporadas (a atual
    incluida) que aconteceram ANTES daquela segunda-feira e preve os jogos da semana."""
    previsoes = []
    for liga, partidas in dados.items():
        temporadas = sorted({p["temporada"] for p in partidas})
        for pos, temporada in enumerate(temporadas[1:], start=1):
            usadas = set(temporadas[max(0, pos - n_temporadas + 1):pos + 1])
            semanas = defaultdict(list)
            for p in partidas:
                if p["temporada"] == temporada:
                    d = date.fromisoformat(p["data"])
                    semanas[d - timedelta(days=d.weekday())].append(p)
            previstos = 0
            for inicio, jogos in sorted(semanas.items()):
                limite = inicio.isoformat()
                treino = [p for p in partidas if p["temporada"] in usadas and p["data"] < limite]
                if len(treino) < MIN_JOGOS_TREINO:
                    continue
                try:
                    modelo = fabrica_modelo(liga, treino, inicio, xi)
                except Exception:
                    continue
                treino_ate = max(p["data"] for p in treino)
                for p in jogos:
                    jogos_casa, jogos_fora = modelo.jogos_por_time[p["casa"]], modelo.jogos_por_time[p["fora"]]
                    if min(jogos_casa, jogos_fora) < ag.MIN_JOGOS_TIME:
                        continue
                    try:
                        probs = modelo.probs(p["casa"], p["fora"])
                    except Exception:
                        continue
                    previsoes.append({"partida": p, "treino_ate": treino_ate,
                                      "jogos_min": min(jogos_casa, jogos_fora), "probs": probs})
                    previstos += 1
            if mostrar_progresso:
                print(f"  [{liga}] temporada {temporada}: {previstos} jogos simulados")
    return previsoes


# ---------- Simulacao das apostas com as nossas regras ----------

def ganhou_mercado(mercado, ft):
    casa, fora = ft
    return {"H": casa > fora, "D": casa == fora, "A": casa < fora, "O25": casa + fora > 2}[mercado]


def simular(previsoes, edge_min=0.0, teto_edge=None, temporadas=None, faixa_odd=None):
    """faixa_odd=(min, max) troca a regra da odd minima 1.70 por uma faixa (ex: odds
    baixas pra combinadas); o resto das regras continua igual."""
    odd_min, odd_max = faixa_odd if faixa_odd else (ag.ODD_MINIMA, float("inf"))
    apostas = []
    for prev in previsoes:
        p = prev["partida"]
        if temporadas is not None and p["temporada"] not in temporadas:
            continue
        for m in MERCADOS:
            odd, justa = p["odds"].get(m), p["justas"].get(m)
            if odd is None or justa is None:
                continue
            prob = prev["probs"][m]
            edge = prob - justa
            if prob < ag.LIMIAR_PROB or not odd_min <= odd <= odd_max or edge <= edge_min:
                continue
            if teto_edge is not None and edge > teto_edge:
                continue
            fracao = ag.kelly_fracionario(prob, odd)
            if fracao <= 0:
                continue
            justa_fech = p["justas_fech"].get(m)
            apostas.append({
                "data": p["data"], "liga": p["liga"], "temporada": p["temporada"],
                "jogo": f"{p['casa']} x {p['fora']}", "placar": f"{p['ft'][0]}-{p['ft'][1]}",
                "mercado": m, "prob": prob, "justa": justa, "edge": edge, "odd": odd,
                "estrelas": ag.estrelas_do_edge(edge), "fracao": fracao,
                "dentro_corredor": odd <= ag.ODD_MAXIMA,
                "odd_fech": (p["fechamento"] or {}).get(m),
                "clv": odd * justa_fech - 1 if justa_fech else None, "jogos_min": prev["jogos_min"],
                "ganhou": ganhou_mercado(m, p["ft"]),
            })
    return aplicar_banca(apostas)


def aplicar_banca(apostas):
    """Calcula stake/lucro/banca de cada aposta. Apostas do mesmo dia usam a banca do
    inicio do dia (sao feitas juntas). Cada aposta precisa de data, fracao, odd, ganhou."""
    apostas.sort(key=lambda a: a["data"])
    banca = pico = BANCA_INICIAL
    maior_queda = 0.0
    por_dia = defaultdict(list)
    for a in apostas:
        por_dia[a["data"]].append(a)
    for dia in sorted(por_dia):
        banca_dia = banca
        for a in por_dia[dia]:
            a["stake"] = a["fracao"] * banca_dia
            a["lucro"] = a["stake"] * (a["odd"] - 1) if a["ganhou"] else -a["stake"]
            banca += a["lucro"]
            a["banca"] = banca
        pico = max(pico, banca)
        maior_queda = max(maior_queda, (pico - banca) / pico)
    return {"apostas": apostas, "banca_final": banca, "maior_queda": maior_queda}


def resumo(apostas):
    n = len(apostas)
    if not n:
        return None
    apostado = sum(a["stake"] for a in apostas)
    lucro = sum(a["lucro"] for a in apostas)
    lucro_plano = sum((a["odd"] - 1) if a["ganhou"] else -1 for a in apostas)
    clvs = [a["clv"] for a in apostas if a["clv"] is not None]
    return {
        "n": n, "acerto": sum(a["ganhou"] for a in apostas) / n,
        "odd_media": sum(a["odd"] for a in apostas) / n,
        "roi_plano": lucro_plano / n, "lucro": lucro, "roi_kelly": lucro / apostado if apostado else 0,
        "clv": sum(clvs) / len(clvs) if clvs else None,
        "clv_pos": sum(c > 0 for c in clvs) / len(clvs) if clvs else None,
    }


def intervalo_confianca(apostas, reamostragens=2000, semente=42):
    """Intervalo de 95% (bootstrap) do ROI plano e do CLV medio: se o intervalo
    atravessa o zero, o resultado pode ser so sorte."""
    if len(apostas) < 20:
        return None
    rng = ag.np.random.default_rng(semente)
    retornos = ag.np.array([(a["odd"] - 1) if a["ganhou"] else -1.0 for a in apostas])
    amostras = rng.integers(0, len(retornos), size=(reamostragens, len(retornos)))
    rois = retornos[amostras].mean(axis=1)
    saida = {"roi": (float(ag.np.percentile(rois, 2.5)), float(ag.np.percentile(rois, 97.5))), "clv": None}
    clvs = ag.np.array([a["clv"] for a in apostas if a["clv"] is not None])
    if len(clvs) >= 20:
        amostras = rng.integers(0, len(clvs), size=(reamostragens, len(clvs)))
        medias = clvs[amostras].mean(axis=1)
        saida["clv"] = (float(ag.np.percentile(medias, 2.5)), float(ag.np.percentile(medias, 97.5)))
    return saida


# ---------- Relatorio ----------

def _pct(x, casas=1):
    return "   -  " if x is None else f"{x * 100:+.{casas}f}%"


def imprimir_tabela(titulo, grupos):
    print(f"\n{titulo}")
    print(f"  {'':22s} {'apostas':>7s} {'acerto':>7s} {'odd med':>7s} {'ROI':>8s} {'lucro R$':>9s} {'CLV':>8s}")
    for nome, apostas in grupos:
        r = resumo(apostas)
        if not r:
            continue
        print(f"  {nome:22s} {r['n']:7d} {r['acerto']*100:6.1f}% {r['odd_media']:7.2f} {_pct(r['roi_plano']):>8s} "
              f"{r['lucro']:9.2f} {_pct(r['clv']):>8s}")


def agrupar(apostas, chave, ordem=None):
    grupos = defaultdict(list)
    for a in apostas:
        grupos[chave(a)].append(a)
    nomes = ordem if ordem else sorted(grupos)
    return [(n, grupos[n]) for n in nomes if n in grupos]


def imprimir_calibracao(previsoes):
    """Quando o modelo diz X%, acontece mesmo uns X%? E ele erra mais ou menos que a Bet365?"""
    faixas = defaultdict(lambda: [0, 0.0, 0.0, 0])  # n, soma prob modelo, soma prob mercado, acertos
    brier = {"1x2": [0.0, 0.0, 0], "o25": [0.0, 0.0, 0]}  # modelo, mercado, n
    for prev in previsoes:
        p = prev["partida"]
        for m in MERCADOS:
            if m not in p["justas"]:
                continue
            prob, justa, real = prev["probs"][m], p["justas"][m], ganhou_mercado(m, p["ft"])
            f = faixas[min(int(prob * 10), 9)]
            f[0] += 1
            f[1] += prob
            f[2] += justa
            f[3] += real
        if all(k in p["justas"] for k in ("H", "D", "A")):
            b = brier["1x2"]
            for m in ("H", "D", "A"):
                real = ganhou_mercado(m, p["ft"])
                b[0] += (prev["probs"][m] - real) ** 2
                b[1] += (p["justas"][m] - real) ** 2
            b[2] += 1
        if "O25" in p["justas"]:
            real = ganhou_mercado("O25", p["ft"])
            b = brier["o25"]
            b[0] += (prev["probs"]["O25"] - real) ** 2
            b[1] += (p["justas"]["O25"] - real) ** 2
            b[2] += 1

    print("\nCALIBRACAO (todos os jogos, todos os mercados testaveis - nao so os apostados)")
    print(f"  {'modelo disse':14s} {'casos':>6s} {'modelo':>7s} {'Bet365':>7s} {'aconteceu':>10s}")
    for i in range(10):
        n, sp, sj, ac = faixas[i]
        if n >= 30:
            print(f"  {i*10:3d}% a {i*10+10:3d}%    {n:6d} {sp/n*100:6.1f}% {sj/n*100:6.1f}% {ac/n*100:9.1f}%")
    print("\nERRO MEDIO DAS PREVISOES (Brier score - quanto MENOR, melhor)")
    for nome, (bm, bj, n) in brier.items():
        if n:
            vencedor = "o modelo erra MENOS que a Bet365" if bm < bj else "a Bet365 erra menos que o modelo"
            print(f"  {'1X2' if nome == '1x2' else 'Mais/menos 2.5'}: modelo {bm/n:.4f} x Bet365 {bj/n:.4f} "
                  f"({n} jogos) -> {vencedor}")


def imprimir_referencia(previsoes):
    """Controle: apostar as cegas (1 unidade) em todo mercado com odd no corredor, sem modelo nenhum."""
    lucro, n = 0.0, 0
    for prev in previsoes:
        p = prev["partida"]
        for m in MERCADOS:
            odd = p["odds"].get(m)
            if odd and ag.ODD_MINIMA <= odd <= ag.ODD_MAXIMA:
                lucro += (odd - 1) if ganhou_mercado(m, p["ft"]) else -1
                n += 1
    if n:
        print(f"\nREFERENCIA: apostar as cegas em TODO mercado com odd {ag.ODD_MINIMA}-{ag.ODD_MAXIMA} "
              f"({n} apostas) daria ROI de {lucro/n*100:+.1f}% (e o custo da margem da casa).")


def relatorio(previsoes, sim):
    apostas = sim["apostas"]
    violacoes = sum(prev["treino_ate"] >= prev["partida"]["data"] for prev in previsoes)
    print("\n" + "=" * 70)
    print("RESULTADO DO BACKTEST - regras atuais do apostagol_dia.py")
    print("=" * 70)
    print(f"Jogos simulados: {len(previsoes)} | checagem anti-futuro: "
          f"{'OK (nenhum jogo usou dados do futuro)' if not violacoes else f'FALHOU em {violacoes} jogos'}")
    r = resumo(apostas)
    if not r:
        print("Nenhuma aposta passou nas regras.")
        return
    print(f"\nApostas feitas: {r['n']} | acerto: {r['acerto']*100:.1f}% | odd media: {r['odd_media']:.2f}")
    print(f"ROI apostando sempre o mesmo valor: {_pct(r['roi_plano'])}")
    ic = intervalo_confianca(apostas)
    if ic:
        texto_clv = (f" | CLV entre {_pct(ic['clv'][0])} e {_pct(ic['clv'][1])}" if ic["clv"] else "")
        print(f"  (intervalo de 95%: ROI entre {_pct(ic['roi'][0])} e {_pct(ic['roi'][1])}{texto_clv})")
    print(f"Banca (Kelly 25%, teto 2%): R$ {BANCA_INICIAL:.2f} -> R$ {sim['banca_final']:.2f} "
          f"| maior queda no caminho: {sim['maior_queda']*100:.1f}%")
    if r["clv"] is not None:
        print(f"CLV medio: {_pct(r['clv'])} | apostas que 'bateram' a odd de fechamento: {r['clv_pos']*100:.1f}%")

    imprimir_tabela("POR LIGA", agrupar(apostas, lambda a: a["liga"]))
    imprimir_tabela("POR MERCADO", agrupar(apostas, lambda a: MERCADOS[a["mercado"]], list(MERCADOS.values())))
    imprimir_tabela("POR FAIXA DE EDGE (estrelas)",
                    agrupar(apostas, lambda a: f"{a['estrelas']} estr. {FAIXA_POR_ESTRELAS[a['estrelas']]}",
                            [f"{e} estr. {FAIXA_POR_ESTRELAS[e]}" for e in range(2, 7)]))
    imprimir_tabela("POR CORREDOR DE ODD", agrupar(
        apostas, lambda a: "dentro 1.70-2.20" if a["dentro_corredor"] else "fora (acima de 2.20)",
        ["dentro 1.70-2.20", "fora (acima de 2.20)"]))
    imprimir_tabela("POR EXPERIENCIA (jogos na base do time com menos jogos)", agrupar(
        apostas, lambda a: next(nome for limite, nome in FAIXAS_EXPERIENCIA if a["jogos_min"] < limite),
        [nome for _, nome in FAIXAS_EXPERIENCIA]))
    imprimir_tabela("POR TEMPORADA", agrupar(apostas, lambda a: f"{'Brasil' if a['liga'] == 'BSA' else 'Europa'} {a['temporada']}"))
    imprimir_calibracao(previsoes)
    imprimir_referencia(previsoes)
    print("\nNao testado (sem odd historica gratuita): gols 1.5/3.5, 1o tempo, 2o tempo e chance dupla.")
    print("Brasileirao: testado com a odd de FECHAMENTO da Bet365 (a unica disponivel) e sem CLV.")


def salvar_csv(apostas):
    def num(x, casas=4):
        return "" if x is None else f"{x:.{casas}f}".replace(".", ",")

    with open(ARQUIVO_SAIDA, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["data", "liga", "temporada", "jogo", "placar", "mercado", "prob_modelo", "prob_justa_bet365",
                    "edge", "estrelas", "odd", "odd_fechamento", "clv", "stake_%_banca", "stake_R$", "ganhou",
                    "lucro_R$", "banca_depois_R$"])
        for a in apostas:
            w.writerow([a["data"], a["liga"], a["temporada"], a["jogo"], a["placar"], MERCADOS[a["mercado"]],
                        num(a["prob"]), num(a["justa"]), num(a["edge"]), a["estrelas"], num(a["odd"], 2),
                        num(a["odd_fech"], 2), num(a["clv"]), num(a["fracao"] * 100, 2), num(a["stake"], 2),
                        "sim" if a["ganhou"] else "nao", num(a["lucro"], 2), num(a["banca"], 2)])
    print(f"\nTodas as apostas simuladas foram salvas em {ARQUIVO_SAIDA} (abre no Excel).")


# ---------- Otimizacao (modo completo) ----------

def otimizar(dados, previsoes_padrao):
    temporadas_teste = {prev["partida"]["temporada"] for prev in previsoes_padrao}
    validacao = temporadas_teste - TEMPORADAS_OTIMIZACAO
    print("\n" + "=" * 70)
    print("TESTE DE VARIACOES")
    print(f"Escolhe na temporada {sorted(TEMPORADAS_OTIMIZACAO)} e confirma em {sorted(validacao)} "
          "(que a escolha nao viu).")
    print("=" * 70)
    print("Recalculando o modelo com outros pesos de tempo (demora alguns minutos)...")
    previsoes_por_xi = {ag.XI_PESO_TEMPORAL: previsoes_padrao}
    for xi in XIS_TESTE:
        if xi not in previsoes_por_xi:
            previsoes_por_xi[xi] = gerar_previsoes(dados, xi, mostrar_progresso=False)

    linhas = []
    for xi in XIS_TESTE:
        for edge_min in EDGES_MIN_TESTE:
            for teto in TETOS_EDGE_TESTE:
                r_otim = resumo(simular(previsoes_por_xi[xi], edge_min, teto, TEMPORADAS_OTIMIZACAO)["apostas"])
                r_valid = resumo(simular(previsoes_por_xi[xi], edge_min, teto, validacao)["apostas"])
                linhas.append((xi, edge_min, teto, r_otim, r_valid))

    print(f"\n  {'peso tempo':>10s} {'edge min':>8s} {'teto':>6s} | {'escolha: apostas':>16s} {'ROI':>7s} | "
          f"{'confirmacao: apostas':>20s} {'ROI':>7s}")
    for xi, edge_min, teto, ro, rv in linhas:
        atual = "  <- atual" if xi == ag.XI_PESO_TEMPORAL and edge_min == 0 and teto is None else ""
        print(f"  {xi:10.4f} {edge_min*100:6.0f}pp {('-' if teto is None else f'{teto*100:.0f}pp'):>6s} | "
              f"{(ro['n'] if ro else 0):16d} {_pct(ro and ro['roi_plano']):>7s} | "
              f"{(rv['n'] if rv else 0):20d} {_pct(rv and rv['roi_plano']):>7s}{atual}")

    atual = next(l for l in linhas if l[0] == ag.XI_PESO_TEMPORAL and l[1] == 0 and l[2] is None)
    validas = [l for l in linhas if l[3] and l[3]["n"] >= MIN_APOSTAS_OTIMIZACAO]
    if not validas:
        print("\nPoucas apostas pra comparar variacoes com seguranca.")
        return
    melhor = max(validas, key=lambda l: l[3]["roi_plano"])
    print(f"\nMelhor na escolha: peso tempo {melhor[0]}, edge minimo {melhor[1]*100:.0f} p.p., "
          f"teto {'nenhum' if melhor[2] is None else f'{melhor[2]*100:.0f} p.p.'}")
    rv_m, rv_a = melhor[4], atual[4]
    if melhor is atual:
        print("-> E a configuracao atual. Nada a mudar.")
    elif rv_m and rv_a and rv_m["roi_plano"] > rv_a["roi_plano"] and rv_m["n"] >= 50:
        print(f"-> Na confirmacao ela tambem foi melhor que a atual ({_pct(rv_m['roi_plano'])} x "
              f"{_pct(rv_a['roi_plano'])}). Candidata a mudanca - decisao sua.")
    else:
        print("-> Na confirmacao ela NAO repetiu a vantagem. Provavelmente sorte/'decoreba' do passado: "
              "nao recomendo mudar.")


# ---------- Programa principal ----------

def main():
    print("=== ApostaGol - backtest (nosso modelo + nossas regras + odds reais da Bet365) ===\n")
    if len(sys.argv) > 1:
        modo = sys.argv[1].lower()
    else:
        modo = input("1) Rapido (so regras atuais, ~1 min)  2) Completo (+ teste de variacoes, ~2 min): ").strip()
    completo = modo in ("2", "completo")

    dados = carregar_dados()
    print("\nSimulando semana a semana (o modelo e reajustado toda semana so com o passado)...")
    previsoes = gerar_previsoes(dados, ag.XI_PESO_TEMPORAL)
    sim = simular(previsoes)
    relatorio(previsoes, sim)
    salvar_csv(sim["apostas"])
    if completo:
        otimizar(dados, previsoes)
    print("\nLembrete: resultado passado nao garante resultado futuro.")


if __name__ == "__main__":
    main()
