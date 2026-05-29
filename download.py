from utils.sheets import Sheets
import pandas as pd
from time import sleep
import os


def executar(code_sheets: str, paginas_sheets=None, path="data"):
    
    if paginas_sheets is None:
        paginas_sheets = ['lançamentos']

    sheet = Sheets(code_sheets)

    # cria pasta caso não exista
    os.makedirs(path, exist_ok=True)

    for pagina in paginas_sheets:
        data = sheet.get_planilha(pagina)
        df = pd.DataFrame(data[1:], columns=data[0])

        arquivo = f"{pagina}.csv"
        path_arquivo = os.path.join(path, arquivo)

        df.to_csv(path_arquivo, index=False)

        print(f"Download feito {pagina}!!!")

        sleep(0.5)


if __name__ == "__main__":
    CODE_SHEETS = input('Digite o código da planilha: ')
    executar(CODE_SHEETS)