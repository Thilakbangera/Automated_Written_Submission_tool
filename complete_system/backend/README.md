# WS Auto-Generator (V1 - Format-first)

V1 objective: **format generation + correct header metadata** (dates, names, application number, prior art list).
The rest of the WS body is a fixed skeleton that matches your gold WS structure; it will be auto-populated in V2+.

## Run (Windows PowerShell)
```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open: http://127.0.0.1:8000/docs
