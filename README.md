# Concrete Slab Tributary Area

**Live web app:** https://conc-slab-tributary-area-public.vercel.app

Local V1 web app for:
- DXF upload
- layer / unit review
- async tributary processing
- DXF + XLSX download
- desktop packaging for non-technical users

## Deploying the web app

The live app is the `web/` Next.js project on Vercel. It is deployed by the
GitHub Actions workflow `.github/workflows/deploy-vercel.yml` on every push
to `main` (and on demand from the Actions tab). The workflow needs three
repository secrets, set once under Settings > Secrets and variables > Actions:

| Secret | Where to find it |
|---|---|
| `VERCEL_TOKEN` | Vercel > Account Settings > Tokens > Create |
| `VERCEL_ORG_ID` | Vercel > Team or Account Settings > General > ID |
| `VERCEL_PROJECT_ID` | Vercel > project > Settings > General > Project ID |

Both IDs also appear in `web/.vercel/project.json` after running
`npx vercel link` once inside `web/`.

Until the secrets exist the workflow fails on its first step with a message
naming what is missing, and nothing deploys. A one-off deploy from a laptop
is `npx vercel --prod` inside `web/`, logged in to the account that owns the
project. If the repository is instead connected in the Vercel dashboard
(project > Settings > Git), delete the workflow so `main` is not deployed twice.

## Run Local
```bash
uv venv --clear --seed --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/uvicorn app.main:app --reload
```

Open:
`http://127.0.0.1:8000`

## Current Flow
1. Upload DXF
2. Review inferred layers
3. Confirm inches or feet
4. Queue job
5. Download `tributary_output.dxf` and `column_load_takedown.xlsx`

## Desktop App
The desktop build starts the same FastAPI app locally and opens it for the user. Runtime data is written to a user-writable app-data folder instead of the install directory.

Runtime data locations:
- Windows: `%LOCALAPPDATA%\\TributaryAreaTool`
- macOS: `~/Library/Application Support/TributaryAreaTool`
- Linux: `${XDG_DATA_HOME:-~/.local/share}/TributaryAreaTool`

### Latest Release
For non-technical users, the single delivery page should be the GitHub Releases page:
- Latest release: `https://github.com/gabe-kafka/conc-slab-tributary-area-public/releases/latest`

That page should contain:
- `TributaryAreaToolInstaller.exe`
- short install steps
- first-run notes
- SmartScreen / unsigned-app note until signing is added

Maintainers can start from:
- `packaging/windows/GITHUB_RELEASE_BODY.md`

### Run Desktop App From Source
```bash
uv venv --clear --seed --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python desktop_app.py
```

### Build Desktop Bundle
```bash
uv venv --clear --seed --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-build.txt
.venv/bin/python scripts/build_desktop.py
```

Build output:
- macOS: `dist/Tributary Area Tool.app`
- Windows / Linux: `dist/Tributary Area Tool/`

### Build macOS DMG
```bash
.venv/bin/python scripts/build_desktop.py
./scripts/build_macos_dmg.sh
```

macOS release artifacts:
- `dist/Tributary Area Tool.app`
- `dist/TributaryAreaTool-macOS.dmg`

### Test macOS Bundle
This launches the packaged app on a fixed local port without auto-opening a browser:

```bash
TRIBUTARY_APP_PORT=8010 \
TRIBUTARY_APP_NO_BROWSER=1 \
"dist/Tributary Area Tool.app/Contents/MacOS/Tributary Area Tool"
```

Then open:
- `http://127.0.0.1:8010`

### macOS Signing / Notarization
Unsigned local packaging is supported now. For an employee-ready signed release, follow:
- `packaging/macos/NOTARIZATION.md`

### Build Windows Installer
1. Create the Windows build venv:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt -r requirements-build.txt
```

2. Build the Windows desktop bundle:

```powershell
.\.venv\Scripts\python scripts\build_desktop.py
```

3. Install Inno Setup 6 on Windows.
4. Build the installer:

```powershell
iscc packaging/windows/TributaryAreaTool.iss
```

If `iscc` is not on `PATH`, use the installed compiler directly:

```powershell
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" packaging\windows\TributaryAreaTool.iss
```

Installer output:
- `dist/installer/TributaryAreaToolInstaller.exe`

Windows installer behavior:
- Install location: `%LOCALAPPDATA%\Programs\Two-Way Slab Tributary Area`
- Runtime data: `%LOCALAPPDATA%\TributaryAreaTool`
- Start menu shortcut: `Two-Way Slab Tributary Area`
- Desktop shortcut: optional installer task

### Windows Signing
Unsigned local packaging is supported now. For an employee-ready Windows release, code-sign:
- `dist/installer/TributaryAreaToolInstaller.exe`
- `%LOCALAPPDATA%\Programs\Two-Way Slab Tributary Area\Tributary Area Tool.exe` before packaging, if your signing flow supports it
