import mysql.connector
import dotenv
import os
# from time import sleep

dotenv.load_dotenv()

HOST_DB = os.getenv("DB_HOST")
DATABSE = os.getenv("DB_NAME")
USER_DB = os.getenv("DB_USER")
PASS_DB = os.getenv("DB_PASSWORD")

PORT = 3306


def get_connection():
    try:
        connection = mysql.connector.connect(
            host=HOST_DB, database=DATABSE, user=USER_DB, password=PASS_DB, port=PORT)
        return connection
    except mysql.connector.Error as erro:
        print(f"Ao fazer conexão no banco:\n {erro}")
        return None


def existe_contrato_no_banco(contrato):
    query = "SELECT id_contrato FROM contratos WHERE codigo_contrato = %s AND exc_contrato = 'F'"
    connection = None
    try:
        connection = get_connection()
        if connection is None:
            return None
        cursor = connection.cursor()
        cursor.execute(query, (contrato,))
        resultado = cursor.fetchone()
        return resultado[0] if resultado else None
    except mysql.connector.Error as erro:
        print(f"Erro ao verificar se existe contrato:\n{erro}")
    finally:
        if connection and connection.is_connected():
            cursor.close()
            connection.close()


def get_pessoas_no_banco():
    query = "SELECT id_pessoa FROM pessoas_lancamentos;"
    return __get_coluna_banco(query)


def get_categorias_no_banco():
    query = "SELECT id_release FROM release_categories WHERE exc_release = 'F';"
    return __get_coluna_banco(query)


def __get_coluna_banco(query):
    connection = None
    try:
        connection = get_connection()
        if connection is None:
            return []
        cursor = connection.cursor()
        cursor.execute(query)
        resultados = cursor.fetchall()
        lista_ids = [result[0] for result in resultados]
        return lista_ids
    except mysql.connector.Error as erro:
        print(f"Erro ao da get no banco:\n{erro}")
        return []
    finally:
        if connection and connection.is_connected():
            cursor.close()
            connection.close()


def get_id_contrato(codigo_contrato):
    query = "SELECT c.id_contrato FROM contratos c WHERE c.exc_contrato = 'F' AND c.codigo_contrato = %s"
    connection = None
    try:
        connection = get_connection()
        if connection is None:
            return None
        cursor = connection.cursor()
        cursor.execute(query, (codigo_contrato,))
        resultado = cursor.fetchone()
        return resultado[0]
    except mysql.connector.Error as erro:
        print(f"Erro ao da get no banco:\n{erro}")
    finally:
        if connection and connection.is_connected():
            cursor.close()
            connection.close()


def get_faturas_em_aberto(codigo_contrato, mes, ano):
    query = """
    SELECT
        f.id_fatura
    FROM contratos c
        INNER JOIN faturas f ON f.fk_id_contrato = c.id_contrato
    WHERE c.exc_contrato = 'F'
        AND f.exc_fatura = 'F'
        AND f.status_fatura = 'PE'
        AND f.url_boleto_fatura IS NULL
        AND f.pagamento_fatura IS NULL
        AND MONTH(f.vencimento_fatura) >= %s
        AND YEAR(f.vencimento_fatura) >= %s
        AND c.codigo_contrato = %s
    """
    connection = None
    try:
        connection = get_connection()
        if connection is None:
            return []
        cursor = connection.cursor()
        cursor.execute(query, (mes, ano, codigo_contrato))
        resultados = cursor.fetchall()
        lista_ids = [result[0] for result in resultados]
        return lista_ids
    except mysql.connector.Error as erro:
        print(f"Erro ao da get no banco:\n{erro}")
        return []
    finally:
        if connection and connection.is_connected():
            cursor.close()
            connection.close()


def get_faturas_em_aberto_by_id(id_contrato, mes, ano):
    query = """
    SELECT
        f.id_fatura
    FROM faturas f
    WHERE f.fk_id_contrato = %s
        AND f.exc_fatura = 'F'
        AND f.status_fatura = 'PE'
        AND f.url_boleto_fatura IS NULL
        AND f.pagamento_fatura IS NULL
        AND MONTH(f.vencimento_fatura) >= %s
        AND YEAR(f.vencimento_fatura) >= %s
    """
    connection = None
    try:
        connection = get_connection()
        if connection is None:
            return []
        cursor = connection.cursor()
        cursor.execute(query, (id_contrato, mes, ano))
        resultados = cursor.fetchall()
        lista_ids = [result[0] for result in resultados]
        return lista_ids
    except mysql.connector.Error as erro:
        print(f"Erro ao da get no banco:\n{erro}")
        return []
    finally:
        if connection and connection.is_connected():
            cursor.close()
            connection.close()


def get_id_contratos_por_codigos(codigos_contrato):
    if not codigos_contrato:
        return {}
    placeholders = ','.join(['%s'] * len(codigos_contrato))
    query = f"""
    SELECT codigo_contrato, id_contrato
    FROM contratos
    WHERE codigo_contrato IN ({placeholders})
    AND exc_contrato = 'F'
    """
    connection = None
    try:
        connection = get_connection()
        if connection is None:
            return {}
        cursor = connection.cursor()
        cursor.execute(query, tuple(codigos_contrato))
        resultados = cursor.fetchall()
        return {cod: id_c for cod, id_c in resultados}
    except mysql.connector.Error as erro:
        print(f"Erro ao buscar id_contratos no banco:\n{erro}")
        return {}
    finally:
        if connection and connection.is_connected():
            cursor.close()
            connection.close()


def get_contratos_sem_faturas_mes(id_contratos, mes, ano):
    if not id_contratos:
        return []
    placeholders = ','.join(['%s'] * len(id_contratos))
    query = f"""
    SELECT DISTINCT c.id_contrato, c.codigo_contrato
    FROM contratos c
    WHERE c.id_contrato IN ({placeholders})
    AND c.fk_id_status_status_contrato = 2
    AND NOT EXISTS (
        SELECT 1
        FROM faturas f
        WHERE f.fk_id_contrato = c.id_contrato
        AND MONTH(f.vencimento_fatura) = %s
        AND YEAR(f.vencimento_fatura) = %s
        AND f.exc_fatura = 'F'
    )
    """
    connection = None
    try:
        connection = get_connection()
        if connection is None:
            return []
        cursor = connection.cursor()
        cursor.execute(query, tuple(id_contratos) + (mes, ano))
        return cursor.fetchall()
    except mysql.connector.Error as erro:
        print(f"Erro ao verificar faturas no banco:\n{erro}")
        return []
    finally:
        if connection and connection.is_connected():
            cursor.close()
            connection.close()


def get_lancamentos_existentes_mes(id_contratos, id_releases, mes, ano):
    if not id_contratos or not id_releases:
        return []
    placeholders_contratos = ','.join(['%s'] * len(id_contratos))
    placeholders_releases = ','.join(['%s'] * len(id_releases))
    query = f"""
    SELECT DISTINCT
        c.codigo_contrato, c.id_contrato, f.id_fatura, l.fk_id_release
    FROM contratos c
        JOIN faturas f ON f.fk_id_contrato = c.id_contrato
        JOIN lancamentos l ON l.fk_id_fatura = f.id_fatura
    WHERE c.id_contrato IN ({placeholders_contratos})
        AND l.fk_id_release IN ({placeholders_releases})
        AND MONTH(f.vencimento_fatura) = %s
        AND YEAR(f.vencimento_fatura) = %s
        AND c.exc_contrato = 'F'
        AND f.exc_fatura = 'F'
        AND l.exc_lan = 'F'
    """
    connection = None
    try:
        connection = get_connection()
        if connection is None:
            return []
        cursor = connection.cursor()
        cursor.execute(query, tuple(id_contratos) + tuple(id_releases) + (mes, ano))
        return cursor.fetchall()
    except mysql.connector.Error as erro:
        print(f"Erro ao verificar lancamentos existentes no banco:\n{erro}")
        return []
    finally:
        if connection and connection.is_connected():
            cursor.close()
            connection.close()


def insert_grupo_parcelamento(descricao, fk_id_contrato, part_lan, valor_lan, credito_lan, debito_lan, fk_id_release, tipo_grupo):
    connection = None
    try:
        connection = get_connection()
        if connection is None:
            return None
        cursor = connection.cursor()

        insert_query = """
            INSERT INTO tb_lancamento_grupo_parcelamento
            (descricao, fk_id_contrato, part_lan, valor_lan, credito_lan, debito_lan, fk_id_release, tipo_grupo)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        record_to_insert = (descricao, fk_id_contrato, part_lan, valor_lan, credito_lan, debito_lan, fk_id_release, tipo_grupo)
        cursor.execute(insert_query, record_to_insert)
        connection.commit()
        return cursor.lastrowid
    except mysql.connector.Error as erro:
        print(f"Erro ao inserir dados em tb_lancamento_grupo_parcelamento: {erro}")
    finally:
        if connection and connection.is_connected():
            cursor.close()
            connection.close()


def insert_lancamento(desc_lan, forma_lan, parcelas_lan, valor_lan, credito_lan, debito_lan, fk_id_release, fk_id_fatura, fk_id_grupo_parcelamento=None):
    # sleep(5)
    connection = None
    try:
        connection = get_connection()
        if connection is None:
            return None
        cursor = connection.cursor()

        insert_query = """
            INSERT INTO lancamentos
            (desc_lan, forma_lan, parcelas_lan, valor_lan, credito_lan, debito_lan, fk_id_release, fk_id_fatura, exc_lan, part_lan, fk_id_grupo_parcelamento)
            VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        exc_lan = "F"
        record_to_insert = (desc_lan, forma_lan, valor_lan, credito_lan,
                            debito_lan, fk_id_release, fk_id_fatura, exc_lan, parcelas_lan, fk_id_grupo_parcelamento)
        cursor.execute(insert_query, record_to_insert)
        connection.commit()
        return cursor.lastrowid
    except mysql.connector.Error as erro:
        print(f"Erro ao inserir dados no MySQL: {erro}")
    finally:
        if connection and connection.is_connected():
            cursor.close()
            connection.close()


def insert_or_update_lancamento(desc_lan, forma_lan, parcelas_lan, valor_lan, credito_lan, debito_lan, fk_id_release, fk_id_fatura):
    # sleep(5)
    connection = None
    try:
        connection = get_connection()
        if connection is None:
            return None
        cursor = connection.cursor()

        check_query = """SELECT COUNT(*)
            FROM lancamentos l
            WHERE l.fk_id_release = %s
            AND l.fk_id_fatura = %s
            AND l.exc_lan = 'F'
        """
        cursor.execute(check_query, (fk_id_release, fk_id_fatura,))
        result = cursor.fetchone()

        if result[0] > 0:
            update_query = """
                UPDATE lancamentos
                SET desc_lan = %s, parcelas_lan = NULL, part_lan = %s, valor_lan = %s, credito_lan = %s, debito_lan = %s
                WHERE fk_id_release = %s AND fk_id_fatura = %s AND exc_lan = 'F'
            """
            cursor.execute(update_query, (desc_lan, parcelas_lan, valor_lan,
                           credito_lan, debito_lan, fk_id_release, fk_id_fatura))
            connection.commit()
        else:
            insert_query = """
                INSERT INTO lancamentos
                (desc_lan, forma_lan, parcelas_lan, valor_lan, credito_lan, debito_lan, fk_id_release, fk_id_fatura, exc_lan, part_lan)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            exc_lan = "F"
            record_to_insert = (desc_lan, forma_lan, parcelas_lan, valor_lan, credito_lan,
                                debito_lan, fk_id_release, fk_id_fatura, exc_lan, parcelas_lan)
            cursor.execute(insert_query, record_to_insert)
            connection.commit()

        return cursor.lastrowid
    except mysql.connector.Error as erro:
        print(f"Erro ao inserir/atualizar dados no MySQL: {erro}")
    finally:
        if connection and connection.is_connected():
            cursor.close()
            connection.close()
