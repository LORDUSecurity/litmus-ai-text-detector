AI Text Detector — local app
============================

What this is
------------
A single-program version of the detector that runs entirely on your computer.
It starts a small local server and opens the app in your default browser.
Nothing is uploaded anywhere: your history and settings are saved on this PC.

Run it
------
Double-click "AI Text Detector" (or AI-Text-Detector.exe).
A console window opens and the app loads in your browser at:

    http://127.0.0.1:8787

Keep the console window open while you use the app. Close it to stop the server.
Only your own computer can reach this server (it listens on 127.0.0.1 only).

First sign-in
-------------
Username: admin
Password: admin

Change this password right away (Settings, once signed in). Because the app is
local to your PC, the default login is only there to get you started.

Where your data lives
---------------------
Saved detections and the encryption key for any API key you add are stored in:

    %LOCALAPPDATA%\AITextDetector

Uninstalling the app does NOT delete that folder, so reinstalling keeps your
history. Delete it yourself only if you want a clean start.

Making detections work
----------------------
The app ships with the measured model ranking already filled in, but it contains
no API key (nothing is baked into the program). To run live detections:

  1. Sign in as admin.
  2. Add a working key for an endpoint you have access to (BYOK screen), or ask
     your admin to configure a lane.
  3. The detection step talks to a local "n8n" workflow engine on port 5678.
     This local app does not start n8n for you; run it separately (see the
     project README) if you want live verdicts. Browsing the ranking, history,
     and guides works without it.

Which model answers
-------------------
By default one model scores your text — the one measured as most accurate on the
free tier (z-ai/glm-5.3-flash on its own decision band). A second "tie-break"
model exists but is switched OFF out of the box: on that primary it was asked
about 3 of 48 benchmark texts and answered all 3 wrongly, so it costs a second
call for nothing useful. If you sign in as admin, the Configuration tab has the
switch, and turning it on applies to every user of this install (it is a stored
setting, not a browser preference). Ordinary signed-in users can pick a model
from the Ranking tab and add their own API key, but cannot change the bands, the
lanes, or this switch.

If the browser tab shows a blank page
-------------------------------------
The page needs a moment on first launch (the program unpacks itself). Reload the
tab. If it stays blank, check the console window for an error and make sure
nothing else is already using port 8787.
