"""
E-Mail-Benachrichtigung bei Problemen (Upload-Fehler, ungültiger Token,
Kampagne überfällig).

Versand über die Resend-HTTP-API (https://resend.com), NICHT über SMTP:
Render blockiert auf Free-Instanzen ausgehende SMTP-Ports (25/465/587),
eine normale SMTP-Anbindung würde dort also nie ankommen.

Benötigte Environment-Variablen bei Render:
  RESEND_API_KEY   API-Key aus dem Resend-Dashboard
  ALERT_EMAIL_TO   Empfängeradresse (mit dem Absender onboarding@resend.dev
                   erlaubt Resend nur die E-Mail-Adresse, mit der du dich bei
                   Resend registriert hast – dort also dieselbe Adresse nutzen)
Optional:
  ALERT_EMAIL_FROM Absender, Standard: "GartenVision24 <onboarding@resend.dev>"
  ALERT_COOLDOWN_HOURS  Mindestabstand pro Meldungstyp, Standard: 12

Fehlt eine der Pflichtvariablen, wird nur ins Log geschrieben – die App läuft
normal weiter. Ein fehlgeschlagener Mailversand wirft nie eine Exception.
"""

import os
import time

import requests

_last_sent = {}  # key -> Unix-Zeitstempel; geht bei Neustart verloren (ok)


def send_alert(key, subject, body, cooldown_hours=None):
    """Schickt eine Mail, höchstens einmal pro `key` innerhalb der Cooldown-
    Zeit, damit ein Dauerfehler nicht alle 15 Minuten eine Mail auslöst.
    Gibt True zurück, wenn tatsächlich eine Mail verschickt wurde."""
    if cooldown_hours is None:
        cooldown_hours = float(os.environ.get("ALERT_COOLDOWN_HOURS", "12"))

    now = time.time()
    last = _last_sent.get(key)
    if last is not None and now - last < cooldown_hours * 3600:
        return False

    api_key = os.environ.get("RESEND_API_KEY")
    to_addr = os.environ.get("ALERT_EMAIL_TO")
    if not api_key or not to_addr:
        print(f"[alert] (keine Mail konfiguriert) {subject}: {body}")
        return False

    sender = os.environ.get("ALERT_EMAIL_FROM", "GartenVision24 <onboarding@resend.dev>")
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "from": sender,
                "to": [a.strip() for a in to_addr.split(",") if a.strip()],
                "subject": f"[GartenVision24] {subject}",
                "text": body,
            },
            timeout=15,
        )
        if resp.status_code >= 300:
            print(f"[alert] Mailversand fehlgeschlagen ({resp.status_code}): {resp.text[:300]}")
            return False
    except Exception as e:  # nie den Scheduler wegen einer Mail abbrechen
        print(f"[alert] Mailversand fehlgeschlagen: {e}")
        return False

    _last_sent[key] = now
    print(f"[alert] Mail gesendet: {subject}")
    return True
