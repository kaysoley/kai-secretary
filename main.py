import os

from fastapi import FastAPI
from openai import OpenAI

app = FastAPI(title="KAI – Secrétaire Kay Soley")


@app.get("/")
def root():
    return {
        "service": "KAI – Secrétaire Kay Soley",
        "status": "online",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/rag-test")
def rag_test(q: str):
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    response = client.responses.create(
        model="gpt-5.6",
        input=q,
        tools=[
            {
                "type": "file_search",
                "vector_store_ids": [
                    os.environ["OPENAI_VECTOR_STORE_ID"]
                ],
            }
        ],
    )

    return {"answer": response.output_text}
