try:
    import unzip_requirements
except ImportError:
    pass

import json
import os
import boto3
import pymysql
import requests
from datetime import datetime
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload
from google.oauth2.service_account import Credentials

# --- Variáveis de Ambiente ---
DB_HOST             = os.environ['DB_HOST']
DB_USER             = os.environ['DB_USER']
DB_PASSWORD         = os.environ['DB_PASSWORD']
DB_NAME             = os.environ['DB_NAME']
GOOGLE_SECRET_ARN   = os.environ['GOOGLE_SECRET_ARN']
DRIVE_ROOT_FOLDER_ID = # Id do drive do google
TODOS_EM_ABERTO = 3

# --- Clientes AWS ---
sm = boto3.client('secretsmanager')

# --- Conexão com o banco ---
_conn = pymysql.connect(
    host=DB_HOST, user=DB_USER, password=DB_PASSWORD,
    database=DB_NAME, connect_timeout=5
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


def _execute(sql, params=()):
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
        _conn.commit()
        return cursor.lastrowid


def get_faturas_em_aberto_by_id(id_contrato, mes, ano):
    data_inicio = f'{ano}-{mes:02d}-01'
    rows = _query(
        """
        SELECT f.id_fatura, f.vencimento_fatura
        FROM faturas f
        WHERE f.fk_id_contrato      = %s
            AND f.exc_fatura        = 'F'
            AND f.status_fatura     = 'PE'
            AND f.url_boleto_fatura IS NULL
            AND f.pagamento_fatura  IS NULL
            AND f.vencimento_fatura >= %s
        ORDER BY f.vencimento_fatura ASC
        """,
        (id_contrato, data_inicio)
    )
    # retorna lista de (id_fatura, vencimento_fatura)
    return [(r[0], r[1]) for r in rows]


def insert_grupo_parcelamento(descricao, fk_id_contrato, part_lan, valor_lan,
                              credito_lan, debito_lan, fk_id_release, tipo_grupo):
    return _execute(
        """
        INSERT INTO tb_lancamento_grupo_parcelamento
            (descricao, fk_id_contrato, part_lan, valor_lan,
             credito_lan, debito_lan, fk_id_release, tipo_grupo)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (descricao, fk_id_contrato, part_lan, valor_lan,
         credito_lan, debito_lan, fk_id_release, tipo_grupo)
    )


def insert_or_update_lancamento(desc_lan, forma_lan, parcelas_lan, valor_lan, credito_lan,
                                debito_lan, fk_id_release, fk_id_fatura, fk_id_grupo_parcelamento=None):
    """Insere o lançamento. Se já existir (mesmo fk_id_release + fk_id_fatura), atualiza."""
    existente = _query(
        """
        SELECT id_lan FROM lancamentos
        WHERE fk_id_release = %s AND fk_id_fatura = %s AND exc_lan = 'F'
        """,
        (fk_id_release, fk_id_fatura)
    )

    if existente:
        id_lan = existente[0][0]
        _execute(
            """
            UPDATE lancamentos
            SET desc_lan = %s, forma_lan = %s, part_lan = %s, valor_lan = %s,
                credito_lan = %s, debito_lan = %s, fk_id_grupo_parcelamento = %s
            WHERE id_lan = %s
            """,
            (desc_lan, forma_lan, parcelas_lan, valor_lan,
             credito_lan, debito_lan, fk_id_grupo_parcelamento, id_lan)
        )
        print(f"  ♻️  Lançamento #{id_lan} ATUALIZADO na fatura #{fk_id_fatura}")
        return id_lan
    else:
        id_lan = _execute(
            """
            INSERT INTO lancamentos
                (desc_lan, forma_lan, parcelas_lan, valor_lan, credito_lan,
                 debito_lan, fk_id_release, fk_id_fatura, exc_lan, part_lan, fk_id_grupo_parcelamento)
            VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (desc_lan, forma_lan, valor_lan, credito_lan, debito_lan,
             fk_id_release, fk_id_fatura, 'F', parcelas_lan, fk_id_grupo_parcelamento)
        )
        print(f"  ✅ Lançamento #{id_lan} INSERIDO na fatura #{fk_id_fatura}")
        return id_lan


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


# ─── Google Sheets ───────────────────────────────────────────────────────────

def _get_sheets_service():
    secret  = sm.get_secret_value(SecretId=GOOGLE_SECRET_ARN)
    sa_info = json.loads(secret['SecretString'])
    creds   = Credentials.from_service_account_info(
        sa_info,
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    return build('sheets', 'v4', credentials=creds)


def _col_para_letra(col):
    """Converte número de coluna (1-based) para notação A1 (ex: 1→A, 27→AA)."""
    letra = ''
    while col > 0:
        col, resto = divmod(col - 1, 26)
        letra = chr(65 + resto) + letra
    return letra


def marcar_importado(id_planilha, row_num, col_status):
    """Atualiza status_import → 1 na planilha após inserção bem-sucedida no banco."""
    if not id_planilha or not row_num or not col_status:
        return
    try:
        celula  = f"{_col_para_letra(col_status)}{row_num}"
        service = _get_sheets_service()
        service.spreadsheets().values().update(
            spreadsheetId=id_planilha,
            range=celula,
            valueInputOption='RAW',
            body={'values': [[1]]}
        ).execute()
        print(f"  📝 status_import → 1 (célula {celula})")
    except Exception as e:
        print(f"  ⚠️ Não foi possível atualizar status_import na linha {row_num}: {e}")


# ─── Google Drive log ─────────────────────────────────────────────────────────

def _get_drive_service():
    secret  = sm.get_secret_value(SecretId=GOOGLE_SECRET_ARN)
    sa_info = json.loads(secret['SecretString'])
    creds   = Credentials.from_service_account_info(
        sa_info,
        scopes=['https://www.googleapis.com/auth/drive']
    )
    return build('drive', 'v3', credentials=creds)


_cache_pastas: dict = {}


def _get_or_create_pasta_logs():
    """
    Garante a estrutura de pastas no Drive para o dia atual:

        DRIVE_ROOT_FOLDER_ID/
          logs_DD-MM-YYYY/       ← pasta do dia  (id_pasta_data)
            logs_sucesso/        ← arquivos de sucesso do Worker (id_pasta_sucesso)

    Retorna dict com as chaves 'data' e 'sucesso'.
    """
    global _cache_pastas
    if _cache_pastas:
        return _cache_pastas

    service = _get_drive_service()
    hoje    = datetime.now().strftime('%d-%m-%Y')
    nome_pasta_data = f"logs_{hoje}"

    def _encontrar_ou_criar(nome, parent_id):
        res = service.files().list(
            q=(f"name='{nome}' and '{parent_id}' in parents "
               f"and mimeType='application/vnd.google-apps.folder' and trashed=false"),
            fields='files(id)',
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        arquivos = res.get('files', [])
        if arquivos:
            return arquivos[0]['id']
        pasta = service.files().create(
            body={'name': nome, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]},
            supportsAllDrives=True,
            fields='id'
        ).execute()
        print(f"📁 Pasta criada no Drive: '{nome}'")
        return pasta.get('id')

    id_pasta_data    = _encontrar_ou_criar(nome_pasta_data, DRIVE_ROOT_FOLDER_ID)
    id_pasta_sucesso = _encontrar_ou_criar('logs_sucesso',  id_pasta_data)

    _cache_pastas = {'data': id_pasta_data, 'sucesso': id_pasta_sucesso}
    return _cache_pastas


_FORMA_LABEL = {
    1: 'Simples (lançamento único)',
    2: 'Parcelado',
    3: 'Recorrente (todas as faturas em aberto)',
}


def _fmt_valor(valor):
    """Formata número como moeda brasileira: R$ 1.234,56"""
    return f"R$ {float(valor):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')


def _fmt_mes(mes, ano):
    meses = ['Janeiro','Fevereiro','Março','Abril','Maio','Junho',
             'Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']
    return f"{meses[mes - 1]} / {ano}"


def gravar_log_sucesso_geral(inseridos, user_name):
    agora        = datetime.now()
    timestamp    = agora.strftime('%d/%m/%Y às %H:%M:%S')
    codigos_str  = '_'.join(i['codigo'] for i in inseridos)
    nome_arquivo = f"log_SUCESSO_{codigos_str}_{agora.strftime('%Y%m%d_%H%M%S')}.md"

    total_lancamentos = sum(len(i['faturas']) for i in inseridos)

    linhas = [
        "# ✅ Relatório de Lançamentos Inseridos\n",
        f"> Gerado automaticamente em **{timestamp}**",
        f"> Responsável: **{user_name or 'não informado'}**",
        "",
        "---",
        "",
    ]

    for item in inseridos:
        row  = item.get('row', {})
        mes  = item.get('mes', 0)
        ano  = item.get('ano', 0)
        forma_id = row.get('id_aplicar_de_forma', 0)
        forma    = _FORMA_LABEL.get(forma_id, str(forma_id))
        parcelas = row.get('qtd_parcelas', 1)

        linhas += [
            f"## 📄 Contrato: `{item['codigo']}`",
            "",
            "### Dados do lançamento",
            "",
            "| Campo               | Detalhe |",
            "|---------------------|---------|",
            f"| Descrição           | {row.get('descricao', '—')} |",
            f"| Valor               | **{_fmt_valor(row.get('valor', 0))}** |",
            f"| Mês de referência   | {_fmt_mes(mes, ano)} |",
            f"| Forma de aplicação  | {forma} |",
            f"| Parcelas            | {parcelas} |",
            f"| Código de crédito   | {row.get('id_credito', '—')} |",
            f"| Código de débito    | {row.get('id_debito', '—')} |",
            f"| Categoria (release) | {row.get('id_categoria', '—')} |",
            "",
            f"### Faturas afetadas ({len(item['faturas'])} no total)",
            "",
        ]

        for fatura_id, vencimento in item['faturas']:
            venc_str = (vencimento.strftime('%d/%m/%Y')
                        if hasattr(vencimento, 'strftime') else str(vencimento))
            linhas.append(f"- Fatura **#{fatura_id}** — vencimento: {venc_str}")

        linhas += ["", "---", ""]

    linhas += [
        "## 📊 Resumo",
        "",
        "| Métrica                          | Quantidade |",
        "|----------------------------------|------------|",
        f"| Contratos processados            | {len(inseridos)} |",
        f"| Total de lançamentos realizados  | {total_lancamentos} |",
        "",
        "> *Este documento é gerado automaticamente pelo sistema de importação de lançamentos.*",
    ]

    try:
        pastas    = _get_or_create_pasta_logs()
        service   = _get_drive_service()
        resultado = service.files().create(
            body={'name': nome_arquivo, 'parents': [pastas['sucesso']]},
            media_body=MediaInMemoryUpload("\n".join(linhas).encode('utf-8'), mimetype='text/markdown'),
            supportsAllDrives=True,
            fields='id'
        ).execute()
        file_id = resultado.get('id', '')
        link    = f"https://drive.google.com/file/d/{file_id}/view"
        print(f"📝 Log de sucesso '{nome_arquivo}' salvo no Drive.")
        return link
    except Exception as e:
        print(f"⚠️ Não foi possível salvar o log de sucesso no Drive: {e}")
        return None


# ─── Insert de uma linha ──────────────────────────────────────────────────────

def inserir_linha(row, mes, ano):
    id_contrato   = row['id_contrato']
    fk_id_faturas = get_faturas_em_aberto_by_id(id_contrato, mes, ano)

    if row['id_aplicar_de_forma'] != TODOS_EM_ABERTO:
        fk_id_faturas = fk_id_faturas[:row['qtd_parcelas']]

    fk_id_grupo = None

    if row['id_aplicar_de_forma'] == 2:  # PP - parcelamento
        desc_grupo = (
            f"I:{id_contrato}|P:{row['qtd_parcelas']}|V:{row['valor']}"
            f"|C:{row['id_credito']}|D:{row['id_debito']}|R:{row['id_categoria']}|PP"
        )
        fk_id_grupo = insert_grupo_parcelamento(
            descricao=desc_grupo,          fk_id_contrato=id_contrato,
            part_lan=row['qtd_parcelas'],  valor_lan=row['valor'],
            credito_lan=row['id_credito'], debito_lan=row['id_debito'],
            fk_id_release=row['id_categoria'], tipo_grupo='PP'
        )
        print(f"  Grupo de parcelamento criado (id: {fk_id_grupo})")

    elif row['id_aplicar_de_forma'] == 3:  # RR - recorrente
        desc_grupo = (
            f"I:{id_contrato}|V:{row['valor']}"
            f"|C:{row['id_credito']}|D:{row['id_debito']}|R:{row['id_categoria']}|RR"
        )
        fk_id_grupo = insert_grupo_parcelamento(
            descricao=desc_grupo,          fk_id_contrato=id_contrato,
            part_lan=row['qtd_parcelas'],  valor_lan=row['valor'],
            credito_lan=row['id_credito'], debito_lan=row['id_debito'],
            fk_id_release=row['id_categoria'], tipo_grupo='RR'
        )
        print(f"  Grupo recorrente criado (id: {fk_id_grupo})")

    for fk_id_fatura, vencimento in fk_id_faturas:
        lan_id = insert_or_update_lancamento(
            desc_lan=row['descricao'],
            forma_lan=row['id_aplicar_de_forma'],
            parcelas_lan=row['qtd_parcelas'],
            valor_lan=row['valor'],
            credito_lan=row['id_credito'],
            debito_lan=row['id_debito'],
            fk_id_release=row['id_categoria'],
            fk_id_fatura=fk_id_fatura,
            fk_id_grupo_parcelamento=fk_id_grupo
        )
        print(f"  Lançamento #{lan_id} inserido na fatura #{fk_id_fatura}")

    # retorna lista de (id_fatura, vencimento_fatura)
    return fk_id_faturas


# ─── Handler ─────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    print("🚀 Worker iniciado")

    inseridos    = []
    user_name    = ''
    response_url = ''
    ultimo_msg   = None   # mensagem com indice == total_a_inserir

    for record in event['Records']:
        msg          = json.loads(record['body'])
        codigo       = msg.get('codigo_contrato', '')
        user_name    = msg.get('user_name', '')
        response_url = msg.get('response_url', '')

        print(f"📥 Inserindo lançamentos do contrato: {codigo}")

        mes_ano_str = str(msg.get('mes_ano_import', ''))
        mes = int(mes_ano_str.split('/')[0])
        ano = int(mes_ano_str.split('/')[1])

        row = {
            'id_contrato':         msg['id_contrato'],
            'codigo_contrato':     codigo,
            'id_categoria':        msg['id_categoria'],
            'id_aplicar_de_forma': msg['id_aplicar_de_forma'],
            'qtd_parcelas':        msg['qtd_parcelas'],
            'id_debito':           msg['id_debito'],
            'id_credito':          msg['id_credito'],
            'descricao':           msg['descricao'],
            'valor':               msg['valor'],
            'mes_ano_import':    msg['mes_ano_import'],
        }

        faturas = inserir_linha(row, mes, ano)
        print(f"✅ {len(faturas)} lançamento(s) inserido(s) para {codigo}")
        inseridos.append({'codigo': codigo, 'faturas': faturas, 'row': row, 'mes': mes, 'ano': ano})

        marcar_importado(
            id_planilha=msg.get('id_planilha'),
            row_num=msg.get('row_num'),
            col_status=msg.get('col_status'),
        )

        if msg.get('indice') == msg.get('total_a_inserir'):
            ultimo_msg = msg

    if inseridos:
        gravar_log_sucesso_geral(inseridos, user_name)
        total_lan = sum(len(i['faturas']) for i in inseridos)   # faturas é lista de (id, vencimento)
        print(f"✅ Worker concluído: {len(inseridos)} contrato(s), {total_lan} lançamento(s) inserido(s)/atualizado(s).")

    if ultimo_msg:
        total_a_inserir = ultimo_msg.get('total_a_inserir', len(inseridos))
        total_erros     = ultimo_msg.get('total_erros', 0)
        link_pasta      = ultimo_msg.get('link_pasta', '')
        icone           = ultimo_msg.get('icone', '✅')
        status_msg      = ultimo_msg.get('status_msg', 'Importação e inserção concluídas!')

        msg_slack = (
            f"{icone} *{status_msg}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📥 Enviados para inserção: *{total_a_inserir}*\n"
            f"❌ Com erro:               *{total_erros}*\n"
            f"✅ Importados com sucesso: *{total_a_inserir}*\n"
        )
        if link_pasta:
            msg_slack += f"📁 Relatórios: <{link_pasta}|Abrir pasta de logs>\n"
        msg_slack += "✅ *Inserção concluída com sucesso!*"

        notificar_slack(response_url, msg_slack)
