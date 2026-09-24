import os
import json
import tempfile

from fastapi import FastAPI
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build


app = FastAPI(title="KAI - Secretaire Kay Soley")

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
VECTOR_STORE_ID = os.environ["OPENAI_VECTOR_STORE_ID"]


@app.get("/")
def root():
    return {
        "service": "KAI - Secretaire Kay Soley",
        "status": "online",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/rag-test")
def rag_test(q: str):
    response = client.responses.create(
        model="gpt-5.6",
        input=q,
        tools=[
            {
                "type": "file_search",
                "vector_store_ids": [VECTOR_STORE_ID],
            }
        ],
    )

    return {"answer": response.output_text}


@app.get("/rag-pilot")
def rag_pilot():
    content = """
# KAI - Base metier Kay Soley - Test pilote

## Lauramar
Proprietaire : Marcel Blanc
Formule : Premium
Commission Kay Soley : 25 % TTC

## Ti Kay Paradi
Proprietaire : Loic Portier
Formule : Premium
Commission Kay Soley : 25 % TTC

## Villa Goyave
Proprietaire : Sandrine Brasset
Formule : Support commercial
Commission Kay Soley : 15 % TTC
"""

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        encoding="utf-8",
        delete=False,
    ) as f:
        f.write(content)
        path = f.name

    with open(path, "rb") as f:
        uploaded_file = client.files.create(
            file=f,
            purpose="assistants",
        )

    client.vector_stores.files.create_and_poll(
        vector_store_id=VECTOR_STORE_ID,
        file_id=uploaded_file.id,
    )

    os.remove(path)

    return {
        "status": "completed",
        "file_id": uploaded_file.id,
        "vector_store_id": VECTOR_STORE_ID,
    }


@app.get("/google-test")
def google_test():
    service_account_info = json.loads(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    )

    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
    )

    sheets = build("sheets", "v4", credentials=credentials)

    result = (
        sheets.spreadsheets()
        .values()
        .get(
            spreadsheetId="1ie_nDiDpetvHmAQjuiPQOlAh5tUEJ51pdGMudafAAWw",
            range="Propriétaires!A1:G10",
        )
        .execute()
    )

    return {
        "status": "connected",
        "rows": result.get("values", []),
    }
@app.get("/google-sources")
def google_sources():
    service_account_info = json.loads(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    )

    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )

    drive = build("drive", "v3", credentials=credentials)

    result = drive.files().list(
        q="trashed = false",
        fields="files(id,name,mimeType,modifiedTime)",
        pageSize=100,
    ).execute()

    return {
        "sources": result.get("files", [])
    }
