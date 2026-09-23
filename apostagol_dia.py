"""
ApostaGol - Scan diario de valor (football-data.org + penaltyblog)

Competicoes: as 12 do plano gratuito do football-data.org. A cada execucao o
script mostra quantos jogos cada uma tem no dia e voce escolhe uma ou mais.

Mercados analisados, todos calculados na mesma passada (sem chamadas extras):
    - Total de gols no jogo (mais de X.5)
    - Total de gols no 1o tempo
    - Total de gols no 2o tempo
    - Resultado: vitoria do mandante, empate, vitoria do visitante
    - Chance dupla: 1X (casa ou empate), X2 (empate ou fora), 12 (casa ou fora)

Modelo (camada estatistica, via biblioteca penaltyblog):
    - Dixon-Coles ajustado por competicao com os jogos da temporada atual + a
      anterior, dando mais peso aos jogos recentes. Um modelo pro jogo todo,
      um pro 1o tempo e um pro 2o tempo.
    - Margem da casa removida pelo metodo de Shin (penaltyblog.implied).

Regras de negocio (nao mudaram): odd minima 1.70, corredor 1.70-2.20 (alerta,
nao bloqueia), piso de probabilidade 1/1.70, Kelly fracionario 25% limitado a
2% da banca, apostas sempre simples.

Como usar:
    pip install -r requirements.txt
    python apostagol_dia.py

Fonte: https://www.football-data.org/ (plano gratuito: 10 requisicoes/minuto)
O historico de cada competicao fica salvo em disco (pasta cache_historico),
entao so a primeira execucao do dia gasta requisicoes com ele.
"""

import json
import math
import os
import time
from collections import Counter, deque
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import penaltyblog as pb
import requests
from scipy.stats import poisson

PASTA = os.path.dirname(os.path.abspath(__file__))
BASE_URL = "https://api.football-data.org/v4"
CONFIG_FILE = os.path.join(PASTA, "config_footballdata.json")
PASTA_CACHE = os.path.join(PASTA, "cache_historico")
ARQUIVO_CALIBRACAO = os.path.join(PASTA, "calibracao.json")  # gerado pelo calibrar.py

# codigo -> (nome, jogos em campo neutro?, quantos anos volta pra "temporada anterior")
COMPETICOES = {
    "BSA": ("Brasileirao Serie A", False, 1),
    "PL": ("Premier League", False, 1),
    "ELC": ("Championship", False, 1),
    "PD": ("La Liga (Primera Division)", False, 1),
    "SA": ("Serie A (Italia)", False, 1),
    "BL1": ("Bundesliga", False, 1),
    "FL1": ("Ligue 1", False, 1),
    "DED": ("Eredivisie", False, 1),
    "PPL": ("Primeira Liga", False, 1),
    "CL": ("UEFA Champions League", False, 1),
    "WC": ("Copa do Mundo FIFA", True, 4),
    "EC": ("Eurocopa", True, 4),
}

ODD_MINIMA = 1.70
ODD_MAXIMA = 2.20  # corredor sugerido: odds muito altas costumam ter probabilidade real baixa demais
LIMIAR_PROB = 1 / ODD_MINIMA  # ~0.588 -> chance minima pra valer a pena com odd 1.70+ (usado no backtest)

# Painel diario: mostra os mercados mais provaveis separados pela "odd justa" (1 / probabilidade)
PROB_MINIMA_PAINEL = 1 / ODD_MAXIMA  # ~45% -> tudo que teria odd justa de ate 2.20
FAIXAS_PAINEL = [
    ("MAIS SEGURAS", 1.20, 1.40),  # abaixo de 1.20 a casa paga quase nada (e perdeu no backtest)
    ("EQUILIBRADAS", 1.40, 1.70),
    ("ODDS MAIORES", 1.70, ODD_MAXIMA),
]
ITENS_POR_FAIXA = 10
ALERTA_EDGE_ALTO = 0.15  # edge acima disso e mais provavel ser limitacao do modelo do que valor real

LINHAS_GOLS = [1.5, 2.5, 3.5]
LINHAS_GOLS_1T = [0.5, 1.5]
LINHAS_GOLS_2T = [0.5, 1.5, 2.5]
MAX_GOLS_MODELO = 10  # tamanho da grade de placares (0 a 10 gols pra cada lado)

XI_PESO_TEMPORAL = 0.0018  # decaimento do peso dos jogos antigos (valor padrao do Dixon-Coles)
MIN_JOGOS_TIME = 5  # time com menos jogos que isso no historico fica de fora (modelo instavel)
METODO_DEVIG = "shin"  # remocao da margem: shin corrige o vies favorito/azarao

LIMITE_REQ_MINUTO = 9  # API permite 10/min; deixa 1 de folga
CACHE_HORAS_TEMPORADA_ATUAL = 6
FUSO_BRASIL = timezone(timedelta(hours=-3))  # "jogo do dia" = data no horario de Brasilia

ATRIBUTO_GRADE = {
    "vitoria_casa": "home_win", "empate": "draw", "vitoria_fora": "away_win",
    "dupla_1x": "double_chance_1x", "dupla_x2": "double_chance_x2", "dupla_12": "double_chance_12",
}


# ---------- Configuracao da chave da API ----------

def carregar_api_key():
    # na nuvem (GitHub Actions) a chave vem de um Secret, via variavel de ambiente
    if os.environ.get("FOOTBALL_DATA_TOKEN"):
        return os.environ["FOOTBALL_DATA_TOKEN"].strip()
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            dados = json.load(f)
            if dados.get("api_key"):
                return dados["api_key"]
    chave = input("Cole sua chave (Token) do football-data.org: ").strip()
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"api_key": chave}, f)
    print(f"Chave salva em {CONFIG_FILE}.\n")
    return chave


# ---------- Cliente da API com controle do limite de 10 req/min ----------

class ClienteAPI:
    def __init__(self, api_key):
        self.headers = {"X-Auth-Token": api_key}
        self.chamadas = deque()  # horario das chamadas do ultimo minuto
        self.pausa_ate = 0.0

    def _esperar_vaga(self):
        agora = time.monotonic()
        while self.chamadas and agora - self.chamadas[0] > 60:
            self.chamadas.popleft()
        espera = self.pausa_ate - agora
        if len(self.chamadas) >= LIMITE_REQ_MINUTO:
            espera = max(espera, 60.5 - (agora - self.chamadas[0]))
        if espera > 0:
            if espera > 2:
                print(f"  (limite de requisicoes da API gratuita - aguardando {espera:.0f}s...)")
            time.sleep(espera)

    def get(self, caminho, **params):
        for _ in range(4):
            self._esperar_vaga()
            resp = requests.get(f"{BASE_URL}{caminho}", headers=self.headers, params=params, timeout=30)
            self.chamadas.append(time.monotonic())
            reset = int(resp.headers.get("X-RequestCounter-Reset", 60) or 60)
            if resp.status_code == 429:
                self.pausa_ate = time.monotonic() + reset + 1
                continue
            if resp.headers.get("X-Requests-Available-Minute") == "0":
                self.pausa_ate = time.monotonic() + reset + 1
            return resp
        resp.raise_for_status()
        return resp


# ---------- Jogos do dia (uma chamada so pra todas as competicoes) ----------

def data_brasil(jogo):
    utc = datetime.fromisoformat(jogo["utcDate"].replace("Z", "+00:00"))
    return utc.astimezone(FUSO_BRASIL)


def buscar_partidas(cliente, data_ini, data_fim):
    resp = cliente.get("/matches", dateFrom=data_ini.isoformat(), dateTo=data_fim.isoformat(),
                       competitions=",".join(COMPETICOES))
    if resp.status_code != 200:
        print(f"  [debug] erro HTTP {resp.status_code}: {resp.text[:200]}")
        return []
    return resp.json().get("matches", [])


def buscar_jogos_do_dia(cliente, data_ref):
    """Retorna {codigo_competicao: [jogos]} so com jogos ainda nao iniciados cuja
    data no horario de Brasilia e a data pedida (jogo 21h30 de Brasilia ja e o
    dia seguinte em UTC, por isso busca dois dias e filtra aqui)."""
    por_comp = {}
    for jogo in buscar_partidas(cliente, data_ref, data_ref + timedelta(days=1)):
        if data_brasil(jogo).date() != data_ref:
            continue
        if jogo.get("status") not in ("SCHEDULED", "TIMED"):
            continue
        por_comp.setdefault(jogo["competition"]["code"], []).append(jogo)
    return por_comp


def proximas_datas(cliente, data_ref):
    """{codigo: [datas com jogo nos proximos 10 dias]} (horario de Brasilia)."""
    datas = {}
    for jogo in buscar_partidas(cliente, data_ref, data_ref + timedelta(days=10)):
        if jogo.get("status") not in ("SCHEDULED", "TIMED"):
            continue  # adiado/cancelado/ja jogado nao conta como "proxima data"
        datas.setdefault(jogo["competition"]["code"], set()).add(data_brasil(jogo).date().isoformat())
    return {codigo: sorted(ds) for codigo, ds in datas.items()}


def mostrar_proximas_datas(cliente, data_ref):
    datas = proximas_datas(cliente, data_ref)
    if not datas:
        print("\nNenhuma das 12 competicoes tem jogo nos proximos 10 dias nessa base de dados "
              "(pode ser data FIFA / pausa de calendario).")
        return
    print("\nDatas com jogos nos proximos 10 dias (horario de Brasilia):")
    for codigo in COMPETICOES:
        if codigo in datas:
            print(f"  {codigo:4s} {COMPETICOES[codigo][0]:28s} {', '.join(datas[codigo])}")
    print("Roda o script de novo usando uma dessas datas.")


# ---------- Historico por competicao (com cache em disco) ----------

def _placar_partida(p):
    """Extrai o placar de 90 minutos. Em mata-mata com prorrogacao/penaltis o
    fullTime da API inclui esses gols, entao usa regularTime quando existir."""
    placar = p.get("score") or {}
    ft = placar.get("regularTime") or placar.get("fullTime") or {}
    ht = placar.get("halfTime") or {}
    if ft.get("home") is None or ft.get("away") is None:
        return None
    ht_valido = ht.get("home") is not None and ht.get("away") is not None
    return {
        "data": p["utcDate"][:10],
        "casa": p["homeTeam"]["id"], "fora": p["awayTeam"]["id"],
        "ft": [ft["home"], ft["away"]],
        "ht": [ht["home"], ht["away"]] if ht_valido else None,
    }


def carregar_temporada(cliente, codigo, ano):
    """Partidas encerradas de uma temporada. Temporada ja terminada fica em cache
    pra sempre; a atual e baixada de novo a cada CACHE_HORAS_TEMPORADA_ATUAL horas."""
    os.makedirs(PASTA_CACHE, exist_ok=True)
    arquivo = os.path.join(PASTA_CACHE, f"{codigo}_{ano}.json")
    if os.path.exists(arquivo):
        try:
            with open(arquivo, "r", encoding="utf-8") as f:
                cache = json.load(f)
            encerrada = cache.get("fim_temporada") and cache["fim_temporada"] < (date.today() - timedelta(days=3)).isoformat()
            idade_h = (time.time() - cache["baixado_em"]) / 3600
            if encerrada or idade_h < CACHE_HORAS_TEMPORADA_ATUAL:
                return cache["partidas"]
        except (ValueError, KeyError):
            pass  # cache corrompido - baixa de novo

    resp = cliente.get(f"/competitions/{codigo}/matches", season=ano, status="FINISHED")
    if resp.status_code != 200:
        print(f"  [{codigo}] temporada {ano} indisponivel (HTTP {resp.status_code}) - seguindo sem ela.")
        return []
    brutas = resp.json().get("matches", [])
    partidas = [x for x in (_placar_partida(p) for p in brutas) if x]
    fim = brutas[0]["season"].get("endDate") if brutas else None
    with open(arquivo, "w", encoding="utf-8") as f:
        json.dump({"baixado_em": time.time(), "fim_temporada": fim, "partidas": partidas}, f)
    return partidas


# ---------- Modelo estatistico (penaltyblog) ----------

FATOR_FORMA = {"1": 1.15, "2": 1.07, "3": 1.0, "4": 0.93, "5": 0.85}


class ModeloCompeticao:
    """Modelos Dixon-Coles da mesma competicao: jogo todo, 1o tempo e 2o tempo.
    data_base/xi/periodos existem pro backtest (data_base = "hoje" daquele momento
    do passado; periodos=("total",) quando a fonte nao tem placar de intervalo)."""

    def __init__(self, codigo, partidas, data_base=None, xi=XI_PESO_TEMPORAL,
                 periodos=("total", "primeiro_tempo", "segundo_tempo")):
        self.neutro = COMPETICOES[codigo][1]
        self.data_base = data_base or date.today()
        self.xi = xi
        self.jogos_por_time = Counter()
        for p in partidas:
            self.jogos_por_time[p["casa"]] += 1
            self.jogos_por_time[p["fora"]] += 1

        # a API tem alguns placares de intervalo errados (maior que o final) - ficam fora dos modelos 1T/2T
        com_ht = [p for p in partidas if p["ht"] and p["ht"][0] <= p["ft"][0] and p["ht"][1] <= p["ft"][1]]
        receitas = {
            "total": (partidas, lambda p: p["ft"]),
            "primeiro_tempo": (com_ht, lambda p: p["ht"]),
            "segundo_tempo": (com_ht, lambda p: [p["ft"][0] - p["ht"][0], p["ft"][1] - p["ht"][1]]),
        }
        self.modelos = {periodo: self._ajustar(*receitas[periodo]) for periodo in periodos}
        self.params = {periodo: m.get_params() for periodo, m in self.modelos.items()}
        self.rho = {periodo: float(p["rho"]) for periodo, p in self.params.items()}

    def _ajustar(self, partidas, gols):
        pesos = pb.models.dixon_coles_weights(pd.to_datetime([p["data"] for p in partidas]), self.xi,
                                              base_date=pd.Timestamp(self.data_base))
        modelo = pb.models.DixonColesGoalModel(
            [gols(p)[0] for p in partidas], [gols(p)[1] for p in partidas],
            [str(p["casa"]) for p in partidas], [str(p["fora"]) for p in partidas],
            weights=pesos, neutral_venue=[int(self.neutro)] * len(partidas),
        )
        modelo.fit()
        return modelo

    def time_conhecido(self, time_id):
        return self.jogos_por_time[time_id] >= MIN_JOGOS_TIME

    def gols_esperados(self, casa_id, fora_id):
        """{periodo: (gols esperados casa, gols esperados fora, rho)}. Calcula direto dos
        parametros (mesma formula do predict) porque o predict() da biblioteca trava com
        gols esperados extremos (ex: time recem-promovido com poucos jogos na base)."""
        c, f = str(casa_id), str(fora_id)
        saida = {}
        for periodo, p in self.params.items():
            vantagem_casa = 0.0 if self.neutro else p["home_advantage"]
            lam_casa = math.exp(p[f"attack_{c}"] + p[f"defence_{f}"] + vantagem_casa)
            lam_fora = math.exp(p[f"attack_{f}"] + p[f"defence_{c}"])
            saida[periodo] = (lam_casa, lam_fora, self.rho[periodo])
        return saida


def grade_dixon_coles(lam_casa, lam_fora, rho):
    """Grade de placares Dixon-Coles a partir dos gols esperados. Nao usa
    pb.models.create_dixon_coles_grid porque na versao 1.12.2 ela troca lambda/mu
    na correcao dos placares 1-0 e 0-1 (diverge do predict() do proprio modelo e
    do artigo original: tau(1,0) = 1 + mu*rho, tau(0,1) = 1 + lambda*rho).
    O rho e limitado a faixa valida pra esses gols esperados (senao a correcao
    gera probabilidade negativa quando algum lambda e muito alto)."""
    rho = min(max(rho, -1 / lam_casa, -1 / lam_fora), 1 / (lam_casa * lam_fora), 1.0)
    gols = np.arange(MAX_GOLS_MODELO + 1)
    matriz = np.outer(poisson.pmf(gols, lam_casa), poisson.pmf(gols, lam_fora))
    matriz[0, 0] *= 1 - rho * lam_casa * lam_fora
    matriz[1, 0] *= 1 + rho * lam_fora
    matriz[0, 1] *= 1 + rho * lam_casa
    matriz[1, 1] *= 1 - rho
    return pb.models.FootballProbabilityGrid(np.clip(matriz, 0, None), lam_casa, lam_fora)


def prob_mercado(c, ajuste_casa=1.0, ajuste_fora=1.0):
    """Probabilidade do mercado do candidato, remontando a grade Dixon-Coles com os
    gols esperados multiplicados pelos ajustes manuais (forma/desfalque)."""
    periodo = c["campo"] if c["tipo"] == "gols" else "total"
    lam_casa, lam_fora, rho = c["esperados"][periodo]
    grade = grade_dixon_coles(lam_casa * ajuste_casa, lam_fora * ajuste_fora, rho)
    if c["tipo"] == "gols":
        return grade.total_goals("over", c["linha"])
    return getattr(grade, ATRIBUTO_GRADE[c["campo"]])


def remover_margem(*odds):
    """Probabilidades justas (sem a margem da casa) de TODOS os desfechos de um
    mercado, na mesma ordem das odds, e a margem total. Retorna (None, None) se as
    odds nao formarem um mercado valido (ex: erro de digitacao)."""
    try:
        r = pb.implied.calculate_implied(list(odds), method=METODO_DEVIG)
    except (ValueError, ZeroDivisionError) as e:
        print(f"Nao consegui remover a margem com essas odds ({e}). Confere os valores digitados.")
        return None, None
    return r.probabilities, r.margin


def estrelas_do_edge(edge):
    if edge < 0:
        return 1
    if edge < 0.03:
        return 2
    if edge < 0.07:
        return 3
    if edge < 0.12:
        return 4
    if edge < 0.18:
        return 5
    return 6


def kelly_fracionario(prob, odd, fracao=0.25, teto=0.02):
    b = odd - 1
    if b <= 0:
        return 0.0
    q = 1 - prob
    kelly_completo = (b * prob - q) / b
    return min(max(0.0, kelly_completo * fracao), teto)


def perguntar_forma(nome_time):
    print(f"Forma recente de {nome_time}: 1) Otima 2) Boa 3) Neutra 4) Ruim 5) Pessima")
    return FATOR_FORMA.get((input("Escolha (1-5, padrao 3): ").strip() or "3"), 1.0)


def perguntar_desfalque(nome_time):
    resp = input(f"{nome_time} tem desfalque importante essa semana? (s/n): ").strip().lower()
    return 0.85 if resp == "s" else 1.0


# ---------- Entradas seguras ----------

def pedir_indice(mensagem, quantidade):
    texto = input(mensagem).strip()
    if not texto:
        return None
    if not texto.isdigit():
        print("Isso nao e um numero - roda o script de novo e digite so o numero da lista.")
        return None
    valor = int(texto)
    if valor < 1 or valor > quantidade:
        print(f"Numero fora da lista (escolha entre 1 e {quantidade}).")
        return None
    return valor - 1


def pedir_float(mensagem):
    texto = input(mensagem).strip().replace(",", ".")
    try:
        valor = float(texto)
    except ValueError:
        print(f'Valor invalido: "{texto}". Use um numero tipo 1.85.')
        return None
    if valor <= 1.0:
        print(f"Odd {valor} nao faz sentido (odd decimal e sempre maior que 1.00).")
        return None
    return valor


def pedir_competicoes(disponiveis):
    """disponiveis: lista de codigos com jogo no dia. Retorna os escolhidos."""
    texto = input("\nQuais competicoes analisar? Numeros separados por virgula (ex: 1,3) "
                  "ou Enter pra todas com jogo: ").strip()
    if not texto:
        return disponiveis
    escolhidas = []
    for parte in texto.replace(" ", "").split(","):
        if not parte.isdigit() or not 1 <= int(parte) <= len(disponiveis):
            print(f'Ignorando "{parte}" (fora da lista).')
            continue
        codigo = disponiveis[int(parte) - 1]
        if codigo not in escolhidas:
            escolhidas.append(codigo)
    return escolhidas


# ---------- Escaneamento de candidatos (todos os mercados numa passada) ----------

def escanear_candidatos(codigo, jogos, modelo):
    candidatos = []
    for jogo in jogos:
        casa_id, casa_nome = jogo["homeTeam"]["id"], jogo["homeTeam"]["name"]
        fora_id, fora_nome = jogo["awayTeam"]["id"], jogo["awayTeam"]["name"]
        faltando = [nome for tid, nome in [(casa_id, casa_nome), (fora_id, fora_nome)]
                    if not modelo.time_conhecido(tid)]
        if faltando:
            print(f"  [{codigo}] pulando {casa_nome} x {fora_nome}: historico insuficiente de "
                  f"{', '.join(faltando)} (menos de {MIN_JOGOS_TIME} jogos na base)")
            continue

        base = {"competicao": codigo, "jogo": jogo, "casa_nome": casa_nome, "fora_nome": fora_nome,
                "esperados": modelo.gols_esperados(casa_id, fora_id)}
        for m in mercados_do_jogo(casa_nome, fora_nome):
            c = {**base, **m}
            c["prob_modelo"] = prob_mercado(c)
            c["prob"] = calibrar(c, c["prob_modelo"])
            if c["prob"] >= PROB_MINIMA_PAINEL:
                candidatos.append(c)
    return candidatos


def mercados_do_jogo(casa_nome, fora_nome):
    """Todos os mercados analisados (o calibrar.py usa a mesma lista)."""
    mercados = []
    for campo, linhas, rotulo in [
        ("total", LINHAS_GOLS, "gols"),
        ("primeiro_tempo", LINHAS_GOLS_1T, "gols no 1T"),
        ("segundo_tempo", LINHAS_GOLS_2T, "gols no 2T"),
    ]:
        for linha in linhas:
            mercados.append({"tipo": "gols", "campo": campo, "linha": linha,
                             "rotulo": f"mais de {linha} {rotulo}"})
    for campo, rotulo in [
        ("vitoria_casa", f"vitoria do {casa_nome}"),
        ("empate", "empate"),
        ("vitoria_fora", f"vitoria do {fora_nome}"),
        ("dupla_1x", f"dupla chance {casa_nome} ou empate (1X)"),
        ("dupla_x2", f"dupla chance empate ou {fora_nome} (X2)"),
        ("dupla_12", f"dupla chance {casa_nome} ou {fora_nome} (12)"),
    ]:
        mercados.append({"tipo": "resultado", "campo": campo, "linha": None, "rotulo": rotulo})
    return mercados


# ---------- Calibracao (corrige o otimismo do modelo com o historico) ----------

def chave_mercado(c):
    return f"{c['tipo']}|{c['campo']}|{c['linha']}"


def _carregar_calibracao():
    try:
        with open(ARQUIVO_CALIBRACAO, "r", encoding="utf-8") as f:
            return json.load(f).get("mercados", {})
    except (OSError, ValueError):
        return {}  # sem arquivo (ou arquivo ruim): usa a probabilidade do modelo sem correcao


def calibrar(c, prob):
    """Aplica a correcao aprendida pelo calibrar.py pra esse mercado (se existir)."""
    coef = CALIBRACAO.get(chave_mercado(c))
    if not coef:
        return prob
    p = min(max(prob, 1e-4), 1 - 1e-4)
    z = coef[0] * math.log(p / (1 - p)) + coef[1]
    return 1 / (1 + math.exp(-z))


CALIBRACAO = _carregar_calibracao()


# ---------- Finalizacao de um candidato escolhido ----------

def finalizar_candidato(c, forma_casa, forma_fora, desfalque_casa, desfalque_fora):
    ajuste_casa = forma_casa * desfalque_casa
    ajuste_fora = forma_fora * desfalque_fora
    prob_ajustada = calibrar(c, prob_mercado(c, ajuste_casa, ajuste_fora))
    periodo = c["campo"] if c["tipo"] == "gols" else "total"
    lam_casa = c["esperados"][periodo][0] * ajuste_casa
    lam_fora = c["esperados"][periodo][1] * ajuste_fora
    extra = (f"Gols esperados (Dixon-Coles{'' if periodo == 'total' else ', ' + c['rotulo'].split()[-1]}): "
             f"{c['casa_nome']} {lam_casa:.2f} x {lam_fora:.2f} {c['fora_nome']}")

    if c["tipo"] == "gols":
        odd_over = pedir_float(f"Odd na Bet365/Betano pra '{c['rotulo']}': ")
        if odd_over is None:
            return
        odd_under = pedir_float(f"Odd na Bet365/Betano pra o lado de baixo ('menos de {c['linha']}...'): ")
        if odd_under is None:
            return
        justas, margem = remover_margem(odd_over, odd_under)
        if justas is None:
            return
        _imprimir_resultado(c, prob_ajustada, justas[0], margem, odd_over, extra)
        return

    # tipo == "resultado": pede as 3 odds do 1X2 pra conseguir remover a margem de qualquer mercado derivado
    print("\nPra calcular a margem certinho, preciso das 3 odds do resultado (1X2) desse jogo.")
    odd_casa = pedir_float(f"Odd vitoria {c['casa_nome']} (1): ")
    if odd_casa is None:
        return
    odd_empate = pedir_float("Odd empate (X): ")
    if odd_empate is None:
        return
    odd_fora = pedir_float(f"Odd vitoria {c['fora_nome']} (2): ")
    if odd_fora is None:
        return
    justas, margem = remover_margem(odd_casa, odd_empate, odd_fora)
    if justas is None:
        return
    jc, je, jf = justas
    mapa = {
        "vitoria_casa": (jc, odd_casa), "empate": (je, odd_empate), "vitoria_fora": (jf, odd_fora),
        "dupla_1x": (jc + je, None), "dupla_x2": (je + jf, None), "dupla_12": (jc + jf, None),
    }
    prob_justa, odd_aposta = mapa[c["campo"]]
    if odd_aposta is None:
        # chance dupla: a probabilidade justa vem do 1X2, mas a stake usa a odd real oferecida
        odd_aposta = pedir_float(f"Odd oferecida pra '{c['rotulo']}': ")
        if odd_aposta is None:
            return

    grade = grade_dixon_coles(lam_casa, lam_fora, c["esperados"]["total"][2])
    extra += (f"\nModelo (1X2): casa {grade.home_win*100:.1f}% | empate {grade.draw*100:.1f}% | "
              f"fora {grade.away_win*100:.1f}%")
    _imprimir_resultado(c, prob_ajustada, prob_justa, margem, odd_aposta, extra)


def _imprimir_resultado(c, prob_modelo, prob_justa, margem, odd_aposta, extra=""):
    edge = prob_modelo - prob_justa
    nota = estrelas_do_edge(edge)
    print("\n" + "=" * 55)
    print(f"[{c['competicao']}] {c['casa_nome']} x {c['fora_nome']} - {c['rotulo']}")
    print("=" * 55)
    if extra:
        print(extra)
    print(f"Probabilidade estimada (nosso modelo): {prob_modelo * 100:.1f}%")
    print(f"Margem da casa (overround): {margem * 100:.1f}%")
    print(f"Probabilidade justa (sem a margem, metodo {METODO_DEVIG}): {prob_justa * 100:.1f}%")
    print(f"Edge: {edge * 100:.1f} pontos percentuais")
    print(f"\nNOTA: {'*' * nota}{'.' * (6 - nota)}  ({nota}/6 estrelas)")
    if edge >= ALERTA_EDGE_ALTO:
        print(f"Cuidado: edge acima de {ALERTA_EDGE_ALTO*100:.0f} pontos quase sempre e limitacao do modelo "
              "(time mudou muito, lesao que o modelo nao sabe...), nao presente da casa.")

    if odd_aposta < ODD_MINIMA:
        print(f"Atencao: odd ({odd_aposta:.2f}) abaixo do minimo de {ODD_MINIMA} que voce definiu.")
    elif odd_aposta > ODD_MAXIMA:
        print(f"Atencao: odd ({odd_aposta:.2f}) fora do corredor recomendado "
              f"({ODD_MINIMA}-{ODD_MAXIMA}). Confira com atencao.")
    else:
        print(f"Odd dentro do corredor recomendado ({ODD_MINIMA}-{ODD_MAXIMA}).")

    if edge > 0:
        stake = kelly_fracionario(prob_modelo, odd_aposta)
        print(f"\nSugestao de stake (Kelly fracionario 25%, limitado a 2% da banca): {stake * 100:.2f}%")
        print("(Orientacao matematica de gestao de risco, nao garantia de lucro.)")
    print("\nLembrete: leitura organizada dos dados, nao garantia de resultado.")


# ---------- Programa principal ----------

def calcular_candidatos(cliente, jogos_por_comp, codigos):
    """Historico -> modelo -> cenarios de cada competicao escolhida (usado pelo terminal
    e pelo gerar_site.py)."""
    candidatos = []
    for codigo in codigos:
        jogos = jogos_por_comp[codigo]
        ano = int(jogos[0]["season"]["startDate"][:4])
        anos = [ano, ano - COMPETICOES[codigo][2]]
        print(f"\n[{codigo}] carregando historico das temporadas {anos[1]} e {anos[0]} "
              f"(usa cache em disco quando possivel)...")
        partidas = []
        for a in anos:
            partidas += carregar_temporada(cliente, codigo, a)
        if len(partidas) < 30:
            print(f"  [{codigo}] so {len(partidas)} jogos no historico - pouco pra ajustar o modelo, pulando.")
            continue
        print(f"  [{codigo}] ajustando Dixon-Coles com {len(partidas)} jogos...")
        try:
            modelo = ModeloCompeticao(codigo, partidas)
        except Exception as e:
            print(f"  [{codigo}] nao consegui ajustar o modelo ({e}) - pulando essa competicao.")
            continue
        candidatos += escanear_candidatos(codigo, jogos, modelo)
    return candidatos


def montar_painel(candidatos):
    """Separa os candidatos por faixa de odd justa e pega os mais provaveis de cada uma,
    no maximo um cenario por jogo em cada faixa (pra variar as opcoes)."""
    painel = []
    for nome, odd_min, odd_max in FAIXAS_PAINEL:
        faixa = sorted((c for c in candidatos if odd_min <= 1 / c["prob"] < odd_max),
                       key=lambda c: c["prob"], reverse=True)
        itens, jogos_usados = [], set()
        for c in faixa:
            if c["jogo"]["id"] not in jogos_usados:
                itens.append(c)
                jogos_usados.add(c["jogo"]["id"])
        painel.append((nome, odd_min, odd_max, itens[:ITENS_POR_FAIXA]))
    return painel


def listar_candidatos(painel):
    exibidos = []
    for nome, odd_min, odd_max, itens in painel:
        print(f"\n--- {nome} (odd justa {odd_min:.2f} a {odd_max:.2f}) ---")
        if not itens:
            print("  (nada nessa faixa hoje)")
        for c in itens:
            exibidos.append(c)
            hora = data_brasil(c["jogo"]).strftime("%H:%M")
            print(f"{len(exibidos):3d}. {hora} [{c['competicao']}] {c['casa_nome']} x {c['fora_nome']} - {c['rotulo']}"
                  f" | chance {c['prob'] * 100:.1f}% | odd justa {1 / c['prob']:.2f}")
    return exibidos


def main():
    print("=== ApostaGol - scan diario de valor (football-data.org + penaltyblog) ===\n")
    cliente = ClienteAPI(carregar_api_key())

    data_texto = input("Data pra analisar (AAAA-MM-DD, Enter para hoje): ").strip()
    try:
        data_ref = date.fromisoformat(data_texto) if data_texto else date.today()
    except ValueError:
        print(f'Data invalida: "{data_texto}". Use o formato AAAA-MM-DD, ex: 2026-09-28.')
        return

    print(f"\nBuscando jogos de {data_ref.isoformat()} nas {len(COMPETICOES)} competicoes gratuitas...")
    jogos_por_comp = buscar_jogos_do_dia(cliente, data_ref)
    if not jogos_por_comp:
        print("Nenhum jogo (ainda nao iniciado) nesse dia. Procurando nos proximos 10 dias...")
        mostrar_proximas_datas(cliente, data_ref)
        return

    disponiveis = [codigo for codigo in COMPETICOES if codigo in jogos_por_comp]
    print("\nCompeticoes com jogo nesse dia:")
    for i, codigo in enumerate(disponiveis):
        print(f"  {i + 1:2d}. {codigo:4s} {COMPETICOES[codigo][0]:28s} {len(jogos_por_comp[codigo])} jogo(s)")
    sem_jogo = [codigo for codigo in COMPETICOES if codigo not in jogos_por_comp]
    if sem_jogo:
        print(f"  (sem jogo: {', '.join(sem_jogo)})")

    escolhidas = pedir_competicoes(disponiveis)
    if not escolhidas:
        print("Nenhuma competicao escolhida.")
        return

    candidatos = calcular_candidatos(cliente, jogos_por_comp, escolhidas)
    if not candidatos:
        print(f"\nNenhum mercado com chance de pelo menos {PROB_MINIMA_PAINEL*100:.0f}% hoje.")
        print("(Normal em dias fracos - tenta outra data.)")
        return

    painel = montar_painel(candidatos)
    print("\n" + "=" * 70)
    print(f"PAINEL DO DIA {data_ref.strftime('%d/%m/%Y')} - os cenarios mais provaveis de cada faixa")
    print("=" * 70)
    calib = "corrigida pelo historico (calibrar.py)" if CALIBRACAO else "do modelo, SEM calibracao (rode calibrar.py)"
    print(f"Chance = {calib}. Odd justa = 1 / chance: se a casa pagar ACIMA dela, o")
    print("cenario esta bem pago. Na pratica a casa quase sempre paga um pouco abaixo (a margem dela).")
    print("Combinada: a odd justa e a multiplicacao das odds justas dos jogos escolhidos.")
    exibidos = listar_candidatos(painel)

    print("\nQuer responder forma recente/desfalque em cada analise, ou pular e usar padrao neutro "
          "(mais rapido)?")
    modo_rapido = input("1) Perguntar sempre  2) Pular (usar neutro)  - escolha (Enter = 2): ").strip() != "1"

    while True:
        indice = pedir_indice("\nQuer conferir a odd real de algum cenario? Digite o numero (Enter pra sair): ",
                              len(exibidos))
        if indice is None:
            break
        c = exibidos[indice]

        if modo_rapido:
            forma_casa = forma_fora = 1.0
            desfalque_casa = desfalque_fora = 1.0
        else:
            forma_casa = perguntar_forma(c["casa_nome"])
            forma_fora = perguntar_forma(c["fora_nome"])
            desfalque_casa = perguntar_desfalque(c["casa_nome"])
            desfalque_fora = perguntar_desfalque(c["fora_nome"])

        finalizar_candidato(c, forma_casa, forma_fora, desfalque_casa, desfalque_fora)

        print("\n" + "-" * 55)
        print("Voltando pro painel (os dados ja buscados nao precisam ser buscados de novo).")
        listar_candidatos(painel)


if __name__ == "__main__":
    try:
        main()
    except requests.exceptions.HTTPError as e:
        print(f"\nErro na chamada da API: {e}")
        print("Confira se sua chave (Token) esta correta.")
    except Exception as e:
        print(f"\nOcorreu um erro: {e}")
