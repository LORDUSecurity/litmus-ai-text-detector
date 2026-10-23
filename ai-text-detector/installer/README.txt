Litmus — local app
==================

What this is
------------
A single-program version of the detector that runs entirely on your computer.
It starts a small local server and opens the app in your default browser.
Nothing is uploaded anywhere: your history and settings are saved on this PC.

Run it
------
Double-click "Litmus" (the program file itself is still named
AI-Text-Detector.exe, and your data folder is %LOCALAPPDATA%\AITextDetector).
A console window opens and the app loads in your browser at:

    http://127.0.0.1:8787

Keep the console window open while you use the app. Close it to stop the server.
Only your own computer can reach this server (it listens on 127.0.0.1 only).

No sign-in
----------
The local app opens straight into the detector. There is no login page and no
account to create: this install is one person's machine, so the program signs you
in as its owner before the page loads. Everything is still yours alone — the
server listens on 127.0.0.1 only, so nobody else can reach it and nobody else
can be that owner.

History, BYOK keys and the "Models & keys" / "Advanced parameters" tabs all work
with no password.
The owner account still has the seeded password (admin / admin) and the sign-in
endpoints still answer, so a script or a second browser tab pointed at
http://127.0.0.1:8787 can log in the usual way. Change that password from
Settings if anything other than you will sit at this PC.

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

  1. Add a working key for an endpoint you have access to (BYOK screen, under
     "Models & keys"). On this install you are the owner, so there is nobody to ask.
  2. Press Detect. The scoring happens inside this program — it does not need n8n,
     a workflow server or Docker. The only thing outside your PC is the model call
     itself, so you need internet reach to the provider the chosen model names
     (NVIDIA, OpenRouter, or a gateway URL you set yourself).

Browsing the ranking, history and guides works with no key at all; only a Detect
needs one, and without a key it answers a clear "no provider key stored" rather
than inventing a score.

Which model answers
-------------------
By default one model scores your text — the one measured as most accurate on the
free tier (z-ai/glm-5.3-flash on its own decision band). A second "tie-break"
model exists but is switched OFF out of the box: on that primary it was asked
about 3 of 48 benchmark texts and answered all 3 wrongly, so it costs a second
call for nothing useful. "Advanced parameters" has the switch, and turning it on
applies to every run of this install (it is a stored setting, not a browser
preference). The Ranking tab is where you pick a model and attach your own key;
the bands, the lanes and the tie-break switch live only under "Models & keys" and
"Advanced parameters", which this install opens for you because you own it.

If the browser tab shows a blank page
-------------------------------------
The page needs a moment on first launch (the program unpacks itself). Reload the
tab. If it stays blank, check the console window for an error and make sure
nothing else is already using port 8787.
