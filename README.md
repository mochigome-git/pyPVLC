Python application to analyze HP SmartCard programming logs and post results to a Supabase database.

This application provides a GUI using Tkinter for analyzing HP SmartCard programming logs
and uploading summarized results to Supabase database. User can specify device and job details, select
log file, and initiate analysis. The program identifies key metrics, such as the number of programmed
and verified entries in the log file, and posts the results if conditions are met. Additionally, the
application saves configuration settings and allows for the automatic deletion of logs after a successful
database upload.

Key Features:
- Device and job details entry (including job month and job quantity).
- Log file selection and analysis to count keywords (programmed and passed verification).
- Automated posting of analysis results to a Supabase database.
- Custom message display using Tkinter pop-up windows.
- Configuration management for storing device name and log file path.
- Log file deletion after successful database post.

Classes:
    LogAnalyzerApp: Main Tkinter application class for log analysis and database posting.

Functions:
    show_custom_message(title, message, icon_path=None): Displays a custom message box with an optional icon.

Dependencies:
- tkinter, PIL (Pillow), supabase, configparser
- Ensure Supabase credentials are set before running the app.

Usage:
    Run the script, enter job details, select a log file, and click 'Analyze and Post to Database' to process
    the file and post results to the configured Supabase table.

## Two-station mode (programming PC + verification PC)

Use this when one PC only writes the programming log and another PC only writes the
verification log. The result is published as ONE metrics row, the same as a single PC.

Setup, on each PC:
1. Pick the role in the **Station** dropdown: `Program station` or `Verify station`
   (`Single PC` is the original behaviour). It is saved to `config.ini` as `station_role`.
2. Use the same `TENANT_ID`, `MACHINE_ID`, `SUPABASE_*` and `MQTT_*` values on both PCs.
3. Optional: set the same `LINE_DEVICE_ID` on both PCs. Otherwise the program station's
   `DEVICE_ID` is used for the merged row.

How it works:
- Each station counts only its own keyword (`programmed` or `passed verification`), checks it
  against the job quantity, and uploads its half to Storage under `{tenant}/pending/{job}/`
  (`program.txt/json`, `verify.txt/json`). The local log is deleted once its half is uploaded.
- Whichever station submits second sees both halves, takes an atomic `claim.json` (upload with
  no upsert, so only one PC can win), checks that job quantity, model name and product code
  match, recounts both logs, uploads one merged log to `{tenant}/logs/`, and publishes the
  metrics row plus the lot event. Then it clears the pending folder.
- When a job number is typed, the app shows whether the other station has already submitted
  and prefills empty fields from it.
- If details don't match, nothing is published. Fix the wrong station and press the button
  again; if its local log is gone, the copy in storage is reused.
- A claim left by a crashed merge is taken over after `PENDING_CLAIM_STALE_SEC` (default 300 s).

The merged row adds `status.mode = "two_station"` and `status.stations` (device id and name of
each station); every other field is the same as in single-PC mode.
