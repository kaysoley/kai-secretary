import os
import json
import tempfile
import re

from fastapi import FastAPI, HTTPException
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build


app = FastAPI(title="KAI - Secretaire Kay Soley")

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
VECTOR_STORE_ID = os.environ["OPENAI_VECTOR_STORE_ID"]


# ============================================================
# SOURCES RAG KAI
# ============================================================

RAG_SOURCES = {
    "1ie_nDiDpetvHmAQjuiPQOlAh5tUEJ51pdGMudafAAWw":
        "Liste clients Propriétaires 2026",

    "1Ks71cDeSQjO51lpmrHIQSZKn_CNlmLN5klFh6E8HG1w":
        "Configuration options annonces clients Plateformes - 2026",

    "1tO3-_4beEVlGB3_S8SGdg1XH06svtJU7KRmc2o87vMY":
        "Annuaire Prestataires et Artisans",

    "1QKu9XhvBz8JZT9oRuMZTabzuAqdEQHLXdaZ8sxMAWVU":
        "Répartition concierges et ménage",

    "1yKhLMr2BdvgaW5_BohMsFtnlU5fkWj8eUpMgxv7FBjA":
        "Grille Responsabilité",
}


# ============================================================
# GOOGLE
# ============================================================

def get_google_credentials():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])

    return service_account.Credentials.from_service_account_info(
        info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
    )


def get_google_services():
    credentials = get_google_credentials()

    sheets = build(
        "sheets",
        "v4",
        credentials=credentials,
        cache_discovery=False,
    )

    drive = build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )

    return sheets, drive


# ============================================================
# GOOGLE SHEET -> TEXTE POUR LE RAG
# ============================================================

def sheet_to_text(sheets, spreadsheet_id, source_name):
    metadata = (
        sheets.spreadsheets()
        .get(spreadsheetId=spreadsheet_id)
        .execute()
    )

    sections = [
        f"# Source Kay Soley : {source_name}",
        "",
        f"Identifiant source Google : {spreadsheet_id}",
        "",
    ]

    for sheet in metadata.get("sheets", []):
        title = sheet["properties"]["title"]

        result = (
            sheets.spreadsheets()
            .values()
            .get(
                spreadsheetId=spreadsheet_id,
                range=f"'{title}'",
            )
            .execute()
        )

        rows = result.get("values", [])

        sections.append(f"## Onglet : {title
