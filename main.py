import os
import json
import tempfile
import re
import logging
import urllib.request
import urllib.error

from fastapi import BackgroundTasks

from fastapi import FastAPI, HTTPException, Request, Query
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
   # ============================================================
# WHATSAPP WEBHOOK
# ============================================================

WHATSAPP_VERIFY_TOKEN = os.environ["WHATSAPP_VERIFY_TOKEN"]


@app.get("/webhook")
def verify_whatsapp_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """
    Vérification du webhook demandée par Meta.
    """

    if (
        hub_mode == "subscribe"
        and hub_verify_token == WHATSAPP_VERIFY_TOKEN
    ):
        return int(hub_challenge)

    raise HTTPException(
        status_code=403,
        detail="Invalid verification token",
    )


# Identifiants nécessaires à l'envoi des réponses WhatsApp.
WHATSAPP_ACCESS_TOKEN = os.environ["WHATSAPP_ACCESS_TOKEN"]
WHATSAPP_PHONE_NUMBER_ID = os.environ["WHATSAPP_PHONE_NUMBER_ID"]

# Sécurité : seuls les numéros explicitement autorisés peuvent consulter le RAG.
# Exemple de variable Railway : KAI_ALLOWED_WHATSAPP_NUMBERS=596696199338
ALLOWED_WHATSAPP_NUMBERS = {
    number.strip().lstrip("+")
    for number in os.getenv("KAI_ALLOWED_WHATSAPP_NUMBERS", "").split(",")
    if number.strip()
}

logger = logging.getLogger("kai.whatsapp")


def send_whatsapp_text(recipient: str, text: str) -> None:
    """Envoie un message texte via WhatsApp Cloud API."""
    url = (
        "https://graph.facebook.com/v26.0/"
        f"{WHATSAPP_PHONE_NUMBER_ID}/messages"
    )
    body = json.dumps({
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "text",
        "text": {"preview_url": False, "body": text[:4096]},
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            logger.info("WhatsApp outbound HTTP %s", response.status)
    except urllib.error.HTTPError as exc:
        error_text = exc.read().decode("utf-8", errors="replace")
        logger.error("WhatsApp outbound HTTP %s: %s", exc.code, error_text[:800])
        raise


def process_whatsapp_text(sender: str, message: str) -> None:
    """Traite le message en arrière-plan, sans bloquer l'ACK du webhook."""
    try:
        response = client.responses.create(
            model="gpt-5.6",
            instructions=(
                "Tu es KAI, secrétaire IA interne de Kay Soley, une conciergerie "
                "en Martinique. Réponds en français de façon claire et concise. "
                "Utilise la recherche documentaire pour les questions sur Kay Soley. "
                "Ne fabrique pas d'informations absentes des documents. "
                "Ne prétends jamais avoir exécuté une action (mail, facture, tâche) "
                "si aucun outil ne l'a réellement réalisée."
            ),
            input=message,
            tools=[{
                "type": "file_search",
                "vector_store_ids": [VECTOR_STORE_ID],
            }],
        )
        answer = (response.output_text or "").strip()
        if not answer:
            answer = "J'ai bien reçu ton message, mais je n'ai pas pu formuler de réponse."
        send_whatsapp_text(sender, answer)
        logger.info("KAI replied to authorized sender")
    except Exception:
        logger.exception("KAI message processing failed")


@app.post("/webhook")
async def receive_whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    """Accuse réception des événements Meta et traite les textes autorisés."""
    payload = await request.json()
    queued = 0

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                continue
            value = change.get("value", {})
            for message in value.get("messages", []):
                if message.get("type") != "text":
                    continue
                sender = str(message.get("from", "")).lstrip("+")
                body = message.get("text", {}).get("body", "").strip()
                if not sender or not body:
                    continue
                if sender not in ALLOWED_WHATSAPP_NUMBERS:
                    logger.warning("Ignored WhatsApp message from unauthorized sender")
                    continue
                background_tasks.add_task(process_whatsapp_text, sender, body)
                queued += 1

    logger.info("WhatsApp webhook received; queued_messages=%s", queued)
    return {"status": "received"}
