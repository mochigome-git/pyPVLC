"""
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
"""

import os
import tkinter as tk
import configparser
import threading
import stat
from tkinter import filedialog, messagebox
from dotenv import load_dotenv
from PIL import Image, ImageTk  
from supabase import create_client, Client

# Supabase credentials
# Load environment variables from .env file
load_dotenv()
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Function to display a custom message box
def show_custom_message(title, message, icon_path=None):
    """ Function to display a custom message box """
    custom_message_box = tk.Toplevel()
    custom_message_box.title(title)

    # Set the window size
    custom_message_box.geometry("300x230")

    # If an icon is provided, add it to the message box
    if icon_path:
        try:
            img = Image.open(icon_path)
            img = img.resize((30, 30), Image.Resampling.LANCZOS)
            icon = ImageTk.PhotoImage(img)
            icon_label = tk.Label(custom_message_box, image=icon)
            icon_label.image = icon  # Keep a reference to avoid garbage collection
            icon_label.pack(side="top", pady=(10, 5))  # Add some padding to the icon
        except Exception as e:
            print(f"Error loading icon: {e}")

    # Add a label for the message
    message_label = tk.Label(custom_message_box, text=message, wraplength=250, padx=10, pady=10)
    message_label.pack()

    # Add an OK button to close the message box
    ok_button = tk.Button(custom_message_box, text="OK", command=custom_message_box.destroy)
    ok_button.pack(pady=10)

def delete_log_file(file_path):
    # Convert to raw string format for Windows path handling
    file_path = os.path.normpath(file_path)  # Normalize path to avoid issues with backslashes

    # Ensure the path points to a file, not a directory
    if os.path.isfile(file_path):
        # Make file writable if necessary
        os.chmod(file_path, stat.S_IWRITE)
        try:
            os.remove(file_path)
            print(f"Deleted: {file_path}")
        except PermissionError as e:
            print(f"Permission error: Unable to delete {file_path}: {e}")
        except Exception as e:
            print(f"Unexpected error: {e}")
    else:
        print(f"The specified path does not point to a valid file: {file_path}")

class LogAnalyzerApp:
    """ Main Tkinter application class for log analysis and database posting """
    CONFIG_FILE = "config.ini"

    def __init__(self, root):
        self.root = root
        self.root.title("Log Analyzer")
        self.file_path = None

        # Load configurations
        self.config = configparser.ConfigParser()
        self.load_config()

        # GUI Elements
        self.setup_gui()

        # Automatically select log file if path is found in configuration
        if self.last_file_path:
            self.file_path = self.last_file_path
            threading.Thread(target=self.load_log_file, daemon=True).start()
        else:
            self.select_log_file()  # Prompt user to select a log file

    def setup_gui(self):
        tk.Label(self.root, text="Device Name:").grid(row=0, column=0, padx=5, pady=5, sticky="e")
        self.device_name_entry = tk.Entry(self.root)
        self.device_name_entry.grid(row=0, column=1, padx=5, pady=5)
        self.device_name_entry.insert(0, self.device_name) 

        tk.Label(self.root, text="Job Month:").grid(row=1, column=0, padx=5, pady=5, sticky="e")
        self.month_var = tk.StringVar(value="JAN")
        month_options = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
        self.month_menu = tk.OptionMenu(self.root, self.month_var, *month_options)
        self.month_menu.grid(row=1, column=1, padx=5, pady=5, sticky="w")

        tk.Label(self.root, text="Job Number:").grid(row=2, column=0, padx=5, pady=5, sticky="e")
        self.job_number_entry = tk.Entry(self.root)
        self.job_number_entry.grid(row=2, column=1, padx=5, pady=5)

        tk.Label(self.root, text="Job Quantity:").grid(row=3, column=0, padx=5, pady=5, sticky="e")
        self.quantity_entry = tk.Entry(self.root)
        self.quantity_entry.grid(row=3, column=1, padx=5, pady=5)

        self.select_file_button = tk.Button(self.root, text="Select Log File", command=self.select_log_file)
        self.select_file_button.grid(row=4, column=0, columnspan=2, padx=5, pady=5)

        self.analyze_button = tk.Button(self.root, text="Analyze and Post to Database", command=self.analyze_and_post)
        self.analyze_button.grid(row=5, column=0, columnspan=2, padx=5, pady=10)

    def load_config(self):
        """ Load device name and last file path from configuration file """
        if os.path.exists(self.CONFIG_FILE):
            self.config.read(self.CONFIG_FILE)
            self.device_name = self.config.get("Settings", "device_name", fallback="")
            self.last_file_path = self.config.get("Settings", "last_file_path", fallback="")
        else:
            self.device_name = ""
            self.last_file_path = ""

    def save_config(self):
        """ Save device name and last file path to configuration file """
        self.config["Settings"] = {
            "device_name": self.device_name_entry.get(),
            "last_file_path": self.file_path if self.file_path else ""
        }
        with open(self.CONFIG_FILE, "w") as configfile:
            self.config.write(configfile)

    def select_log_file(self):
        """
        Opens a file dialog for the user to select a log file.
        
        Allows the user to browse and select a log file
        with a .txt extension. If a file is selected, it displays a
        message confirming the selection and saves the file path in
        the 
        """
        self.file_path = filedialog.askopenfilename(filetypes=[("Log files", "*.txt")])
        if self.file_path:
            messagebox.showinfo("File Selected", f"Selected log file: {os.path.basename(self.file_path)}")
            self.save_config()  # Save the selected file path in config

    def load_log_file(self):
        """ This function is executed in a separate thread """
        try:
            # Simulate a delay for loading the log file (e.g., reading from disk)
            if os.path.isfile(self.file_path):
                messagebox.showinfo("Log File Loaded", f"Loaded log file from configuration: {os.path.basename(self.file_path)}")
            else:
                messagebox.showwarning("File Not Found", "The log file path is invalid.")
        except Exception as e:
            messagebox.showerror("Error", f"An error occurred while loading the log file:\n{e}")

    def read_and_analyze_log(self):
        """
        Reads and analyzes the selected log file for specific keywords.
        
        Reads the content of the selected log file and counts
        the occurrences of the keywords "programmed" and "passed verification".
        If no file has been selected, it prompts the user to select a file.
        
        Returns:
            tuple: A tuple containing two integers:
                   - programmed_count: The number of times "programmed" appears in the log file.
                   - verified_count: The number of times "passed verification" appears in the log file.
        """
        if not self.file_path:
            messagebox.showwarning("No File Selected", "Please select a log file first.")
            return None, None

        with open(self.file_path, "r") as log_file:
            log_content = log_file.read()

        programmed_count = log_content.lower().count("programmed")
        verified_count = log_content.lower().count("passed verification")

        return programmed_count, verified_count

    def post_to_database(self, job, job_quantity, programmed, verified):
        data = {
            "job_order": job,
            "job_quantity": job_quantity,
            "programmed": programmed,
            "verified": verified,
            "device": self.device_name_entry.get()  # Use the entered device name
        }
        
        # Assuming `supabase` is already configured and connected
        try:
            # Insert the data into the Supabase table
            response = supabase.table("ij_coding_log_ver1").insert(data).execute()

            # Check if the response data matches what was inserted
            if response.data and len(response.data) > 0:
                inserted_record = response.data[0]
                # Ensure the inserted record contains the same data as the original data
                if all(inserted_record[key] == data[key] for key in data):
                    return True
                else:
                    print("Inserted data does not match:", inserted_record)
                    return False
            else:
                print("Failed to post data:", response.error)
                return False
        except Exception as exception:
            print("An error occurred:", exception)
            return False

    def analyze_and_post(self):
        """
        Posts data to the Supabase database for tracking job and verification details.

        This method constructs a dictionary containing information about a job (job ID, quantity,
        programmed count, verified count, and device name) and inserts it into the "ij_coding_log_ver1"
        table in the Supabase database. It verifies that the inserted data matches the intended data
        before confirming a successful post.

        Args:
            job (str): The job order identifier.
            job_quantity (int): The quantity of items for the job.
            programmed (int): The count of items programmed.
            verified (int): The count of items verified as passed.

        Returns:
            bool: True if the data was successfully posted and verified in the database,
                False otherwise (including cases of data mismatch, insertion failure, or exceptions).
        
        Raises:
            Exception: Any exceptions that occur during the database insertion are caught and logged,
                    resulting in a False return value.

        Notes:
            - This method assumes `supabase` is already configured and connected.
            - It accesses `self.device_name_entry.get()` to retrieve the device name.
        """
        # Save the device name to the config when analyze is triggered
        self.save_config()

        # Combine job month and job number
        job_month = self.month_var.get()
        job_number = self.job_number_entry.get().strip()
        if not job_number.isdigit():
            messagebox.showwarning("Invalid Input", "Please enter a valid numeric job number.")
            return
        job = f"{job_month} {job_number}"

        # Validate job quantity
        job_quantity = self.quantity_entry.get().strip()
        if not job_quantity.isdigit():
            messagebox.showwarning("Invalid Input", "Please enter a valid job quantity.")
            return
        job_quantity = int(job_quantity)

        programmed, verified = self.read_and_analyze_log()

        if programmed is None or verified is None:
            return

        # Display results
        result_message = (f"Job: {job}\n"
                          f"Job Quantity: {job_quantity}\n"
                          f"Programmed: {programmed}\n"
                          f"Verified: {verified}\n")

        post_success = False
        if programmed >= job_quantity:
            if verified >= job_quantity:
                result_message += "\nCondition met. Attempting to post data to the database."
                post_success = self.post_to_database(job, job_quantity, programmed, verified)
                if post_success:
                    result_message += "\nData successfully posted to the database."
                    show_custom_message("Success", result_message, "accept.png")                      
                    # Delete log file after successful post
                    try:
                        delete_log_file(self.file_path)
                        messagebox.showinfo("Log File Deleted", "The log file has been deleted successfully.")
                    except Exception as e:
                        print(f"Error deleting log file: {e}")
                else:
                    result_message += "\nFailed to post data to the database."
                    show_custom_message("Error", result_message, "close.png")
            else:
                result_message += "\nCondition not met: Verified count is less than job quantity."
                show_custom_message("Condition Not Met", result_message, "close.png")
        else:
            result_message += "\nCondition not met: Programmed count is less than job count."
            show_custom_message("Condition Not Met", result_message, "close.png")

if __name__ == "__main__":
    root = tk.Tk()
    app = LogAnalyzerApp(root)
    root.mainloop()








    