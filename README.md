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
