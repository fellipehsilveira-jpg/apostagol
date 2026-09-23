# ApostaGol — Resumo do projeto para o Claude Code

## O que é
Ferramenta pessoal em Python que cruza dados históricos de futebol com odds reais
(Bet365/Betano, digitadas manualmente) para encontrar apostas de valor (value betting).
Uso pessoal, sem fins comerciais.

## Estado atual
- Script funcional: `apostagol_dia.py` (anexo/na mesma pasta do projeto)
- Fonte de dados: **football-data.org**, plano gratuito
  - Auth: header `X-Auth-Token` com o token da conta
  - Chave salva em `config_footballdata.json`
  - Limite: **10 requisições/minuto** — importante respeitar isso
  - Temporada atual (2026) já liberada de graça (diferente da API-Football, que só libera
    2022-2024 no plano free — testamos isso primeiro e não funcionou)
  - Endpoint de partidas do dia: `/v4/competitions/{codigo}/matches?dateFrom=X&dateTo=Y`
    (usar `dateFrom`/`dateTo`, não o filtro `date` sozinho — já tivemos bug de filtro
    sendo ignorado e trazendo o campeonato inteiro)
  - Endpoint de últimas partidas de um time: `/v4/teams/{id}/matches?limit=10&status=FINISHED`
  - Sem endpoint de lesões nem de escanteios/cartões no plano gratuito — por isso o
    escopo de mercados é só gols/resultado (ver abaixo)

## Escopo de mercados (decisão consciente, não mexer sem avisar)
- Total de gols no jogo (mais de X.5)
- Total de gols no 1º tempo
- Total de gols no 2º tempo
- Resultado: vitória casa / empate / vitória fora
- Chance dupla: 1X, X2, 12
- **Não** inclui escanteios/cartões (dado não disponível de graça) nem arbitragem/surebet
  (decisão consciente — foco é só value betting)

## Regras de negócio já definidas com o usuário
- Odd mínima: **1.70**
- Corredor recomendado: **1.70–2.20** (fora disso, alerta mas não bloqueia)
- Piso de probabilidade pra virar candidato: `1 / odd_minima` (~58.8%)
- Gestão de banca: Kelly fracionário (25% do Kelly cheio), limitado a **2% da banca** por entrada
- Apostas sempre simples (singles), nunca combinadas
- Forma recente e desfalques: hoje são perguntas manuais (1-5 pra forma, s/n pra desfalque);
  usuário pode optar por "modo rápido" que pula essas perguntas (assume neutro)

## Modelo estatístico atual (a ser substituído/melhorado com penaltyblog)
- Poisson **independente** simples (não Dixon-Coles) para estimar gols/resultado
- Lambda de cada time = média de gols marcados nas últimas 10 partidas (ajustada por
  forma recente e desfalque, ambos multiplicadores manuais)
- De-vig (remoção da margem da casa) feito na mão com método proporcional simples
- **Sabemos que isso é uma simplificação e o usuário já foi avisado**: edges muito altos
  (tipo 20+ pontos percentuais) são sinal de limitação do modelo, não de oportunidade real

## PRÓXIMA TAREFA (o que o usuário quer agora)
1. **Adicionar as outras 11 competições gratuitas do football-data.org** (hoje só temos
   BSA rodando). Lista completa:
   - WC — FIFA World Cup
   - CL — UEFA Champions League
   - BL1 — Bundesliga
   - DED — Eredivisie
   - BSA — Campeonato Brasileiro Série A
   - PD — Primera Division
   - FL1 — Ligue 1
   - ELC — Championship
   - PPL — Primeira Liga
   - EC — European Championship
   - SA — Serie A (Itália)
   - PL — Premier League

   Objetivo: usuário escolhe uma ou mais competições por execução, **sem substituir**
   o que já existe — é para aumentar o volume de jogos analisados por dia, mantendo a
   mesma lógica de negócio (odd mínima 1.70, Kelly, etc.)

2. **Integrar a biblioteca `penaltyblog`** (https://github.com/martineastwood/penaltyblog,
   `pip install penaltyblog`) para substituir nossas fórmulas caseiras pelas dela:
   - Modelo de gols: usar Dixon-Coles (ou o que a lib recomendar) em vez do Poisson
     independente que fizemos na mão
   - Remoção de margem da casa (implied odds / overround): usar o módulo de odds
     implícitas da lib em vez do nosso cálculo proporcional manual
   - **Importante**: o repositório tem um skill file em
     `.claude/skills/penaltyblog/SKILL.md` com contexto completo da API da lib — ler
     esse arquivo antes de integrar, ele já traz os exemplos de uso corretos
   - Manter nossas regras de negócio (odd 1.70, corredor, Kelly, singles) por cima do
     modelo novo — a lib entra na camada de cálculo estatístico, não muda as decisões
     de produto que já tomamos

## Cuidados/lições aprendidas nessa conversa (pra não repetir)
- Sempre validar sintaxe do arquivo antes de entregar (já tivemos bugs de edição que
  quebraram funções sem querer)
- API-Football (a que usamos antes de trocar) tem plano free travado em temporadas
  antigas — não voltar pra ela
- O filtro de data da football-data.org precisa ser `dateFrom`/`dateTo`, testado e
  funcionando
- Rate limit de 10 req/min é real e trava rápido com múltiplas competições — vale
  pensar em cache persistente de médias de time em disco (já tínhamos começado a
  implementar isso, ver se o Claude Code quer manter a abordagem ou fazer diferente
  com mais robustez, já que agora pode rodar e testar de verdade)
- Scraping de casas de apostas (Bet365/Betano) foi descartado por causa dos termos de
  uso — odds continuam sendo digitadas manualmente pelo usuário, isso é intencional
