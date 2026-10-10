import os
import json
import tempfile
import re
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
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


# ============================================================
# TODOIST — API REST v1
# ============================================================

TODOIST_API_TOKEN = os.environ["TODOIST_API_TOKEN"]
TODOIST_BASE_URL = "https://api.todoist.com/api/v1"


def todoist_request(method: str, path: str, payload=None, params=None):
    """Accès Todoist côté serveur : ne jamais exposer le jeton au modèle."""
    from urllib.parse import urlencode
    url = TODOIST_BASE_URL + path
    if params:
        url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {TODOIST_API_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {"success": True}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        logger.warning("Todoist API error HTTP %s: %s", exc.code, detail[:300])
        return {"error": "Erreur Todoist", "http_status": exc.code, "details": detail[:300]}
    except Exception:
        logger.exception("Todoist connection failed")
        return {"error": "Connexion Todoist indisponible"}


def todoist_list(path: str, params=None, max_pages=5):
    """Parcourt les résultats paginés, avec une limite de sécurité."""
    all_results = []
    cursor = None
    for _ in range(max_pages):
        query = dict(params or {})
        if cursor:
            query["cursor"] = cursor
        result = todoist_request("GET", path, params=query)
        if isinstance(result, dict) and "error" in result:
            return result
        if isinstance(result, list):
            all_results.extend(result)
            break
        if not isinstance(result, dict):
            return {"error": "Format de réponse Todoist inattendu"}
        all_results.extend(result.get("results", []))
        cursor = result.get("next_cursor")
        if not cursor:
            break
    return {"results": all_results, "truncated": bool(cursor)}


TODOIST_TOOLS = [
    {"type": "function", "name": "todoist_projects", "description": "Lister les projets Todoist avec leurs identifiants.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}, "strict": True},
    {"type": "function", "name": "todoist_sections", "description": "Lister les sections d'un projet Todoist.",
     "parameters": {"type": "object", "properties": {"project_id": {"type": "string"}},
                    "required": ["project_id"], "additionalProperties": False}, "strict": True},
    {"type": "function", "name": "todoist_tasks", "description": "Consulter les tâches actives, filtrées par projet, section ou échéance. Pour une recherche textuelle, parcourir les résultats.",
     "parameters": {"type": "object", "properties": {
         "project_id": {"type": ["string", "null"]},
         "section_id": {"type": ["string", "null"]}},
         "required": ["project_id", "section_id"], "additionalProperties": False}, "strict": True},
    {"type": "function", "name": "todoist_create_task", "description": "Créer une tâche Todoist. Priorité Todoist 4 = P1 (urgente), 3 = P2, 2 = P3, 1 = P4.",
     "parameters": {"type": "object", "properties": {
         "content": {"type": "string"}, "description": {"type": ["string", "null"]},
         "project_id": {"type": ["string", "null"]}, "section_id": {"type": ["string", "null"]},
         "priority": {"type": ["integer", "null"]}, "due_string": {"type": ["string", "null"]}},
         "required": ["content", "description", "project_id", "section_id", "priority", "due_string"],
         "additionalProperties": False}, "strict": True},
    {"type": "function", "name": "todoist_update_task", "description": "Modifier le contenu, la description, la priorité ou l'échéance d'une tâche existante.",
     "parameters": {"type": "object", "properties": {
         "task_id": {"type": "string"}, "content": {"type": ["string", "null"]},
         "description": {"type": ["string", "null"]}, "priority": {"type": ["integer", "null"]},
         "due_string": {"type": ["string", "null"]}},
         "required": ["task_id", "content", "description", "priority", "due_string"],
         "additionalProperties": False}, "strict": True},
    {"type": "function", "name": "todoist_move_task", "description": "Déplacer une tâche dans un projet ou une section. Confirmer le bon projet/section avant déplacement.",
     "parameters": {"type": "object", "properties": {
         "task_id": {"type": "string"}, "project_id": {"type": ["string", "null"]},
         "section_id": {"type": ["string", "null"]}},
         "required": ["task_id", "project_id", "section_id"], "additionalProperties": False}, "strict": True},
    {"type": "function", "name": "todoist_complete_task", "description": "Marquer comme terminée une tâche explicitement désignée par l'utilisateur.",
     "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}},
                    "required": ["task_id"], "additionalProperties": False}, "strict": True},
]


def execute_todoist_tool(name: str, args: dict):
    if name == "todoist_projects":
        return todoist_list("/projects")
    if name == "todoist_sections":
        return todoist_list("/sections", {"project_id": args["project_id"]})
    if name == "todoist_tasks":
        return todoist_list("/tasks", {k: args.get(k) for k in ("project_id", "section_id")})
    if name == "todoist_create_task":
        payload = {k: v for k, v in args.items() if v is not None}
        if not payload.get("content", "").strip():
            return {"error": "Contenu de tâche manquant"}
        if "priority" in payload and payload["priority"] not in (1, 2, 3, 4):
            return {"error": "Priorité incorrecte"}
        return todoist_request("POST", "/tasks", payload)
    if name == "todoist_update_task":
        task_id = args["task_id"]
        payload = {k: v for k, v in args.items() if k != "task_id" and v is not None}
        if not payload:
            return {"error": "Aucune modification indiquée"}
        if "priority" in payload and payload["priority"] not in (1, 2, 3, 4):
            return {"error": "Priorité incorrecte"}
        return todoist_request("POST", f"/tasks/{task_id}", payload)
    if name == "todoist_move_task":
        task_id = args["task_id"]
        payload = {k: v for k, v in args.items() if k != "task_id" and v is not None}
        if not payload:
            return {"error": "Projet ou section manquant"}
        return todoist_request("POST", f"/tasks/{task_id}/move", payload)
    if name == "todoist_complete_task":
        return todoist_request("POST", f"/tasks/{args['task_id']}/close")
    return {"error": "Outil non autorisé"}


def process_whatsapp_text(sender: str, message: str) -> None:
    """Répond aux messages WhatsApp et exécute les outils Todoist autorisés."""
    try:
        local_now = datetime.now(ZoneInfo("America/Martinique"))
        instructions = (
            "Tu es KAI, secrétaire IA interne de Kay Soley, conciergerie en Martinique. "
            "Réponds en français de façon claire et concise. "
            f"Date et heure locales actuelles : {local_now.strftime('%Y-%m-%d %H:%M')} (Martinique, UTC-4). "
            "Utilise la recherche documentaire pour les questions sur Kay Soley. "
            "Tu as accès aux outils Todoist. Pour agir sur une tâche existante, recherche "
            "son identifiant exact et vérifie le projet. Ne devine jamais un identifiant. "
            "Les priorités Todoist sont inversées : P1=4, P2=3, P3=2, P4=1. "
            "N'exécute une modification que si elle est clairement demandée. "
            "Demande confirmation avant un changement important ou ambigu. "
            "Ne supprime jamais de tâche (aucun outil de suppression disponible). "
            "Ne prétends pas avoir envoyé d'email, créé un événement ou effectué une action "
            "si aucun outil ne l'a réellement exécutée. "
            "Quand l'API signale une erreur, dis clairement que l'action a échoué. "
            "Un message WhatsApp transféré n'identifie pas forcément son auteur ou son logement : "
            "demande une précision si nécessaire."
        )
        tools = [{"type": "file_search", "vector_store_ids": [VECTOR_STORE_ID]}] + TODOIST_TOOLS
        response = client.responses.create(
            model="gpt-5.6", instructions=instructions, input=message, tools=tools,
        )
        for _ in range(6):
            calls = [item for item in response.output if item.type == "function_call"]
            if not calls:
                break
            outputs = []
            for call in calls:
                try:
                    args = json.loads(call.arguments)
                    result = execute_todoist_tool(call.name, args)
                except Exception:
                    logger.exception("Todoist tool processing failed")
                    result = {"error": "Erreur interne pendant l'action Todoist"}
                outputs.append({
                    "type": "function_call_output", "call_id": call.call_id,
                    "output": json.dumps(result, ensure_ascii=False)[:50000],
                })
            response = client.responses.create(
                model="gpt-5.6", instructions=instructions,
                previous_response_id=response.id, input=outputs, tools=tools,
            )
        else:
            send_whatsapp_text(sender, "La demande nécessite trop d'étapes. Peux-tu la simplifier ?")
            return
        answer = (response.output_text or "").strip()
        if not answer:
            answer = "J'ai reçu ton message, mais je n'ai pas pu formuler de réponse."
        send_whatsapp_text(sender, answer)
        logger.info("KAI replied to authorized sender")
    except Exception:
        logger.exception("KAI message processing failed")
        try:
            send_whatsapp_text(sender, "Une erreur technique est survenue. Vérifie les logs Railway.")
        except Exception:
            logger.exception("Could not send error notification")


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
