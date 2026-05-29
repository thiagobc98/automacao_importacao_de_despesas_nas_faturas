from __future__ import print_function
import os.path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from gspread_dataframe import set_with_dataframe
import gspread

import pandas as pd
import dotenv
import os
dotenv.load_dotenv()

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
PATH_TOKEN_SHEETS_JSON = os.getenv("PATH_TOKEN_SHEETS_JSON")
PATH_CREDENCIAL_SHEETS_JSON = os.getenv("PATH_CREDENCIAL_SHEETS_JSON")
PATH_TOKEN_MASTER_LANE_SHEETS = os.getenv("PATH_TOKEN_MASTER_LANE_SHEETS")

class Sheets: 
    
    def __init__(self, code_sheets: str):
        self.CODE_SHEETS = code_sheets
        self.SPREADSHEETS = self.sheets()

    def set_code_sheets(self, new_code_sheets: str):
        self.CODE_SHEETS = new_code_sheets
        
    def login(self):
        creds = None
        if os.path.exists(PATH_TOKEN_SHEETS_JSON):
            creds = Credentials.from_authorized_user_file(PATH_TOKEN_SHEETS_JSON, SCOPES)
            
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(PATH_CREDENCIAL_SHEETS_JSON, SCOPES)
                creds = flow.run_local_server(port=0)
            # Save the credentials for the next run
            with open(PATH_TOKEN_SHEETS_JSON, 'w') as token:
                token.write(creds.to_json())

        return creds
        
    def sheets(self):
        print('Connectando ao sheets...')
        creds = self.login()
        service = build('sheets', 'v4', credentials=creds)
        spreadsheets = service.spreadsheets()
        return spreadsheets

    def get_planilha(self, pag_range):
        try:
            result = self.SPREADSHEETS.values().get(spreadsheetId=self.CODE_SHEETS, range=pag_range).execute()
            data = result['values']
            return data
        except HttpError as err:
            print(err)

    def upload_to_sheets(self, df: pd.DataFrame, pagina_sheets: str) -> None:
        print(f'Fazendo upload ao sheets {pagina_sheets}...')
        gc = gspread.service_account(filename= PATH_TOKEN_MASTER_LANE_SHEETS)
        Sheet = gc.open_by_key(self.CODE_SHEETS)
        Worksheet = Sheet.worksheet(pagina_sheets)
        set_with_dataframe(Worksheet, df)
        print('Upload feito com sucesso!!!')

    def clear_sheets(self, pagina_sheets: str)->None:
        print(f'limpando a página sheets {pagina_sheets}...')
        gc = gspread.service_account(filename= PATH_TOKEN_MASTER_LANE_SHEETS)
        Sheet = gc.open_by_key(self.CODE_SHEETS)
        Worksheet = Sheet.worksheet(pagina_sheets)
        Worksheet.clear()
    
    def clear_and_upload(self, df: pd.DataFrame, pagina_sheets: str):
        self.clear_sheets(pagina_sheets)
        self.upload_to_sheets(df, pagina_sheets)