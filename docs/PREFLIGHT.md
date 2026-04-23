# REIG preflight checklist

One-time setup before `make prepare && make demo`. Each step produces
at least one value that lands in `.env`.

## 1. Retell workspace

1. Sign in at https://dashboard.retellai.com.
2. Settings → API Keys → Create New Key → copy the value.
3. Export it in `.env` as `REIG_RETELL_API_KEY=...`.

## 2. ngrok domain

Retell webhooks need a stable HTTPS URL. Free-tier reserved domains are fine.

1. Sign in at https://dashboard.ngrok.com.
2. Domains → Create Domain → copy the chosen `<subdomain>.ngrok-free.dev`.
3. `ngrok config add-authtoken <your-token>` on your host machine.
4. Once the API is up, run `ngrok http --domain=<your-subdomain>.ngrok-free.dev 8000` in a separate terminal.
5. Export `REIG_NGROK_DOMAIN=<subdomain>.ngrok-free.dev`.

## 3. Salesforce org + custom field

You'll need a Developer Edition org (or a sandbox you control).

### 3a. `External_Call_Id__c` custom field on Lead

1. Setup → Object Manager → Lead → Fields & Relationships → **New**.
2. Data Type = `Text`, Length = `64`, Field Label = `External Call Id`,
   Field Name auto-fills to `External_Call_Id`.
3. Check both **External ID** and **Unique**.
4. Save. This gives REIG's `PATCH /sobjects/Lead/External_Call_Id__c/{call_id}` its idempotency key.

### 3b. External Client App for OAuth

1. Setup → App Manager → **New External Client App**.
2. Name: `REIG Gateway`. Contact email: your address.
3. Enable OAuth Settings ✓.
4. Callback URL: `https://<your-ngrok-subdomain>/admin/oauth/callback`.
5. Selected OAuth Scopes: `Access the identity URL service (id, profile, email, address, phone)` **and** `Access and manage your data (api)` **and** `Perform requests at any time (refresh_token, offline_access)`.
6. Save. Click **Manage Consumer Details** → copy both values:
   * Consumer Key → `REIG_SFDC_CLIENT_ID`
   * Consumer Secret → `REIG_SFDC_CLIENT_SECRET`

### 3c. Relax IP restrictions

Without this, OAuth refresh token exchanges from ngrok egress IPs will 401.

1. Setup → App Manager → find `REIG Gateway` → **Manage**.
2. Edit Policies → IP Relaxation = **Relax IP restrictions** → Save.
3. **Wait 10 minutes** for propagation before driving a refresh.

### 3d. Exported `.env` values

```bash
REIG_SFDC_CLIENT_ID=<consumer key from 3b>
REIG_SFDC_CLIENT_SECRET=<consumer secret from 3b>
REIG_SFDC_LOGIN_URL=https://login.salesforce.com
REIG_SFDC_API_VERSION=v60.0
REIG_SFDC_CALLBACK_URL=https://<ngrok>/admin/oauth/callback
REIG_SFDC_INSTANCE_URL=https://<your-org>.develop.my.salesforce.com
```

## 4. Encryption key

pgcrypto uses this to encrypt refresh tokens at rest.

```bash
openssl rand -base64 32
# copy into .env:
REIG_ENCRYPTION_KEY=<output>
```

## 5. Configure `.env`

```bash
cp .env.example .env
# edit .env and fill every value above
```

`.env` is gitignored. `.env.example` is the committed template.

## 6. Complete the OAuth dance (post-`make demo`)

Once the stack is up and ngrok is pointed at :8000:

1. `make seed` to create the two demo tenants + keys. Grab `TENANT_LENDING_ID` from `.env.local.seeds`.
2. Visit:
   `https://<ngrok>/admin/oauth/authorize?tenant_id=<TENANT_LENDING_ID>`
3. Sign in to Salesforce in the browser; approve the OAuth scopes.
4. The callback writes `credentials.refresh_token_encrypted` for that tenant.
5. Repeat for `TENANT_HEALTH_ID` if you plan to demo both.

From here, `make fire-webhook ...` lands redacted Leads in Salesforce and the Jaeger UI (http://localhost:16686) shows the full span tree.
