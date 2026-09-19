# Building and deploying the agent

## Build (on a Windows machine, once per release)

```powershell
cd agent
.\build\build.ps1 -ServerUrl "https://presence.yourcompany.com"
```

That produces `build\dist\PresenceTracker.exe` — a single ~20MB executable with
no Python installation required on the target laptop.

**Sign it.** An unsigned executable triggers SmartScreen on every laptop and
generates support tickets on day one:

```powershell
.\build\build.ps1 -ServerUrl "https://presence.yourcompany.com" `
                  -SignCertThumbprint "YOUR_CERT_THUMBPRINT"
```

## Package the installer

```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" `
    /DServerUrl="https://presence.yourcompany.com" build\installer.iss
```

The server URL is compiled into the installer and written to
`C:\ProgramData\Presence\config.json`, which is admin-writable only — an
employee cannot point the agent at a different server.

## Deploy to laptops

Silent install, suitable for Group Policy, Intune or any deployment tool:

```
PresenceTracker-Setup.exe /VERYSILENT /NORESTART
```

The installer:
- copies the executable to `C:\Program Files\Presence\`
- writes the machine-wide config
- registers a **scheduled task** that starts the agent at logon and restarts it
  every 5 minutes if it is not running
- adds a Startup shortcut as a second, softer mechanism

## Why both a scheduled task and a Run key

The Run key is convenience: it survives reinstalls and needs no admin rights.
The scheduled task is the part that matters, because it brings the agent back
after someone ends the process.

Neither is the real enforcement. **The enforcement is server-side**: a laptop
that stops sending heartbeats is reported to the team leader within the
configured grace period regardless of *why* it stopped. Killing the agent
does not make an employee look active — it makes them look offline, which is
more visible, not less.

## Verifying an installation

```powershell
& "C:\Program Files\Presence\PresenceTracker.exe" --check
```

Prints the server URL, whether the laptop is enrolled, which idle-detection
API is in use, and whether the server is reachable.

## Enrolling a laptop

Either:
- the employee signs in with their work username and password, or
- an admin issues a one-time enrollment code from **People → Enroll device**
  and the employee types it in, or it is applied silently:

```powershell
& "C:\Program Files\Presence\PresenceTracker.exe" --enroll ABCD-1234-EFGH
```

## macOS

The agent's idle detection already has a macOS backend (IOKit `HIDIdleTime`).
To ship it you need a `.app` bundle plus a LaunchAgent plist, and Apple
notarization. The Python side needs no changes.
