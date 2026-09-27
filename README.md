[README.md](https://github.com/user-attachments/files/32700725/README.md)
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

## 2. Google Cloud OAuth einrichten (Web statt Desktop!)

Für den Login direkt im Dashboard (ohne lokales Python) brauchst du einen
OAuth-Client vom Typ **"Web-Anwendung"**, nicht "Desktop-App":

1. In der Google Cloud Console → APIs & Dienste → Anmeldedaten →
   "OAuth-Client-ID erstellen" → Anwendungstyp **"Webanwendung"**.
2. Bei "Autorisierte Redirect-URIs" eintragen:
   `https://<dein-render-service>.onrender.com/oauth/callback`
   (die genaue Render-URL bekommst du nach dem ersten Deploy in Schritt 3;
   du kannst den OAuth-Client danach jederzeit bearbeiten und die URI
   nachtragen).
3. Die heruntergeladene JSON-Datei **nicht als Datei ablegen**, sondern
   ihren kompletten Inhalt gleich als Environment-Variable
   `GOOGLE_CLIENT_SECRETS` bei Render eintragen (Schritt 3).

## 3. Auf Render deployen

1. Repo mit diesem Code auf GitHub pushen (bereits erledigt).
2. Auf render.com: "New" → "Blueprint" → Repo auswählen (nutzt `render.yaml`).
3. Environment-Variablen setzen:
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_KEY`
   - `GOOGLE_CLIENT_SECRETS` (kompletter Inhalt der OAuth-Client-JSON aus Schritt 2)
4. Deploy starten. Die App läuft dann unter `https://<dein-service>.onrender.com`.
5. Falls in Schritt 2 die Redirect-URI noch fehlte: jetzt in der Google
   Cloud Console beim OAuth-Client nachtragen:
   `https://<dein-service>.onrender.com/oauth/callback`.

## 4. YouTube-Accounts verbinden (direkt im Browser, kein lokales Python)

1. In Supabase in der Tabelle `accounts` einen Account anlegen, z. B.
   `name: gartenvision24.de`, `youtube_token_key: gartenvision24`.
2. Im Dashboard oben rechts auf "🔑 YouTube-Login" klicken (oder direkt
   `https://<dein-service>.onrender.com/oauth` öffnen).
3. Account-Namen eingeben (muss exakt `youtube_token_key` entsprechen),
   auf "Mit Google anmelden" klicken, im Google-Consent-Screen den
   gewünschten Kanal auswählen und bestätigen.
4. Die folgende Seite zeigt dir Variablenname (z. B. `YT_TOKEN_GARTENVISION24`)
   und Wert zum Kopieren.
5. Bei Render unter "Environment" diese Variable hinzufügen und speichern
   (Render startet den Dienst danach automatisch neu).
6. Für jeden weiteren Account Schritte 2–5 wiederholen.

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
