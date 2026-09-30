"""
Prüft alle aktiven Kampagnen, ob laut Intervall ein neues Video fällig ist,
und lädt in diesem Fall das nächste Video aus der Warteschlange zu YouTube
hoch (Wiederverwendung der Multi-Account-Logik aus upload_video.py).
"""

from datetime import datetime, timedelta, timezone

from youtube_upload import get_authenticated_service, upload_file, download_to_tempfile
import os


def _is_due(campaign):
    if not campaign.get("last_posted_at"):
        return True
    last = datetime.fromisoformat(campaign["last_posted_at"].replace("Z", "+00:00"))
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
            # Warteschlange leer -> Kampagne als erledigt markieren.
            supabase.table("campaigns").update({"status": "done"}).eq("id", campaign["id"]).execute()
            continue

        video = next_video_result.data[0]
        accounts = _resolve_accounts(supabase, campaign)

        if not accounts:
            # Kein Account gefunden (falsche/fehlende account_id oder Kategorie
            # ohne zugeordnete Accounts) -> klar als Fehler markieren, statt
            # fälschlicherweise "posted" zu setzen.
            supabase.table("videos").update(
                {
                    "status": "failed",
                    "error_message": (
                        f"Kein passender Account gefunden (target_type="
                        f"{campaign['target_type']}, account_id="
                        f"{campaign.get('account_id')}, category="
                        f"{campaign.get('category')})."
                    ),
                }
            ).eq("id", video["id"]).execute()
            continue

        last_youtube_id = None
        last_error = None
        upload_succeeded = False

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
            except Exception as e:
                last_error = str(e)
                continue
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.remove(tmp_path)

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
        else:
            # Upload bei ALLEN Accounts fehlgeschlagen -> als failed markieren.
            # last_posted_at bewusst NICHT aktualisieren, damit die Kampagne
            # beim nächsten Scheduler-Lauf erneut (mit demselben Video) fällig
            # ist, statt das Video stillschweigend zu verlieren.
            supabase.table("videos").update(
                {"status": "failed", "error_message": last_error or "Unbekannter Fehler beim Upload."}
            ).eq("id", video["id"]).execute()

    return posted_ids
