# Changelog

All notable changes to Builders Truss QR are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/) — MAJOR.MINOR.PATCH.

---

## [Unreleased]

---

## [0.9.0] — 2026-02-23

### Added
- **Unified application** — merges Sticker Manager (0.7.8.3) and Cloud Manager (0.6.2.8.1)
  into a single `BuildersQRLabels.py` entry point
- Job validation, sticker PDF generation, and QR code embedding
- Cloud upload support for Dropbox and OneDrive
- SQLite database (`builders_qr_labels.db`) for multi-machine sync via network share
- Per-machine config file (`config.json`) with shared settings stored in the DB
- Auto-update check at startup — reads `version.json` from the network share and
  shows a dismissible yellow banner when a newer installer is available
- PyInstaller build spec (`BuildersQRLabels.spec`) for standalone `.exe` bundle
- Inno Setup installer script (`installer/BuildersQRLabels.iss`)
- One-command build script (`build.bat`)

### Versioning convention
| Increment | Meaning |
|---|---|
| PATCH `0.9.x` | Bug fixes, small tweaks |
| MINOR `0.x.0` | New features, non-breaking changes |
| MAJOR `1.0.0` | First production-validated release |

---

*Legacy versions for reference:*
- *TC - Cloud Manager `0.6.2.8.1`*
- *TC - Sticker Manager `0.7.8.3`*
