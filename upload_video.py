#!/usr/bin/env python3
"""
Lädt Videos automatisiert auf YouTube hoch (YouTube Data API v3).

Zwei Betriebsarten:

1) Einzelnes Video per lokaler Datei oder URL:
    python upload_video.py --file="/pfad/zum/video.mp4" --title="..."
    python upload_video.py --url="https://cloud-anbieter.tld/video.mp4" --title="..."

2) Batch-/Cronjob-Betrieb über ein Manifest (JSON-Liste von Videos mit Cloud-URL
   und Metadaten). Bereits hochgeladene Videos werden übersprungen, damit
   wiederholte Cron-Läufe keine Duplikate erzeugen:
    python upload_video.py --manifest="videos.json"

   Beispiel videos.json (mit mehreren Accounts/Kanälen):
   [
     {
       "id": "video-001",
       "url": "https://cloud-anbieter.tld/pfad/video1.mp4",
       "title": "Mein Sommerurlaub",
       "description": "Surfen in Santa Cruz",
       "keywords": "surfen,santa cruz",
       "category": "22",
       "privacyStatus": "private",
       "account": "kanal_a"
     },
     {
       "id": "video-002",
       "url": "https://cloud-anbieter.tld/pfad/video2.mp4",
       "title": "Noch ein Video",
       "account": "kanal_b"
     }
   ]

   "id" ist ein von dir vergebener eindeutiger Bezeichner (z. B. Dateiname),
   über den ein bereits hochgeladenes Video erkannt wird. Fehlende optionale
   Felder greifen auf die --title/--description/... Standardwerte zurück.

   "account" wählt aus, mit welchem YouTube-Kanal hochgeladen wird (siehe
   Mehrere Accounts unten). Fehlt das Feld, wird der Standard-Account
   verwendet (--account bzw. "default").

Mehrere Accounts/Kanäle:
   Für jeden Account wird ein eigenes Token benötigt, das einmalig lokal
   per Browser-Login erzeugt wird:
    python upload_video.py --account="kanal_a" --file="test.mp4"
    python upload_video.py --account="kanal_b" --file="test.mp4"

   Das legt tokens/kanal_a.json bzw. tokens/kanal_b.json an. Alle Accounts
   können dieselbe client_secrets.json (dieselbe Google-Cloud-App) nutzen.
   Achtung: Das 100-Uploads/Tag-Limit gilt pro Google-Cloud-Projekt, nicht
   pro Kanal – teilt sich also über alle Accounts hinweg, die dasselbe
   Projekt/dieselbe client_secrets.json verwenden.

Voraussetzungen:
    pip install google-api-python-client google-auth-oauthlib google-auth-httplib2 requests

Einrichtung:
    1. Projekt in der Google Cloud Console anlegen: https://console.cloud.google.com/
    2. "YouTube Data API v3" aktivieren.
    3. OAuth-Client-ID vom Typ "Desktop-App" erstellen und als
       client_secrets.json im selben Verzeichnis wie dieses Skript speichern.
    4. Für jeden Account einmal interaktiv ausführen (--account + --file), um
       dich im Browser mit dem jeweiligen Google-Konto/Kanal anzumelden.
       Dabei entsteht tokens/<account>.json, das der Cronjob danach
       wiederverwendet, ohne dass nochmal ein Login nötig ist.
"""

import argparse
import http.client
import httplib2
import json
import os
import random
import tempfile
import time

import requests

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

# Retry-Logik übernehmen wir selbst, daher keine automatischen Retries der
# HTTP-Bibliothek.
httplib2.RETRIES = 1

# Maximale Anzahl an Wiederholungsversuchen bei einem fehlgeschlagenen Upload.
MAX_RETRIES = 10

# Bei diesen Exceptions wird grundsätzlich erneut versucht.
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

# Bei diesen HTTP-Statuscodes wird ebenfalls erneut versucht.
RETRIABLE_STATUS_CODES = [500, 502, 503, 504]

CLIENT_SECRETS_FILE = "client_secrets.json"
TOKEN_DIR = "tokens"
DEFAULT_ACCOUNT = "default"

# Dieser Scope erlaubt nur das Hochladen von Videos auf den eigenen Kanal.
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"

VALID_PRIVACY_STATUSES = ("public", "private", "unlisted")

# Datei, in der die IDs bereits hochgeladener Videos aus dem Manifest
# festgehalten werden, damit der Cronjob keine Duplikate hochlädt.
UPLOADED_STATE_FILE = "uploaded.json"

MISSING_CLIENT_SECRETS_MESSAGE = f"""
FEHLER: client_secrets.json nicht gefunden unter:
   {os.path.abspath(os.path.join(os.path.dirname(__file__), CLIENT_SECRETS_FILE))}

Bitte lade die Datei aus der Google Cloud Console herunter:
https://console.cloud.google.com/
und speichere sie unter diesem Namen im selben Verzeichnis wie dieses Skript.
"""


def get_authenticated_service(account=DEFAULT_ACCOUNT):
    """Authentifiziert den Nutzer via OAuth2 für den angegebenen Account/Kanal
    und gibt den API-Client zurück.

    Beim ersten Aufruf für einen Account öffnet sich ein Browserfenster zur
    Anmeldung; bei mehreren Marken-Kanälen unter demselben Google-Konto
    zeigt Google dabei eine Kanalauswahl. Danach wird das Token lokal unter
    tokens/<account>.json zwischengespeichert, sodass künftige Läufe (z. B.
    in einem Cronjob) ohne erneute Anmeldung funktionieren.
    """
    os.makedirs(TOKEN_DIR, exist_ok=True)
    token_path = os.path.join(TOKEN_DIR, f"{account}.json")

    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, [YOUTUBE_UPLOAD_SCOPE])

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRETS_FILE):
                exit(MISSING_CLIENT_SECRETS_MESSAGE)
            print(f"Login für Account '{account}' erforderlich – Browser öffnet sich...")
            flow = InstalledAppFlow.from_client_secrets_file(
                CLIENT_SECRETS_FILE, [YOUTUBE_UPLOAD_SCOPE]
            )
            creds = flow.run_local_server(port=0)

        # Token für künftige, nicht-interaktive Läufe speichern.
        with open(token_path, "w") as token:
            token.write(creds.to_json())

    return build(YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION, credentials=creds)


def download_to_tempfile(url):
    """Lädt eine Datei von einer URL in eine temporäre Datei und gibt deren
    Pfad zurück. Der Aufrufer ist dafür verantwortlich, die Datei danach
    wieder zu löschen."""
    print(f"Lade Video von {url} herunter...")
    suffix = os.path.splitext(url.split("?")[0])[1] or ".mp4"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)

    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with open(tmp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8 * 1024 * 1024):
                if chunk:
                    f.write(chunk)

    print(f"Download abgeschlossen: {tmp_path}")
    return tmp_path


def upload_file(youtube, file_path, title, description, keywords, category, privacy_status):
    tags = keywords.split(",") if keywords else None

    body = dict(
        snippet=dict(
            title=title,
            description=description,
            tags=tags,
            categoryId=category,
        ),
        status=dict(privacyStatus=privacy_status),
    )

    insert_request = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        # chunksize=-1 lädt die Datei in einem einzigen HTTP-Request hoch
        # (bei Fehlern wird trotzdem ab der letzten Stelle fortgesetzt).
        # Für instabile Verbindungen z. B. 1024*1024 (1 MB) verwenden.
        media_body=MediaFileUpload(file_path, chunksize=-1, resumable=True),
    )

    return resumable_upload(insert_request)


def initialize_upload(options):
    """Verarbeitet ein einzelnes Video: entweder eine lokale Datei (--file)
    oder eine URL (--url), die zunächst heruntergeladen wird."""
    youtube = get_authenticated_service(options.account)
    tmp_path = None
    try:
        if options.url:
            file_path = tmp_path = download_to_tempfile(options.url)
        else:
            file_path = options.file

        return upload_file(
            youtube,
            file_path,
            options.title,
            options.description,
            options.keywords,
            options.category,
            options.privacyStatus,
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def load_uploaded_ids():
    if os.path.exists(UPLOADED_STATE_FILE):
        with open(UPLOADED_STATE_FILE, "r") as f:
            return set(json.load(f))
    return set()


def mark_uploaded(video_id):
    uploaded = load_uploaded_ids()
    uploaded.add(video_id)
    with open(UPLOADED_STATE_FILE, "w") as f:
        json.dump(sorted(uploaded), f, indent=2)


def process_manifest(manifest_path, defaults):
    with open(manifest_path, "r") as f:
        entries = json.load(f)

    uploaded_ids = load_uploaded_ids()
    results = []
    youtube_services = {}  # Cache: account -> authentifizierter Service

    for entry in entries:
        entry_id = entry.get("id") or entry["url"]

        if entry_id in uploaded_ids:
            print(f"Überspringe '{entry_id}' (bereits hochgeladen).")
            continue

        account = entry.get("account", getattr(defaults, "account", DEFAULT_ACCOUNT))
        if account not in youtube_services:
            youtube_services[account] = get_authenticated_service(account)
        youtube = youtube_services[account]

        tmp_path = None
        try:
            tmp_path = download_to_tempfile(entry["url"])
            youtube_video_id = upload_file(
                youtube,
                tmp_path,
                entry.get("title", defaults.title),
                entry.get("description", defaults.description),
                entry.get("keywords", defaults.keywords),
                entry.get("category", defaults.category),
                entry.get("privacyStatus", defaults.privacyStatus),
            )
            mark_uploaded(entry_id)
            results.append((entry_id, account, youtube_video_id))
        except Exception as e:
            # Ein fehlgeschlagenes Video soll den Rest des Batches nicht
            # abbrechen; es bleibt unmarkiert und wird beim nächsten
            # Cron-Lauf erneut versucht.
            print(f"Fehler bei '{entry_id}' (Account '{account}'): {e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    return results


def resumable_upload(insert_request):
    """Lädt die Datei mit exponentiellem Backoff bei Fehlern hoch."""
    response = None
    error = None
    retry = 0

    while response is None:
        try:
            print("Video wird hochgeladen...")
            status, response = insert_request.next_chunk()
            if response is not None:
                if "id" in response:
                    print(f"Video mit der ID '{response['id']}' wurde erfolgreich hochgeladen.")
                    return response["id"]
                else:
                    exit(f"Upload fehlgeschlagen, unerwartete Antwort: {response}")
        except HttpError as e:
            if e.resp.status in RETRIABLE_STATUS_CODES:
                error = f"Ein wiederholbarer HTTP-Fehler {e.resp.status} ist aufgetreten:\n{e.content}"
            else:
                raise
        except RETRIABLE_EXCEPTIONS as e:
            error = f"Ein wiederholbarer Fehler ist aufgetreten: {e}"

        if error is not None:
            print(error)
            retry += 1
            if retry > MAX_RETRIES:
                exit("Maximale Anzahl an Versuchen erreicht, breche ab.")

            max_sleep = 2 ** retry
            sleep_seconds = random.random() * max_sleep
            print(f"Warte {sleep_seconds:.1f} Sekunden und versuche es erneut...")
            time.sleep(sleep_seconds)
            error = None


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Lädt Video(s) auf YouTube hoch.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="Pfad zu einer lokalen Video-Datei")
    source.add_argument("--url", help="URL zu einem Video in der Cloud (wird heruntergeladen)")
    source.add_argument(
        "--manifest",
        help="Pfad zu einer JSON-Datei mit mehreren Videos (Batch-/Cronjob-Betrieb)",
    )
    parser.add_argument(
        "--account",
        default=DEFAULT_ACCOUNT,
        help="Name des Accounts/Kanals (bestimmt tokens/<account>.json). "
        "Bei mehreren Marken-Kanälen pro Kanal einen eigenen Namen vergeben.",
    )
    parser.add_argument("--title", default="Test Title", help="Titel des Videos")
    parser.add_argument("--description", default="Test Description", help="Beschreibung des Videos")
    parser.add_argument(
        "--category",
        default="22",
        help="Numerische YouTube-Kategorie-ID (siehe videoCategories.list). "
        "22 = People & Blogs",
    )
    parser.add_argument("--keywords", default="", help="Kommagetrennte Keywords")
    parser.add_argument(
        "--privacyStatus",
        choices=VALID_PRIVACY_STATUSES,
        default="private",
        help="Sichtbarkeit des Videos (Standard: private, zur Sicherheit beim Testen)",
    )
    return parser


if __name__ == "__main__":
    args = build_arg_parser().parse_args()

    if args.file and not os.path.exists(args.file):
        exit("Bitte gib mit --file= eine gültige Datei an.")

    try:
        if args.manifest:
            results = process_manifest(args.manifest, args)
            print(f"\n{len(results)} Video(s) in diesem Lauf hochgeladen.")
            for entry_id, account, video_id in results:
                print(f"  [{account}] {entry_id} -> https://youtu.be/{video_id}")
        else:
            initialize_upload(args)
    except HttpError as e:
        print(f"Ein HTTP-Fehler {e.resp.status} ist aufgetreten:\n{e.content}")
