FROM python:3.11-slim

WORKDIR /app

COPY rootfs/app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY rootfs/app/ .

EXPOSE 8090
EXPOSE 8091

CMD ["python", "serve.py"]
