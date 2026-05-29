import json
import boto3
import os
import time
import hashlib
import hmac
import base64
from urllib.parse import parse_qs

# --- Variáveis de Ambiente ---
SLACK_SIGNING_SECRET = os.environ['SLACK_SIGNING_SECRET']
QUEUE_URL            = os.environ['QUEUE_URL']

# --- Clientes AWS ---
sqs = boto3.client('sqs')


def verify_slack_request(headers, body):
    headers = {k.lower(): v for k, v in headers.items()}

    timestamp = headers.get('x-slack-request-timestamp', '0')
    signature = headers.get('x-slack-signature', '')

    if abs(time.time() - int(timestamp)) > 60 * 5:
        return False

    basestring          = f'v0:{timestamp}:{body}'.encode('utf-8')
    slack_signing_secret = SLACK_SIGNING_SECRET.encode('utf-8')

    my_signature = 'v0=' + hmac.new(
        slack_signing_secret,
        basestring,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(my_signature, signature)


def lambda_handler(event, context):
    print("🚀 Router iniciado")

    body_str = event.get('body', '')

    if event.get('isBase64Encoded', False):
        body_str = base64.b64decode(body_str).decode('utf-8')

    if not verify_slack_request(event.get('headers', {}), body_str):
        print("❌ Solicitação rejeitada: assinatura do Slack inválida ou expirada.")
        return {'statusCode': 401, 'body': 'Verificação falhou.'}

    print("✅ Solicitação verificada com sucesso")

    parsed_body   = {k: v[0] for k, v in parse_qs(body_str).items()}
    trigger_id    = parsed_body.get('trigger_id')
    slack_command = parsed_body.get('command', '').replace('/', '').strip().lower()
    response_url  = parsed_body.get('response_url')
    user_name     = parsed_body.get('user_name', '')

    print(f"🔎 Comando recebido: /{slack_command} | Usuário: {user_name}")

    if slack_command == 'import-lancamentos':
        id_planilha = parsed_body.get('text', '').strip()
        print(f"📋 ID da planilha informado: {id_planilha}")

        if not id_planilha:
            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({
                    'response_type': 'ephemeral',
                    'text': '⚠️ Informe o ID da planilha. Ex: `/import_lancamentos <id_planilha>`'
                })
            }

        sqs.send_message(
            QueueUrl=QUEUE_URL,
            MessageBody=json.dumps({
                'command_type': 'import-lancamentos',
                'response_url': response_url,
                'user_name':    user_name,
                'id_planilha':  id_planilha,
            }),
            MessageGroupId='ImportLancamentos',
            MessageDeduplicationId=trigger_id or str(time.time())
        )

        print("✅ Solicitação encaminhada para processamento")

        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({
                'response_type': 'ephemeral',
                'text': '⏳ Importação iniciada! Os lançamentos estão sendo processados...'
            })
        }

    # ── outros comandos ──
    sqs.send_message(
        QueueUrl=QUEUE_URL,
        MessageBody=json.dumps({
            'response_url': response_url,
            'user_name':    user_name,
            'command_text': parsed_body.get('text', '').strip(),
            'command_type': slack_command,
        }),
        MessageGroupId='ImportContratosSlack',
        MessageDeduplicationId=trigger_id or str(time.time())
    )

    return {
        'statusCode': 200,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps({
            'response_type': 'ephemeral',
            'text': '✅ Solicitação recebida! Processando... ⏳'
        })
    }
