import os
import json
import tempfile
import re

from fastapi import FastAPI, HTTPException
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build


# ============================================================
# APPLICATION
# ============================================================

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

    return credentials


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
# GOOGLE SHEET -> TEXTE RAG
# ============================================================

def sheet_to_text(
    sheets,
    spreadsheet_id,
    source_name,
):
    metadata = (
        sheets.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id
        )
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

        sections.append(
            f"## Onglet : {title}"
        )
        sections.append("")

        if not rows:
            sections.append(
                "(Onglet vide)"
            )
            sections.append("")
            continue

        headers = rows[0]

        for row_number, row in enumerate(
            rows[1:],
            start=2,
        ):
            if not any(
                str(value).strip()
                for value in row
            ):
                continue

            sections.append(
                f"### Ligne {row_number}"
            )

            max_columns = max(
                len(headers),
                len(row),
            )

            for index in range(max_columns):

                if (
                    index < len(headers)
                    and str(headers[index]).strip()
                ):
                    header = str(
                        headers[index]
                    ).strip()
                else:
                    header = (
                        f"Colonne {index + 1}"
                    )

                if index < len(row):
                    value = str(
                        row[index]
                    ).strip()
                else:
                    value = ""

                if value:
                    sections.append(
                        f"- {header} : {value}"
                    )

            sections.append("")

    return "\n".join(sections)


# ============================================================
# OPENAI VECTOR STORE
# ============================================================

def safe_filename(name):
    filename = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        name,
    )

    return f"KAI_RAG_{filename}.txt"


def find_existing_vector_files(
    source_filename,
):
    matches = []

    page = client.vector_stores.files.list(
        vector_store_id=VECTOR_STORE_ID,
        limit=100,
    )

    while True:

        for vector_file in page.data:

            try:
                openai_file = (
                    client.files.retrieve(
                        vector_file.id
                    )
                )

                if (
                    openai_file.filename
                    == source_filename
                ):
                    matches.append(
                        vector_file.id
                    )

            except Exception:
                pass

        if not getattr(
            page,
            "has_more",
            False,
        ):
            break

        page = (
            client.vector_stores.files.list(
                vector_store_id=VECTOR_STORE_ID,
                limit=100,
                after=page.data[-1].id,
            )
        )

    return matches


def delete_old_vector_versions(
    source_filename,
):
    old_ids = find_existing_vector_files(
        source_filename
    )

    for file_id in old_ids:

        try:
            client.vector_stores.files.delete(
                vector_store_id=VECTOR_STORE_ID,
                file_id=file_id,
            )
        except Exception:
            pass

        try:
            client.files.delete(
                file_id
            )
        except Exception:
            pass

    return len(old_ids)


def upload_to_vector_store(
    source_name,
    content,
):
    filename = safe_filename(
        source_name
    )
    deleted_versions = delete_old_vector_versions(filename)

    path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            encoding="utf-8",
            delete=False,
        ) as temp:
            temp.write(content)
            path = temp.name

        with open(path, "rb") as file_handle:
            uploaded_file = client.files.create(
                file=(filename, file_handle),
                purpose="assistants",
            )

        client.vector_stores.files.create_and_poll(
            vector_store_id=VECTOR_STORE_ID,
            file_id=uploaded_file.id,
        )

        return {
            "source": source_name,
            "filename": filename,
            "file_id": uploaded_file.id,
            "old_versions_deleted": deleted_versions,
            "status": "synced",
        }

    finally:
        if path and os.path.exists(path):
            os.remove(path)


# ============================================================
# ENDPOINTS
# ============================================================

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


@app.post("/sync-rag")
def sync_rag():
    try:
        sheets, _ = get_google_services()

        results = []

        for spreadsheet_id, source_name in RAG_SOURCES.items():
            try:
                content = sheet_to_text(
                    sheets,
                    spreadsheet_id,
                    source_name,
                )

                result = upload_to_vector_store(
                    source_name,
                    content,
                )

                results.append(result)

            except Exception as exc:
                results.append(
                    {
                        "source": source_name,
                        "status": "error",
                        "error": str(exc),
                    }
                )

        synced = sum(
            1
            for item in results
            if item["status"] == "synced"
        )

        return {
            "status": "completed",
            "vector_store_id": VECTOR_STORE_ID,
            "sources_expected": len(RAG_SOURCES),
            "sources_synced": synced,
            "results": results,
        }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=str(exc),
        )
   
