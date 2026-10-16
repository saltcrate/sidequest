FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY engine.py ledger.py worker.py payments.py mcp_http.py server.py ./

ENV PORT=8080
ENV SIDEQUEST_DB=/data/sidequest.db
ENV SIDEQUEST_STATE_DB=/data/state.db
ENV MPP_REALM=saltcrate-sidequest.fly.dev

EXPOSE 8080

CMD ["python", "server.py"]
