"""
Prüft alle aktiven Kampagnen, ob laut Intervall ein neues Video fällig ist,
und lädt in diesem Fall das nächste Video aus der Warteschlange zu YouTube
hoch (Wiederverwendung der Multi-Account-Logik aus upload_video.py).

Bei Problemen (Upload-Fehler, ungültiger Token, überfällige Kampagne) wird
zusätzlich eine E-Mail verschickt (siehe notifier.py).
"""

import os
from datetime import datetime, timedelta, timezone

from google.auth.exceptions import RefreshError

from notifier import send_alert
from youtube_upload import get_authenticated_service, upload_file, download_to_tempfile


def _parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_due(campaign):
    if not campaign.get("last_posted_at"):
        return True
    last = _parse_ts(campaign["last_posted_at"])
    return datetime.now(timezone.utc) >= last + timedelta(hours=campaign["interval_hours"])


def _resolve_accounts(supabase, campaign):
    """Gibt die Liste der Accounts zurück, auf die diese Kampagne postet."""
    if campaign["target_type"] == "single_account":
        acc = supabase.table("accounts").select("*").eq("id", campaign["account_id"]).execute().data
        return acc
    else:
        return (
            supabase.table("accounts")
            .select("*")
            .eq("category", campaign["category"])
            .execute()
            .data
        )


def post_next_due_videos(supabase):
    posted_ids = []

    campaigns = supabase.table("campaigns").select("*").eq("status", "active").execute().data

    for campaign in campaigns:
        if not _is_due(campaign):
            continue

        next_video_result = (
            supabase.table("videos")
            .select("*")
            .eq("campaign_id", campaign["id"])
            .eq("status", "queued")
            .order("position")
            .limit(1)
            .execute()
        )
        if not next_video_result.data:
            # Warteschlange leer -> von vorne beginnen (Endlosschleife):
            # alle Videos dieser Kampagne zurück auf 'queued' setzen, damit
            # der nächste Durchlauf direkt wieder beim ersten Video startet.
            supabase.table("videos").update(
                {
                    "status": "queued",
                    "youtube_video_id": None,
                    "posted_at": None,
                    "error_message": None,
                }
            ).eq("campaign_id", campaign["id"]).neq("status", "queued").execute()

            next_video_result = (
                supabase.table("videos")
                .select("*")
                .eq("campaign_id", campaign["id"])
                .order("position")
                .limit(1)
                .execute()
            )
            if not next_video_result.data:
                # Kampagne hat tatsächlich gar keine Videos -> nichts zu tun.
                continue

        video = next_video_result.data[0]
        accounts = _resolve_accounts(supabase, campaign)

        if not accounts:
            # Kein Account gefunden (falsche/fehlende account_id oder Kategorie
            # ohne zugeordnete Accounts) -> klar als Fehler markieren, statt
            # fälschlicherweise "posted" zu setzen.
            message = (
                f"Kein passender Account gefunden (target_type="
                f"{campaign['target_type']}, account_id="
                f"{campaign.get('account_id')}, category="
                f"{campaign.get('category')})."
            )
            supabase.table("videos").update(
                {"status": "failed", "error_message": message}
            ).eq("id", video["id"]).execute()
            send_alert(
                f"noaccount:{campaign['id']}",
                f"Kampagne \"{campaign['name']}\": kein Account gefunden",
                message,
            )
            continue

        last_youtube_id = None
        last_error = None
        upload_succeeded = False
        auth_failed_accounts = []

        for account in accounts:
            tmp_path = None
            try:
                tmp_path = download_to_tempfile(video["url"])
                youtube = get_authenticated_service(account["youtube_token_key"] or account["name"])
                last_youtube_id = upload_file(
                    youtube,
                    tmp_path,
                    title=campaign["name"],
                    description=campaign.get("caption") or "",
                    keywords="",
                    category="22",
                    privacy_status="public",
                )
                upload_succeeded = True
            except RefreshError as e:
                # Token ungültig/widerrufen: kein Problem des Videos, sondern
                # des Logins -> später Kampagne pausieren statt Video verbrennen.
                last_error = str(e)
                auth_failed_accounts.append(account["name"])
                continue
            except Exception as e:
                last_error = str(e)
                continue
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.remove(tmp_path)

        if auth_failed_accounts:
            send_alert(
                f"auth:{campaign['id']}",
                f"YouTube-Login ungültig ({', '.join(auth_failed_accounts)})",
                "Der gespeicherte YouTube-Token wurde von Google abgelehnt:\n"
                f"{last_error}\n\n"
                f"Kampagne: {campaign['name']}\n"
                f"Betroffene Accounts: {', '.join(auth_failed_accounts)}\n\n"
                "Lösung: /oauth aufrufen, neu einloggen, den neuen Token bei "
                "Render in YT_TOKEN_<ACCOUNT> eintragen"
                + (
                    " und die Kampagne im Tab 'Kampagnen' fortsetzen."
                    if not upload_succeeded
                    else "."
                ),
            )

        if upload_succeeded:
            supabase.table("videos").update(
                {
                    "status": "posted",
                    "posted_at": datetime.now(timezone.utc).isoformat(),
                    "youtube_video_id": last_youtube_id,
                }
            ).eq("id", video["id"]).execute()
            supabase.table("campaigns").update(
                {"last_posted_at": datetime.now(timezone.utc).isoformat()}
            ).eq("id", campaign["id"]).execute()
            posted_ids.append(last_youtube_id)
        elif auth_failed_accounts:
            # Das Video bleibt in der Warteschlange ('queued'); die Kampagne
            # wird pausiert, damit nicht bei jedem Scheduler-Lauf ein weiteres
            # Video als fehlgeschlagen markiert wird.
            supabase.table("campaigns").update({"status": "paused"}).eq(
                "id", campaign["id"]
            ).execute()
        else:
            # Upload bei ALLEN Accounts fehlgeschlagen -> als failed markieren.
            # last_posted_at bewusst NICHT aktualisieren, damit die Kampagne
            # beim nächsten Scheduler-Lauf erneut fällig ist.
            error_text = last_error or "Unbekannter Fehler beim Upload."
            supabase.table("videos").update(
                {"status": "failed", "error_message": error_text}
            ).eq("id", video["id"]).execute()
            send_alert(
                f"uploadfail:{campaign['id']}",
                f"Upload fehlgeschlagen: {campaign['name']}",
                f"Kampagne: {campaign['name']}\nVideo: {video['url']}\n\nFehler:\n{error_text}",
            )

    return posted_ids


def check_overdue_campaigns(supabase):
    """Meldet aktive Kampagnen, die deutlich länger nichts gepostet haben als
    ihr Intervall erlaubt (Intervall x2 + 1 Stunde Toleranz). Fängt auch
    stille Fehler ab, bei denen kein einzelner Upload-Fehler gemeldet wurde,
    und schlägt nach einem Ausfall der Instanz beim nächsten Start an."""
    now = datetime.now(timezone.utc)
    campaigns = supabase.table("campaigns").select("*").eq("status", "active").execute().data

    for campaign in campaigns:
        interval = timedelta(hours=float(campaign["interval_hours"]))
        tolerance = interval * 2 + timedelta(hours=1)

        if campaign.get("last_posted_at"):
            reference = _parse_ts(campaign["last_posted_at"])
            what = "zuletzt gepostet"
        elif campaign.get("created_at"):
            reference = _parse_ts(campaign["created_at"])
            what = "angelegt"
        else:
            continue

        if now - reference > tolerance:
            hours = (now - reference).total_seconds() / 3600
            send_alert(
                f"overdue:{campaign['id']}",
                f"Kampagne \"{campaign['name']}\" postet nicht mehr",
                f"Die Kampagne \"{campaign['name']}\" (Intervall "
                f"{campaign['interval_hours']} h) wurde {what} vor "
                f"{hours:.0f} Stunden und ist damit deutlich überfällig.\n\n"
                "Bitte im Dashboard unter 'Postfach' und in den Render-Logs "
                "nach der Ursache sehen.",
            )
