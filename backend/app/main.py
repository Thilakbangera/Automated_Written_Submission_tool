from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .routers import generate

# 👇 Add your allowed frontend origins here
ALLOWED_ORIGINS = [
    "http://localhost:3000",                 # local testing
    "https://lextriatech.netlify.app",       # your Netlify frontend
]

app = FastAPI(title="WS Auto-Generator", version="0.1.1")

# 👇 Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,   # IMPORTANT (do not set True)
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(generate.router)

@app.get("/health")
def health():
    return {"status": "ok"}