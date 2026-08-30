# Church Phone Line — Setup Tool

A desktop app that sets up a phone line on a Twilio account, entirely on
Twilio's own infrastructure. Once you click "Provision", everything runs
on Twilio — you don't need to keep this app open, and there's no server
for you to host or maintain.

**This tool is built for one Twilio account running one phone line.**
It remembers that line locally (in `config.json`) so that running it
again updates the existing line's recordings rather than setting up a
second one. If you're managing lines for several churches, use a
separate Twilio account (and a separate copy of this app/folder) per
church.

## How it works

- A UK phone number is either bought fresh or selected from numbers
  already on the account.
- The welcome message and recording(s) are uploaded to Twilio as static
  files (Twilio Assets).
- A small piece of call-handling logic (a Twilio Function) is uploaded
  alongside them and wired up as the number's webhook.
- Twilio serves all of this indefinitely with nothing running on your side.

To change the weekly recording, run the app again — it detects the
existing line and updates its recordings rather than provisioning a new
one.

## Prerequisites

1. **Python 3.10 or later.** Check with `python3 --version`.
2. **A Twilio account** for the church (or your own account, if you're
   managing numbers on their behalf). Sign up at
   [twilio.com/try-twilio](https://www.twilio.com/try-twilio).
   - Trial accounts can buy numbers but voice calls are limited until
     the account is upgraded (added billing details) — upgrade before
     going live.
3. Your **Account SID** and **Auth Token**, found on the dashboard at
   [console.twilio.com](https://console.twilio.com). Treat the Auth
   Token like a password.

## Setup

```bash
cd church-phone-line
pip install -r requirements.txt
```

## Running the app

```bash
python3 app.py
```

1. Enter the Account SID and Auth Token. On your first ever run these
   have to be typed in; after that, the app remembers them (see
   "Saved credentials" below) and prefills them for you.
2. **Choose the phone number** (only shown the first time you run the
   app — see below):
   - **Use a number already on this account** — click "Load numbers
     from account" and pick one from the dropdown.
   - **Buy a new number** — optionally enter a UK area code (e.g.
     `0113` for Leeds, `020` for London) to narrow the search, click
     **Search**, then pick a specific number from the results. Leave
     the area code blank to search nationally.
3. Choose the welcome message MP3.
4. Pick a call flow:
   - **Single recording** — plays the welcome message, then one
     recording, then hangs up.
   - **Menu** — plays the welcome message, then reads out the menu
     options (e.g. "Press 1 for the Sunday sermon"), and plays the
     recording matching whichever digit the caller presses.
5. Click **Provision / update phone line**. Progress is shown in the
   log panel.
6. Once complete, the phone number is shown — call it to test.

### Updating the recording each week

Run the app again. Once a line has been provisioned, the app skips
number selection entirely, shows "Existing line found: <number>", and
pre-fills the current welcome message, mode, and (in menu mode) every
option's digit and label — each with a **▶ Listen** button so you can
hear exactly what's currently live before deciding what to change.

Leave a file picker untouched to keep that recording as-is; only choose
a replacement MP3 for the parts you actually want to change. **Only
files that have genuinely changed are re-uploaded** — the app compares
a sha256 hash of each file (and of `voice.js`) against what was last
deployed, and skips anything identical. If nothing at all has changed,
the app skips the Twilio Build/Deploy step entirely and just confirms
the line is already up to date.

If you've edited something directly in the Twilio Console (see "Power
users" below) and want to guarantee everything is freshly pushed
regardless of what the app's local record thinks changed, tick **"Force
re-upload of all files"** before clicking Provision.

If you ever need to change the number or start again from scratch, use
**"Forget this line and start over"**. This only clears the app's local
memory of the line — it does not release the Twilio number or delete
anything from the Twilio account, so nothing is lost, and you can point
the app at a different existing number or buy a new one afterwards.

### Saved credentials

After a successful setup, the Account SID and Auth Token are saved to
`config.json` so you don't have to retype them each time. The Auth
Token is **encrypted before being written**, using a key derived from
this machine's own identity (see `secure_storage.py`) — the key itself
is never stored. Practically, this means:

- The app can decrypt and reuse the token on this machine without you
  re-entering it.
- If `config.json` is copied to a different computer, decryption will
  fail there and the app will simply ask for the Auth Token again — it
  will never silently use a broken or wrong credential.

This is a convenience measure, not a vault — anyone with access to this
machine, logged in as the same user, could in principle derive the same
key. Treat the machine itself as you would any device holding live API
credentials.

### Power users: editing directly in the Twilio Console

The Twilio Service this app creates is set up as UI-editable, so you're
not limited to this app for changes. Click **"Open in Twilio Console"**
(shown once a line exists) to jump straight to it under **Develop >
Functions & Assets**, where you can edit `voice.js`, tweak an Asset, or
inspect the `CONFIG_JSON` environment variable by hand, and deploy your
changes with the Console's own "Deploy All" button.

One thing to know: this app's change-detection relies on its own local
record of what's already deployed. If you make changes in the Console,
the app won't automatically know about them — tick **"Force re-upload
of all files"** the next time you run it to make sure your local files
take precedence again, or continue managing that particular file from
the Console going forward.

### Where state is kept

The app stores a single `config.json` file next to `app.py`: the
Twilio SIDs needed to update the line, the (encrypted) credentials, the
current call-flow configuration, and a content hash of every uploaded
file. If you move the app to a different machine, either copy this file
across (the Auth Token will need re-entering, per above) or just
re-provision, which will detect no existing line and let you pick a
number again.

## Costs (Twilio pay-as-you-go, UK local number)

- Number rental: **$3.50/month**
- Inbound calls: **$0.01/minute**
- Hosting the recordings and call logic (Twilio Assets & Functions):
  **free**, within generous limits (25MB per file, 1,000 files per
  account)

No charge for how often the number is called or the recordings are
updated.

**Purchasing a number is confirmed before it happens.** Selecting "Use a
number already on this account" or updating an existing line never
triggers a charge. Only "Buy a new number" does, and the app shows a
confirmation dialog stating the number and the recurring cost before
going ahead — nothing is purchased silently.

## Troubleshooting

- **"No available UK local numbers were found"** — Twilio's UK local
  inventory is occasionally thin in specific area codes; try again, or
  raise a support ticket with Twilio if it persists.
- **Build fails / times out** — check the Twilio Console under
  Functions & Assets > Services for the church's service; the build log
  there usually explains the failure (most often an unsupported file
  type — double check the files are genuinely MP3s).
- **Trial account calls get cut off with a "trial account" message** —
  upgrade the Twilio account (add a payment method) to remove trial
  restrictions on inbound voice.
- **Changing this later by hand** — everything the app creates is
  visible and editable in the normal Twilio Console (Functions & Assets,
  and Phone Numbers), if you ever need to intervene manually.

## Notes for future maintenance

- `twilio_backend.py` — all Twilio API calls.
- `secure_storage.py` — machine-linked encryption for the saved Auth Token.
- `functions/voice.js` — the call-handling logic that runs on Twilio;
  edit this and re-run the app to redeploy it (or edit it directly in
  the Console — see "Power users" above).
- `app.py` — the Tkinter GUI.
- To package this as a standalone executable for non-technical users,
  use [PyInstaller](https://pyinstaller.org/):
  `pyinstaller --onefile --add-data "functions:functions" app.py`
  (bundle the `functions/` folder alongside the executable).
