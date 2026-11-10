#!/usr/bin/env bash
# Deploys the engine to Google Cloud: the API on Cloud Run, a Cloud SQL Postgres database, the
# keys in Secret Manager, scheduled jobs for the email inbox, Drive, ClickUp and Slack, and the
# dashboard as a second service behind Google sign-in (IAP).
#
#   PROJECT=your-gcp-project DASHBOARD_ACCESS=domain:acme.com ./deploy/deploy.sh
#
# Keys are secrets, not files. Pass any you have as environment variables the first time
# (ANTHROPIC_API_KEY, OPENAI_API_KEY, SLACK_BOT_TOKEN...; see SECRET_KEYS below). They're
# stored in Secret Manager as tle-<name>, and later runs reuse what's there. TLE_API_TOKEN is
# generated if there isn't one, and if there's no model key the script asks which AI to use
# (Claude, OpenAI or Gemini) and for its key.
#
# Everything else (company, people, themes, models, sources) is set in the dashboard. The
# service creates its tables when it starts.
#
# DASHBOARD_ACCESS is who may open the dashboard: domain:acme.com, group:team@acme.com or
# user:jane@acme.com (comma-separated for several). Set DASHBOARD=0 to skip the dashboard.
# DATABASE_URL, if set, is used instead of creating Cloud SQL (Neon, Supabase, your own server).
#
# Run from the repo root after `gcloud auth login`.
set -euo pipefail

PROJECT="${PROJECT:?Set PROJECT to your GCP project ID}"
REGION="${REGION:-europe-west2}"
SERVICE="${SERVICE:-thought-leadership}"
SA_NAME="${SA_NAME:-tle-runner}"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
DRIVE_SCHEDULE="${DRIVE_SCHEDULE:-0 * * * *}"    # hourly; set to "" to skip the Drive job
CLICKUP_SCHEDULE="${CLICKUP_SCHEDULE:-*/15 * * * *}"  # every 15 minutes; set to "" to skip the ClickUp job
SLACK_SCHEDULE="${SLACK_SCHEDULE:-*/15 * * * *}"      # every 15 minutes, for Slack channels saved as sessions; "" to skip
EMAIL_SCHEDULE="${EMAIL_SCHEDULE:-*/5 * * * *}"  # every 5 minutes; set to "" to skip the email job
DASHBOARD="${DASHBOARD:-1}"
DASHBOARD_SERVICE="${SERVICE}-dashboard"
DASHBOARD_ACCESS="${DASHBOARD_ACCESS:-}"
SQL_INSTANCE="${SQL_INSTANCE:-${SERVICE}-db}"
DB_TIER="${DB_TIER:-db-f1-micro}"
DB_NAME=thought_leadership
DB_USER=tle

# The secrets the engine reads. Each is stored as tle-<name> (e.g. tle-anthropic-api-key).
SECRET_KEYS=(TLE_API_TOKEN ANTHROPIC_API_KEY ANTHROPIC_WORKSPACE_ID OPENAI_API_KEY GEMINI_API_KEY
             SLACK_BOT_TOKEN SLACK_SIGNING_SECRET CLICKUP_API_TOKEN)

# Who may open the dashboard, checked now rather than after the slow steps.
if [[ -n "$DASHBOARD_ACCESS" ]]; then
  IFS=',' read -ra MEMBERS <<< "$DASHBOARD_ACCESS"
  for member in "${MEMBERS[@]}"; do
    member=$(printf '%s' "$member" | tr '[:upper:]' '[:lower:]')
    if ! [[ "$member" =~ ^domain:[a-z0-9.-]+\.[a-z]{2,}$ || "$member" =~ ^(user|group):[^@[:space:]]+@[a-z0-9.-]+\.[a-z]{2,}$ ]]; then
      echo "DASHBOARD_ACCESS has \"$member\", which isn't domain:yourcompany.com, group:team@yourcompany.com or"
      echo "user:name@yourcompany.com. Separate several with commas, no spaces."
      exit 1
    fi
    # A Google Workspace domain receives mail, so one with no mail servers is most likely a typo.
    domain=${member#*:}; domain=${domain#*@}
    if command -v dig >/dev/null && [[ -z "${ACCESS_UNCHECKED:-}" ]] && [[ -z "$(dig +short MX "$domain")" ]]; then
      echo "$domain has no mail servers, so it doesn't look like a Google Workspace domain. Check the"
      echo "spelling in DASHBOARD_ACCESS (or set ACCESS_UNCHECKED=1 to go ahead anyway)."
      exit 1
    fi
  done
fi

gcloud config set project "$PROJECT" >/dev/null
CLOUD_SQL=""
[[ -z "${DATABASE_URL:-}" ]] && CLOUD_SQL=1

echo "==> Enabling APIs"
APIS=(run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com
      drive.googleapis.com cloudscheduler.googleapis.com gmail.googleapis.com iamcredentials.googleapis.com
      iap.googleapis.com cloudresourcemanager.googleapis.com)
ROLES=(roles/secretmanager.secretAccessor roles/run.invoker)
[[ -n "$CLOUD_SQL" ]] && APIS+=(sqladmin.googleapis.com) && ROLES+=(roles/cloudsql.client)
gcloud services enable "${APIS[@]}"

echo "==> Service account"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" --display-name "Thought Leadership Engine"
for role in "${ROLES[@]}"; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" --role "$role" \
    --condition=None >/dev/null
done
# Lets the service account sign its delegated Gmail token (it has no key file).
gcloud iam service-accounts add-iam-policy-binding "$SA" --member "serviceAccount:$SA" \
  --role roles/iam.serviceAccountTokenCreator >/dev/null

echo "==> Secrets"
secret_name() { echo "tle-$(echo "$1" | tr '[:upper:]_' '[:lower:]-')"; }
store_secret() {  # name, value: creates the secret or adds a version, never printing the value
  if gcloud secrets describe "$1" >/dev/null 2>&1; then
    printf '%s' "$2" | gcloud secrets versions add "$1" --data-file=- >/dev/null
  else
    printf '%s' "$2" | gcloud secrets create "$1" --data-file=- --replication-policy=automatic >/dev/null
  fi
}
gcloud secrets describe "$(secret_name TLE_API_TOKEN)" >/dev/null 2>&1 || [[ -n "${TLE_API_TOKEN:-}" ]] || \
  TLE_API_TOKEN=$(openssl rand -hex 32)
if ! { gcloud secrets describe "$(secret_name ANTHROPIC_API_KEY)" >/dev/null 2>&1 || [[ -n "${ANTHROPIC_API_KEY:-}" ]] || \
       gcloud secrets describe "$(secret_name OPENAI_API_KEY)" >/dev/null 2>&1 || [[ -n "${OPENAI_API_KEY:-}" ]] || \
       gcloud secrets describe "$(secret_name GEMINI_API_KEY)" >/dev/null 2>&1 || [[ -n "${GEMINI_API_KEY:-}" ]]; } \
   && [[ -t 0 ]]; then
  echo "    Which AI should read and tag what comes in? You can change it later under Models in Settings."
  echo "      1) Anthropic (Claude)   2) OpenAI   3) Google Gemini"
  read -rp "    Pick 1, 2 or 3 [1]: " choice
  case "${choice:-1}" in
    2) KEY_NAME=OPENAI_API_KEY; WHERE="platform.openai.com/api-keys" ;;
    3) KEY_NAME=GEMINI_API_KEY; WHERE="aistudio.google.com/apikey" ;;
    *) KEY_NAME=ANTHROPIC_API_KEY; WHERE="console.anthropic.com" ;;
  esac
  read -rsp "    $KEY_NAME from $WHERE (not shown; Enter to skip): " key_value; echo
  [[ -n "$key_value" ]] && printf -v "$KEY_NAME" '%s' "$key_value" && export "${KEY_NAME?}"
  unset key_value
  echo "    Voice notes and recordings need OpenAI or Gemini; Claude doesn't take audio."
fi
SECRET_FLAGS=()
for key in "${SECRET_KEYS[@]}"; do
  name=$(secret_name "$key")
  if [[ -n "${!key:-}" ]]; then
    store_secret "$name" "${!key}"
    echo "    $key -> $name (updated)"
  elif ! gcloud secrets describe "$name" >/dev/null 2>&1; then
    continue
  fi
  SECRET_FLAGS+=("${key}=${name}:latest")
done

SERVICE_SQL=()  # flags that connect services and jobs to Cloud SQL, when it's used
JOB_SQL=()
if [[ -z "$CLOUD_SQL" ]]; then
  store_secret tle-database-url "$DATABASE_URL"
  SECRET_FLAGS+=("DATABASE_URL=tle-database-url:latest")
  echo "    DATABASE_URL -> tle-database-url (your own Postgres)"
else
  echo "==> Cloud SQL ($SQL_INSTANCE)"
  if ! gcloud sql instances describe "$SQL_INSTANCE" >/dev/null 2>&1; then
    echo "    Creating a Postgres instance; this takes several minutes."
    gcloud sql instances create "$SQL_INSTANCE" --database-version POSTGRES_16 --edition enterprise \
      --tier "$DB_TIER" --region "$REGION" --storage-size 10 --storage-auto-increase
  fi
  gcloud sql databases describe "$DB_NAME" --instance "$SQL_INSTANCE" >/dev/null 2>&1 || \
    gcloud sql databases create "$DB_NAME" --instance "$SQL_INSTANCE"
  CONNECTION=$(gcloud sql instances describe "$SQL_INSTANCE" --format 'value(connectionName)')
  # The password lives in the tle-database-url secret. The user is made to match it on every run,
  # so a database created after the secret (say, after an earlier attempt failed) still works.
  if gcloud secrets describe tle-database-url >/dev/null 2>&1; then
    DB_PASSWORD=$(gcloud secrets versions access latest --secret tle-database-url | sed -E 's#^postgresql://[^:]+:([^@]*)@.*#\1#')
  else
    DB_PASSWORD=$(openssl rand -hex 24)
  fi
  if gcloud sql users list --instance "$SQL_INSTANCE" --format 'value(name)' | grep -qx "$DB_USER"; then
    gcloud sql users set-password "$DB_USER" --instance "$SQL_INSTANCE" --password "$DB_PASSWORD" >/dev/null
  else
    gcloud sql users create "$DB_USER" --instance "$SQL_INSTANCE" --password "$DB_PASSWORD" >/dev/null
  fi
  # Cloud Run reaches the instance through a socket under /cloudsql.
  DB_URL="postgresql://${DB_USER}:${DB_PASSWORD}@/${DB_NAME}?host=/cloudsql/${CONNECTION}"
  if [[ "$(gcloud secrets versions access latest --secret tle-database-url 2>/dev/null)" != "$DB_URL" ]]; then
    store_secret tle-database-url "$DB_URL"
  fi
  unset DB_PASSWORD DB_URL
  SECRET_FLAGS+=("DATABASE_URL=tle-database-url:latest")
  SERVICE_SQL=(--add-cloudsql-instances "$CONNECTION")
  JOB_SQL=(--set-cloudsql-instances "$CONNECTION")
  echo "    DATABASE_URL -> tle-database-url"
fi
SECRETS=$(IFS=,; echo "${SECRET_FLAGS[*]}")

echo "==> Deploying $SERVICE"
# Unauthenticated at the Cloud Run level because Slack's webhook can't sign in;
# every endpoint except / and /health checks TLE_API_TOKEN or the Slack signature.
# CPU stays allocated so Slack messages finish tagging after the 3-second reply.
gcloud run deploy "$SERVICE" --source . --region "$REGION" --service-account "$SA" \
  --allow-unauthenticated --no-cpu-throttling --memory 2Gi --timeout 600 \
  --set-secrets "$SECRETS" ${SERVICE_SQL[@]+"${SERVICE_SQL[@]}"}

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')
gcloud run services update "$SERVICE" --region "$REGION" --update-env-vars "PUBLIC_URL=$URL" >/dev/null
IMAGE=$(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(spec.template.spec.containers[0].image)')

# Tables are created when the service starts, so the old one-off job isn't needed.
gcloud run jobs delete "${SERVICE}-init-db" --region "$REGION" --quiet >/dev/null 2>&1 || true

scheduled_job() {  # job suffix, tle command, cron schedule
  local job="${SERVICE}-$1"
  echo "==> $job ($3)"
  gcloud run jobs deploy "$job" --image "$IMAGE" --region "$REGION" --service-account "$SA" \
    --command tle --args "$2" --set-secrets "$SECRETS" ${JOB_SQL[@]+"${JOB_SQL[@]}"} \
    --max-retries 1 --task-timeout 30m --memory 2Gi
  if gcloud scheduler jobs describe "$job" --location "$REGION" >/dev/null 2>&1; then
    gcloud scheduler jobs delete "$job" --location "$REGION" --quiet
  fi
  gcloud scheduler jobs create http "$job" --location "$REGION" --schedule "$3" \
    --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/${job}:run" \
    --http-method POST --oauth-service-account-email "$SA"
}

# Each job does nothing until Settings gives it something to read.
NOTES=()
if [[ -n "$EMAIL_SCHEDULE" ]]; then
  scheduled_job email-sync sync-email "$EMAIL_SCHEDULE"
  CLIENT_ID=$(gcloud iam service-accounts describe "$SA" --format 'value(oauth2ClientId)')
  NOTES+=("Email (optional): a Workspace admin allows client ID $CLIENT_ID the scope"
          "  https://www.googleapis.com/auth/gmail.modify"
          "  (Admin console > Security > Access and data control > API controls > Domain-wide delegation),"
          "  then the inbox address goes under Email in Settings.")
fi
[[ -n "$DRIVE_SCHEDULE" ]] && scheduled_job drive-sync sync-drive "$DRIVE_SCHEDULE"
NOTES+=("Drive (optional): share each folder with $SA (Viewer), then pick it in Settings.")
[[ -n "$CLICKUP_SCHEDULE" ]] && scheduled_job clickup-sync sync-clickup "$CLICKUP_SCHEDULE"
[[ -n "$SLACK_SCHEDULE" ]] && scheduled_job slack-sync sync-slack "$SLACK_SCHEDULE"

if [[ "$DASHBOARD" == "1" ]]; then
  echo "==> Dashboard ($DASHBOARD_SERVICE, behind IAP)"
  PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format 'value(projectNumber)')
  AUDIENCE="/projects/${PROJECT_NUMBER}/locations/${REGION}/services/${DASHBOARD_SERVICE}"
  # Same image and secrets. IAP signs people in; the app checks IAP's header against AUDIENCE.
  gcloud run deploy "$DASHBOARD_SERVICE" --image "$IMAGE" --region "$REGION" --service-account "$SA" \
    --no-allow-unauthenticated --iap --memory 1Gi --timeout 300 \
    --set-secrets "$SECRETS" --set-env-vars "TLE_IAP_AUDIENCE=$AUDIENCE" ${SERVICE_SQL[@]+"${SERVICE_SQL[@]}"}
  # IAP calls the service as its own service agent.
  gcloud run services add-iam-policy-binding "$DASHBOARD_SERVICE" --region "$REGION" \
    --member "serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com" \
    --role roles/run.invoker >/dev/null
  if [[ -n "$DASHBOARD_ACCESS" ]]; then
    IFS=',' read -ra MEMBERS <<< "$DASHBOARD_ACCESS"
    for member in "${MEMBERS[@]}"; do
      # A typo here (a domain that doesn't exist) is reported, and the rest still finishes.
      if error=$(gcloud iap web add-iam-policy-binding --resource-type=cloud-run --service "$DASHBOARD_SERVICE" \
          --region "$REGION" --member "$member" --role roles/iap.httpsResourceAccessor 2>&1 >/dev/null); then
        echo "    $member can open the dashboard"
      else
        echo "    Couldn't give $member access: ${error##*: }"
        NOTES+=("Dashboard: $member couldn't be given access (${error##*: }). Check the spelling and rerun"
                "  with the right DASHBOARD_ACCESS, or add people in the IAP console.")
      fi
    done
  else
    NOTES+=("Dashboard: nobody can open it yet. Rerun with DASHBOARD_ACCESS=domain:yourcompany.com,"
            "  or add people in the IAP console.")
  fi
  DASHBOARD_URL=$(gcloud run services describe "$DASHBOARD_SERVICE" --region "$REGION" --format 'value(status.url)')
fi

echo "==> Checking the deployed setup (tle check, as the service account)"
gcloud run jobs deploy "${SERVICE}-check" --image "$IMAGE" --region "$REGION" --service-account "$SA" \
  --command tle --args check --set-secrets "$SECRETS" ${JOB_SQL[@]+"${JOB_SQL[@]}"} --max-retries 0 >/dev/null
if gcloud run jobs execute "${SERVICE}-check" --region "$REGION" --wait >/dev/null 2>&1; then
  CHECK="everything that's set up works"
else
  CHECK="something needs fixing"
fi
CHECK_LOGS="gcloud logging read 'resource.type=cloud_run_job AND resource.labels.job_name=${SERVICE}-check' --limit 40 --format 'value(textPayload)' --freshness 10m"

echo
echo "Done."
echo "  Check:                $CHECK. Read it with:"
echo "                        $CHECK_LOGS"
echo "                        Run it again any time: gcloud run jobs execute ${SERVICE}-check --region $REGION --wait"
echo "  Upload form:          $URL/ingest"
echo "  API:                  $URL"
echo "  Slack events URL:     $URL/slack/events"
echo "  ChatGPT action:       $URL/openapi.json"
echo "  Claude (MCP):         $URL/mcp, with the API token from:"
echo "                        gcloud secrets versions access latest --secret tle-tle-api-token"
[[ -n "${DASHBOARD_URL:-}" ]] && echo "  Dashboard:            $DASHBOARD_URL/dashboard"
for note in "${NOTES[@]+"${NOTES[@]}"}"; do echo "  $note"; done
