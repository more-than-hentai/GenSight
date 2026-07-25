FROM python:3.12-slim

WORKDIR /opt/gensight

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web

ENV GENSIGHT_DATA_DIR=/opt/gensight/data
VOLUME ["/opt/gensight/data"]

EXPOSE 8090
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
