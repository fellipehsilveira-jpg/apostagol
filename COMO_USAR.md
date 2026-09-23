# ApostaGol: como usar

Ferramenta pessoal que cruza o histórico dos jogos com as odds que você digita, para encontrar
apostas de valor. Ela organiza os dados, mas **não garante resultado**.

---

## 1. Abrindo o programa

**Jeito fácil:** dê dois cliques em `rodar_apostagol.bat`. Uma janela preta abre com o programa e,
no final, espera você apertar uma tecla antes de fechar.

**Pelo terminal:**
```
cd "C:\Users\Fellipe Honorio\Downloads\Primeiro APP"
python apostagol_dia.py
```

**Primeira vez em outro computador:** instale as bibliotecas antes de rodar:
```
python -m pip install -r requirements.txt
```

---

## 2. As perguntas do programa, na ordem

| Pergunta | O que responder |
|---|---|
| `Data pra analisar` | Enter para hoje, ou uma data no formato `2026-10-10` |
| `Quais competicoes analisar?` | Os números da lista separados por vírgula (`1,2`), ou Enter para todas |
| `1) Perguntar sempre 2) Pular` | `2` para ir rápido (forma e desfalque ficam neutros). `1` se você quiser informar forma e desfalques |
| `Quer conferir a odd real de algum cenário?` | O número de um cenário do painel. Enter encerra o programa |
| `Forma recente` (só no modo 1) | 1 = ótima ... 3 = neutra ... 5 = péssima |
| `Tem desfalque importante?` (só no modo 1) | `s` ou `n` |
| `Odd ...` | A odd **real** que você está vendo na Bet365/Betano. Pode usar ponto ou vírgula (`1.85` ou `1,85`) |

Que odds o programa pede em cada tipo de mercado:
- **Gols (mais de X):** a odd do "mais de" **e** a do "menos de" da mesma linha.
- **Resultado (casa/empate/fora):** as 3 odds do 1X2.
- **Chance dupla:** as 3 odds do 1X2 **e** a odd da chance dupla que você vai apostar.

> Se não houver jogo na data escolhida, o programa mostra as próximas datas com jogo.

**O painel do dia.** Depois de calcular, o programa mostra até 10 cenários em cada uma de 3 faixas,
sempre o mais provável de cada jogo:

| Faixa | Odd justa | Para quê |
|---|---|---|
| MAIS SEGURAS | 1.20 a 1.40 | Acerta mais e paga pouco. Boa para montar combinadas |
| EQUILIBRADAS | 1.40 a 1.70 | O meio-termo entre acerto e prêmio |
| ODDS MAIORES | 1.70 a 2.20 | Paga mais e acerta menos |

- **Chance** é a probabilidade que o modelo calculou.
- **Odd justa** é 1 ÷ chance. Se a Bet365/Betano pagar **acima** dela, o cenário está bem pago. Na
  prática, a casa quase sempre paga um pouco abaixo.
- **Numa combinada**, a odd justa é a multiplicação das odds justas. Por exemplo, 1.25 × 1.30 = 1.63.

Escolha os cenários que quiser. Não é para apostar em todos.

**Chance corrigida pelo histórico.** O modelo sozinho erra para lados diferentes conforme o mercado.
Ele é pessimista em "mais de 0.5 gols no 2T" e otimista em "mais de 3.5 gols". O `calibrar.py`
compara as previsões passadas com o que aconteceu e aprende uma correção para cada mercado. O
painel usa essa correção automaticamente (o cabeçalho avisa). Rode `python calibrar.py` uma vez por
mês (leva uns 3 minutos) para a correção acompanhar os jogos novos.

**Tempo de espera:** a API gratuita só permite 10 consultas por minuto. Na primeira execução do
dia, o programa baixa o histórico e pode levar uns 2 minutos (ele avisa quando está esperando).
Depois disso, cada execução leva uns 10 segundos.

---

## 3. Como o programa decide

1. Busca os jogos do dia nas competições que você escolheu.
2. Estuda as duas últimas temporadas de cada competição e estima a força de ataque e de defesa
   de cada time. Isso é o modelo **Dixon-Coles**, da biblioteca penaltyblog, e os jogos recentes
   pesam mais.
3. Calcula a chance de cada mercado: gols no jogo, gols no 1º tempo, gols no 2º tempo, resultado
   e chance dupla.
4. Mostra como **candidato** tudo que tiver pelo menos **58,8%** de chance, que é o mínimo para
   valer a pena com odd 1.70.
5. Quando você digita as odds, ele tira a margem da casa (método **Shin**) e compara a chance
   da casa com a do modelo.

---

## 4. Lendo o resultado

| Linha | Significado |
|---|---|
| Probabilidade estimada | A chance calculada pelo **nosso modelo** |
| Margem da casa | O lucro embutido nas odds pela casa (normalmente de 4 a 8%) |
| Probabilidade justa | A chance que **a casa** está dando, já sem a margem |
| Edge | A nossa chance menos a chance da casa. **Positivo = possível valor** |
| Nota (estrelas) | Um resumo visual do edge |
| Corredor | Diz se a odd está entre 1.70 e 2.20, a faixa recomendada |
| Stake | Quanto da banca apostar: Kelly 25%, **no máximo 2%** |

Exemplo: com banca de R$ 500 e stake de 1,50%, a aposta é de R$ 7,50.

---

## 5. Regras de ouro

1. **Digite as odds certas e dos dois lados.** Sem elas, o cálculo da margem fica errado.
2. **Edge acima de 15 pontos quase sempre é erro do modelo** (lesão, técnico novo, time que mudou
   muito). O programa avisa quando isso acontece. Os edges bons de verdade costumam ficar entre
   2 e 8 pontos.
3. **O topo da lista quase nunca paga 1.70.** Um mercado com 90% de chance paga perto de 1.10 na
   casa. Os candidatos interessantes costumam ficar entre 58% e 65%.
4. **Não passe da stake sugerida** e aposte sempre em simples, nunca em múltiplas.
5. **Anote tudo** numa planilha: data, jogo, mercado, odd, stake e resultado. Só depois de umas 100
   apostas dá para avaliar se o modelo funciona.

---

## No celular

Todo dia às 6h (horário de Brasília), o GitHub roda o programa sozinho, mesmo com o seu PC desligado, e
publica o painel de **hoje e amanhã** no site do ApostaGol (o endereço fica em Settings → Pages
do repositório).

- **Virar "aplicativo":** abra o site no celular e escolha "Adicionar à tela inicial" (no Android,
  pelo menu ⋮ do Chrome; no iPhone, pelo botão Compartilhar do Safari).
- **Conferir odd:** toque num cenário, digite as odds da Bet365/Betano e toque em Calcular. As contas
  são as mesmas do programa no PC. A banca que você digitar fica salva só no seu celular.
- **Combinada:** toque em "+ combinada" em até 3 cenários de jogos diferentes. A barra de baixo mostra
  a chance e a odd justa da combinada.
- **Atualizar na hora:** no GitHub, abra Actions → Painel diario → Run workflow.
- **Calibração:** é refeita sozinha uma vez por mês.
- **Segurança:** a chave da API fica guardada nos Secrets do GitHub e nunca aparece no código nem no
  site.

Para testar o site no PC: `python gerar_site.py 2026-10-10` gera a pasta `site/`.

---

## 6. Backtest: testar o método no passado

Dê dois cliques em `rodar_backtest.bat` (ou rode `python backtest.py`).
- **Modo rápido** (cerca de 1 minuto): mostra o resultado com as regras atuais.
- **Modo completo** (cerca de 2 minutos): também testa 24 variações de ajuste.

O backtest refaz as temporadas passadas **semana a semana**. Em cada semana, o modelo só conhece os
jogos anteriores a ela, e o programa aplica as nossas regras às **odds reais que a Bet365 oferecia**
(planilhas gratuitas do football-data.co.uk). Todas as apostas simuladas ficam salvas em
`backtest_apostas.csv`, que abre no Excel.

| Número | Significado |
|---|---|
| ROI | Lucro dividido pelo total apostado. Negativo = perdeu dinheiro |
| Banca | Quanto R$ 1000 teriam virado usando a stake sugerida (Kelly) |
| Maior queda | O pior tombo da banca no caminho |
| CLV | Se a odd **caiu** depois da aposta (CLV positivo), o mercado concordou com você. É o melhor sinal de que existe valor de verdade |
| Calibração | Quando o modelo diz 60%, acontece mesmo uns 60%? |
| Brier | O erro médio das previsões. Mostra quem acerta mais, o modelo ou a Bet365 |
| Referência | O resultado de apostar às cegas (dá o custo da margem da casa, cerca de −5%) |
| Teste de variações | Escolhe a melhor variação numa temporada e confere se ela repete nas seguintes. Se não repetir, era sorte |

Mercados testados: casa, empate, fora e mais de 2.5 gols (no Brasileirão, só casa, empate e fora).
Gols 1.5/3.5, 1º tempo, 2º tempo e chance dupla **não** têm odd histórica gratuita e não são testados.

---

## 7. Arquivos da pasta

| Arquivo | Para que serve | Pode apagar? |
|---|---|---|
| `apostagol_dia.py` | O programa | Não |
| `rodar_apostagol.bat` | O atalho de dois cliques | Pode, mas é útil manter |
| `backtest.py` / `rodar_backtest.bat` | O teste do método no passado | Não |
| `experimentos.py` | Compara variações do modelo no backtest (`python experimentos.py`, ~5 min) | Não |
| `calibrar.py` / `calibracao.json` | Aprende a correção das chances / guarda essa correção | Não. Se apagar o `.json`, o painel volta a usar a chance sem correção |
| `gerar_site.py` / `web/painel.html` | Gera o site do celular / o modelo da página | Não |
| `.github/workflows/painel.yml` | A automação diária na nuvem | Não |
| `.gitignore` | Impede que a chave e os caches subam para o GitHub | **Não, nunca** |
| `comparacao.py` | Testa o método de comparação de casas no backtest (`python comparacao.py`, ~30 s) | Não |
| `cache_backtest/` | As planilhas históricas com odds | Pode. O programa baixa de novo |
| `backtest_apostas.csv` | As apostas simuladas no último backtest | Pode |
| `config_footballdata.json` | Sua chave da API (**não compartilhe**) | Não. Se apagar, o programa pede a chave de novo |
| `cache_historico/` | O histórico salvo | Pode. O programa baixa de novo |
| `requirements.txt` | Lista das bibliotecas necessárias | Não |
| `apostagol_dia_v1_poisson.py` | Backup da versão antiga | Pode |
| `config.json`, `ligas_cache*.json`, `python apostagol_dia.py.txt` | Sobras da API antiga | Pode |

---

## 8. Problemas comuns

| Mensagem | O que fazer |
|---|---|
| `'python' não é reconhecido...` | O Python não está instalado ou não está no PATH. Reinstale pelo python.org marcando "Add Python to PATH" |
| `No module named 'penaltyblog'` (ou `requests`) | Rode `python -m pip install -r requirements.txt` |
| `Erro na chamada da API` / HTTP 403 | Chave errada ou vencida. Apague o `config_footballdata.json` e rode de novo para colar a chave |
| `aguardando XXs...` | É normal: é o limite de consultas por minuto. Só espere |
| `historico insuficiente` | O time tem poucos jogos na base (recém-promovido, por exemplo), então o jogo fica de fora |
| Nenhum jogo na data | Pode ser data FIFA. O programa mostra as próximas datas com jogo |

Se aparecer qualquer outro erro, copie a mensagem inteira e cole no Claude Code.
