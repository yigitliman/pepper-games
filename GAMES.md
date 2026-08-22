# Pepper Voice Games & Admin Panel

Interactive voice games and a web admin panel for a **SoftBank Robotics Pepper**
robot, driven from a Windows PC over the naoqi API.

Speech is transcribed and answered by an [Open WebUI](https://openwebui.com/)
server (Whisper for speech-to-text, `gpt-oss-120b` for chat). Everything runs in
the Python 2.7 (32-bit) `pepper` conda environment with the pynaoqi SDK - see the
main [`readme.md`](readme.md) for the environment, WLAN and SDK setup.

---

## What's inside

| File | What it does |
|------|--------------|
| `pepper_echo.py` | Core pipeline: record from Pepper's mics, download the wav, transcribe it, and repeat it back. Also imported by the games. |
| `pepper_parrot_game.py` | **Parrot game** - say something, Pepper shows it on the tablet and repeats it like a parrot. |
| `pepper_qa_game.py` | **Ask Pepper** - ask a question out loud, Pepper thinks with the AI model and answers. |
| `pepper_guess_game.py` | **Guess What** - Pepper describes an animal / country / city (AI-generated clue) and the kids guess. The tablet has a **HINT** button (next clue) and a **SKIP** button (reveal and move on). Guesses are judged by the model, so German answers are accepted too. |
| `pepper_kiosk.py` | **Tablet menu** - shows a touch menu on the tablet (the four games + English/Deutsch). Tapping a game launches it, then the menu comes back. Lets visitors play straight from the tablet, no PC needed. |
| `pepper_admin.py` | **Web admin panel** - control Pepper (speech, posture, LEDs, volume, tablet), launch/stop the games, and a built-in wiki. Standard-library HTTP server, no Flask. |
| `start_pepper_admin.ps1` | Launcher for the admin panel (sets up `PYTHONPATH`, opens the browser). |

---

## Setup on a fresh PC

Cloning the repo gives you the code and docs, but a few machine-specific pieces
are deliberately NOT in the repo and must be set up on each PC before anything
will run:

1. **Clone the repo** and open a terminal in its folder.

2. **pynaoqi SDK** (not included, ~126 MB, licensed). Download the Python 2.7
   win32 SDK from SoftBank/Aldebaran, unpack it under `sdk/` so that a
   `.../lib/qi/__init__.py` exists. The launcher finds it automatically; if you
   run the games by hand, put that `lib` folder on `PYTHONPATH`.

3. **Python environment** (32-bit Python 2.7 conda env named `pepper`):
   ```powershell
   conda create -n pepper
   conda activate pepper
   conda config --env --set subdir win-32
   conda install python=2.7
   pip install paramiko requests
   ```

4. **HCTL_TOKEN** (Open WebUI bearer token). ALL games need it (speech-to-text),
   not just Q&A. Save it once as a user variable so the launcher picks it up:
   ```powershell
   setx HCTL_TOKEN "eyJhbGci..."
   ```
   Tokens expire, so refresh it from Open WebUI when it stops working.

5. **PEPPER_SSH_PASSWORD** (Pepper's SSH/hotspot password, ask a lab member).
   Every game needs it for tablet uploads and downloading recordings:
   ```powershell
   setx PEPPER_SSH_PASSWORD "..."
   ```

6. **Pepper on the same network.** Turn Pepper on and make sure it joins the
   `Pepper` hotspot; note its IP (default `192.168.137.214`).

7. **Launcher path.** `start_pepper_admin.ps1` expects miniconda at
   `%USERPROFILE%\AppData\Local\miniconda3\envs\pepper\python.exe`. If your
   Python lives elsewhere, edit that line (otherwise it falls back to whatever
   `python` is on PATH, which may be the wrong one).

After that, double-click `Start Pepper Admin.bat`, or run the games directly as
shown below.

---

## Running the games

```powershell
# Ask Pepper (question & answer)
python pepper_qa_game.py     -ip 192.168.137.214 -l en          # or -l de

# Parrot (listen & repeat)
python pepper_parrot_game.py -ip 192.168.137.214 -l en

# Guess What (Pepper describes, kids guess; tablet HINT + SKIP buttons)
python pepper_guess_game.py  -ip 192.168.137.214 -l en -c mixed # animals|countries|cities|mixed

# Echo (single or looping repeat)
python pepper_echo.py        -ip 192.168.137.214 -d 6 --loop
```

Common options: `-l en|de` language, `-d <seconds>` record length,
`-r <n>` number of rounds (`0` = endless), `-ip <addr>` Pepper's IP
(auto-detected on the hotspot subnet if the given one is down). Guess What also
takes `-c <category>` and `-a <n>` (wrong guesses before the answer is revealed).

When Pepper shows the green **"Listening…"** screen (and its eyes turn blue),
speak clearly and close to the robot. If it can't hear you it says so and asks
you to repeat instead of guessing.

## Tablet menu (kiosk)

Let visitors start games straight from Pepper's tablet:

```powershell
python pepper_kiosk.py -ip 192.168.137.214 -r 3     # 3 rounds per tapped game
```

It shows a touch menu (Parrot / Ask Pepper / Guess What / Echo and an
English/Deutsch toggle). Tapping a game launches it; when it finishes its rounds
the menu comes back. The tablet talks to the robot through `qimessaging.js`
(the page writes the choice into ALMemory and the kiosk polls it), so
`qimessaging.js` must sit in the robot web root next to the pages
(the games/kiosk expect it at `http://198.18.0.1/qimessaging.js`).

## Running the admin panel

```powershell
$env:HCTL_TOKEN = "eyJhbGci..."
$env:PEPPER_SSH_PASSWORD = "..."
.\start_pepper_admin.ps1            # then open http://127.0.0.1:8080
```

The panel listens on loopback only. That is deliberate: its endpoints make
Pepper speak and move and start processes on the host, so an open port here
hands the robot to anyone who can route to the machine.

To drive it from a phone or a second laptop, bind wider and the panel will
demand a token:

```powershell
python pepper_admin.py --host 0.0.0.0
# prints http://0.0.0.0:8080/?token=<generated>  - open it exactly as printed
```

The token arrives in the URL, moves into an `HttpOnly` cookie on first load,
and every later request is rejected without it. Set `PEPPER_ADMIN_TOKEN`
beforehand to pin a token of your own instead of getting a fresh one per start.

The panel has three tabs:

- **Control** - connect, speak, volume, posture (wake / stand / sit / rest),
  autonomous life, eye LEDs, and tablet display (plus a best-effort
  "fix tablet service" button).
- **Games** - launch Parrot / Q&A / Echo with the chosen language, record length
  and rounds, and watch the live output.
- **Wiki** - the cheat-sheet below, always at hand.

---

## Troubleshooting

### The tablet stays on its home screen
`ALTabletService` has dropped out of naoqi. It is provided by the tablet's own
Android app; when that crashes the service disappears (all other services keep
running). The admin header shows **tablet: down**. Fix: tap the tablet / relaunch
its app, or reboot the robot - it re-registers on its own. **No code change is
needed**; the web-display pipeline (upload HTML to the robot-page web root, then
`showWebview('http://198.18.0.1/parrot.html')`) is correct.

### Pepper gives irrelevant answers / mishears you
- The games record **all four microphones** and send the **loudest single
  channel** to Whisper (recording one channel directly is much quieter, and
  averaging the spaced mics smears speech).
- **Force the language** (`-l en` / `-l de`). `auto` mis-detects even clean
  English as German.
- Speak **clearly and close**, after the green "Listening" screen appears.
  Recorded speech sits only just above the noise floor, so silence and speech
  look the same by amplitude - the game keys off the transcript being empty.
- The STT server returns no confidence score; an empty transcript means
  "not heard" and the game asks you to repeat.

### Q&A: Pepper reads its own "thinking"
`gpt-oss-120b` is a reasoning model. Its reasoning normally comes back in a
separate `reasoning` field, but the game also strips any leaked
`<think>...</think>` so only the final answer is spoken.

---

## Reference (quick facts)

| | |
|---|---|
| Robot IP (Mobile Hotspot) | `192.168.137.214`, subnet `192.168.137.x` |
| naoqi port | `9559` |
| SSH login | user `nao`, password in env var `PEPPER_SSH_PASSWORD` (port 22) |
| STT / chat server | `https://hctlsrvb.edu.sot.tum.de` |
| Tablet web root | `/home/nao/.local/share/PackageManager/apps/robot-page/html/` → `http://198.18.0.1/` |
| Python | conda `pepper`, Python 2.7 32-bit + pynaoqi on `PYTHONPATH` |

> **Security note:** the SSH/hotspot password and the HCTL token are read from
> the `PEPPER_SSH_PASSWORD` and `HCTL_TOKEN` environment variables -- never
> hardcode either in code or docs, since this repo may become public. Ask a
> lab member for the actual values.

---
