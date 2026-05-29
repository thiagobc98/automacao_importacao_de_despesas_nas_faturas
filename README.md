# auto-slack-importacao-lancamentos

Sistema de automação que permite importar lançamentos financeiros em massa para o banco de dados a partir de uma planilha Google Sheets, acionado diretamente pelo Slack via slash command.

---

## Sumário

- [Visão Geral](#visão-geral)
- [Arquitetura](#arquitetura)
- [Fluxo Completo](#fluxo-completo)
- [Estrutura do Projeto](#estrutura-do-projeto)
- [Planilha Google Sheets](#planilha-google-sheets)
- [Modos de Aplicação](#modos-de-aplicação)
- [Validações](#validações)
- [Banco de Dados](#banco-de-dados)
- [Variáveis de Ambiente](#variáveis-de-ambiente)
- [Infraestrutura AWS](#infraestrutura-aws)
- [Deploy](#deploy)
- [Execução Local](#execução-local)

---

## Visão Geral

O usuário digita um slash command no Slack informando o ID de uma planilha Google Sheets. O sistema lê os dados, valida tudo, e insere os lançamentos financeiros nas faturas corretas do banco de dados — sem nenhuma intervenção manual.

```
Slack → /importar <id_planilha>
          ↓
      Router Lambda
      (lê planilha, valida, distribui)
          ↓
      Fila SQS FIFO
          ↓
      Worker Lambda
      (insere lançamentos no banco)
```

---

## Arquitetura

O sistema usa duas Lambdas separadas conectadas por uma fila SQS FIFO:

### Router (`SlkRouter_import_lan`)
- Recebe o webhook do Slack (HTTP POST)
- Lê a planilha Google Sheets
- Valida todos os dados
- Executa duas verificações pré-insert no banco
- Distribui um contrato por mensagem na fila SQS

### Worker (`SlkWorker_import_lan`)
- Consome a fila SQS (1 mensagem por vez, processamento sequencial)
- Busca as faturas em aberto do contrato no banco
- Cria grupos de parcelamento se necessário
- Insere os lançamentos nas faturas

A separação em duas Lambdas resolve dois problemas: o timeout do Slack (3 segundos para responder) e o processamento sequencial e confiável de múltiplos contratos.

---

## Fluxo Completo

```
1. Usuário → Slack: /importar 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms

2. Slack → Router Lambda (HTTP POST com assinatura HMAC)

3. Router Lambda:
   a. Verifica assinatura do Slack (segurança)
   b. Lê planilha "lançamentos" pelo ID informado
   c. Processa o DataFrame (pula 3 linhas de cabeçalho, faz casting de tipos)
   d. Busca os id_contratos no banco pelo codigo_contrato
   e. Converte valores monetários (formato BR: 1.234,56 → 1234.56)
   f. Valida campos (pessoas, categorias, regras de negócio)
   g. Valida pré-insert:
      - Contratos sem faturas no mês/ano informado
      - Lançamentos já existentes para o mesmo contrato/categoria/mês
   h. Se tudo OK: envia uma mensagem SQS por contrato
   i. Responde ao Slack com status

4. SQS FIFO → Worker Lambda (1 mensagem por vez)

5. Worker Lambda:
   a. Lê os dados do contrato da mensagem
   b. Busca faturas em aberto (filtradas por mês/ano de mes_ano_insercao)
   c. Limita quantidade conforme modo de aplicação
   d. Cria grupo de parcelamento (PP ou RR) se necessário
   e. Insere um lançamento por fatura
   f. Retorna 200 (SQS remove a mensagem da fila)
```

---

## Estrutura do Projeto

```
auto-slack-importacao-lancamentos/
│
├── .github/workflows/
│   └── deploy-dev.yml          # CI/CD: deploy automático no push para staging
│
├── cloudformation/
│   ├── sqs.yaml                # Fila SQS FIFO + Dead Letter Queue + KMS
│   └── security-group.yaml     # Security group da Lambda na VPC
│
├── env/
│   ├── dev.yaml                # Configurações de VPC/subnets para dev
│   └── prod.yaml               # Configurações de VPC/subnets para prod
│
├── src/
│   ├── SlkRouter_import_lan/           # Lambda Router (entry point Slack)
│   │   ├── main.py                     # Handler principal
│   │   ├── requirements.txt
│   │   └── utils/utils/
│   │       ├── tool_db_slack.py        # Funções de banco de dados
│   │       ├── sheets.py               # Integração Google Sheets
│   │       └── valida_doc.py           # Validadores de documentos BR
│   │
│   └── SlkWorker_import_lan/           # Lambda Worker (consome SQS)
│       ├── main.py                     # Handler principal
│       ├── requirements.txt
│       └── utils/
│           ├── tool_db_slack.py        # Funções de banco de dados
│           ├── sheets.py               # Integração Google Sheets
│           └── valida_doc.py           # Validadores de documentos BR
│
├── serverless.yaml             # Definição das Lambdas (Serverless Framework)
├── .env.local.exemple          # Exemplo de variáveis de ambiente para rodar local
├── run_local.py                # Script para simular execução local
└── main_slack.py               # Script auxiliar local
```

---

## Planilha Google Sheets

A planilha deve ter uma aba chamada **`lançamentos`** com o seguinte formato:

> As 3 primeiras linhas são ignoradas (cabeçalhos visuais). Os dados começam na linha 4.

| Coluna | Tipo | Descrição |
|---|---|---|
| `codigo_contrato` | string | Código do contrato (ex: `CT-001`) |
| `descricao` | string | Descrição do lançamento (mínimo 2 caracteres) |
| `id_aplicar_de_forma` | int | Modo de aplicação: `1`, `2` ou `3` |
| `qtd_parcelas` | int | Quantidade de parcelas |
| `valor` | float | Valor (formato BR: `1.234,56`) |
| `id_credito` | int | ID da pessoa de crédito |
| `id_debito` | int | ID da pessoa de débito |
| `id_categoria` | int | ID da categoria (release) |
| `mes_ano_insercao` | string | Mês/Ano alvo no formato `MM/AAAA` (ex: `05/2025`) |

### Como acionar

No Slack, use o comando:

```
/importar <ID_DA_PLANILHA_GOOGLE>
```

O ID da planilha é a parte da URL do Google Sheets entre `/d/` e `/edit`:

```
https://docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms/edit
                                        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                        Este é o ID
```

---

## Modos de Aplicação

O campo `id_aplicar_de_forma` define como o lançamento é distribuído entre as faturas:

| Valor | Nome | Comportamento | `qtd_parcelas` |
|---|---|---|---|
| `1` | Simples | Aplica o lançamento em **todas** as faturas em aberto do mês | deve ser `1` |
| `2` | Parcelado (PP) | Aplica em **N faturas** (a quantidade definida em `qtd_parcelas`), cria um grupo de parcelamento `PP` | deve ser `> 1` |
| `3` | Recorrente (RR) | Aplica em **1 fatura**, cria um grupo recorrente `RR` | deve ser `1` |

---

## Validações

O Router executa validações em duas etapas antes de enviar qualquer mensagem para a fila.

### Etapa 1 — Validação de campos

Para cada linha da planilha:

- `id_credito` deve existir na tabela `pessoas_lancamentos`
- `id_debito` deve existir na tabela `pessoas_lancamentos`
- `id_categoria` deve existir na tabela `release_categories` (e não estar excluído)
- `descricao` deve ter mais de 1 caractere
- Combinação `id_aplicar_de_forma` / `qtd_parcelas` deve ser válida (ver tabela acima)
- `codigo_contrato` deve existir no banco e não estar excluído

### Etapa 2 — Validação pré-insert (relatórios SQL)

Usando o `mes_ano_insercao` da planilha como referência de mês/ano:

**Relatório 1 — Contratos sem faturas no mês:**
Verifica se cada contrato possui ao menos uma fatura com `vencimento_fatura` no mês/ano informado e com status ativo (`fk_id_status_status_contrato = 2`). Se não tiver, bloqueia a importação.

**Relatório 2 — Lançamentos já existentes:**
Verifica se já existe algum lançamento ativo (`exc_lan = 'F'`) para a mesma combinação de contrato + categoria + mês/ano. Evita duplicatas.

Se qualquer erro for encontrado nas duas etapas, o Slack exibe a mensagem de erro detalhada e a importação é cancelada completamente.

---

## Banco de Dados

Tabelas utilizadas:

| Tabela | Uso |
|---|---|
| `contratos` | Busca por `codigo_contrato`, verifica status ativo |
| `faturas` | Busca faturas em aberto por contrato e mês/ano |
| `lancamentos` | Inserção dos lançamentos financeiros |
| `tb_lancamento_grupo_parcelamento` | Criação de grupos PP (parcelado) e RR (recorrente) |
| `pessoas_lancamentos` | Validação de IDs de crédito e débito |
| `release_categories` | Validação de IDs de categoria |

### Campos relevantes de `faturas` usados no filtro

```sql
MONTH(f.vencimento_fatura) = {mes}
YEAR(f.vencimento_fatura)  = {ano}
f.exc_fatura = 'F'          -- não excluída
f.status_fatura = 'PE'      -- pendente (apenas no Worker)
f.url_boleto_fatura IS NULL -- sem boleto gerado (apenas no Worker)
f.pagamento_fatura IS NULL  -- sem pagamento (apenas no Worker)
```

---

## Variáveis de Ambiente

Copie `.env.local.exemple` para `.env.local` e preencha:

| Variável | Descrição |
|---|---|
| `DB_HOST` | Host do banco MySQL |
| `DB_NAME` | Nome do banco de dados |
| `DB_USER` | Usuário do banco |
| `DB_PASSWORD` | Senha do banco |
| `QUEUE_URL` | URL da fila SQS FIFO |
| `SLACK_SIGNING_SECRET` | Secret para validar assinatura dos webhooks do Slack |
| `PATH_TOKEN_SHEETS_JSON` | Caminho para o arquivo de token OAuth2 do Google |
| `PATH_CREDENCIAL_SHEETS_JSON` | Caminho para o arquivo de credenciais OAuth2 do Google |
| `PATH_TOKEN_MASTER_LANE_SHEETS` | Caminho para a service account JSON do Google (usado no upload) |

Em produção, os valores são lidos do **AWS Secrets Manager** no path configurado em `env/prod.yaml`.

---

## Infraestrutura AWS

### SQS FIFO (`cloudformation/sqs.yaml`)

- **MainFifoQueue:** Fila principal com ordenação garantida e deduplicação por conteúdo
  - Retenção: 4 dias
  - Visibility timeout: 6 minutos (maior que o timeout do Worker de 5 minutos)
  - Dead Letter Queue após 3 tentativas falhas
- **WorkerDlqFifo:** Dead Letter Queue para mensagens com falha
  - Retenção: 14 dias para análise
- **SqsKmsKey:** Chave KMS com rotação anual para criptografia das mensagens

### Lambdas (`serverless.yaml`)

| | Router | Worker |
|---|---|---|
| Timeout | 60s | 300s |
| Memória | 512 MB | 1024 MB |
| Trigger | HTTP POST `/processar` | SQS (batchSize: 1) |
| Concorrência | paralela | 1 por vez (FIFO) |

---

## Deploy

O deploy é feito automaticamente via **GitHub Actions** ao fazer push para a branch `staging`.

```
push → staging branch
    ↓
GitHub Actions (deploy-dev.yml)
    ↓
Assume IAM Role (InfraDeployAccess)
    ↓
serverless deploy --stage dev
```

Para deploy manual:

```bash
# Instalar dependências do Serverless
npm install -g serverless
serverless plugin install -n serverless-deployment-bucket
serverless plugin install -n serverless-prune-plugin
serverless plugin install -n serverless-python-requirements
serverless plugin install -n serverless-iam-roles-per-function

# Deploy para dev
serverless deploy --stage dev

# Deploy para prod
serverless deploy --stage prod
```

---

## Execução Local

Para testar localmente sem o Slack:

```bash
# 1. Copiar e preencher as variáveis de ambiente
cp .env.local.exemple .env.local

# 2. Instalar dependências
pip install -r src/SlkRouter_import_lan/requirements.txt

# 3. Rodar o script local
python run_local.py
```

O arquivo `run_local.py` simula o evento que chegaria do Slack, passando o ID da planilha diretamente.
