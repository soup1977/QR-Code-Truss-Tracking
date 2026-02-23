# OneDrive Setup Request — BuildersQRLabels App

We need OneDrive connected to a desktop app that uploads job files and generates shareable links for QR codes. No end users will sign in — the app runs silently using a single shared account.

---

## What We Need IT To Do

1. **Create a dedicated Microsoft 365 account** (e.g., `QRcodeFileUser@yourdomain.com`) with a license that includes OneDrive for Business.

2. **Register an app in Microsoft Entra ID** named `BuildersQRLabels`, single-tenant, no redirect URI.

3. **Grant the app `Files.ReadWrite.All` (Application permission)** on Microsoft Graph and provide admin consent.

4. **Create a client secret** for the app and share it with us securely along with the **Client ID** and **Tenant ID**.

5. **Confirm that anonymous sharing links are allowed** in the OneDrive admin center, or let us know if we need to use organization-scoped links instead.

---

## What We'll Give Back To You

Nothing — this is a one-time setup. Once we have the Client ID, Tenant ID, and Client Secret, the app handles everything from there.

---

## Questions?

A detailed technical reference with API examples and security notes is available at `docs/OneDrive-IT-Setup.md` in the project repo.
