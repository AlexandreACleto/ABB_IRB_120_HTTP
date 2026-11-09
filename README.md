# ABB IRB 120 via TCP/HTTP

Painel web para controlar até **três ABB IRB 120** (controlador IRC5, RobotWare 5.14) pela rede local: movimento por
botões ou por **controle de Xbox**, **sequências gravadas** e um **gêmeo digital 3D** que acompanha os braços.
Funciona em qualquer navegador moderno da LAN e não precisa instalar nada além do Python.

> **Segurança.** O painel **não consegue interromper um movimento que já começou**. A parada de emergência é sempre o
> botão físico do robô / FlexPendant. Comece com passo pequeno, velocidade baixa e a área livre.

## Como funciona

```
navegador ──HTTP/JSON (porta 8080)──▶ painel_robos.py ──TCP texto (porta 1025)──▶ PyServer.mod (RAPID no IRC5)
```

| Camada | O que é usado |
|---|---|
| Enlace e rede | Ethernet, IPv4 (`192.168.0.x/24`) |
| Transporte | TCP, porta **1025** (o robô é o servidor, o PC é o cliente) |
| Aplicação | Protocolo de **texto** próprio: uma mensagem ASCII por comando (até 80 caracteres), resposta `OK ...` ou `ERR ...` |

O robô precisa da opção **616-1 PC Interface** (Socket Messaging). Comandos do `PyServer.mod`:

| Comando | Resposta |
|---|---|
| `PING` | `OK PONG` |
| `POS` | `OK x y z rx ry rz` (mm e graus) |
| `JOINTS` | `OK j1 j2 j3 j4 j5 j6` (graus) |
| `MOVEJ j1 … j6 v [z]` | `OK DONE` |
| `MOVEL x y z rx ry rz v [z]` | `OK DONE` |

`v` é a velocidade (mm/s) e `z` é o raio de curva opcional (mm). Com `z > 0` o robô emenda no movimento seguinte sem
parar: é assim que o jog do controle fica contínuo (interpolado). O protocolo é texto puro e sem autenticação no
robô: mantenha a rede dos robôs isolada.

## Início rápido

Requisitos: Windows, **Python 3.12** (só biblioteca padrão, sem `pip install`) e um navegador baseado em Chromium.

**Treinar sem robô (simulador):** dê dois cliques em `2_Iniciar_TREINO_simulador.bat`. Ele sobe 3 robôs simulados e
o painel, e abre o navegador. Nenhum robô real se move.

**Robôs reais:** dê dois cliques em `1_Iniciar_painel_ROBOS_REAIS.bat`, ou rode:

```bash
python painel_robos.py --open --robot esq=192.168.0.11 --robot dir=192.168.0.12 --robot centro=192.168.0.14
```

O terminal imprime o link com o token de acesso (gerado a cada execução). Use o link com `localhost` no PC onde o
controle de Xbox está ligado: o navegador só libera o controle nesse endereço.

## Preparar cada robô (uma vez)

| Robô | IP | Módulo RAPID |
|---|---|---|
| `esq` | `192.168.0.11` | `rapid/robo_11/PyServer.mod` |
| `dir` | `192.168.0.12` | `rapid/robo_12/PyServer.mod` |
| `centro` | `192.168.0.14` | `rapid/robo_14/PyServer.mod` |

1. **IP da LAN** (RobotWare 5.14): Reiniciar → Avançado → **X-Start** → *Settings* → *Use the following IP settings*,
   com máscara `255.255.255.0` e **gateway `192.168.0.1`** (não pode ficar vazio; não há roteador).
2. **Módulo:** carregue o `PyServer.mod` do robô pelo FlexPendant (pen drive) e rode `PyMain`
   (*Debug → PP to Routine*). O FlexPendant deve mostrar *"PyServer: aguardando cliente na porta 1025"*.
3. **PC:** IP fixo `192.168.0.10/24` na placa de rede ligada ao switch dos robôs.

Os três módulos são iguais, só muda o `SERVER_IP`. A fonte única é `rapid/PyServer.mod`. O jog contínuo exige a
**versão atual** do módulo; com a antiga o painel avisa e o jog não funciona (botões, posições e sequências continuam).

O passo a passo ilustrado, com fotos do FlexPendant, está em [`docs/`](docs/).

## O que o painel faz

- **Mover:** botões cartesianos (X, Y, Z) e por junta (J1 a J6), com passo e velocidade ajustáveis, para um ou vários
  robôs ao mesmo tempo (se um comando for recusado, nenhum se move). Também vai a uma posição absoluta.
- **Sequências:** grava pontos (juntas dos braços selecionados), salva em `sequencias.json` e executa em ordem,
  com os braços juntos. Só dá para parar entre pontos.
- **Gêmeo digital:** desenho 3D em Canvas (sem bibliotecas) com a cinemática direta do IRB 120. Mostra o destino em
  sombra azul durante movimentos por junta e confere o TCP calculado com o `POS` do robô. **Arraste um braço** no
  desenho para mudar a posição dele na cena (Shift + arrastar gira); a posição fica salva no navegador.

### Controle de Xbox

Segure **LB ou RB** (botão de segurança) para mover. Soltou, o robô para em menos de meio segundo.

| Controle | Função |
|---|---|
| Analógico esquerdo | J1 (←→) e J2 (↑↓) |
| Analógico direito | J4 (←→) e J3 (↑↓) |
| RT / LT | J5 (+ / −) |
| D-pad ←→ | J6 |
| Y | Troca de braço (cada um, depois todos) |
| A | Grava o ponto atual na sequência em gravação |
| B (com LB/RB apertado) | Volta à posição em que o robô estava ao conectar |
| X | Fecha/abre a garra (só indicador; ainda sem comando ao robô) |
| Back | Alterna juntas / cartesiano |
| L3 (segurar) | Movimento lento |

A atribuição de cada eixo é configurável na página. No controle, a velocidade máxima é 100 mm/s.

## Estrutura

```
painel_robos.py        servidor HTTP do painel e a lógica de jog/sequências
painel/index.html      página única (HTML + CSS + JS): painel, controle de Xbox e gêmeo digital
robo_cliente.py        cliente Python do protocolo (validação de limites)
multi_robo.py          vários robôs em paralelo
simulador_robo.py      simulador do PyServer.mod para treinar sem robô
conectar_robo.py       leitura via PC SDK da ABB (somente leitura)
monitor_robo.py        monitor em tempo real via PC SDK (somente leitura)
rapid/                 PyServer.mod e uma cópia por robô (só muda o IP)
docs/                  guias em Word e Markdown
1_Iniciar_...bat       atalhos de duplo clique
```

`conectar_robo.py` e `monitor_robo.py` são opcionais e precisam do PC SDK da ABB (RobotStudio) e do `pythonnet`.

## Estado do projeto

- O painel, o jog contínuo, as sequências e o gêmeo digital foram testados com o **simulador**.
- O `PyServer.mod` com raio de curva **ainda não foi validado em um controlador IRC5**. Teste primeiro no controlador
  virtual do RobotStudio ou no robô com velocidade baixa. Os ajustes finos do jog (`JOG_DT`, `JOG_SMOOTH`,
  `JOG_TIMEOUT`, `JOG_ZONE_MAX`) ficam no topo de `painel_robos.py`.
- Falta o comando de garra no `PyServer.mod`.
- Licença: ainda não definida.
