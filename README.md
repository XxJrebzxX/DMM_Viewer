# DMM Viewer

Live readout, scrolling graphs, min/max/average and CSV logging for HP/Agilent
**34401A** multimeters over **GPIB** or **RS-232**. Measurement only: the app
never drives any outputs.

## Run

```
pip install -r requirements.txt

python app.py                    # real meters (needs Keysight IO Libraries / VISA)
python app.py --demo             # simulated meters, no hardware needed
python app.py --demo --browser   # UI in your web browser, for working on the design
```

In `--browser` mode, edit anything in `ui/` and refresh the page to see it.
All colours are CSS variables at the top of `ui/style.css`.

## Using it

- **Add DMM** opens a panel for the meter chosen in the drop-down. **Rescan** looks again.
- GPIB meters are found automatically. **Serial ports are only checked if you list them**
  in the *Also scan* box (e.g. `COM3`). You can also list GPIB addresses there (`9` or
  `GPIB0::9::INSTR`).
- Each panel has its own function, range and speed. Slower speeds give more digits and less noise.
- Panels fill the window. The graph icon in a panel's header hides its graph, and the reading
  grows to fill the space (readings, stats and logging carry on). The math strip has the same icon.
- **Return to Local** hands the meter back to its front panel; **Resume** takes control again
  (and re-applies the panel's settings).
- **Math** (top bar button, or ✕ on the strip to hide it) combines two meters: A − B, A + B,
  A × B (V × A shows W), A ÷ B (V ÷ A shows Ω), % deviation and dB. It needs two open panels and
  shows how far apart in time the two readings were.
- **Start CSV log** writes every reading to `Documents\DMM_Viewer_logs\`.
- **Theme** (top right): Dark, Light, or VFD, which looks like the 34401A's own display with
  segment digits and the Rmt / Man / 4W / Rear / ERROR indicators.
- Panels, their settings and the theme are remembered in `%APPDATA%\DMM_Viewer\settings.json`.

The VFD theme uses the DSEG14 font by keshikan (SIL Open Font License, see `ui/fonts/DSEG-LICENSE.txt`).

## RS-232 settings

The app must match the meter's front-panel I/O menu. The default is the 34401A factory setting
(9600 baud, even parity / 7 data bits, 2 stop bits, DTR/DSR handshake). To change it, edit
`"serial"` in `settings.json`, e.g. `{"baud": 9600, "parity": "none"}`.

A 34401A uses either GPIB or RS-232 (chosen in its I/O menu), not both at once.

## Building the .exe

```
py -3.14 -m pip install -r requirements.txt pyinstaller
build_exe.bat
```

This makes the folder version in `dist\DMM_Viewer\` (about 55 MB). Copy the **whole folder**
to the lab PC and run `DMM_Viewer.exe`; it also accepts `--demo`. The `_internal` folder must stay
next to the .exe.

The target PC still needs Keysight VISA (or NI-VISA) and the Microsoft Edge WebView2 runtime
(included with Windows 11). Python does not need to be installed there.

## Files

| File | What it does |
|---|---|
| `app.py` | Starts the app, the API the UI calls, settings, CSV logging, browser mode |
| `meters.py` | 34401A commands, GPIB / RS-232 transport, reader threads, simulated meter |
| `ui/` | The interface (HTML, CSS, JavaScript; no external libraries) |
