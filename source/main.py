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
import boto3
from datetime import datetime
from tkinter import filedialog, messagebox
from dotenv import load_dotenv
from PIL import Image, ImageTk
from supabase import create_client, Client
from botocore.exceptions import (
    ClientError,
    BotoCoreError,
    NoCredentialsError,
    PartialCredentialsError,
)

# Load environment variables from .env file
load_dotenv()
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Retrieve credentials from environment variables
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
AWS_DEFAULT_REGION = os.environ.get("AWS_DEFAULT_REGION")

# Initialize the S3 client
s3 = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    region_name=AWS_DEFAULT_REGION,
)


def upload_to_s3(s3_client, data, job, job_quantity):
    """
    Uploads data to an S3 bucket with a dynamically generated filename.
    If a file with the same name exists, a sequence number is added to the filename.
    Verifies the upload by re-checking object existence.

    Returns:
    (success: bool, file_name: str or None, error_message: str or None)
    """
    bucket_name = os.environ.get("BUCKET_NAME")
    if not bucket_name:
        error_msg = "BUCKET_NAME must be set in the environment variables."
        print(error_msg)
        return False, None, error_msg

    # Generate base name
    current_date = datetime.now().strftime("%Y-%m-%d")
    base_file_name = f"logs/{current_date}-{job}-{job_quantity}.txt"

    # Find non-conflicting filename
    sequence = 1
    file_name = base_file_name

    try:
        while check_file_exists(s3_client, bucket_name, file_name):
            file_name = f"logs/{current_date}-{job}-{job_quantity}-{sequence}.txt"
            sequence += 1
    except Exception as e:
        error_msg = f"Error checking file existence: {str(e)}"
        print(f"[ERROR] {error_msg}")
        return False, None, error_msg

    try:
        # Upload the data
        s3_client.put_object(Bucket=bucket_name, Key=file_name, Body=data)

        # Verify the upload by checking the object again
        if not check_file_exists(s3_client, bucket_name, file_name):
            error_msg = (
                "Upload failed verification — file does not exist in S3 after upload."
            )
            print(error_msg)
            return False, None, error_msg

        print(f"Verified upload OK → {file_name}")
        return True, file_name, None

    except NoCredentialsError:
        error_msg = "AWS credentials not found. Please check your AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY."
        print(f"[ERROR] {error_msg}")
        return False, None, error_msg

    except PartialCredentialsError:
        error_msg = "Incomplete AWS credentials. Please ensure both AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are set."
        print(f"[ERROR] {error_msg}")
        return False, None, error_msg

    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        error_message = e.response["Error"]["Message"]

        if error_code == "NoSuchBucket":
            error_msg = f"S3 bucket '{bucket_name}' does not exist."
        elif error_code == "AccessDenied" or error_code == "403":
            error_msg = (
                f"Access denied (403 Forbidden) to S3 bucket '{bucket_name}'.\n\n"
                f"Required IAM permissions:\n"
                f"  • s3:PutObject (to upload files)\n"
                f"  • s3:GetObject (to verify uploads)\n"
                f"  • s3:ListBucket (to check existing files)\n\n"
                f"Please contact your AWS administrator to grant these permissions."
            )
        elif error_code == "InvalidAccessKeyId":
            error_msg = "Invalid AWS Access Key ID."
        elif error_code == "SignatureDoesNotMatch":
            error_msg = "AWS Secret Access Key is incorrect."
        else:
            error_msg = f"AWS ClientError ({error_code}): {error_message}"

        print(f"[ERROR] {error_msg}")
        return False, None, error_msg

    except BotoCoreError as e:
        error_msg = f"BotoCore Error: {str(e)}"
        print(f"[ERROR] {error_msg}")
        return False, None, error_msg

    except Exception as e:
        error_msg = f"Unexpected error during S3 upload: {type(e).__name__} - {str(e)}"
        print(f"[ERROR] {error_msg}")
        return False, None, error_msg


def check_file_exists(s3_client, bucket_name, file_name):
    """
    Check if a file exists in the S3 bucket.
    """
    try:
        s3_client.head_object(Bucket=bucket_name, Key=file_name)
        return True
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "404":
            return False
        elif error_code == "403":
            # Forbidden - lack of permissions, treat as file doesn't exist
            # but this indicates a permission issue
            print(
                f"[WARNING] 403 Forbidden when checking {file_name}. Permission issue detected."
            )
            return False
        else:
            # Re-raise for other errors
            raise


def show_custom_message(title, message, icon_path=None):
    """Function to display a custom message box"""
    custom_message_box = tk.Toplevel()
    custom_message_box.title(title)
    custom_message_box.geometry("400x300")

    if icon_path:
        try:
            img = Image.open(icon_path)
            img = img.resize((30, 30), Image.Resampling.LANCZOS)
            icon = ImageTk.PhotoImage(img)
            icon_label = tk.Label(custom_message_box, image=icon)
            icon_label.image = icon
            icon_label.pack(side="top", pady=(10, 5))
        except Exception as e:
            print(f"Error loading icon: {e}")

    message_label = tk.Label(
        custom_message_box, text=message, wraplength=350, padx=10, pady=10
    )
    message_label.pack()

    ok_button = tk.Button(
        custom_message_box, text="OK", command=custom_message_box.destroy
    )
    ok_button.pack(pady=10)


def delete_log_file(file_path):
    file_path = os.path.normpath(file_path)

    if os.path.isfile(file_path):
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
    CONFIG_FILE = "config.ini"

    def __init__(self, root):
        self.root = root
        self.root.title("Log Analyzer")
        self.file_path = None

        self.config = configparser.ConfigParser()
        self.load_config()

        self.setup_gui()

        if self.last_file_path:
            self.file_path = self.last_file_path
            threading.Thread(target=self.load_log_file, daemon=True).start()
        else:
            self.select_log_file()

    def setup_gui(self):
        tk.Label(self.root, text="Device Name:").grid(
            row=0, column=0, padx=5, pady=5, sticky="e"
        )
        self.device_name_entry = tk.Entry(self.root)
        self.device_name_entry.grid(row=0, column=1, padx=5, pady=5)
        self.device_name_entry.insert(0, self.device_name)

        tk.Label(self.root, text="Job Month:").grid(
            row=1, column=0, padx=5, pady=5, sticky="e"
        )
        self.month_var = tk.StringVar(value="JAN")
        month_options = [
            "JAN",
            "FEB",
            "MAR",
            "APR",
            "MAY",
            "JUN",
            "JUL",
            "AUG",
            "SEP",
            "OCT",
            "NOV",
            "DEC",
        ]
        self.month_menu = tk.OptionMenu(self.root, self.month_var, *month_options)
        self.month_menu.grid(row=1, column=1, padx=5, pady=5, sticky="w")

        tk.Label(self.root, text="Job Number:").grid(
            row=2, column=0, padx=5, pady=5, sticky="e"
        )
        self.job_number_entry = tk.Entry(self.root)
        self.job_number_entry.grid(row=2, column=1, padx=5, pady=5)

        tk.Label(self.root, text="Job Quantity:").grid(
            row=3, column=0, padx=5, pady=5, sticky="e"
        )
        self.quantity_entry = tk.Entry(self.root)
        self.quantity_entry.grid(row=3, column=1, padx=5, pady=5)

        self.select_file_button = tk.Button(
            self.root, text="Select Log File", command=self.select_log_file
        )
        self.select_file_button.grid(row=4, column=0, columnspan=2, padx=5, pady=5)

        self.analyze_button = tk.Button(
            self.root,
            text="Analyze and Post to Database",
            command=self.analyze_and_post,
        )
        self.analyze_button.grid(row=5, column=0, columnspan=2, padx=5, pady=10)

    def load_config(self):
        if os.path.exists(self.CONFIG_FILE):
            self.config.read(self.CONFIG_FILE)
            self.device_name = self.config.get("Settings", "device_name", fallback="")
            self.last_file_path = self.config.get(
                "Settings", "last_file_path", fallback=""
            )
        else:
            self.device_name = ""
            self.last_file_path = ""

    def save_config(self):
        self.config["Settings"] = {
            "device_name": self.device_name_entry.get(),
            "last_file_path": self.file_path if self.file_path else "",
        }
        with open(self.CONFIG_FILE, "w") as configfile:
            self.config.write(configfile)

    def select_log_file(self):
        self.file_path = filedialog.askopenfilename(filetypes=[("Log files", "*.txt")])
        if self.file_path:
            messagebox.showinfo(
                "File Selected",
                f"Selected log file: {os.path.basename(self.file_path)}",
            )
            self.save_config()

    def load_log_file(self):
        try:
            if os.path.isfile(self.file_path):
                messagebox.showinfo(
                    "Log File Loaded",
                    f"Loaded log file from configuration: {os.path.basename(self.file_path)}",
                )
            else:
                messagebox.showwarning(
                    "File Not Found", "The log file path is invalid."
                )
        except Exception as e:
            messagebox.showerror(
                "Error", f"An error occurred while loading the log file:\n{e}"
            )

    def read_and_analyze_log(self):
        if not self.file_path:
            messagebox.showwarning(
                "No File Selected", "Please select a log file first."
            )
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
            "device": self.device_name_entry.get(),
        }

        try:
            response = supabase.table("ij_coding_log_ver1").insert(data).execute()

            if response.data and len(response.data) > 0:
                inserted_record = response.data[0]
                if all(inserted_record[key] == data[key] for key in data):
                    # Return the inserted record ID for potential rollback
                    return True, inserted_record.get("id")
                else:
                    print("Inserted data does not match:", inserted_record)
                    return False, None
            else:
                print("Failed to post data:", response.error)
                return False, None
        except Exception as exception:
            print("An error occurred:", exception)
            return False, None

    def rollback_database(self, record_id):
        """
        Rollback the database entry by deleting the record with the given ID.
        """
        try:
            response = (
                supabase.table("ij_coding_log_ver1")
                .delete()
                .eq("id", record_id)
                .execute()
            )
            if response.data:
                print(f"Successfully rolled back database entry with ID: {record_id}")
                return True
            else:
                print(f"Failed to rollback database entry: {response.error}")
                return False
        except Exception as e:
            print(f"Error during rollback: {e}")
            return False

    def analyze_and_post(self):
        self.save_config()

        job_month = self.month_var.get()
        job_number = self.job_number_entry.get().strip()
        if not job_number.isdigit():
            messagebox.showwarning(
                "Invalid Input", "Please enter a valid numeric job number."
            )
            return
        job = f"{job_month} {job_number}"

        job_quantity = self.quantity_entry.get().strip()
        if not job_quantity.isdigit():
            messagebox.showwarning(
                "Invalid Input", "Please enter a valid job quantity."
            )
            return
        job_quantity = int(job_quantity)

        programmed, verified = self.read_and_analyze_log()

        if programmed is None or verified is None:
            return

        result_message = (
            f"Job: {job}\n"
            f"Job Quantity: {job_quantity}\n"
            f"Programmed: {programmed}\n"
            f"Verified: {verified}\n"
        )

        if programmed >= job_quantity:
            if verified >= job_quantity:
                if programmed > job_quantity + 10 or verified > job_quantity + 10:
                    messagebox.showinfo(
                        "Alert",
                        "The difference between the counts and job quantity is more than 10. Please review.",
                    )

                result_message += (
                    "\nCondition met. Attempting to post data to the database."
                )
                post_success, record_id = self.post_to_database(
                    job, job_quantity, programmed, verified
                )

                if post_success:
                    result_message += "\nData successfully posted to the database."

                    # Try to upload to S3
                    try:
                        with open(self.file_path, "rb") as file:
                            file_content = file.read()

                        success, s3_file_name, error_msg = upload_to_s3(
                            s3, file_content, job, job_quantity
                        )

                        if success:
                            result_message += (
                                f"\nData uploaded to S3 successfully: {s3_file_name}"
                            )
                            show_custom_message("Success", result_message, "accept.png")

                            # Delete log file after successful upload
                            delete_log_file(self.file_path)
                            messagebox.showinfo(
                                "Log File Deleted",
                                "The log file has been deleted successfully.",
                            )
                        else:
                            # S3 upload failed - rollback database entry
                            error_detail = f"\n\nS3 Upload Error Details:\n{error_msg}"

                            rollback_success = self.rollback_database(record_id)
                            if rollback_success:
                                result_message = (
                                    f"S3 upload failed. Database entry has been rolled back.\n\n"
                                    f"Job: {job}\n"
                                    f"The log file was NOT deleted and database entry was NOT saved."
                                    f"{error_detail}"
                                )
                            else:
                                result_message = (
                                    f"CRITICAL: S3 upload failed AND rollback failed!\n\n"
                                    f"Database entry (ID: {record_id}) may need manual deletion.\n"
                                    f"{error_detail}"
                                )

                            show_custom_message(
                                "Upload Failed - Rolled Back",
                                result_message,
                                "close.png",
                            )

                    except FileNotFoundError:
                        error_msg = f"File not found: {self.file_path}"
                        print(error_msg)
                        rollback_success = self.rollback_database(record_id)
                        messagebox.showerror(
                            "File Error",
                            f"{error_msg}\n\nDatabase entry rolled back: {rollback_success}",
                        )
                    except Exception as e:
                        error_msg = f"Unexpected error: {type(e).__name__} - {str(e)}"
                        print(error_msg)
                        rollback_success = self.rollback_database(record_id)
                        messagebox.showerror(
                            "Error",
                            f"{error_msg}\n\nDatabase entry rolled back: {rollback_success}",
                        )
                else:
                    result_message += "\nFailed to post data to the database."
                    show_custom_message("Error", result_message, "close.png")
            else:
                result_message += (
                    "\nCondition not met: Verified count is less than job quantity."
                )
                show_custom_message("Condition Not Met", result_message, "close.png")
        else:
            result_message += (
                "\nCondition not met: Programmed count is less than job count."
            )
            show_custom_message("Condition Not Met", result_message, "close.png")


if __name__ == "__main__":
    root = tk.Tk()
    app = LogAnalyzerApp(root)
    root.mainloop()
