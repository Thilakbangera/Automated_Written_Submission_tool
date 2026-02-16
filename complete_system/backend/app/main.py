from fastapi import FastAPI
from .routers import generate

app = FastAPI(title="WS Auto-Generator", version="0.1.1")
app.include_router(generate.router)

@app.get("/health")
def health():
    return {"status": "ok"}
