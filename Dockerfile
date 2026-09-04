FROM python:3.12-alpine

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY devantech.py eth2mqtt.py devantech.ini /app/

# devantech.ini here is only the seed copy for a fresh /config (see
# ensure_ini_file() in eth2mqtt.py) - the live one lives on the addon_config
# share so edits survive rebuilds/updates.
CMD ["python3", "-u", "/app/eth2mqtt.py"]
