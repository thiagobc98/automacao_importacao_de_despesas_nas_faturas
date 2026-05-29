import pandas as pd
import utils.tool_db_slack as tool_db
from time import sleep
from datetime import datetime
import sys

PESSOAS_NO_BANCO = tool_db.get_pessoas_no_banco()
CATEGORIAS_NO_BANCO = tool_db.get_categorias_no_banco()
CATEGORIA_IPTU = 2
TODOS_EM_ABERTO = 3
g_historico_log = ''
g_contador_log = 0

def converter_para_float(valor):
    valor = str(valor).replace('.', '').replace(',', '.')
    return float(valor)

def valida_dados(row):
    # print(f"Validando contrato: {row['codigo_contrato']}")
    # dict = {"Contrato": row['codigo_contrato']}
    print(f"Validando contrato: {row['id_contrato']}")
    dict = {"Contrato": row['id_contrato']}

    dict['credito'] = True if row['id_credito'] in PESSOAS_NO_BANCO else False
    dict['debito'] = True if row['id_debito'] in PESSOAS_NO_BANCO else False

    dict['aplicar_de_forma'] = False
    dict['qtd_parcelas'] = False
    if row['id_aplicar_de_forma'] == 2 and row['qtd_parcelas'] > 1:
        dict['aplicar_de_forma'] = True
        dict['qtd_parcelas'] = True
    if row['id_aplicar_de_forma'] in (1,3) and row['qtd_parcelas'] == 1:
        dict['aplicar_de_forma'] = True
        dict['qtd_parcelas'] = True

    dict['descricao'] = True if len(row['descricao']) > 1 else False
    dict['categoria'] = True if row['id_categoria'] in CATEGORIAS_NO_BANCO else False
    dict['id_contrato'] = True # if tool_db.existe_contrato_no_banco(row['id_contrato']) else False

    return dict

def insert(row):
    codigos_especiais = [
        "CB2307942P/A",
        "SP2406716NGO",
        "L-BH2407767NTDO",
        "L-RJ2407830NGOD",
        "L-VP2407848NTOD",
        "L-SP2407831AD",
        "M-TS24080115",
        "M-LEH2412033",
        "M-LEH2412058",
        "M-LEH2412086",
        "M-LEH2412107",
        "M-VZ2407112",
        "M-MON2203002C",
        "M-MON1906005C",
        "M-MON2406013CP",
        "M-MON2311050CP",
        "M-MON2407051C",
        "M-MON2406055C",
        "M-MON2409090C",
        "M-MAV2107019L",
        "NM-VZ2407072"
    ]

    # if row['codigo_contrato'] in codigos_especiais:
    #     print(f"Contrato não inserido, aguardando financeiro: {row['codigo_contrato']}")
    #     generete_log(log_file,f"\n----------------\n {row['codigo_contrato']} - Não atualizado")
    # else:
    #     print(f"Inserindo/Autalizando contrato: {row['codigo_contrato']}")
    #     generete_log(log_file,f"\n----------------\n {row['codigo_contrato']}")

    print(f"Inserindo/Atualizando contrato: {row['id_contrato']}")
    generete_log(log_file, f"\n----------------\n {row['id_contrato']}")

    # fk_id_faturas = tool_db.get_faturas_em_aberto(row['codigo_contrato'])
    fk_id_faturas = tool_db.get_faturas_em_aberto_by_id(row['id_contrato'])

    if row['id_aplicar_de_forma'] != TODOS_EM_ABERTO:
        fk_id_faturas = fk_id_faturas[:row['qtd_parcelas']]
    
    fk_id_grupo_parcelamento = None


    if row['id_aplicar_de_forma'] == 2:
        # Parcelamento (PP): monta descricao e insere em tb_lancamento_grupo_parcelamento
        desc_grupo = f"I:{row['id_contrato']}|P:{row['qtd_parcelas']}|V:{row['valor']}|C:{row['id_credito']}|D:{row['id_debito']}|R:{row['id_categoria']}|PP"
        fk_id_grupo_parcelamento = tool_db.insert_grupo_parcelamento(
            descricao=desc_grupo,
            fk_id_contrato=row['id_contrato'],
            part_lan=row['qtd_parcelas'],
            valor_lan=row['valor'],
            credito_lan=row['id_credito'],
            debito_lan=row['id_debito'],
            fk_id_release=row['id_categoria'],
            tipo_grupo='PP'
        )
        generete_log(log_file, f"Grupo Parcelamento (PP) criado: {fk_id_grupo_parcelamento}")
    elif row['id_aplicar_de_forma'] == 3:
        # Recorrente (RR): monta descricao e insere em tb_lancamento_grupo_parcelamento
        desc_grupo = f"I:{row['id_contrato']}|V:{row['valor']}|C:{row['id_credito']}|D:{row['id_debito']}|R:{row['id_categoria']}|RR"
        fk_id_grupo_parcelamento = tool_db.insert_grupo_parcelamento(
            descricao=desc_grupo,
            fk_id_contrato=row['id_contrato'],
            part_lan=row['qtd_parcelas'],
            valor_lan=row['valor'],
            credito_lan=row['id_credito'],
            debito_lan=row['id_debito'],
            fk_id_release=row['id_categoria'],
            tipo_grupo='RR'
        )
        generete_log(log_file, f"Grupo Recorrente (RR) criado: {fk_id_grupo_parcelamento}")

    # if row['id_categoria'] == CATEGORIA_IPTU:

    # for fk_id_fatura in fk_id_faturas:
    #     id = tool_db.insert_or_update_lancamento(desc_lan=row['descricao'],
    #                                 forma_lan=row['id_aplicar_de_forma'],
    #                                 parcelas_lan=row['qtd_parcelas'],
    #                                 valor_lan=row['valor'],
    #                                 credito_lan=row['id_credito'],
    #                                 debito_lan=row['id_debito'],
    #                                 fk_id_release=row['id_categoria'],
    #                                 fk_id_fatura=fk_id_fatura)
    #     generete_log(log_file, f"SUCESSO - Fatura: {fk_id_fatura} | fk_id_release: {row['id_categoria']} | lançamento: {id}")

    # else:

    # for fk_id_fatura in fk_id_faturas:
    #     id = tool_db.insert_lancamento(desc_lan=row['descricao'],
    #                                 forma_lan=row['id_aplicar_de_forma'],
    #                                 parcelas_lan=row['qtd_parcelas'],
    #                                 valor_lan=row['valor'],
    #                                 credito_lan=row['id_credito'],
    #                                 debito_lan=row['id_debito'],
    #                                 fk_id_release=row['id_categoria'],
    #                                 fk_id_fatura=fk_id_fatura)
    #     generete_log(log_file, f"Atualizado com sucesso Fatura: {fk_id_fatura} lançamento: {id}")

    for fk_id_fatura in fk_id_faturas:
        id = tool_db.insert_lancamento(
            desc_lan=row['descricao'],
            forma_lan=row['id_aplicar_de_forma'],
            parcelas_lan=row['qtd_parcelas'],
            valor_lan=row['valor'],
            credito_lan=row['id_credito'],
            debito_lan=row['id_debito'],
            fk_id_release=row['id_categoria'],
            fk_id_fatura=fk_id_fatura,
            fk_id_grupo_parcelamento=fk_id_grupo_parcelamento
        )
        generete_log(log_file, f"Atualizado com sucesso Fatura: {fk_id_fatura} lançamento: {id}")

    generete_log(log_file, f'Faturas: {fk_id_faturas}')
    # sleep(35)

def validar_pre_insert(df, mes, ano, log_file):
    id_contratos = df['id_contrato'].tolist()
    tem_erros = False
    log_relatorios = fr"logs/log_erros_relatorios_{datetime.now().strftime('%d%m%Y')}.txt"
    linhas_relatorio = []

    sem_faturas = tool_db.get_contratos_sem_faturas_mes(id_contratos, mes, ano)
    if sem_faturas:
        generete_log(log_file, f"\n===== CONTRATOS SEM FATURAS EM {mes:02d}/{ano} =====")
        linhas_relatorio.append(f"===== CONTRATOS SEM FATURAS EM {mes:02d}/{ano} =====")
        for id_c, cod_c in sem_faturas:
            generete_log(log_file, f"  id_contrato={id_c} | codigo={cod_c}")
            linhas_relatorio.append(f"  id_contrato={id_c} | codigo={cod_c}")
        tem_erros = True

    id_releases = df['id_categoria'].unique().tolist()
    lancamentos_existentes = tool_db.get_lancamentos_existentes_mes(id_contratos, id_releases, mes, ano)
    pares_df = {(row['id_contrato'], row['id_categoria']) for _, row in df.iterrows()}
    conflitos = [r for r in lancamentos_existentes if (r[1], r[3]) in pares_df]

    if conflitos:
        generete_log(log_file, f"\n===== LANÇAMENTOS JÁ EXISTENTES EM {mes:02d}/{ano} =====")
        linhas_relatorio.append(f"\n===== LANÇAMENTOS JÁ EXISTENTES EM {mes:02d}/{ano} =====")
        for cod_c, id_c, id_f, id_rel in conflitos:
            generete_log(log_file, f"  codigo={cod_c} | id_contrato={id_c} | id_fatura={id_f} | id_release={id_rel}")
            linhas_relatorio.append(f"  codigo={cod_c} | id_contrato={id_c} | id_fatura={id_f} | id_release={id_rel}")
        tem_erros = True

    if linhas_relatorio:
        with open(log_relatorios, 'w', encoding='utf-8') as f:
            f.write('\n'.join(linhas_relatorio))

    return tem_erros


def generete_log(log_file, text):
    global g_historico_log
    global g_contador_log

    g_historico_log += '\n ' + text
    g_contador_log += 1

    if g_contador_log == 20:
        g_contador_log = 0
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write('\n '+ g_historico_log)
        g_historico_log = ''
    #generete_log(log_file,text)
    #with open(log_file, 'a', encoding='utf-8') as f:
    #    f.write('\n'+text)

if __name__ == '__main__':
    # path_dados = fr"/app/data/lançamentos.csv"
    path_dados = fr"data/lançamentos.csv"
    global log_file
    # log_file = fr"/app/logs/log_importacao_lancamento_{datetime.now().strftime('%d%m%Y')}.txt"
    log_file = fr"logs/log_importacao_lancamento_{datetime.now().strftime('%d%m%Y')}.txt"

    print(f"Iniciando importação dados...")
    with open(log_file, 'w', encoding='utf-8') as f:
        f.write(f"Iniciando importação dados...\n")

    df = (pd.read_csv(path_dados)
            .iloc[3:]
            .dropna(subset=['codigo_contrato'])
            .map(lambda x: x.strip() if isinstance(x, str) else x)
            .astype({
                'id_categoria': int,
                'id_aplicar_de_forma': int,
                'qtd_parcelas': int,
                'id_debito': int,
                'id_credito': int,
        })
    )

    mapa_ids = tool_db.get_id_contratos_por_codigos(df['codigo_contrato'].tolist())
    df['id_contrato'] = df['codigo_contrato'].map(mapa_ids)

    codigos_nao_encontrados = df[df['id_contrato'].isna()]['codigo_contrato'].tolist()
    if codigos_nao_encontrados:
        generete_log(log_file, f"\nContratos não encontrados no banco: {codigos_nao_encontrados}")

    df = df.dropna(subset=['id_contrato'])
    df['id_contrato'] = df['id_contrato'].astype(int)

    generete_log(log_file, f'\n\n ----- INICIANDO A VALIDAÇÃO DE DADOS ----- \n\n')

    df['valor'] = df['valor'].apply(converter_para_float)

    logs = df.apply(valida_dados, axis=1)

    erros = False
    for log in logs:
        generete_log(log_file,f'\n----------------\n{log["Contrato"]}')
        count = 0

        if not log['id_contrato']:
            #generete_log(log_file,f'codigo_contrato: Não existe no banco')

            generete_log(log_file,f'id_contrato: Não existe no banco')
            count += 1

        if not log['categoria']:
            generete_log(log_file,f'categoria: Não existe encontra no banco')
            count += 1

        if not log['credito']:
            generete_log(log_file,f'Credito: Não existe essa pessoa no banco')
            count += 1

        if not log['debito']:
            generete_log(log_file,f'Debito: Não existe essa pessoa no banco')
            count += 1

        if not log['aplicar_de_forma']:
            generete_log(log_file,f'aplicar de forma: Precisa ser 1, 2 ou 3')
            count += 1

        if not log['qtd_parcelas']:
            generete_log(log_file,f'qtd_parcelas: inválidos!!!')
            count += 1

        if not log['descricao']:
            generete_log(log_file,f'descricao: precisa ter uma descricão com mais de 1 letra')
            count += 1

        if count != 0:
            generete_log(log_file,f"TOTAL DE ERROS: {count}")
            erros = True

    generete_log(log_file, f'\n\n ----- FINALIZADO A VALIDAÇÃO DE DADOS ----- \n\n')

    mes_ano_str = str(df['mes_ano_insercao'].iloc[0])
    mes_validacao, ano_validacao = int(mes_ano_str.split('/')[0]), int(mes_ano_str.split('/')[1])

    if not erros:
        generete_log(log_file, f'\n\n ----- VALIDAÇÃO PRÉ-INSERT ({mes_validacao:02d}/{ano_validacao}) ----- \n\n')
        tem_erros_pre_insert = validar_pre_insert(df, mes_validacao, ano_validacao, log_file)
        generete_log(log_file, f'\n\n ----- FIM VALIDAÇÃO PRÉ-INSERT ----- \n\n')

        if tem_erros_pre_insert:
            generete_log(log_file, "\n\nFORAM ENCONTRADOS PROBLEMAS NA VALIDAÇÃO PRÉ-INSERT - ABORTANDO IMPORTAÇÃO!!!!")
        else:
            generete_log(log_file,"\n\n************ INICIANDO IMPORTAÇÃO **************")
            response = df.apply(insert, axis=1)
            generete_log(log_file,f"\n\nImportação completa!!!!!")
    else:
        generete_log(log_file,"\n\nTEVE ERROS!!!!")

    with open(log_file, 'a', encoding='utf-8') as f:
        f.write('\n'+ g_historico_log)
