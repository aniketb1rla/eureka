# Going live

These are the steps to put Eureka Studio on Google Cloud Run with Supabase. Run them yourself, in order.
Replace `PROJECT`, `REGION`, `YOUR_DOMAIN` and `SUPABASE_REF`. Run every `python -m` command from the
`eureka-studio` folder with the `.venv` active.

None of this has run against real Supabase or Cloud Run yet, so expect to adjust details the first time.

## Rules for the key

- **Use a new key made only for this app.** The old app stored a key in the browser's `localStorage`, so treat any
  key that was ever pasted there as exposed, and delete it in Google AI Studio once this one works.
- Restrict the new key to the **Generative Language API** in the Google Cloud console.
- Set a **budget alert** on the Google project. The app's daily caps limit its own use, but only a budget alert tells
  you if the key is used somewhere else.
- Never put a key in a chat, a `.env` file, an environment variable, a screenshot or a command line. The app refuses to
  start if `GEMINI_API_KEY` or `GOOGLE_API_KEY` is set in its environment.

## 1. Supabase

1. Create a project. Choose a region close to your Cloud Run region.
2. In the SQL editor, run all of [supabase.sql](supabase.sql). It creates the tables, turns on row level security with
   no policies, gives the public API roles no access, and creates the private `studio-private` bucket.
3. Under **Authentication > Sign In / Providers**:
   - Keep **Confirm email** on.
   - Turn on **Google** and add your Google OAuth client id and secret there. The secret stays in Supabase.
4. Under **Authentication > URL Configuration**, set the site URL to `https://YOUR_DOMAIN` and add
   `https://YOUR_DOMAIN/auth/callback` as a redirect URL.
5. Under **Authentication > Emails**, set up your own SMTP sender. Supabase's built-in sender only allows a few emails an
   hour, which isn't enough for public sign-ups.
6. Under **Authentication > Attack Protection**, turn on the CAPTCHA with Cloudflare Turnstile and paste the Turnstile
   **secret** there. The public **site key** goes in `TURNSTILE_SITE_KEY` in step 3.
7. Copy these for later:
   - the project URL and the publishable (anon) key, which are public
   - the service role key, which is secret
   - the **session pooler** connection string, which is secret. Cloud Run needs the pooler because the direct connection
     is IPv6 only. Don't use the transaction pooler on port 6543.

## 2. Google Cloud

```bash
gcloud config set project PROJECT
```

```bash
gcloud services enable run.googleapis.com secretmanager.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
```

```bash
gcloud iam service-accounts create eureka-studio --display-name="Eureka Studio"
```

Store the three secrets. Each command hides what you paste:

```bash
python -m scripts.push_secret gemini-api-key --from-credential-manager
```

```bash
python -m scripts.push_secret studio-database-url
```

```bash
python -m scripts.push_secret supabase-service-key
```

Let only the app's own service account read them. Run this once per secret name:

```bash
gcloud secrets add-iam-policy-binding gemini-api-key --member="serviceAccount:eureka-studio@PROJECT.iam.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"
```

```bash
gcloud secrets add-iam-policy-binding studio-database-url --member="serviceAccount:eureka-studio@PROJECT.iam.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"
```

```bash
gcloud secrets add-iam-policy-binding supabase-service-key --member="serviceAccount:eureka-studio@PROJECT.iam.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"
```

## 3. Deploy

Before deploying, run the checks:

```bash
python -m pytest -q
```

```bash
python -m scripts.secret_scan
```

Then deploy. Secrets are mounted as files, not environment variables, so they stay out of the Cloud Run console and crash reports.

```bash
gcloud run deploy eureka-studio --source . --region=REGION --service-account=eureka-studio@PROJECT.iam.gserviceaccount.com --allow-unauthenticated --no-cpu-throttling --min-instances=1 --max-instances=3 --memory=2Gi --cpu=2 --timeout=300 --set-secrets="/secrets/gemini/key=gemini-api-key:latest,/secrets/database/url=studio-database-url:latest,/secrets/supabase/key=supabase-service-key:latest" --set-env-vars="APP_ENV=production,GEMINI_MODE=real,GEMINI_API_KEY_FILE=/secrets/gemini/key,DATABASE_URL_FILE=/secrets/database/url,SUPABASE_SERVICE_KEY_FILE=/secrets/supabase/key,STORAGE_BACKEND=supabase,SUPABASE_BUCKET=studio-private,PUBLIC_ORIGIN=https://YOUR_DOMAIN,SUPABASE_URL=https://SUPABASE_REF.supabase.co,SUPABASE_ANON_KEY=YOUR_PUBLISHABLE_KEY,TURNSTILE_SITE_KEY=YOUR_TURNSTILE_SITE_KEY"
```

Why these flags:

- `--no-cpu-throttling --min-instances=1`: generation runs on a background worker inside the app. Without these, Cloud
  Run would pause it between requests and jobs would stall.
- `--max-instances=3`: limits how much can run at once. The worker's job leases make several instances safe.

The app refuses to start in production if any of these settings is missing or unsafe, and the error says which one.

Point `YOUR_DOMAIN` at the service with a Cloud Run domain mapping or a load balancer. `PUBLIC_ORIGIN` must match it
exactly, or every write is rejected as cross-site.

## 4. Check it

- [ ] Sign up with email: the confirmation arrives, the link works, and the allowance starts.
- [ ] Sign in with Google.
- [ ] Make one image from a template, animate it, then download both.
- [ ] Open the media link in a private window: it must refuse.
- [ ] A second account can't open the first account's items.
- [ ] Browser devtools: the page's scripts, API responses and cookies contain no key. The session cookie is `HttpOnly`.
- [ ] Cloud Run logs contain no key after a deliberately failed generation.
- [ ] In the Supabase API docs panel, a request with the anon key to `studio_generations` returns nothing.

## If a key ever leaks

1. Delete it in Google AI Studio. This stops it straight away.
2. Make a new key, then run `python -m scripts.set_gemini_key` and
   `python -m scripts.push_secret gemini-api-key --from-credential-manager`.
3. Redeploy with the same command so a new instance picks up the new version.
4. Check spending on the Google project.
