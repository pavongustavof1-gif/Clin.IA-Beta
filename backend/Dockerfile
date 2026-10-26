FROM python:3.11-slim

# Stage E1 cleanup: removed the Pango/Cairo/GDK-PixBuf apt install that
# used to live here — those are WeasyPrint's system dependencies, and
# WeasyPrint isn't in requirements.txt. PDFs are rendered by ReportLab
# (a pure-Python library, no system rendering deps needed).

WORKDIR /app

COPY requirements.txt .

# #82 (eMudhra CA compromise): the compromised intermediates must be
# distrusted in BOTH trust stores — the system store (urllib → all Supabase
# REST/Storage/Auth/JWKS calls) and certifi (requests/httpx → ICD-11,
# Resend, Gemini, AssemblyAI; floor set in requirements.txt). Placed after
# the requirements COPY so this layer is rebuilt, not reused from cache,
# whenever requirements.txt changes.
RUN apt-get update && apt-get install -y --only-upgrade ca-certificates openssl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD gunicorn app:app --bind 0.0.0.0:$PORT
