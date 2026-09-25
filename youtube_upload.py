"""
YouTube-Upload-Funktionen, wiederverwendet aus dem Cronjob-Skript.

Unterschied zur GitHub-Actions-Version: Render hat kein persistentes
Dateisystem zwischen Deploys, daher kommen die OAuth-Tokens hier aus
Umgebungsvariablen (YT_TOKEN_<ACCOUNT>, GROSSGESCHRIEBEN) statt aus
tokens/<account>.json. Die Tokens selbst erzeugst du weiterhin einmalig
lokal (siehe upload_video.py --account=... --file=...) und trägst den
Inhalt dann als Environment-Variable bei Render ein.
"""

import os
import tempfile
import time
import random
import http.client

import httplib2
import requests
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

httplib2.RETRIES = 1
MAX_RETRIES = 10
RETRIABLE_EXCEPTIONS = (
    httplib2.HttpLib2Error,
    IOError,
    http.client.NotConnected,
    http.client.IncompleteRead,
    http.client.ImproperConnectionState,
    http.client.CannotSendRequest,
    http.client.CannotSendHeader,
    http.client.ResponseNotReady,
    http.client.BadStatusLine,
)
RETRIABLE_STATUS_CODES = [500, 502, 503, 504]

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"


def get_authenticated_service(account_key):
    """Baut den YouTube-Client aus einem in der Umgebungsvariable
    YT_TOKEN_<ACCOUNT_KEY> hinterlegten OAuth-Token (JSON-String, wie ihn
    upload_video.py lokal unter tokens/<account>.json erzeugt)."""
    env_name = f"YT_TOKEN_{account_key.upper()}"
    token_json = os.environ.get(env_name)
    if not token_json:
        raise RuntimeError(
            f"Keine Umgebungsvariable {env_name} gefunden. Token für "
            f"Account '{account_key}' lokal erzeugen und als Render-"
            f"Environment-Variable hinterlegen."
        )

    creds = Credentials.from_authorized_user_info(
        __import__("json").loads(token_json), [YOUTUBE_UPLOAD_SCOPE]
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    return build(YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION, credentials=creds)


def download_to_tempfile(url):
    suffix = os.path.splitext(url.split("?")[0])[1] or ".mp4"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with open(tmp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8 * 1024 * 1024):
                if chunk:
                    f.write(chunk)
    return tmp_path


def upload_file(youtube, file_path, title, description, keywords, category, privacy_status):
    tags = keywords.split(",") if keywords else None
    body = dict(
        snippet=dict(title=title, description=description, tags=tags, categoryId=category),
        status=dict(privacyStatus=privacy_status),
    )
    insert_request = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=MediaFileUpload(file_path, chunksize=-1, resumable=True),
    )
    return _resumable_upload(insert_request)


def _resumable_upload(insert_request):
    response = None
    error = None
    retry = 0
    while response is None:
        try:
            status, response = insert_request.next_chunk()
            if response is not None:
                if "id" in response:
                    return response["id"]
                raise RuntimeError(f"Upload fehlgeschlagen, unerwartete Antwort: {response}")
        except HttpError as e:
            if e.resp.status in RETRIABLE_STATUS_CODES:
                error = f"Wiederholbarer HTTP-Fehler {e.resp.status}: {e.content}"
            else:
                raise
        except RETRIABLE_EXCEPTIONS as e:
            error = f"Wiederholbarer Fehler: {e}"

        if error is not None:
            retry += 1
            if retry > MAX_RETRIES:
                raise RuntimeError("Maximale Anzahl an Versuchen erreicht.")
            time.sleep(random.random() * (2 ** retry))
            error = None
