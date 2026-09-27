import json
import os
from datetime import datetime, timezone

from flask import Flask, render_template, request, redirect, url_for, flash, session
from werkzeug.middleware.proxy_fix import ProxyFix
from supabase import create_client
from apscheduler.schedulers.background import BackgroundScheduler
from google_auth_oauthlib.flow import Flow

from poster import post_next_due_videos

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
app.config["PREFERRED_URL_SCHEME"] = "https"
# Render sitzt hinter einem Proxy; ohne das erkennt Flask HTTPS nicht korrekt,
# was den OAuth-Redirect (unten) kaputt machen würde.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
GOOGLE_CLIENT_SECRETS = os.environ.get("GOOGLE_CLIENT_SECRETS")


# ---------- Navigation / Tabs ----------

@app.route("/")
def single_post():
    accounts = supabase.table("accounts").select("*").order("name").execute().data
    return render_template("single_post.html", accounts=accounts, active_tab="post")


@app.route("/new-campaign", methods=["GET", "POST"])
def new_campaign():
    accounts = supabase.table("accounts").select("*").order("name").execute().data
    categories = sorted({a["category"] for a in accounts if a.get("category")})
    existing_campaigns = (
        supabase.table("campaigns")
        .select("id,name")
        .eq("status", "active")
        .order("name")
        .execute()
        .data
    )

    if request.method == "POST":
        form = request.form
        existing_campaign_id = form.get("existing_campaign_id") or None

        video_urls = [
            line.strip()
            for line in form.get("video_urls", "").splitlines()
            if line.strip()
        ]

        if existing_campaign_id:
            campaign_id = existing_campaign_id
        else:
            target_type = "single_account" if form.get("target") == "account" else "category"

            campaign = {
                "name": form["name"],
                "target_type": target_type,
                "account_id": form.get("account_id") if target_type == "single_account" else None,
                "category": form.get("category") if target_type == "category" else None,
                "interval_hours": float(form.get("interval_hours") or 6),
                "caption": form.get("caption") or None,
            }
            result = supabase.table("campaigns").insert(campaign).execute()
            campaign_id = result.data[0]["id"]

        # Aktuelle höchste Position in der Warteschlange ermitteln, damit neue
        # Videos hinten angehängt werden statt vorhandene zu überschreiben.
        existing = (
            supabase.table("videos")
            .select("position")
            .eq("campaign_id", campaign_id)
            .order("position", desc=True)
            .limit(1)
            .execute()
            .data
        )
        next_position = (existing[0]["position"] + 1) if existing else 0

        rows = [
            {"campaign_id": campaign_id, "url": url, "position": next_position + i}
            for i, url in enumerate(video_urls)
        ]
        if rows:
            supabase.table("videos").insert(rows).execute()

        flash(f"Kampagne gespeichert, {len(rows)} Video(s) zur Warteschlange hinzugefügt.")
        return redirect(url_for("campaigns"))

    return render_template(
        "new_campaign.html",
        accounts=accounts,
        categories=categories,
        existing_campaigns=existing_campaigns,
        active_tab="new_campaign",
    )


@app.route("/campaigns")
def campaigns():
    rows = (
        supabase.table("campaigns")
        .select("*, accounts(name)")
        .order("created_at", desc=True)
        .execute()
        .data
    )
    for c in rows:
        counts = (
            supabase.table("videos")
            .select("status", count="exact")
            .eq("campaign_id", c["id"])
            .eq("status", "queued")
            .execute()
        )
        c["queued_count"] = counts.count or 0
    return render_template("campaigns.html", campaigns=rows, active_tab="campaigns")


@app.route("/campaigns/<campaign_id>/pause", methods=["POST"])
def pause_campaign(campaign_id):
    supabase.table("campaigns").update({"status": "paused"}).eq("id", campaign_id).execute()
    return redirect(url_for("campaigns"))


@app.route("/campaigns/<campaign_id>/resume", methods=["POST"])
def resume_campaign(campaign_id):
    supabase.table("campaigns").update({"status": "active"}).eq("id", campaign_id).execute()
    return redirect(url_for("campaigns"))


@app.route("/inbox")
def inbox():
    rows = (
        supabase.table("videos")
        .select("*, campaigns(name)")
        .eq("status", "failed")
        .order("created_at", desc=True)
        .execute()
        .data
    )
    return render_template("inbox.html", failed_videos=rows, active_tab="inbox")


# ---------- YouTube-Login (einmalig pro Account, direkt im Browser) ----------

@app.route("/oauth")
def oauth_start_page():
    """Einstiegsseite: Account-Namen eingeben, dann Google-Login starten."""
    accounts = supabase.table("accounts").select("name,youtube_token_key").execute().data
    return render_template("oauth_start.html", accounts=accounts, active_tab="oauth")


@app.route("/oauth/start")
def oauth_start():
    if not GOOGLE_CLIENT_SECRETS:
        return "Umgebungsvariable GOOGLE_CLIENT_SECRETS ist nicht gesetzt.", 500

    account = request.args.get("account", "default").strip()
    flow = Flow.from_client_config(
        json.loads(GOOGLE_CLIENT_SECRETS),
        scopes=[YOUTUBE_UPLOAD_SCOPE],
        redirect_uri=url_for("oauth_callback", _external=True),
    )
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",  # erzwingt, dass ein refresh_token mitgeliefert wird
        include_granted_scopes="true",
    )
    session["oauth_state"] = state
    session["oauth_account"] = account
    return redirect(auth_url)


@app.route("/oauth/callback")
def oauth_callback():
    if not GOOGLE_CLIENT_SECRETS:
        return "Umgebungsvariable GOOGLE_CLIENT_SECRETS ist nicht gesetzt.", 500

    state = session.get("oauth_state")
    account = session.get("oauth_account", "default")

    flow = Flow.from_client_config(
        json.loads(GOOGLE_CLIENT_SECRETS),
        scopes=[YOUTUBE_UPLOAD_SCOPE],
        state=state,
        redirect_uri=url_for("oauth_callback", _external=True),
    )
    flow.fetch_token(authorization_response=request.url)

    token_json = flow.credentials.to_json()
    env_name = f"YT_TOKEN_{account.upper().replace('-', '_')}"

    return render_template(
        "oauth_done.html",
        token_json=token_json,
        env_name=env_name,
        account=account,
        active_tab="oauth",
    )


# ---------- Hintergrund-Scheduler: postet fällige Videos ----------

def run_scheduler_job():
    with app.app_context():
        posted = post_next_due_videos(supabase)
        if posted:
            print(f"[scheduler] {len(posted)} Video(s) gepostet: {posted}")


scheduler = BackgroundScheduler()
scheduler.add_job(run_scheduler_job, "interval", minutes=15, next_run_time=datetime.now(timezone.utc))
scheduler.start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
