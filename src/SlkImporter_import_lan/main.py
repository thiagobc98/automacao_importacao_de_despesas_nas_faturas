try:
    import unzip_requirements
except ImportError:
    pass

import json
import os
import boto3
import pymysql
import requests
import gspread
from datetime import datetime
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

# --- Variáveis de Ambiente ---
DB_HOST             = os.environ['DB_HOST']
DB_USER             = os.environ['DB_USER']
DB_PASSWORD         = os.environ['DB_PASSWORD']
DB_NAME             = os.environ['DB_NAME']
GOOGLE_SECRET_ARN   = os.environ['GOOGLE_SECRET_ARN']
WORKER_QUEUE_URL    = os.environ['WORKER_QUEUE_URL']
DRIVE_ROOT_FOLDER_ID = '1qe_PJlaCmXZuEz8yvQdxq8gZhBrJXv7W'

# --- Clientes AWS ---
sqs = boto3.client('sqs')
sm  = boto3.client('secretsmanager')

# --- Conexão com o banco ---
_conn = pymysql.connect(
    host=DB_HOST, user=DB_USER, password=DB_PASSWORD,
    database=DB_NAME, connect_timeout=30,    # ✅ Timeout aumentado
    read_timeout=30,       # ✅ Timeout de leitura
    write_timeout=30,      # ✅ Timeout de escrita
    charset='utf8mb4',     # ✅ Charset recomendado
    autocommit=True        # ✅ Auto-commit para operações simples
)
print("✅ Conexão com o banco de dados estabelecida.")


# ─── DB helpers ──────────────────────────────────────────────────────────────

def _query(sql, params=()):
    global _conn
    try:
        _conn.ping(reconnect=True)
    except Exception:
        _conn = pymysql.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASSWORD,
            database=DB_NAME, connect_timeout=5
        )
    with _conn.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def get_id_contrato(codigo_contrato):
    rows = _query(
        "SELECT id_contrato FROM contratos WHERE codigo_contrato = %s AND exc_contrato = 'F'",
        (codigo_contrato,)
    )
    return rows[0][0] if rows else None


def get_pessoas_no_banco():
    return {r[0] for r in _query("SELECT id_pessoa FROM pessoas_lancamentos;")}


def get_categorias_no_banco():
    return {r[0] for r in _query("SELECT id_release FROM release_categories WHERE exc_release = 'F';")}


def relatorio_lancamentos_na_fatura(id_contrato, id_release, mes, ano):
    """Relatório 3.1 — Lançamentos já existentes na fatura (serão atualizados pelo Worker)."""
    return list(_query(
        """
        SELECT c.codigo_contrato, c.id_contrato, f.id_fatura,
               f.vencimento_fatura, l.fk_id_release
        FROM contratos c
            JOIN faturas f ON f.fk_id_contrato = c.id_contrato
            JOIN lancamentos l ON l.fk_id_fatura = f.id_fatura
        WHERE c.id_contrato     = %s
            AND l.fk_id_release = %s
            AND MONTH(f.vencimento_fatura) = %s
            AND YEAR(f.vencimento_fatura)  = %s
            AND c.exc_contrato = 'F'
            AND f.exc_fatura   = 'F'
            AND l.exc_lan      = 'F'
        """,
        (id_contrato, id_release, mes, ano)
    ))


def relatorio_contrato_sem_fatura(id_contrato, mes, ano):
    """Relatório 3.2 — Contrato sem fatura gerada no mês."""
    rows = _query(
        """
        SELECT COUNT(*) FROM faturas f
        WHERE f.fk_id_contrato = %s
            AND MONTH(f.vencimento_fatura) = %s
            AND YEAR(f.vencimento_fatura)  = %s
            AND f.exc_fatura = 'F'
        """,
        (id_contrato, mes, ano)
    )
    return rows[0][0] == 0  # True = sem fatura


def relatorio_fatura_com_boleto(id_contrato, mes, ano):
    """Relatório 3.3 — Fatura do mês já possui boleto gerado (url_boleto_fatura IS NOT NULL)."""
    return list(_query(
        """
        SELECT c.codigo_contrato, c.id_contrato, f.id_fatura, f.vencimento_fatura
        FROM contratos c
            JOIN faturas f ON f.fk_id_contrato = c.id_contrato
        WHERE c.id_contrato             = %s
            AND f.exc_fatura            = 'F'
            AND c.exc_contrato          = 'F'
            AND f.url_boleto_fatura     IS NOT NULL
            AND MONTH(f.vencimento_fatura) = %s
            AND YEAR(f.vencimento_fatura)  = %s
        """,
        (id_contrato, mes, ano)
    ))


# ─── Validação de campos ──────────────────────────────────────────────────────

def valida_campos(row, pessoas, categorias):
    erros = []
    if row['id_credito'] not in pessoas:
        erros.append(f"Crédito (código {row['id_credito']}) não encontrado no sistema")
    if row['id_debito'] not in pessoas:
        erros.append(f"Débito (código {row['id_debito']}) não encontrado no sistema")
    if row['id_categoria'] not in categorias:
        erros.append(f"Categoria (código {row['id_categoria']}) não encontrada no sistema")
    forma    = row['id_aplicar_de_forma']
    parcelas = row['qtd_parcelas']
    if forma == 2 and parcelas <= 1:
        erros.append("Forma de aplicação parcelada (PP) requer mais de 1 parcela")
    if forma in (1, 3) and parcelas != 1:
        erros.append("Forma de aplicação selecionada requer exatamente 1 parcela")
    if len(str(row.get('descricao', '')).strip()) <= 1:
        erros.append("Descrição muito curta (deve ter mais de 1 caractere)")
    return erros


# ─── Slack ───────────────────────────────────────────────────────────────────

def notificar_slack(response_url, texto):
    try:
        requests.post(
            response_url,
            json={'response_type': 'ephemeral', 'text': texto},
            timeout=5
        )
    except Exception as e:
        print(f"⚠️ Falha ao notificar Slack: {e}")


# ─── Google Sheets ────────────────────────────────────────────────────────────

def _get_gc():
    secret  = sm.get_secret_value(SecretId=GOOGLE_SECRET_ARN)
    sa_info = json.loads(secret['SecretString'])
    creds   = Credentials.from_service_account_info(
        sa_info,
        scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
    )
    return gspread.authorize(creds)


def ler_planilha(id_planilha):
    """
    Lê a planilha retornando apenas as linhas com status_import = '0' (pendentes).

    Returns:
        col_status — número da coluna (1-based) de status_import, ou None se ausente
        rows       — lista de dicts; cada um inclui '_row_num' (linha 1-based na planilha)
    """
    gc         = _get_gc()
    ws         = gc.open_by_key(id_planilha).get_worksheet(0)
    all_values = ws.get_all_values()

    if len(all_values) < 5:
        return None, []

    headers    = all_values[0]
    data_rows  = all_values[4:]   # dados a partir da linha 5 (header=1, linhas 2-4 reservadas)
    col_status = (headers.index('status_import') + 1) if 'status_import' in headers else None

    rows = []
    for i, row in enumerate(data_rows):
        if not any(cell.strip() for cell in row):
            continue
        row_dict = dict(zip(headers, row))
        # ignora linhas já importadas (status_import = '1')
        if col_status is not None and str(row_dict.get('status_import', '0')).strip() != '0':
            continue
        row_dict['_row_num'] = i + 5   # posição real na planilha (1-based)
        rows.append(row_dict)

    return col_status, rows


def converter_para_float(valor):
    return float(str(valor).replace('.', '').replace(',', '.'))


# ─── Google Drive log ─────────────────────────────────────────────────────────

def _get_drive_service():
    secret  = sm.get_secret_value(SecretId=GOOGLE_SECRET_ARN)
    sa_info = json.loads(secret['SecretString'])
    creds   = Credentials.from_service_account_info(
        sa_info,
        scopes=['https://www.googleapis.com/auth/drive']
    )
    return build('drive', 'v3', credentials=creds)


_cache_pastas: dict = {}   # evita chamadas repetidas ao Drive na mesma execução


def _get_or_create_pasta_logs():
    """
    Garante a estrutura de pastas no Drive para o dia atual:

        DRIVE_ROOT_FOLDER_ID/
          logs_DD-MM-YYYY/       ← pasta do dia  (id_pasta_data)
            logs_sucesso/        ← arquivos de sucesso do Worker (id_pasta_sucesso)

    Retorna dict com as chaves 'data' e 'sucesso'.
    Usa cache de módulo para não criar pastas duplicadas dentro da mesma execução.
    """
    global _cache_pastas
    if _cache_pastas:
        return _cache_pastas

    service = _get_drive_service()
    hoje    = datetime.now().strftime('%d-%m-%Y')
    nome_pasta_data = f"logs_{hoje}"

    def _encontrar_ou_criar(nome, parent_id, mime='application/vnd.google-apps.folder'):
        res = service.files().list(
            q=(f"name='{nome}' and '{parent_id}' in parents "
               f"and mimeType='{mime}' and trashed=false"),
            fields='files(id)',
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        arquivos = res.get('files', [])
        if arquivos:
            return arquivos[0]['id']
        pasta = service.files().create(
            body={'name': nome, 'mimeType': mime, 'parents': [parent_id]},
            supportsAllDrives=True,
            fields='id'
        ).execute()
        print(f"📁 Pasta criada no Drive: '{nome}'")
        return pasta.get('id')

    id_pasta_data    = _encontrar_ou_criar(nome_pasta_data, DRIVE_ROOT_FOLDER_ID)
    id_pasta_sucesso = _encontrar_ou_criar('logs_sucesso',  id_pasta_data)

    _cache_pastas = {'data': id_pasta_data, 'sucesso': id_pasta_sucesso}
    return _cache_pastas


def _criar_arquivo_drive(nome_arquivo, conteudo, pasta='data'):
    """Cria arquivo .md no Drive. pasta='data' → pasta do dia; pasta='sucesso' → logs_sucesso/"""
    pastas   = _get_or_create_pasta_logs()
    folder   = pastas[pasta]
    service  = _get_drive_service()
    resultado = service.files().create(
        body={'name': nome_arquivo, 'parents': [folder]},
        media_body=MediaInMemoryUpload(conteudo.encode('utf-8'), mimetype='text/markdown'),
        supportsAllDrives=True,
        fields='id'
    ).execute()
    file_id = resultado.get('id', '')
    return f"https://drive.google.com/file/d/{file_id}/view"


def gravar_planilha_lancamentos_existentes(duplicados, mes, ano):
    """
    Gera uma planilha CSV com os lançamentos que já existiam na fatura
    e serão ATUALIZADOS pelo Worker. Sobe para o Drive e retorna o link.

    duplicados: lista de tuplas (codigo_contrato, id_contrato, id_fatura,
                                  vencimento_fatura, fk_id_release)
    """
    timestamp    = datetime.now().strftime('%Y%m%d_%H%M%S')
    nome_arquivo = f"lancamentos_existentes_{mes:02d}_{ano}_{timestamp}.csv"

    linhas = ['codigo_contrato,id_contrato,id_fatura,vencimento_fatura,id_release']
    for row in duplicados:
        codigo, id_contrato, id_fatura, vencimento, id_release = row
        venc_str = vencimento.strftime('%d/%m/%Y') if hasattr(vencimento, 'strftime') else str(vencimento)
        linhas.append(f"{codigo},{id_contrato},{id_fatura},{venc_str},{id_release}")

    conteudo_csv = '\n'.join(linhas)

    try:
        pastas    = _get_or_create_pasta_logs()
        service   = _get_drive_service()
        resultado = service.files().create(
            body={'name': nome_arquivo, 'parents': [pastas['data']]},
            media_body=MediaInMemoryUpload(
                conteudo_csv.encode('utf-8'),
                mimetype='text/csv'
            ),
            supportsAllDrives=True,
            fields='id'
        ).execute()
        file_id = resultado.get('id', '')
        link    = f"https://drive.google.com/file/d/{file_id}/view"
        print(f"📊 Planilha de lançamentos existentes salva no Drive: {nome_arquivo}")
        return link
    except Exception as ex:
        print(f"⚠️ Não foi possível salvar a planilha de lançamentos existentes: {ex}")
        return None


def gravar_planilha_boletos_emitidos(boletos, mes, ano):
    """
    Gera CSV com as faturas que já possuem boleto emitido no mês.
    Esses contratos são bloqueados da importação.

    boletos: lista de tuplas (codigo_contrato, id_contrato, id_fatura, vencimento_fatura)
    """
    timestamp    = datetime.now().strftime('%Y%m%d_%H%M%S')
    nome_arquivo = f"boletos_emitidos_{mes:02d}_{ano}_{timestamp}.csv"

    linhas = ['codigo_contrato,id_contrato,id_fatura,vencimento_fatura']
    for row in boletos:
        codigo, id_contrato, id_fatura, vencimento = row
        venc_str = vencimento.strftime('%d/%m/%Y') if hasattr(vencimento, 'strftime') else str(vencimento)
        linhas.append(f"{codigo},{id_contrato},{id_fatura},{venc_str}")

    conteudo_csv = '\n'.join(linhas)

    try:
        pastas    = _get_or_create_pasta_logs()
        service   = _get_drive_service()
        resultado = service.files().create(
            body={'name': nome_arquivo, 'parents': [pastas['data']]},
            media_body=MediaInMemoryUpload(
                conteudo_csv.encode('utf-8'),
                mimetype='text/csv'
            ),
            supportsAllDrives=True,
            fields='id'
        ).execute()
        file_id = resultado.get('id', '')
        link    = f"https://drive.google.com/file/d/{file_id}/view"
        print(f"🔒 Planilha de boletos emitidos salva no Drive: {nome_arquivo}")
        return link
    except Exception as ex:
        print(f"⚠️ Não foi possível salvar a planilha de boletos emitidos: {ex}")
        return None


_SECAO_ICONE = {
    'Contrato não encontrado':     '📄 Validação do Contrato',
    'Dados inválidos na planilha': '📝 Dados da Planilha',
    'Fatura não gerada no mês':    '🗓️ Fatura do Mês',
    'Fatura com boleto gerado':    '🔒 Boleto Já Emitido',
    'Lançamento duplicado':        '🔁 Lançamento Duplicado',
    'Erro inesperado':             '⚠️ Erro Inesperado',
    'Erro ao abrir planilha':      '📂 Acesso à Planilha',
}


def _como_arrumar(texto_erro):
    t = texto_erro.lower()
    if 'obrigatório' in t and 'vazio' in t:
        return "Preencha todos os campos obrigatórios na planilha. Verifique se há células em branco nas colunas indicadas."
    if 'formato de mes_ano' in t:
        return "Corrija o campo mes_ano_import para o formato MM/AAAA (ex: 06/2026)."
    if 'crédito' in t and 'não encontrado' in t:
        return "Verifique se o código de crédito informado existe no sistema."
    if 'débito' in t and 'não encontrado' in t:
        return "Verifique se o código de débito informado existe no sistema."
    if 'categoria' in t and 'não encontrada' in t:
        return "Verifique se o código da categoria informado existe no sistema."
    if 'parcelada' in t:
        return "Altere a quantidade de parcelas para um valor maior que 1 na planilha."
    if 'exatamente 1 parcela' in t:
        return "Altere a quantidade de parcelas para 1 na planilha."
    if 'descrição muito curta' in t:
        return "Preencha a descrição com pelo menos 2 caracteres."
    if 'nenhuma fatura' in t:
        return "Certifique-se de que a fatura do mês/ano informado já foi gerada no sistema antes de importar."
    if 'boleto já gerado' in t or 'boleto emitido' in t:
        return "A fatura deste mês já possui boleto emitido. Cancele o boleto."
    if 'já existem' in t:
        return "Este lançamento já existe nesta fatura. Remova a linha da planilha ou verifique se o mês/ano está correto."
    if 'inacessível' in t:
        return "Verifique se a planilha foi compartilhada corretamente e tente novamente."
    if 'não existe no sistema' in t or 'não encontrado' in t:
        return "Verifique se o código está correto na planilha."
    return "Verifique os dados na planilha e tente novamente."


def gravar_log_erros_geral(erros, user_name, id_planilha, total_contratos=0, total_sucessos=0, tempo_execucao=None):
    timestamp    = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
    nome_arquivo = f"log_ERROS_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"

    erros_por_contrato = {}
    for e in erros:
        erros_por_contrato.setdefault(e['codigo'], []).append(e)

    linhas = [
        "# 📋 RELATÓRIO DE CORREÇÕES DA PLANILHA\n",
        f"**Data:** {timestamp}  ",
        f"**Planilha lida:** `{id_planilha}`",
        "",
        "---",
        "",
    ]

    for codigo, erros_contrato in erros_por_contrato.items():
        linhas += [f"## 🚨 CONTRATO: `{codigo}`", ""]

        secoes = {}
        for e in erros_contrato:
            secoes.setdefault(e['relatorio'], []).extend(e['dados'].split('; '))

        for relatorio, detalhes in secoes.items():
            titulo = _SECAO_ICONE.get(relatorio, f"📄 {relatorio}")
            linhas += [f"### {titulo}", ""]
            for detalhe in detalhes:
                linhas += [
                    f"* ❌ **Erro:** {detalhe}",
                    f"* 👉 **Como arrumar:** {_como_arrumar(detalhe)}",
                    "",
                ]

        linhas += ["---", ""]

    total_rejeitados = len(erros_por_contrato)
    linhas += [
        "## 📊 RESUMO",
        "",
        "| Métrica | Qtd |",
        "|---|---|",
        f"| Total de contratos na planilha | {total_contratos} |",
        f"| Contratos INSERIDOS com sucesso | {total_sucessos} |",
        f"| Contratos REJEITADOS (com erro) | {total_rejeitados} |",
        "",
        "---",
    ]

    if tempo_execucao:
        linhas += ["", f"**⏱️ Tempo total de execução:** `{tempo_execucao}`"]

    try:
        link = _criar_arquivo_drive(nome_arquivo, "\n".join(linhas))
        print(f"📝 Arquivo de erros '{nome_arquivo}' salvo no Drive com sucesso.")
        return link
    except Exception as ex:
        print(f"⚠️ Não foi possível salvar o arquivo de erros no Drive: {ex}")
        return None


def gravar_log_sucessos_geral(sucessos, user_name, id_planilha):
    timestamp    = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    nome_arquivo = "log_aprovados_e_enviados_import.md"
    linhas = [
        "# Relatório de Sucesso — Importação de Lançamentos\n",
        "| Campo               | Valor |",
        "|---------------------|-------|",
        f"| Data e Hora         | {timestamp} |",
        f"| Usuário             | {user_name} |",
        f"| Planilha            | {id_planilha} |",
        f"| Total de contratos  | {len(sucessos)} |",
        "\n## Contratos Aprovados e Enviados para Inserção\n",
    ]
    for codigo in sucessos:
        linhas.append(f"- {codigo}")
    try:
        link = _criar_arquivo_drive(nome_arquivo, "\n".join(linhas))
        print(f"📝 Arquivo de sucesso '{nome_arquivo}' salvo no Drive com sucesso.")
        return link
    except Exception as ex:
        print(f"⚠️ Não foi possível salvar o arquivo de sucesso no Drive: {ex}")
        return None


# ─── Handler ─────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    print("🚀 Importer iniciado")

    for record in event['Records']:
        msg          = json.loads(record['body'])
        id_planilha  = msg.get('id_planilha', '')
        response_url = msg.get('response_url', '')
        user_name    = msg.get('user_name', '')

        print(f"📨 Nova solicitação recebida — Planilha: {id_planilha} | Usuário: {user_name}")
        print(f"📋 Abrindo planilha: {id_planilha}")
        notificar_slack(response_url, "🔄 Abrindo e lendo a planilha...")

        try:
            col_status, rows = ler_planilha(id_planilha)
        except Exception as e:
            print(f"❌ Não foi possível ler a planilha '{id_planilha}': {e}")
            notificar_slack(response_url, f"❌ Não foi possível acessar a planilha.\nVerifique se ela foi compartilhada corretamente e tente novamente.")
            gravar_log_erros_geral(
                erros=[{'codigo': 'N/A', 'relatorio': 'Erro ao abrir planilha', 'dados': f"Planilha '{id_planilha}' inacessível — {e}"}],
                user_name=user_name,
                id_planilha=id_planilha
            )
            return

        if not rows:
            print("⚠️ Planilha aberta, mas sem linhas pendentes (status_import = 0) a partir da linha 5.")
            notificar_slack(response_url, "⚠️ Nenhuma linha pendente encontrada na planilha.\nTodas já foram importadas ou a planilha está vazia.")
            return

        notificar_slack(response_url, f"📊 {len(rows)} lançamento(s) encontrado(s). Iniciando validações...")
        print(f"📊 {len(rows)} linha(s) pendente(s) encontrada(s) na planilha.")

        inicio          = datetime.now()
        pessoas         = get_pessoas_no_banco()
        categorias      = get_categorias_no_banco()
        erros           = []
        aprovados       = []   # payloads completos prontos para SQS
        duplicados      = []
        boletos_emitidos = []
        mes             = 0
        ano             = 0

        _CAMPOS_OBRIGATORIOS = [
            'id_categoria', 'id_aplicar_de_forma', 'qtd_parcelas',
            'id_debito', 'id_credito', 'valor', 'mes_ano_import', 'status_import',
        ]

        # ── 1ª passagem: validar tudo, coletar aprovados ──────────────────────
        for row_data in rows:
            codigo = row_data.get('codigo_contrato', '')
            try:
                vazios = [c for c in _CAMPOS_OBRIGATORIOS if not str(row_data.get(c, '')).strip()]
                if vazios:
                    erros.append({'codigo': codigo, 'relatorio': 'Dados inválidos na planilha',
                                  'dados': f"Campo(s) obrigatório(s) vazio(s): {', '.join(vazios)}"})
                    continue

                row = {
                    'codigo_contrato':     codigo,
                    'id_categoria':        int(row_data['id_categoria']),
                    'id_aplicar_de_forma': int(row_data['id_aplicar_de_forma']),
                    'qtd_parcelas':        int(row_data['qtd_parcelas']),
                    'id_debito':           int(row_data['id_debito']),
                    'id_credito':          int(row_data['id_credito']),
                    'descricao':           row_data.get('descricao', ''),
                    'valor':               converter_para_float(row_data['valor']),
                    'mes_ano_import':    row_data.get('mes_ano_import', ''),
                }

                id_contrato = get_id_contrato(codigo)
                if not id_contrato:
                    erros.append({'codigo': codigo, 'relatorio': 'Contrato não encontrado',
                                  'dados': f"O contrato '{codigo}' não existe no sistema."})
                    continue

                row['id_contrato'] = id_contrato

                mes_ano_str    = str(row['mes_ano_import'])
                partes_mes_ano = mes_ano_str.split('/')
                if len(partes_mes_ano) != 2 or not all(p.strip().isdigit() for p in partes_mes_ano):
                    erros.append({'codigo': codigo, 'relatorio': 'Dados inválidos na planilha',
                                  'dados': f"Formato de mes_ano_import inválido: '{mes_ano_str}'. Use MM/AAAA."})
                    continue
                mes = int(partes_mes_ano[0])
                ano = int(partes_mes_ano[1])

                erros_campos = valida_campos(row, pessoas, categorias)
                if erros_campos:
                    erros.append({'codigo': codigo, 'relatorio': 'Dados inválidos na planilha',
                                  'dados': '; '.join(erros_campos)})
                    continue

                if relatorio_contrato_sem_fatura(id_contrato, mes, ano):
                    erros.append({'codigo': codigo, 'relatorio': 'Fatura não gerada no mês',
                                  'dados': f"Nenhuma fatura encontrada para {mes:02d}/{ano}."})
                    continue

                faturas_com_boleto = relatorio_fatura_com_boleto(id_contrato, mes, ano)
                if faturas_com_boleto:
                    ids_faturas = ', '.join(str(r[2]) for r in faturas_com_boleto)
                    erros.append({'codigo': codigo, 'relatorio': 'Fatura com boleto gerado',
                                  'dados': f"Boleto já emitido para {len(faturas_com_boleto)} fatura(s) "
                                           f"em {mes:02d}/{ano} (id(s): {ids_faturas})."})
                    boletos_emitidos.extend((r[0], r[1], r[2], r[3]) for r in faturas_com_boleto)
                    continue

                lan_existente = relatorio_lancamentos_na_fatura(id_contrato, row['id_categoria'], mes, ano)
                if lan_existente:
                    duplicados.extend(lan_existente)
                    print(f"  ⚠️  Contrato {codigo}: {len(lan_existente)} lançamento(s) já existente(s) → serão atualizados.")

                aprovados.append({
                    'response_url':        response_url,
                    'user_name':           user_name,
                    'codigo_contrato':     codigo,
                    'id_contrato':         id_contrato,
                    'id_categoria':        row['id_categoria'],
                    'id_aplicar_de_forma': row['id_aplicar_de_forma'],
                    'qtd_parcelas':        row['qtd_parcelas'],
                    'id_debito':           row['id_debito'],
                    'id_credito':          row['id_credito'],
                    'descricao':           row['descricao'],
                    'valor':               row['valor'],
                    'mes_ano_import':    row['mes_ano_import'],
                    'id_planilha':         id_planilha,
                    'row_num':             row_data.get('_row_num'),
                    'col_status':          col_status,
                })
                print(f"  ✅ Contrato {codigo} aprovado.")

            except Exception as e:
                print(f"❌ Erro inesperado ao processar o contrato {codigo}: {e}")
                erros.append({'codigo': codigo, 'relatorio': 'Erro inesperado', 'dados': str(e)})

        # ── Grava logs no Drive ───────────────────────────────────────────────
        tempo = str(datetime.now() - inicio).split('.')[0]
        gravar_log_erros_geral(erros, user_name, id_planilha,
                               total_contratos=len(rows), total_sucessos=len(aprovados),
                               tempo_execucao=tempo) if erros else None
        gravar_log_sucessos_geral([a['codigo_contrato'] for a in aprovados],
                                  user_name, id_planilha) if aprovados else None
        gravar_planilha_lancamentos_existentes(duplicados, mes, ano) if duplicados else None
        gravar_planilha_boletos_emitidos(boletos_emitidos, mes, ano) if boletos_emitidos else None

        # ── Link da pasta do dia ──────────────────────────────────────────────
        try:
            pasta_id   = _get_or_create_pasta_logs()['data']
            link_pasta = f"https://drive.google.com/drive/folders/{pasta_id}"
        except Exception:
            link_pasta = ''

        # ── Monta dados do resumo para o Worker enviar no Slack ──────────────
        total_a_inserir = len(aprovados)
        total_erros     = len(erros)
        icone      = "✅" if not erros else ("⚠️" if aprovados else "❌")
        status_msg = ("Importação concluída com sucesso!" if not erros else
                     ("Importação concluída com erros em alguns contratos." if aprovados else
                      "Importação finalizada. Nenhum contrato foi aprovado."))

        print(f"✅ {total_a_inserir} aprovado(s) para inserção, {total_erros} com erro(s).")

        # ── Se nenhum aprovado, notifica agora e encerra ──────────────────────
        if not aprovados:
            msg = (f"{icone} *{status_msg}*\n"
                   f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                   f"❌ Com erro: *{total_erros}*\n")
            if link_pasta:
                msg += f"📁 Relatórios: <{link_pasta}|Abrir pasta de logs>"
            notificar_slack(response_url, msg)
            return

        # ── 2ª passagem: envia ao SQS com índice e dados do resumo ───────────
        for i, payload in enumerate(aprovados, start=1):
            payload['indice']          = i
            payload['total_a_inserir'] = total_a_inserir
            payload['total_erros']     = total_erros
            payload['link_pasta']      = link_pasta
            payload['icone']           = icone
            payload['status_msg']      = status_msg
            sqs.send_message(
                QueueUrl=WORKER_QUEUE_URL,
                MessageBody=json.dumps(payload),
                MessageGroupId='Worker',
                MessageDeduplicationId=f"{id_planilha}_{payload['codigo_contrato']}_{payload['id_categoria']}"
            )
        print(f"📤 {total_a_inserir} mensagem(ns) enviada(s) ao Worker.")
