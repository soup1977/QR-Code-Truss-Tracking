# OneDrive Integration — IT Admin Setup Guide

**App:** BuildersQRLabels
**Purpose:** A desktop application that uploads job files to a dedicated OneDrive account and generates shareable folder links, which are embedded into QR code stickers. The app runs on multiple machines using a single service account — end users never sign in to Microsoft.

---

## Step 1 — Create a Dedicated Service Account

Provision a new Microsoft 365 user (e.g., `QRcodeFileUser@yourdomain.com`) with any license that includes OneDrive for Business (Business Basic or higher). No admin roles are needed. The app will operate entirely within this account's OneDrive.

---

## Step 2 — Register the App in Microsoft Entra ID

1. Go to [entra.microsoft.com](https://entra.microsoft.com) > **Identity > Applications > App registrations > New registration**
2. Name: `BuildersQRLabels` (or similar)
3. Supported account types: **Accounts in this organizational directory only**
4. No redirect URI needed
5. After creation, note the **Application (client) ID** and **Directory (tenant) ID**

---

## Step 3 — Add API Permissions

1. App registration > **API permissions > Add a permission > Microsoft Graph > Application permissions**
2. Add: **`Files.ReadWrite.All`**
3. Click **Grant admin consent** (requires Global Admin)

> **Note:** `Files.ReadWrite.All` with app-only permissions grants the app read/write access to every user's OneDrive in the tenant — not just the service account. This is an unavoidable trade-off with app-only auth. The app only operates on the service account's drive, but the permission is technically broader. If this is a concern, an alternative is delegated access using the service account's credentials, which scopes access to only that account.

---

## Step 4 — Create a Client Secret

1. App registration > **Certificates & secrets > New client secret**
2. Set an expiration and copy the value immediately (it will not be shown again)
3. Provide the app with:
   - `client_id` (Application ID)
   - `client_secret` (the value just copied)
   - `tenant_id` (Directory ID)

These are stored in the app's `config.json` on each machine.

---

## Step 5 — How the App Authenticates

The app uses the **MSAL Python library** with the **client credentials flow** (no user login):

```python
app = msal.ConfidentialClientApplication(
    client_id,
    client_credential=client_secret,
    authority=f"https://login.microsoftonline.com/{tenant_id}"
)
result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
```

The access token is short-lived (~1 hour) and re-fetched automatically. No refresh tokens are involved.

---

## Step 6 — File Operations and Sharing Links

All Graph API calls reference the service account by UPN — **`/me` is not valid in app-only context** and will return an error.

**Upload a file:**
```
PUT /users/QRcodeFileUser@yourdomain.com/drive/root:/Cloud Manager Uploads/{job_id}/{filename}:/content
```

**Create a sharing link:**
```
POST /users/QRcodeFileUser@yourdomain.com/drive/root:/Cloud Manager Uploads/{job_id}:/createLink
```

```json
{
  "type": "view",
  "scope": "anonymous"
}
```

The `webUrl` in the response becomes the QR code target.

---

## Optional: Link Expiration

The `createLink` body accepts an optional `expirationDateTime` field:

```json
{
  "type": "view",
  "scope": "anonymous",
  "expirationDateTime": "2027-01-01T00:00:00Z"
}
```

**This feature is available but has a significant downside for this use case:**

QR codes are printed on physical stickers and affixed to physical trusses. A truss may sit in a warehouse or on a job site for months or years. If the sharing link expires, the QR code on the sticker **permanently stops working** — there is no way to update a printed sticker.

| Approach | Pros | Cons |
|---|---|---|
| No expiration | Links always work; safe for physical stickers | Links are permanent unless manually revoked |
| Short expiration (e.g., 90 days) | Limits exposure if a link leaks | Stickers become useless after expiry |
| Long expiration (e.g., 5+ years) | Practical safety margin for physical use | Still possible to outlive if records are kept long-term |

**Recommendation:** Omit expiration for production sticker use. If expiration is ever used, set it to align with the expected job file retention policy, not a generic short window.

---

## Security Notes

- **Verify anonymous links are allowed:** Check the OneDrive admin center (admin.onedrive.com > Sharing) — many organizations disable anonymous sharing by policy. If anonymous scope is blocked, use `"scope": "organization"` instead (links require tenant sign-in to open).
- **Rotate the client secret** before it expires; expiration is set at creation time in Entra.
- **Never commit** `config.json` (which contains the secret) to source control.
