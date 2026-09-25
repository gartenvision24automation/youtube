# GartenVision24 Automation

Dashboard zum Anlegen von Video-Kampagnen (Warteschlange + Intervall-Posting) auf YouTube, Deployment auf Render, Datenhaltung in Supabase.

## 1. Supabase einrichten

1. Projekt auf supabase.com anlegen (falls noch nicht vorhanden).
2. Im SQL-Editor `schema.sql` einmal ausführen.
3. Unter Project Settings → API: `Project URL` und `service_role` Key kopieren
   (den **service_role**-Key, nicht den `anon`-Key, da die App serverseitig
   schreibt/liest).
4. In der Tabelle `accounts` deine Accounts eintragen, z. B.:
   ```
   name: gartenvision24.de
   category: Garten
   platform: youtube
   youtube_token_key: gartenvision24
   ```

## 2. YouTube-Tokens erzeugen

Für jeden Account einmal **lokal** (nicht auf Render):

```
pip install google-api-python-client google-auth-oauthlib google-auth-httplib2 requests
python upload_video.py --account="gartenvision24" --file="test.mp4"
```

Der Wert bei `--account` muss zum `youtube_token_key` des Accounts in
Supabase passen. Das erzeugt `tokens/gartenvision24.json` – dessen Inhalt
brauchst du gleich als Environment-Variable.

## 3. Auf Render deployen

1. Repo mit diesem Code auf GitHub pushen (Render kann direkt von dort deployen).
2. Auf render.com: "New" → "Blueprint" → Repo auswählen (nutzt `render.yaml`).
3. Environment-Variablen setzen:
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_KEY`
   - Pro Account: `YT_TOKEN_<ACCOUNT_KEY_GROSSGESCHRIEBEN>` = kompletter Inhalt
     der jeweiligen `tokens/<account>.json`
     (z. B. `YT_TOKEN_GARTENVISION24`)
4. Deploy starten. Die App läuft dann unter `https://<dein-service>.onrender.com`.

## 4. Funktionsweise

- **Neue Kampagne**: Formular speichert Kampagne + Video-Warteschlange in Supabase.
- **Hintergrund-Scheduler**: läuft alle 15 Minuten innerhalb der Web-App und
  prüft, ob laut `interval_hours` einer Kampagne ein neues Video fällig ist;
  postet dann das nächste Video der Warteschlange.
- **Kampagnen**: Übersicht mit Status, Warteschlangen-Länge, Pause/Fortsetzen.
- **Postfach**: zeigt fehlgeschlagene Uploads zur manuellen Prüfung.

⚠️ Hinweis zum Render Free/Starter-Plan: Der Dienst kann bei Inaktivität in
den Schlafmodus gehen, wodurch der interne Scheduler pausiert. Für
zuverlässiges Timing empfiehlt sich zusätzlich ein Render Cron Job (oder ein
externer Pinger), der z. B. stündlich eine `/health`-Route aufruft, damit die
App wach bleibt – sag Bescheid, wenn ich das ergänzen soll.

## 5. Bekannte Lücken / nächste Schritte

- "Einzelner Post" postet aktuell nur das Formular, aber ruft noch keinen
  Upload aus – bei Bedarf ergänze ich das (analog zu `poster.py`).
- Aktuell nur YouTube integriert. Falls "Reels" auch Instagram/TikTok meint,
  bräuchten wir dafür jeweils eigene API-Anbindungen.
- Titelbild-Logik (Titelbild-URL / Video-Sekunde) wird aktuell gespeichert,
  aber beim YouTube-Upload noch nicht als Thumbnail gesetzt – YouTube
  erlaubt das über `thumbnails().set()`, das kann ich ergänzen.
