"""
ApostaGol - Scan diario de valor (football-data.org)

Por enquanto so o Brasileirao Serie A esta disponivel de graca nessa API
(o plano gratuito do football-data.org cobre 12 competicoes fixas, e a
Serie B/copas nao estao entre elas). Quando fizer sentido pagar um plano
mais completo, adicionamos mais competicoes.

Mercados analisados, todos calculados na mesma passada (sem chamadas extras):
    - Total de gols no jogo (mais de X.5)
    - Total de gols no 1o tempo
    - Total de gols no 2o tempo
    - Resultado: vitoria do mandante, empate, vitoria do visitante
    - Chance dupla: 1X (casa ou empate), X2 (empate ou fora), 12 (casa ou fora)

A logica de valor e a mesma de sempre: odd minima de 1.70, e so aponta como
candidato o que tiver probabilidade real (nosso modelo) maior que o piso
necessario pra valer a pena com essa odd.

Como usar:
    pip install requests
    python apostagol_dia.py

Fonte: https://www.football-data.org/ (plano gratuito: 10 requisicoes/minuto,
       12 competicoes incluindo o Brasileirao Serie A, temporada atual liberada)
"""

import json
import math
import os
import time
from datetime import date, timedelta

import requests

BASE_URL = "https://api.football-data.org/v4"
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_footballdata.json")
TIMES_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "times_bsa_cache.json")

COMPETICAO_CODE = "BSA"  # Campeonato Brasileiro Serie A - unica disponivel de graca por enquanto
COMPETICAO_NOME = "Brasileirao Serie A"

ODD_MINIMA = 1.70
ODD_MAXIMA = 2.20  # corredor sugerido: odds muito altas costumam ter probabilidade real baixa demais
LIMIAR_PROB = 1 / ODD_MINIMA  # ~0.588 -> chance minima pra valer a pena com odd 1.70+

LINHAS_GOLS = [1.5, 2.5, 3.5]
LINHAS_GOLS_1T = [0.5, 1.5]
LINHAS_GOLS_2T = [0.5, 1.5, 2.5]
MAX_GOLS_MODELO = 8  # ate quantos gols simular no modelo de resultado (1X2)


# ---------- Configuracao da chave da API ----------

def carregar_api_key():
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


def headers_api(api_key):
    return {"X-Auth-Token": api_key}


# ---------- Chamadas a API ----------

def buscar_times_competicao(headers):
    """Busca todos os times da competicao (uma vez so, depois usa cache) pra poder
    achar o ID de um time pelo nome sem precisar de endpoint de busca."""
    if os.path.exists(TIMES_CACHE_FILE):
        with open(TIMES_CACHE_FILE, "r", encoding="utf-8") as f:
            times = json.load(f)
        if times:
            return times

    resp = requests.get(f"{BASE_URL}/competitions/{COMPETICAO_CODE}/teams", headers=headers)
    resp.raise_for_status()
    times = {str(t["id"]): t["name"] for t in resp.json().get("teams", [])}
    with open(TIMES_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(times, f)
    return times


def buscar_jogos_do_dia(data_ref, headers):
    data_seguinte = data_ref + timedelta(days=1)
    resp = requests.get(f"{BASE_URL}/competitions/{COMPETICAO_CODE}/matches", headers=headers, params={
        "dateFrom": data_ref.isoformat(), "dateTo": data_seguinte.isoformat(),
    })
    if resp.status_code != 200:
        print(f"  [debug] erro HTTP {resp.status_code}: {resp.text[:200]}")
        return []
    corpo = resp.json()
    erros = corpo.get("errors")
    if erros:
        print(f"  [debug] erro da API: {erros}")
        return []
    partidas = corpo.get("matches", [])
    if not partidas:
        print(f"  [debug] resposta ok, mas 0 partidas. resultSet: {corpo.get('resultSet')}")
    return partidas


def buscar_proximas_datas_com_jogos(data_ref, headers, dias=30):
    data_fim = data_ref + timedelta(days=dias)
    resp = requests.get(f"{BASE_URL}/competitions/{COMPETICAO_CODE}/matches", headers=headers, params={
        "dateFrom": data_ref.isoformat(), "dateTo": data_fim.isoformat(),
    })
    if resp.status_code != 200:
        return []
    partidas = resp.json().get("matches", [])
    datas = sorted({p["utcDate"][:10] for p in partidas})
    return datas


def buscar_ultimas_partidas(time_id, headers, quantidade=10):
    resp = requests.get(f"{BASE_URL}/teams/{time_id}/matches", headers=headers, params={
        "limit": quantidade, "status": "FINISHED",
    })
    resp.raise_for_status()
    time.sleep(6.5)  # plano gratuito: 10 requisicoes/minuto - da folga entre chamadas
    return resp.json().get("matches", [])


def medias_time(time_id, headers, cache):
    """Media de gols marcados pelo proprio time (total, 1o tempo, 2o tempo) nas
    ultimas partidas. Usa cache pra nao buscar o mesmo time duas vezes na mesma sessao."""
    if time_id in cache:
        return cache[time_id]

    partidas = buscar_ultimas_partidas(time_id, headers)
    total = primeiro_tempo = segundo_tempo = 0
    n = 0
    for p in partidas:
        placar = p.get("score", {})
        ft = placar.get("fullTime") or {}
        ht = placar.get("halfTime") or {}
        if ft.get("home") is None or ht.get("home") is None:
            continue  # partida sem placar completo cadastrado - pula
        eh_casa = p["homeTeam"]["id"] == time_id
        gols_full = ft["home"] if eh_casa else ft["away"]
        gols_ht = ht["home"] if eh_casa else ht["away"]
        gols_2t = gols_full - gols_ht
        total += gols_full
        primeiro_tempo += gols_ht
        segundo_tempo += gols_2t
        n += 1

    media = {
        "total": total / n if n else 0,
        "primeiro_tempo": primeiro_tempo / n if n else 0,
        "segundo_tempo": segundo_tempo / n if n else 0,
        "partidas_analisadas": n,
    }
    cache[time_id] = media
    return media


# ---------- Modelo estatistico ----------

FATOR_FORMA = {"1": 1.15, "2": 1.07, "3": 1.0, "4": 0.93, "5": 0.85}


def poisson_pmf(k, lam):
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def prob_over(linha, lam):
    limite = math.floor(linha)
    cdf = sum(poisson_pmf(k, lam) for k in range(0, limite + 1))
    return max(0.0, min(1.0, 1 - cdf))


def prob_1x2(lam_casa, lam_fora, max_gols=MAX_GOLS_MODELO):
    """Modelo de Poisson independente (simplificacao - nao ajusta correlacao de
    placares baixos como modelos mais avancados tipo Dixon-Coles)."""
    p_casa = p_empate = p_fora = 0.0
    for i in range(max_gols + 1):
        pi = poisson_pmf(i, lam_casa)
        for j in range(max_gols + 1):
            pj = poisson_pmf(j, lam_fora)
            prob = pi * pj
            if i > j:
                p_casa += prob
            elif i == j:
                p_empate += prob
            else:
                p_fora += prob
    return p_casa, p_empate, p_fora


def prob_justa_multipla(*odds):
    """Remove a margem da casa (overround) usando os odds de TODOS os desfechos
    possiveis de um mercado. Funciona pra mercados de 2 lados (over/under, dupla
    chance) ou 3 lados (1X2). Retorna a lista de probabilidades justas, na mesma
    ordem dos odds passados, e o overround (margem total)."""
    implicitas = [1 / o for o in odds]
    overround = sum(implicitas)
    justas = [imp / overround for imp in implicitas]
    return justas, overround


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
        return float(texto)
    except ValueError:
        print(f'Valor invalido: "{texto}". Use um numero tipo 1.85.')
        return None


# ---------- Escaneamento de candidatos (todos os mercados numa passada) ----------

def escanear_candidatos(jogos, cache, headers):
    candidatos = []
    for jogo in jogos:
        casa_id, casa_nome = jogo["homeTeam"]["id"], jogo["homeTeam"]["name"]
        fora_id, fora_nome = jogo["awayTeam"]["id"], jogo["awayTeam"]["name"]

        m_casa = medias_time(casa_id, headers, cache)
        m_fora = medias_time(fora_id, headers, cache)
        if m_casa["partidas_analisadas"] == 0 or m_fora["partidas_analisadas"] == 0:
            continue

        base = {
            "jogo": jogo, "casa_nome": casa_nome, "fora_nome": fora_nome,
            "casa_id": casa_id, "fora_id": fora_id, "m_casa": m_casa, "m_fora": m_fora,
        }

        # Gols - jogo todo / 1o tempo / 2o tempo
        for chave, linhas, rotulo in [
            ("total", LINHAS_GOLS, "gols"),
            ("primeiro_tempo", LINHAS_GOLS_1T, "gols no 1T"),
            ("segundo_tempo", LINHAS_GOLS_2T, "gols no 2T"),
        ]:
            lam = m_casa[chave] + m_fora[chave]
            for linha in linhas:
                prob = prob_over(linha, lam)
                if prob >= LIMIAR_PROB:
                    candidatos.append({**base, "tipo": "gols", "campo": chave, "linha": linha,
                                        "rotulo": f"mais de {linha} {rotulo}", "prob": prob, "lam": lam})

        # Resultado (1X2) e chance dupla
        p_casa, p_empate, p_fora = prob_1x2(m_casa["total"], m_fora["total"])
        p_1x, p_x2, p_12 = p_casa + p_empate, p_empate + p_fora, p_casa + p_fora
        opcoes = [
            ("vitoria_casa", f"vitoria do {casa_nome}", p_casa),
            ("empate", "empate", p_empate),
            ("vitoria_fora", f"vitoria do {fora_nome}", p_fora),
            ("dupla_1x", f"dupla chance {casa_nome} ou empate (1X)", p_1x),
            ("dupla_x2", f"dupla chance empate ou {fora_nome} (X2)", p_x2),
            ("dupla_12", f"dupla chance {casa_nome} ou {fora_nome} (12)", p_12),
        ]
        for campo, rotulo, prob in opcoes:
            if prob >= LIMIAR_PROB:
                candidatos.append({**base, "tipo": "resultado", "campo": campo, "linha": None,
                                    "rotulo": rotulo, "prob": prob,
                                    "p_casa": p_casa, "p_empate": p_empate, "p_fora": p_fora})

    candidatos.sort(key=lambda x: x["prob"], reverse=True)
    return candidatos


# ---------- Finalizacao de um candidato escolhido ----------

def finalizar_candidato(c, forma_casa, forma_fora, desfalque_casa, desfalque_fora):
    ajuste_casa = forma_casa * desfalque_casa
    ajuste_fora = forma_fora * desfalque_fora

    if c["tipo"] == "gols":
        lam_casa_ajust = c["m_casa"][c["campo"]] * ajuste_casa
        lam_fora_ajust = c["m_fora"][c["campo"]] * ajuste_fora
        lam_ajust = lam_casa_ajust + lam_fora_ajust
        prob_ajustada = prob_over(c["linha"], lam_ajust)

        odd_over = pedir_float(f"Odd na Bet365/Betano pra '{c['rotulo']}': ")
        if odd_over is None:
            return
        odd_under = pedir_float(f"Odd na Bet365/Betano pra o lado de baixo ('menos de {c['linha']}...'): ")
        if odd_under is None:
            return
        justas, overround = prob_justa_multipla(odd_over, odd_under)
        prob_justa = justas[0]
        edge = prob_ajustada - prob_justa
        _imprimir_resultado(c, prob_ajustada, prob_justa, overround, edge, odd_over,
                             extra=f"Esperado: {lam_ajust:.2f} ({c['casa_nome']} {lam_casa_ajust:.2f} + "
                                   f"{c['fora_nome']} {lam_fora_ajust:.2f})")
        return

    # tipo == "resultado": pede as 3 odds do 1X2 pra conseguir de-vigar qualquer mercado derivado
    print(f"\nPra calcular a margem certinho, preciso das 3 odds do resultado (1X2) desse jogo.")
    odd_casa = pedir_float(f"Odd vitoria {c['casa_nome']} (1): ")
    if odd_casa is None:
        return
    odd_empate = pedir_float("Odd empate (X): ")
    if odd_empate is None:
        return
    odd_fora = pedir_float(f"Odd vitoria {c['fora_nome']} (2): ")
    if odd_fora is None:
        return

    justas, overround = prob_justa_multipla(odd_casa, odd_empate, odd_fora)
    jc, je, jf = justas
    mapa_prob_justa = {
        "vitoria_casa": (jc, odd_casa), "empate": (je, odd_empate), "vitoria_fora": (jf, odd_fora),
        "dupla_1x": (jc + je, None), "dupla_x2": (je + jf, None), "dupla_12": (jc + jf, None),
    }
    prob_justa, odd_referencia = mapa_prob_justa[c["campo"]]
    edge = c["prob"] - prob_justa
    odd_mostrar = odd_referencia if odd_referencia else (1 / prob_justa if prob_justa > 0 else 0)
    _imprimir_resultado(c, c["prob"], prob_justa, overround, edge, odd_mostrar,
                         extra=f"Modelo (1X2): casa {c['p_casa']*100:.1f}% | empate {c['p_empate']*100:.1f}% | "
                               f"fora {c['p_fora']*100:.1f}%")


def _imprimir_resultado(c, prob_modelo, prob_justa, overround, edge, odd_referencia, extra=""):
    nota = estrelas_do_edge(edge)
    print("\n" + "=" * 55)
    print(f"{c['casa_nome']} x {c['fora_nome']} - {c['rotulo']}")
    print("=" * 55)
    if extra:
        print(extra)
    print(f"Probabilidade estimada (nosso modelo): {prob_modelo * 100:.1f}%")
    print(f"Margem da casa (overround): {(overround - 1) * 100:.1f}%")
    print(f"Probabilidade justa (sem a margem): {prob_justa * 100:.1f}%")
    print(f"Edge: {edge * 100:.1f} pontos percentuais")
    print(f"\nNOTA: {'*' * nota}{'.' * (6 - nota)}  ({nota}/6 estrelas)")

    if odd_referencia < ODD_MINIMA:
        print(f"Atencao: odd ({odd_referencia:.2f}) abaixo do minimo de {ODD_MINIMA} que voce definiu.")
    elif odd_referencia > ODD_MAXIMA:
        print(f"Atencao: odd ({odd_referencia:.2f}) fora do corredor recomendado "
              f"({ODD_MINIMA}-{ODD_MAXIMA}). Confira com atencao.")
    else:
        print(f"Odd dentro do corredor recomendado ({ODD_MINIMA}-{ODD_MAXIMA}).")

    if edge > 0:
        stake = kelly_fracionario(prob_modelo, odd_referencia)
        print(f"\nSugestao de stake (Kelly fracionario 25%, limitado a 2% da banca): {stake * 100:.2f}%")
        print("(Orientacao matematica de gestao de risco, nao garantia de lucro.)")
    print("\nLembrete: leitura organizada dos dados, nao garantia de resultado.")


# ---------- Programa principal ----------

def main():
    print("=== ApostaGol - scan diario de valor (football-data.org) ===\n")
    print(f"Competicao monitorada: {COMPETICAO_NOME} (unica liberada no plano gratuito por enquanto)\n")
    api_key = carregar_api_key()
    headers = headers_api(api_key)

    data_texto = input("Data pra analisar (AAAA-MM-DD, Enter para hoje): ").strip()
    try:
        data_ref = date.fromisoformat(data_texto) if data_texto else date.today()
    except ValueError:
        print(f'Data invalida: "{data_texto}". Use o formato AAAA-MM-DD, ex: 2026-09-28.')
        return

    print(f"\nBuscando jogos de {data_ref.isoformat()} no {COMPETICAO_NOME}...")
    jogos = buscar_jogos_do_dia(data_ref, headers)
    if not jogos:
        print("Nenhum jogo nesse dia exato. Procurando nos proximos 30 dias pra ver o que existe...")
        datas = buscar_proximas_datas_com_jogos(data_ref, headers)
        if datas:
            print(f"\nEssa competicao tem jogos cadastrados nestas datas: {', '.join(datas)}")
            print("Roda o script de novo usando uma dessas datas.")
        else:
            print(f"\nNao encontrei NENHUM jogo do {COMPETICAO_NOME} nos proximos 30 dias nessa base de dados.")
            print("Pode ser que a temporada atual ainda nao esteja totalmente cadastrada nessa API - "
                  "vale conferir manualmente no site football-data.org se a competicao esta com jogos.")
        return

    print(f"{len(jogos)} jogo(s) encontrados. Calculando medias das ultimas 10 partidas de cada time "
          f"(isso pode demorar um pouco por causa do limite de 10 requisicoes/minuto da API gratuita)...")
    cache = {}
    candidatos = escanear_candidatos(jogos, cache, headers)

    if not candidatos:
        print(f"\nNenhum mercado bateu o piso de probabilidade ({LIMIAR_PROB*100:.1f}%) hoje.")
        print("(Normal em dias fracos - tenta outra data.)")
        return

    print(f"\n{len(candidatos)} candidato(s) com valor potencial (odd minima {ODD_MINIMA}), "
          f"do mais provavel pro menos provavel:\n")
    for i, c in enumerate(candidatos):
        print(f"{i + 1:2d}. {c['casa_nome']} x {c['fora_nome']} - {c['rotulo']} "
              f"- probabilidade: {c['prob'] * 100:.1f}%")

    print("\nQuer responder forma recente/desfalque em cada analise, ou pular e usar padrao neutro "
          "(mais rapido)?")
    modo_rapido = input("1) Perguntar sempre  2) Pular (usar neutro)  - escolha (1 ou 2): ").strip() == "2"

    while True:
        indice = pedir_indice("\nEscolha um candidato pra conferir a odd real (Enter pra sair): ",
                               len(candidatos))
        if indice is None:
            break
        c = candidatos[indice]

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
        print("Voltando pra lista de candidatos (os dados ja buscados nao precisam ser buscados de novo).")
        for i, c2 in enumerate(candidatos):
            print(f"{i + 1:2d}. {c2['casa_nome']} x {c2['fora_nome']} - {c2['rotulo']} "
                  f"- probabilidade: {c2['prob'] * 100:.1f}%")


if __name__ == "__main__":
    try:
        main()
    except requests.exceptions.HTTPError as e:
        print(f"\nErro na chamada da API: {e}")
        print("Confira se sua chave (Token) esta correta.")
    except Exception as e:
        print(f"\nOcorreu um erro: {e}")
