"""Ask Claris backend — FastAPI over the governance DuckDB. Exposes /ask, /meta, /health."""
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Load backend/.env regardless of the launch directory.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from db import DB
from nlq import NLQEngine

STATE = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = DB()
    STATE["db"] = db
    STATE["engine"] = NLQEngine(db)
    print("[claris] ready.")
    yield


app = FastAPI(title="Ask Claris API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class AskBody(BaseModel):
    question: str
    role: str = "Compliance officer"
    detailLevel: str = "Concise"
    revealPii: bool = False


@app.get("/health")
def health():
    db = STATE.get("db")
    return {"ok": db is not None}


@app.get("/meta")
def meta():
    db: DB = STATE["db"]
    return {
        "tables": {t: len(cols) for t, cols in db.tables.items()},
        "champion": "m4_autoencoder",
        "provider": STATE["engine"].provider.name,
        "model": STATE["engine"].provider.model,
    }


@app.post("/ask")
def ask(body: AskBody):
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
    if provider == "anthropic" and not os.getenv("ANTHROPIC_API_KEY"):
        return {"error": "Server is missing ANTHROPIC_API_KEY. Set it in backend/.env."}
    engine: NLQEngine = STATE["engine"]
    ctx = body.model_dump()
    q = ctx.pop("question").strip()
    if not q:
        return {"clarification": "What would you like to ask about the dispute models?"}
    try:
        return engine.ask(q, ctx)
    except Exception as e:
        return {"error": f"Something went wrong handling that question: {str(e).splitlines()[0]}"}
