FROM python:3.11-slim

# Stage E1 cleanup: removed the Pango/Cairo/GDK-PixBuf apt install that
# used to live here — those are WeasyPrint's system dependencies, and
# WeasyPrint isn't in requirements.txt. PDFs are rendered by ReportLab
# (a pure-Python library, no system rendering deps needed).

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD gunicorn app:app --bind 0.0.0.0:$PORT
