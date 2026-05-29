"""
Runner local para testar o fluxo completo sem AWS:

    Router Lambda → (SQS interceptado) → Importer Lambda → (SQS interceptado) → Worker Lambda

Uso:
    python run_local.py <id_planilha_google_sheets>

Exemplo:
    python run_local.py 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms

Pre-requisitos:
    1. Criar .env.local (copie de .env.local.exemple e preencha)
    2. Ter acesso ao banco MySQL e ao Google Sheets/Drive
    3. pip install -r src/SlkImporter_import_lan/requirements.txt
"""

import hashlib
import hmac
import json
import os
import sys
import importlib.util
import time
from datetime import datetime
from unittest.mock import MagicMock
from urllib.parse import urlencode

LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
LOG_FILE = os.path.join(LOGS_DIR, 'logs_erro_planilha.txt')


def _salvar_log(spreadsheet_id: str, texto: str) -> None:
    os.makedirs(LOGS_DIR, exist_ok=True)
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(f"\n{'=' * 62}\n")
        f.write(f"[{timestamp}] Planilha: {spreadsheet_id}\n")
        f.write(f"{'=' * 62}\n")
        f.write(texto + '\n')


# ---------------------------------------------------------------------------
# 1. Carrega .env.local antes de qualquer import do projeto
# ---------------------------------------------------------------------------
def _load_env(path: str) -> None:
    if not os.path.exists(path):
        print(f"[ERRO] {path} nao encontrado.")
        print("       Copie .env.local.exemple para .env.local e preencha os valores.")
        sys.exit(1)

    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

    print("[OK] .env.local carregado")


_load_env('.env.local')

# Segredo local para gerar assinatura HMAC valida no _router_event
_LOCAL_SLACK_SECRET = 'local-secret-testing'
os.environ['SLACK_SIGNING_SECRET'] = _LOCAL_SLACK_SECRET

# Filas SQS sao mockadas localmente — apenas precisam existir como variavel
os.environ.setdefault('QUEUE_URL',          'local-importer-queue')
os.environ.setdefault('WORKER_QUEUE_URL',   'local-worker-queue')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')

ROOT         = os.path.dirname(os.path.abspath(__file__))
ROUTER_DIR   = os.path.join(ROOT, 'src', 'SlkRouter_import_lan')
IMPORTER_DIR = os.path.join(ROOT, 'src', 'SlkImporter_import_lan')
WORKER_DIR   = os.path.join(ROOT, 'src', 'SlkWorker_import_lan')


# ---------------------------------------------------------------------------
# 2. Helpers de evento
# ---------------------------------------------------------------------------
def _router_event(spreadsheet_id: str) -> dict:
    """Evento HTTP POST que o Slack envia ao API Gateway → Router."""
    body       = urlencode({'text': spreadsheet_id, 'command': '/import-lancamentos'})
    timestamp  = str(int(time.time()))
    basestring = f'v0:{timestamp}:{body}'.encode('utf-8')
    signature  = 'v0=' + hmac.new(
        _LOCAL_SLACK_SECRET.encode('utf-8'),
        basestring,
        hashlib.sha256
    ).hexdigest()
    return {
        'httpMethod': 'POST',
        'path': '/processar',
        'headers': {
            'content-type': 'application/x-www-form-urlencoded',
            'x-slack-request-timestamp': timestamp,
            'x-slack-signature': signature,
        },
        'body': body,
        'isBase64Encoded': False,
    }


def _sqs_event(message_body: dict, seq: int = 1) -> dict:
    """Evento SQS generico (batchSize: 1) para Importer ou Worker."""
    return {
        'Records': [
            {
                'messageId':     f'local-{seq:03d}',
                'receiptHandle': f'local-receipt-{seq}',
                'body':          json.dumps(message_body),
                'attributes': {
                    'MessageGroupId':          'local',
                    'SequenceNumber':          str(seq),
                    'ApproximateReceiveCount': '1',
                    'SentTimestamp':           '0',
                },
                'messageAttributes': {},
                'eventSource':    'aws:sqs',
                'eventSourceARN': 'arn:aws:sqs:us-east-2:000000000000:local.fifo',
                'awsRegion':      'us-east-2',
            }
        ]
    }


# ---------------------------------------------------------------------------
# 3. Gerenciador de modulos — evita conflito entre Lambdas
# ---------------------------------------------------------------------------
_MODULES_TO_CLEAN = {'main', 'utils', 'utils.utils', 'utils.tool_db_slack',
                     'utils.utils.tool_db_slack', 'utils.utils.sheets',
                     'utils.sheets', 'utils.valida_doc', 'utils.utils.valida_doc'}


def _clean_modules() -> None:
    for name in list(sys.modules.keys()):
        if name in _MODULES_TO_CLEAN or any(name.startswith(p + '.') for p in _MODULES_TO_CLEAN):
            del sys.modules[name]


def _make_mock_sm():
    """
    Cria mock do secretsmanager usando o JSON local de credenciais Google.
    O caminho é lido de PATH_TOKEN_MASTER_LANE_SHEETS no .env.local.
    """
    google_json_path = os.environ.get('PATH_TOKEN_MASTER_LANE_SHEETS', '')
    if not os.path.exists(google_json_path):
        print(f"[AVISO] Arquivo de credenciais Google não encontrado: {google_json_path}")
        return MagicMock()
    with open(google_json_path, encoding='utf-8') as f:
        secret_content = f.read()
    mock_sm = MagicMock()
    mock_sm.get_secret_value.return_value = {'SecretString': secret_content}
    print(f"[OK] Credenciais Google carregadas de: {google_json_path}")
    return mock_sm


def _load_lambda(lambda_dir: str, mock_boto3_sqs=None, mock_boto3_sm=None):
    """
    Carrega o main.py da Lambda pelo caminho absoluto.
    Intercepta boto3.client('sqs') e/ou boto3.client('secretsmanager') se os mocks forem passados.
    """
    _clean_modules()
    sys.path.insert(0, lambda_dir)

    import boto3
    original_client = boto3.client

    try:
        def _patched_client(service, **kwargs):
            if service == 'sqs' and mock_boto3_sqs is not None:
                return mock_boto3_sqs
            if service == 'secretsmanager' and mock_boto3_sm is not None:
                return mock_boto3_sm
            return original_client(service, **kwargs)

        boto3.client = _patched_client

        spec = importlib.util.spec_from_file_location(
            'lambda_main',
            os.path.join(lambda_dir, 'main.py'),
            submodule_search_locations=[lambda_dir],
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module

    finally:
        boto3.client = original_client
        sys.path.remove(lambda_dir)


# ---------------------------------------------------------------------------
# 4. Router
# ---------------------------------------------------------------------------
def run_router(spreadsheet_id: str) -> dict | None:
    print()
    print("=" * 62)
    print("  ROUTER LAMBDA — SlkRouter_import_lan")
    print("=" * 62)

    mensagens_capturadas: list[dict] = []

    mock_sqs = MagicMock()

    def _interceptar_send(**kwargs):
        body = json.loads(kwargs['MessageBody'])
        mensagens_capturadas.append(body)
        print(f"  [SQS] Mensagem capturada — planilha={body.get('id_planilha')} | comando={body.get('command_type')}")
        return {'MessageId': 'local-msg-1', 'SequenceNumber': '1'}

    mock_sqs.send_message.side_effect = _interceptar_send

    router = _load_lambda(ROUTER_DIR, mock_boto3_sqs=mock_sqs)
    event  = _router_event(spreadsheet_id)

    print(f"\n  Planilha: {spreadsheet_id}\n")
    result = router.lambda_handler(event, context={})

    status_code = result.get('statusCode')
    raw_body    = result.get('body', '{}')
    try:
        body_resp = json.loads(raw_body)
        texto     = body_resp.get('text', str(body_resp))
    except (json.JSONDecodeError, AttributeError):
        texto = raw_body

    print(f"\n  [SLACK RESPONSE — HTTP {status_code}]")
    for linha in texto.splitlines():
        print(f"  {linha}")

    _salvar_log(spreadsheet_id, texto)
    print(f"\n  [LOG] Resposta salva em {LOG_FILE}")

    if status_code != 200 or not mensagens_capturadas:
        print("\n  [STOP] Router nao encaminhou nenhuma mensagem — Importer nao sera executado.\n")
        return None

    print(f"\n  [OK] Mensagem encaminhada para o Importer.\n")
    return mensagens_capturadas[0]


# ---------------------------------------------------------------------------
# 5. Importer
# ---------------------------------------------------------------------------
def run_importer(mensagem_router: dict) -> list[dict]:
    print("=" * 62)
    print("  IMPORTER LAMBDA — SlkImporter_import_lan")
    print("=" * 62)

    mensagens_worker: list[dict] = []

    mock_sqs = MagicMock()

    def _interceptar_send(**kwargs):
        body = json.loads(kwargs['MessageBody'])
        mensagens_worker.append(body)
        print(f"  [SQS] Contrato aprovado para inserção: {body.get('codigo_contrato')}")
        return {'MessageId': f'local-msg-{len(mensagens_worker)}', 'SequenceNumber': str(len(mensagens_worker))}

    mock_sqs.send_message.side_effect = _interceptar_send

    importer = _load_lambda(IMPORTER_DIR, mock_boto3_sqs=mock_sqs, mock_boto3_sm=_make_mock_sm())
    event    = _sqs_event(mensagem_router)

    print(f"\n  Planilha: {mensagem_router.get('id_planilha')}\n")
    importer.lambda_handler(event, context={})

    print(f"\n  [OK] {len(mensagens_worker)} contrato(s) aprovado(s) e enviado(s) para o Worker.\n")
    return mensagens_worker


# ---------------------------------------------------------------------------
# 6. Worker
# ---------------------------------------------------------------------------
def run_worker(mensagens: list[dict]) -> None:
    print("=" * 62)
    print(f"  WORKER LAMBDA — SlkWorker_import_lan ({len(mensagens)} mensagem(ns))")
    print("=" * 62)

    worker = _load_lambda(WORKER_DIR, mock_boto3_sm=_make_mock_sm())

    for i, msg in enumerate(mensagens, start=1):
        print(f"\n  [{i}/{len(mensagens)}] contrato={msg.get('codigo_contrato')} | mes_ano={msg.get('mes_ano_import')}")
        event = _sqs_event(msg, seq=i)
        worker.lambda_handler(event, context={})

    print()


# ---------------------------------------------------------------------------
# 7. Ponto de entrada
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Uso:     python run_local.py <id_planilha_google_sheets>")
        print("Exemplo: python run_local.py 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms")
        sys.exit(1)

    spreadsheet_id = sys.argv[1].strip()

    mensagem_router = run_router(spreadsheet_id)

    if mensagem_router:
        mensagens_worker = run_importer(mensagem_router)
        if mensagens_worker:
            run_worker(mensagens_worker)

    print("[FIM] Execucao local concluida.")
