"""
Python application to analyze HP SmartCard programming logs and publish results
as an `analytics.metrics` row over MQTT (EMQX) — fire-and-forget, no downstream
reply is expected or waited for.

Key Features:
- Device and job details entry (8-digit job number, job quantity, model name,
  product code).
- English / Japanese UI, switchable at runtime from the header. The chosen
  language is saved to config.ini and restored on next launch.
- Log file selection and analysis to count keywords (programmed and passed verification).
- Tenant/device identity loaded from .env (TENANT_ID, DEVICE_ID) instead of hardcoded.
- Results published to EMQX over MQTT (TLS, CA-only auth), shaped to match the
  analytics.metrics table: programmed/verified/job_quantity go in `output`,
  job_order/device/model_name/product_code go in `status`. Publish success
  means EMQX accepted the message (QoS 1), not that any downstream engine
  inserted it — there is no reply to wait for and nothing to roll back.
- Raw log uploaded to Supabase Storage.
- MQTT publish + Storage upload run on a background thread with a modal
  "please wait" indicator, so the GUI doesn't freeze while they're in flight.
- Custom message display using Tkinter pop-up windows.
- Configuration management for storing device name, log file path and language.
- Log file deletion after successful upload.
- Two-station mode: when programming and verification run on separate PCs,
  each PC is set to a station role ("program" or "verify"). Each station
  uploads its half to Supabase Storage under {tenant}/pending/{job}/; the
  station that submits second claims the job (atomic create of `claim.json`),
  merges both halves, and publishes ONE metrics row exactly like a single PC
  would. Either PC can finish first.

Classes:
    MqttPublisher: Fire-and-forget publisher over paho-mqtt for EMQX.
    LogAnalyzerApp: Main Tkinter application class for log analysis and publishing.

Dependencies:
- tkinter, ttkbootstrap, PIL (Pillow), paho-mqtt, supabase, python-dotenv, configparser
- Ensure TENANT_ID, DEVICE_ID, MQTT_*, and SUPABASE_* are set in .env before running.

Usage:
    Run the script, enter job details, select a log file, and click
    'Analyze and Post to Database' to process the file and publish results.
"""

import os
import sys
import json
import ssl
import time
import tkinter as tk
import configparser
import threading
import stat
from datetime import datetime, timezone
from tkinter import filedialog, messagebox
from dotenv import load_dotenv
from PIL import Image, ImageTk
import paho.mqtt.client as mqtt
from supabase import create_client, Client
import ttkbootstrap as tb
from ttkbootstrap.constants import *


def get_base_path():
    """
    Directory the app should treat as 'home' for relative paths (.env,
    ca.pem, accept.png, close.png, config.ini, etc).

    - Running as a script: the folder main.py is in (source/).
    - Running as a PyInstaller --onefile build: the folder the .exe itself
      sits in (dist/), NOT sys._MEIPASS — _MEIPASS is a temp extraction
      folder that's wiped after the process exits, so files like ca.pem
      need to live next to the .exe and be read from there, not bundled
      into the onefile archive.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE_PATH = get_base_path()
ACCEPT_ICON = os.path.join(BASE_PATH, "accept.png")
CLOSE_ICON = os.path.join(BASE_PATH, "close.png")


def resolve_path(env_value, default_filename):
    """Resolve a .env path value against BASE_PATH if it isn't absolute."""
    value = env_value or default_filename
    return value if os.path.isabs(value) else os.path.join(BASE_PATH, value)


# ---------------------------------------------------------------------------
# i18n
# ---------------------------------------------------------------------------
JOB_NUMBER_LENGTH = 8

DEFAULT_LANGUAGE = "en"

# Station roles. "combined" = one PC has a log with both programming and
# verification (original behaviour). "program" / "verify" = two-station line.
ROLE_COMBINED = "combined"
ROLE_PROGRAM = "program"
ROLE_VERIFY = "verify"
STATION_ROLES = (ROLE_COMBINED, ROLE_PROGRAM, ROLE_VERIFY)
DEFAULT_ROLE = ROLE_COMBINED

# Keyword each station counts in its own log.
ROLE_KEYWORDS = {
    ROLE_PROGRAM: "programmed",
    ROLE_VERIFY: "passed verification",
}

# Shown in the language dropdown, in each language's own script.
LANGUAGE_NAMES = {
    "en": "English",
    "ja": "日本語",
}

# Segoe UI has no Japanese glyphs; Yu Gothic UI is the native Windows UI
# font for Japanese and also renders Latin text cleanly.
LANGUAGE_FONTS = {
    "en": "Segoe UI",
    "ja": "Yu Gothic UI",
}

TRANSLATIONS = {
    "en": {
        "app_title": "Log Analyzer",
        "header": "HP SmartCard Log Analyzer",
        "language": "Language",
        "mqtt_checking": "MQTT: checking…",
        "mqtt_connected": "MQTT: Connected",
        "mqtt_disconnected": "MQTT: Disconnected",
        "job_details": "Job Details",
        "device_name": "Device Name",
        "job_number": "Job Number (8 digits)",
        "job_quantity": "Job Quantity",
        "model_name": "Model Name",
        "product_code": "Product Code",
        "log_file": "Log File",
        "no_file": "No file selected",
        "browse": "Browse…",
        "analyze_btn": "Analyze and Post to Database",
        "ok": "OK",
        "log_files_filter": "Log files",
        "file_not_found_title": "File Not Found",
        "file_not_found_msg": "The log file path is invalid.",
        "error_title": "Error",
        "load_error_msg": "An error occurred while loading the log file:\n{error}",
        "no_file_title": "No File Selected",
        "no_file_msg": "Please select a log file first.",
        "invalid_input_title": "Invalid Input",
        "invalid_job_number": "Job number must be exactly 8 digits.",
        "invalid_quantity": "Please enter a valid job quantity.",
        "res_job": "Job: {job}",
        "res_qty": "Job Quantity: {qty}",
        "res_programmed": "Programmed: {n}",
        "res_verified": "Verified: {n}",
        "cond_not_met_title": "Condition Not Met",
        "cond_programmed_low": "Condition not met: Programmed count is less than job quantity.",
        "cond_verified_low": "Condition not met: Verified count is less than job quantity.",
        "cond_diff_high": (
            "Condition not met: the difference between the counts and "
            "job quantity is more than 10. Please review."
        ),
        "loading_upload": "Uploading log to storage…",
        "loading_publish": "Publishing to database…",
        "file_error_title": "File Error",
        "file_read_missing": "File not found: {path}",
        "file_read_error": "Unexpected error reading log file: {error}",
        "upload_failed_title": "Upload Failed",
        "upload_failed_msg": (
            "Storage upload failed. Nothing was published — safe to retry "
            "once this is fixed.\n\n"
            "Job: {job}\n"
            "The log file was NOT deleted.\n\n"
            "Storage Upload Error Details:\n{error}"
        ),
        "upload_ok": "Log uploaded to storage successfully.",
        "publish_failed_msg": (
            "Failed to publish data to MQTT.\n"
            "The log file was NOT deleted (it's already in Storage as "
            "{path}, so this is safe to retry — the publish step alone)."
        ),
        "published": "Data published.",
        "lot_published": "Lot creation event published.",
        "lot_failed": "WARNING: Failed to publish lot event: {error}",
        "success_title": "Success",
        "deleted_title": "Log File Deleted",
        "deleted_msg": "The log file has been deleted successfully.",
        # --- Two-station mode ---------------------------------------------
        "station_role": "Station",
        "role_combined": "Single PC (program + verify)",
        "role_program": "Program station",
        "role_verify": "Verify station",
        "role_name_program": "program",
        "role_name_verify": "verify",
        "res_station": "Station: {role}",
        "res_own_count": "Count: {n}",
        "cond_own_low": "Condition not met: {role} count is less than job quantity.",
        "cond_own_high": (
            "Condition not met: {role} count is more than job quantity + 10. "
            "Please review."
        ),
        "partner_unknown": "",
        "partner_checking": "Checking other station…",
        "partner_waiting": "Other station ({role}): not submitted yet",
        "partner_ready": (
            "Other station ({role}) submitted: qty {qty}, model {model}, "
            "product {product}"
        ),
        "partner_check_failed": "Could not check other station: {error}",
        "loading_part": "Uploading this station's log…",
        "loading_merge": "Merging both stations…",
        "part_failed_msg": (
            "Upload of this station's log failed. Nothing was published — "
            "safe to retry.\nThe log file was NOT deleted.\n\n"
            "Error details:\n{error}"
        ),
        "part_uploaded": "This station's log uploaded ({role}).",
        "waiting_title": "Submitted — Waiting",
        "waiting_msg": (
            "Waiting for the {role} station. When it submits job {job}, "
            "the results will be merged and published automatically."
        ),
        "claimed_title": "Submitted",
        "claimed_msg": (
            "The other station is merging and publishing job {job} right now. "
            "Nothing more to do here."
        ),
        "merge_failed_title": "Merge Failed",
        "merge_failed_msg": (
            "Both stations have submitted, but the merge failed. Nothing was "
            "published. Both logs are still in storage — resubmit from either "
            "station to retry.\n\nError details:\n{error}"
        ),
        "mismatch_title": "Job Details Mismatch",
        "mismatch_msg": (
            "The two stations entered different details for job {job}. "
            "Nothing was published.\n\n{fields}\n\n"
            "Fix the details on the station that is wrong and press the "
            "button again. If its local log was already deleted, the copy "
            "in storage is reused automatically."
        ),
        "reused_part": "Local log not found — reusing this station's copy in storage.",
        "mismatch_field": "{field}: this station \"{mine}\" vs other station \"{theirs}\"",
        "merged_ok": "Merged with the {role} station's log.",
        "split_publish_failed_msg": (
            "Failed to publish data to MQTT. Both logs are still in storage — "
            "resubmit from either station to retry."
        ),
    },
    "ja": {
        "app_title": "ログアナライザー",
        "header": "HP SmartCard ログアナライザー",
        "language": "言語",
        "mqtt_checking": "MQTT: 確認中…",
        "mqtt_connected": "MQTT: 接続済み",
        "mqtt_disconnected": "MQTT: 未接続",
        "job_details": "ジョブ詳細",
        "device_name": "デバイス名",
        "job_number": "ジョブオーダーNO.",
        "job_quantity": "ジョブ数量",
        "model_name": "モデル名",
        "product_code": "商品コード",
        "log_file": "ログファイル",
        "no_file": "ファイルが選択されていません",
        "browse": "参照…",
        "analyze_btn": "解析してデータベースに送信",
        "ok": "OK",
        "log_files_filter": "ログファイル",
        "file_not_found_title": "ファイルが見つかりません",
        "file_not_found_msg": "ログファイルのパスが無効です。",
        "error_title": "エラー",
        "load_error_msg": "ログファイルの読み込み中にエラーが発生しました:\n{error}",
        "no_file_title": "ファイル未選択",
        "no_file_msg": "先にログファイルを選択してください。",
        "invalid_input_title": "入力エラー",
        "invalid_job_number": "ジョブ番号は8桁の数字で入力してください。",
        "invalid_quantity": "有効なジョブ数量を入力してください。",
        "res_job": "ジョブ: {job}",
        "res_qty": "ジョブ数量: {qty}",
        "res_programmed": "書き込み済み: {n}",
        "res_verified": "検証合格: {n}",
        "cond_not_met_title": "条件未達",
        "cond_programmed_low": "条件未達: 書き込み数がジョブ数量より少ないです。",
        "cond_verified_low": "条件未達: 検証合格数がジョブ数量より少ないです。",
        "cond_diff_high": "条件未達: カウントとジョブ数量の差が10を超えています。確認してください。",
        "loading_upload": "ログをストレージにアップロード中…",
        "loading_publish": "データベースに送信中…",
        "file_error_title": "ファイルエラー",
        "file_read_missing": "ファイルが見つかりません: {path}",
        "file_read_error": "ログファイル読み込み中に予期しないエラーが発生しました: {error}",
        "upload_failed_title": "アップロード失敗",
        "upload_failed_msg": (
            "ストレージへのアップロードに失敗しました。データは送信されていません。"
            "問題を解決した後に再試行できます。\n\n"
            "ジョブ: {job}\n"
            "ログファイルは削除されていません。\n\n"
            "エラー詳細:\n{error}"
        ),
        "upload_ok": "ログをストレージに正常にアップロードしました。",
        "publish_failed_msg": (
            "MQTTへのデータ送信に失敗しました。\n"
            "ログファイルは削除されていません(ストレージには {path} として"
            "保存済みのため、送信のみ再試行できます)。"
        ),
        "published": "データを送信しました。",
        "lot_published": "ロット作成イベントを送信しました。",
        "lot_failed": "警告: ロットイベントの送信に失敗しました: {error}",
        "success_title": "成功",
        "deleted_title": "ログファイル削除",
        "deleted_msg": "ログファイルを削除しました。",
        # --- 2台構成モード -------------------------------------------------
        "station_role": "ステーション",
        "role_combined": "1台構成 (書き込み+検証)",
        "role_program": "書き込みステーション",
        "role_verify": "検証ステーション",
        "role_name_program": "書き込み",
        "role_name_verify": "検証",
        "res_station": "ステーション: {role}",
        "res_own_count": "カウント: {n}",
        "cond_own_low": "条件未達: {role}数がジョブ数量より少ないです。",
        "cond_own_high": "条件未達: {role}数がジョブ数量+10を超えています。確認してください。",
        "partner_unknown": "",
        "partner_checking": "相手ステーションを確認中…",
        "partner_waiting": "相手ステーション({role}): 未送信",
        "partner_ready": (
            "相手ステーション({role})送信済み: 数量 {qty}、モデル {model}、"
            "商品 {product}"
        ),
        "partner_check_failed": "相手ステーションを確認できません: {error}",
        "loading_part": "このステーションのログをアップロード中…",
        "loading_merge": "両ステーションの結果を統合中…",
        "part_failed_msg": (
            "このステーションのログのアップロードに失敗しました。データは送信されていません。"
            "再試行できます。\nログファイルは削除されていません。\n\n"
            "エラー詳細:\n{error}"
        ),
        "part_uploaded": "このステーションのログをアップロードしました({role})。",
        "waiting_title": "送信済み — 待機中",
        "waiting_msg": (
            "{role}ステーションを待っています。ジョブ {job} が送信されると、"
            "自動的に統合・送信されます。"
        ),
        "claimed_title": "送信済み",
        "claimed_msg": (
            "相手ステーションがジョブ {job} を統合・送信中です。"
            "こちらでの操作は不要です。"
        ),
        "merge_failed_title": "統合失敗",
        "merge_failed_msg": (
            "両ステーションの送信は完了していますが、統合に失敗しました。"
            "データは送信されていません。両方のログはストレージに残っているため、"
            "どちらかのステーションから再送信してください。\n\nエラー詳細:\n{error}"
        ),
        "mismatch_title": "ジョブ詳細の不一致",
        "mismatch_msg": (
            "2台のステーションでジョブ {job} の入力内容が異なります。"
            "データは送信されていません。\n\n{fields}\n\n"
            "間違っている方のステーションで修正して再度ボタンを押してください。"
            "ローカルのログが削除済みの場合は、ストレージのコピーが自動的に使われます。"
        ),
        "reused_part": "ローカルログが見つからないため、ストレージのコピーを使用します。",
        "mismatch_field": "{field}: こちら「{mine}」/ 相手「{theirs}」",
        "merged_ok": "{role}ステーションのログと統合しました。",
        "split_publish_failed_msg": (
            "MQTTへのデータ送信に失敗しました。両方のログはストレージに残っているため、"
            "どちらかのステーションから再送信してください。"
        ),
    },
}


# ---------------------------------------------------------------------------
# Load environment variables from .env file
# ---------------------------------------------------------------------------
load_dotenv(os.path.join(BASE_PATH, ".env"))

# --- Tenant / device / machine identity ---------------------------------
TENANT_ID = os.environ.get("TENANT_ID")
DEVICE_ID = os.environ.get("DEVICE_ID")
MACHINE_ID = os.environ.get("MACHINE_ID")

if not TENANT_ID or not DEVICE_ID:
    raise RuntimeError(
        "TENANT_ID and DEVICE_ID must be set in .env before running this app."
    )

if not MACHINE_ID:
    raise RuntimeError("MACHINE_ID must be set in .env before running this app.")

# --- Supabase (used for Storage now — table insert goes over MQTT instead) --
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in .env")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

SUPABASE_STORAGE_BUCKET = os.environ.get("SUPABASE_STORAGE_BUCKET")
if not SUPABASE_STORAGE_BUCKET:
    raise RuntimeError("SUPABASE_STORAGE_BUCKET must be set in .env")

# --- MQTT / EMQX -------------------------------------------------------------
# Mirrors the gopub-edge EMQX setup: TLS on 8883, CA-only auth (ssl:// scheme).
MQTT_HOST = os.environ.get("MQTT_HOST")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))
MQTT_CA_CERT = os.environ.get("MQTT_CA_CERT")  # path to CA cert, CA-only auth
if MQTT_CA_CERT:
    MQTT_CA_CERT = resolve_path(MQTT_CA_CERT, "ca.pem")
MQTT_USERNAME = os.environ.get("MQTT_USERNAME")  # optional
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD")  # optional
MQTT_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", f"log-analyzer-{DEVICE_ID}")

MQTT_TOPIC = os.environ.get("MQTT_TOPIC")

if not MQTT_TOPIC:
    raise RuntimeError("MQTT_TOPIC must be set in .env")

# How long to wait for the broker to accept the publish (QoS 1 PUBACK) —
# this only confirms EMQX received the message, not that any downstream
# engine processed it.
MQTT_PUBLISH_TIMEOUT_SEC = float(os.environ.get("MQTT_PUBLISH_TIMEOUT_SEC", "5"))

# --- Heartbeat ---------------------------------------------------------------
MQTT_HEARTBEAT_INTERVAL_SEC = float(os.environ.get("MQTT_HEARTBEAT_INTERVAL_SEC", "30"))
HEARTBEAT_RESOLUTION = os.environ.get("HEARTBEAT_RESOLUTION", "fast")
HEARTBEAT_METRICS_KIND = os.environ.get("HEARTBEAT_METRICS_KIND", "event")

# Version string for the "fw" field in the heartbeat payload.
APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")

# analytics.metrics.kind / resolution check constraints — a completed job
# posting is a one-off event, not an aggregate or a rolling output window.
METRICS_KIND = "event"
METRICS_RESOLUTION = "event"

# --- Two-station mode ---------------------------------------------------------
# device_id written on the merged metrics row. Set the same value on both PCs
# of a two-station line; if unset, the program station's DEVICE_ID is used so
# the row is the same no matter which PC does the merge.
LINE_DEVICE_ID = os.environ.get("LINE_DEVICE_ID")

# A claim.json older than this (and not marked published) is treated as left
# behind by a crashed merge and may be taken over.
PENDING_CLAIM_STALE_SEC = float(os.environ.get("PENDING_CLAIM_STALE_SEC", "300"))

_APP_START_MONOTONIC = time.monotonic()


def _iso_ms_z(dt):
    """Format like '2026-08-11T03:01:32.580Z'."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _build_heartbeat_row():
    now = datetime.now(timezone.utc)
    return {
        "tenant_id": TENANT_ID,
        "device_id": DEVICE_ID,
        "resolution": HEARTBEAT_RESOLUTION,
        "kind": HEARTBEAT_METRICS_KIND,
        "status": {
            "fw": APP_VERSION,
            "ts": _iso_ms_z(now),
            "kind": "heartbeat",
            "uptime_s": int(time.monotonic() - _APP_START_MONOTONIC),
        },
    }


class MqttPublisher:
    """
    Fire-and-forget publisher over paho-mqtt for EMQX. Publishes with QoS 1
    and waits only for the broker's PUBACK.

    Also publishes a periodic heartbeat row every MQTT_HEARTBEAT_INTERVAL_SEC,
    over the same insert pipeline as job rows.
    """

    def __init__(self):
        self.connected = False
        self._heartbeat_stop = threading.Event()
        self.client = mqtt.Client(client_id=MQTT_CLIENT_ID, protocol=mqtt.MQTTv311)

        if MQTT_USERNAME:
            self.client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

        if MQTT_CA_CERT:
            self.client.tls_set(ca_certs=MQTT_CA_CERT, cert_reqs=ssl.CERT_REQUIRED)

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

        if not MQTT_HOST:
            raise RuntimeError("MQTT_HOST must be set in .env")

        self.client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
        self.client.loop_start()

        threading.Thread(target=self._heartbeat_loop, daemon=True).start()

    def _heartbeat_loop(self):
        while not self._heartbeat_stop.is_set():
            success, error = self.publish(_build_heartbeat_row())
            if not success:
                print(f"[MQTT] Heartbeat publish failed: {error}")
            self._heartbeat_stop.wait(MQTT_HEARTBEAT_INTERVAL_SEC)

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            print(f"[MQTT] Connected, publishing to {MQTT_TOPIC}")
        else:
            self.connected = False
            print(f"[MQTT] Connect failed, rc={rc}")

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        if rc != 0:
            print(f"[MQTT] Unexpected disconnect, rc={rc}")

    def publish(self, row):
        """
        Publish `row` to MQTT_TOPIC and wait only for the broker to accept
        it (QoS 1). Returns (success: bool, error: str or None)
        """
        try:
            result = self.client.publish(MQTT_TOPIC, json.dumps(row), qos=1)
            result.wait_for_publish(timeout=MQTT_PUBLISH_TIMEOUT_SEC)
            print(f"[MQTT] Published to {MQTT_TOPIC} (mid={result.mid})")
            return True, None
        except (ValueError, RuntimeError) as e:
            print(f"[MQTT] Publish failed: {e}")
            return False, f"MQTT publish failed: {e}"

    def close(self):
        self._heartbeat_stop.set()
        self.client.loop_stop()
        self.client.disconnect()


# Single shared MQTT client for the app's lifetime.
mqtt_client = MqttPublisher()


def build_lot_envelope(machine_id=None):
    """Lot-creation request in the shape gokafka-raw's consumer expects."""
    return {
        "tenant_id": TENANT_ID,
        "machine_id": machine_id or MACHINE_ID,
        "kind": "lot",
    }


def publish_lot_record(machine_id=None):
    """Fire-and-forget MQTT publish of a lot-creation event."""
    return mqtt_client.publish(build_lot_envelope(machine_id))


def check_file_exists_in_storage(bucket_name, file_name):
    """Check if an object already exists in a Supabase Storage bucket."""
    parts = file_name.rsplit("/", 1)
    prefix, base_name = (parts[0], parts[1]) if len(parts) > 1 else ("", parts[0])
    try:
        entries = supabase.storage.from_(bucket_name).list(prefix)
    except Exception as e:
        raise RuntimeError(f"Error listing '{prefix}' in bucket '{bucket_name}': {e}")
    return any(entry.get("name") == base_name for entry in entries)


def upload_to_storage(data, job, job_quantity):
    """
    Uploads data to Supabase Storage with a dynamically generated filename.
    If a file with the same name exists, a sequence number is added.
    Verifies the upload by re-checking object existence.

    Returns:
    (success: bool, file_name: str or None, error_message: str or None)
    """
    bucket_name = SUPABASE_STORAGE_BUCKET

    current_date = datetime.now().strftime("%Y-%m-%d")
    tenant_folder = f"{TENANT_ID}/logs"
    base_file_name = f"{tenant_folder}/{current_date}-{job}-{job_quantity}.txt"

    sequence = 1
    file_name = base_file_name

    try:
        while check_file_exists_in_storage(bucket_name, file_name):
            file_name = (
                f"{tenant_folder}/{current_date}-{job}-{job_quantity}-{sequence}.txt"
            )
            sequence += 1
    except Exception as e:
        error_msg = f"Error checking file existence: {str(e)}"
        print(f"[ERROR] {error_msg}")
        return False, None, error_msg

    try:
        supabase.storage.from_(bucket_name).upload(
            path=file_name,
            file=data,
            file_options={"content-type": "text/plain"},
        )

        if not check_file_exists_in_storage(bucket_name, file_name):
            error_msg = (
                "Upload failed verification — file does not exist in "
                "Supabase Storage after upload."
            )
            print(error_msg)
            return False, None, error_msg

        print(f"Verified upload OK → {file_name}")
        return True, file_name, None

    except Exception as e:
        error_msg = (
            f"Unexpected error during Supabase Storage upload: "
            f"{type(e).__name__} - {str(e)}"
        )
        print(f"[ERROR] {error_msg}")
        return False, None, error_msg


# ---------------------------------------------------------------------------
# Two-station mode: pending halves in Supabase Storage
# ---------------------------------------------------------------------------
# Layout, per job:
#   {tenant}/pending/{job}/program.txt   raw log from the program station
#   {tenant}/pending/{job}/program.json  that station's job details + count
#   {tenant}/pending/{job}/verify.txt
#   {tenant}/pending/{job}/verify.json
#   {tenant}/pending/{job}/claim.json   created (no upsert) by the merger
#
# The .json is written after the .txt, so a .json existing means that half is
# complete. Creating claim.json without upsert is atomic on the Storage side: the
# second creator gets a 409 Duplicate, so only one PC ever publishes.


class ClaimHeld(Exception):
    """Another station holds a live claim on this job."""


def other_role(role):
    return ROLE_VERIFY if role == ROLE_PROGRAM else ROLE_PROGRAM


def pending_dir(job):
    return f"{TENANT_ID}/pending/{job}"


def _bucket():
    return supabase.storage.from_(SUPABASE_STORAGE_BUCKET)


def _is_duplicate_error(exc):
    text = str(exc).lower()
    return "409" in text or "duplicate" in text or "already exists" in text


def count_keyword(data, role):
    """Count the station's keyword in a raw log (bytes or str)."""
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    return data.lower().count(ROLE_KEYWORDS[role])


def pending_part_exists(job, role, ext="json"):
    return check_file_exists_in_storage(
        SUPABASE_STORAGE_BUCKET, f"{pending_dir(job)}/{role}.{ext}"
    )


def download_pending_log(job, role):
    return _bucket().download(f"{pending_dir(job)}/{role}.txt")


def read_pending_meta(job, role):
    """Return the station's meta dict, or None if that half isn't submitted."""
    if not pending_part_exists(job, role, "json"):
        return None
    raw = _bucket().download(f"{pending_dir(job)}/{role}.json")
    return json.loads(raw.decode("utf-8"))


def upload_pending_part(job, role, log_bytes, meta):
    """
    Upload (or overwrite) this station's half. Pass log_bytes=None to keep the
    log already in storage and only rewrite the details (.json).
    """
    base = pending_dir(job)
    if log_bytes is not None:
        _bucket().upload(
            path=f"{base}/{role}.txt",
            file=log_bytes,
            file_options={"content-type": "text/plain", "upsert": "true"},
        )
    _bucket().upload(
        path=f"{base}/{role}.json",
        file=json.dumps(meta, ensure_ascii=False).encode("utf-8"),
        file_options={"content-type": "application/json", "upsert": "true"},
    )
    if not pending_part_exists(job, role, "json"):
        raise RuntimeError("Upload failed verification — part not found after upload.")


def _write_claim(job, state, upsert):
    payload = {
        "by": DEVICE_ID,
        "state": state,
        "at": _iso_ms_z(datetime.now(timezone.utc)),
        "epoch": time.time(),
    }
    _bucket().upload(
        path=f"{pending_dir(job)}/claim.json",
        file=json.dumps(payload).encode("utf-8"),
        file_options={
            "content-type": "application/json",
            "upsert": "true" if upsert else "false",
        },
    )


def try_claim(job):
    """
    Atomically claim the merge for `job`. Returns True if this PC won.
    Raises ClaimHeld if another PC holds a live (or already published) claim.
    A stale, unpublished claim (crashed merge) is taken over.
    """
    try:
        _write_claim(job, "merging", upsert=False)
        return True
    except Exception as e:
        if not _is_duplicate_error(e):
            raise

    # Someone holds it — is it stale?
    try:
        existing = json.loads(_bucket().download(f"{pending_dir(job)}/claim.json"))
    except Exception:
        raise ClaimHeld()

    age = time.time() - float(existing.get("epoch", 0))
    if existing.get("state") == "published" or age < PENDING_CLAIM_STALE_SEC:
        raise ClaimHeld()

    print(f"[PENDING] Taking over stale claim on {job} (age {int(age)}s)")
    release_claim(job)
    try:
        _write_claim(job, "merging", upsert=False)
        return True
    except Exception as e:
        if _is_duplicate_error(e):
            raise ClaimHeld()
        raise


def release_claim(job):
    try:
        _bucket().remove([f"{pending_dir(job)}/claim.json"])
    except Exception as e:
        print(f"[PENDING] Failed to release claim on {job}: {e}")


def finish_pending(job):
    """
    After a successful publish: mark the claim as published first (so a
    leftover folder can never be merged twice), then remove the halves, then
    the claim itself.
    """
    base = pending_dir(job)
    try:
        _write_claim(job, "published", upsert=True)
    except Exception as e:
        print(f"[PENDING] Failed to mark {job} published: {e}")
    try:
        _bucket().remove(
            [f"{base}/{r}.{ext}" for r in (ROLE_PROGRAM, ROLE_VERIFY) for ext in ("txt", "json")]
        )
        _bucket().remove([f"{base}/claim.json"])
    except Exception as e:
        print(f"[PENDING] Cleanup of {base} failed (claim kept as published): {e}")


def build_merged_log(program_meta, program_log, verify_meta, verify_log):
    """One file for logs/ so downstream still sees a single storage_path."""

    def header(role, meta):
        return (
            f"===== {role.upper()} STATION | device_id={meta.get('device_id')} "
            f"| device={meta.get('device_name')} | submitted={meta.get('submitted_at')} =====\n"
        ).encode("utf-8")

    return (
        header(ROLE_PROGRAM, program_meta)
        + program_log
        + b"\n\n"
        + header(ROLE_VERIFY, verify_meta)
        + verify_log
    )


def show_custom_message(title, message, icon_path=None, ok_text="OK", font=None):
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

    label_kwargs = {"font": font} if font else {}
    message_label = tk.Label(
        custom_message_box,
        text=message,
        wraplength=350,
        padx=10,
        pady=10,
        **label_kwargs,
    )
    message_label.pack()

    ok_button = tk.Button(
        custom_message_box,
        text=ok_text,
        command=custom_message_box.destroy,
        **label_kwargs,
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
    CONFIG_FILE = os.path.join(BASE_PATH, "config.ini")

    def __init__(self, root):
        self.root = root
        self.root.geometry("620x640")
        self.root.minsize(560, 600)
        self.file_path = None

        # (widget, font_size, bold) — re-fonted when the language changes,
        # since Japanese needs a different font family than English.
        self._font_widgets = []

        self.config = configparser.ConfigParser()
        self.load_config()

        self.setup_gui()
        self._apply_language()
        self.refresh_mqtt_status()

        if self.last_file_path:
            self.file_path = self.last_file_path
            threading.Thread(target=self.load_log_file, daemon=True).start()
        else:
            self.select_log_file()

    # ------------------------------------------------------------------
    # i18n helpers
    # ------------------------------------------------------------------
    def t(self, key, **kwargs):
        """Translate `key` into the current language (falls back to English)."""
        table = TRANSLATIONS.get(self.language, TRANSLATIONS[DEFAULT_LANGUAGE])
        text = table.get(key) or TRANSLATIONS[DEFAULT_LANGUAGE].get(key, key)
        return text.format(**kwargs) if kwargs else text

    def font(self, size=10, bold=False):
        family = LANGUAGE_FONTS.get(self.language, LANGUAGE_FONTS[DEFAULT_LANGUAGE])
        return (family, size, "bold") if bold else (family, size)

    def _track_font(self, widget, size=10, bold=False):
        self._font_widgets.append((widget, size, bold))
        return widget

    def _on_language_selected(self, _event=None):
        selected_name = self.language_var.get()
        for code, name in LANGUAGE_NAMES.items():
            if name == selected_name:
                self.language = code
                break
        self.save_config()
        self._apply_language()

    # ------------------------------------------------------------------
    # Station role
    # ------------------------------------------------------------------
    @property
    def is_split(self):
        return self.station_role in (ROLE_PROGRAM, ROLE_VERIFY)

    def role_label(self, role):
        """Short in-sentence name for a role ('program' / '書き込み')."""
        return self.t(f"role_name_{role}")

    def _role_display_names(self):
        return [self.t(f"role_{r}") for r in STATION_ROLES]

    def _on_role_selected(self, _event=None):
        idx = self.role_menu.current()
        if 0 <= idx < len(STATION_ROLES):
            self.station_role = STATION_ROLES[idx]
        self.save_config()
        self._partner_state = None
        self._render_partner_status()
        self._check_partner()

    def _on_job_number_changed(self, *_):
        self._partner_state = None
        self._render_partner_status()
        if len(self.job_number_var.get()) == JOB_NUMBER_LENGTH:
            self._check_partner()

    def _check_partner(self):
        """Look up the other station's half for the typed job (background)."""
        job = self.job_number_var.get().strip()
        if not self.is_split or len(job) != JOB_NUMBER_LENGTH:
            return
        role = other_role(self.station_role)
        self._partner_state = ("checking",)
        self._render_partner_status()

        def worker():
            try:
                meta = read_pending_meta(job, role)
                state = ("ready", meta) if meta else ("waiting",)
            except Exception as e:
                state = ("failed", str(e))
            self.root.after(0, lambda: self._apply_partner_result(job, state))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_partner_result(self, job, state):
        if job != self.job_number_var.get().strip():
            return  # job number changed while we were checking
        self._partner_state = state
        if state[0] == "ready":
            # Prefill empty fields from the station that submitted first.
            meta = state[1]
            for entry, key in (
                (self.quantity_entry, "job_quantity"),
                (self.model_name_entry, "model_name"),
                (self.product_code_entry, "product_code"),
            ):
                if not entry.get().strip() and meta.get(key) not in (None, ""):
                    entry.insert(0, str(meta[key]))
        self._render_partner_status()

    def _render_partner_status(self):
        if not self.is_split:
            self.partner_label.grid_remove()
            return
        self.partner_label.grid()
        role = self.role_label(other_role(self.station_role))
        state = getattr(self, "_partner_state", None)
        if state is None:
            text, style = self.t("partner_unknown"), SECONDARY
        elif state[0] == "checking":
            text, style = self.t("partner_checking"), SECONDARY
        elif state[0] == "waiting":
            text, style = self.t("partner_waiting", role=role), WARNING
        elif state[0] == "ready":
            meta = state[1]
            text = self.t(
                "partner_ready",
                role=role,
                qty=meta.get("job_quantity", "?"),
                model=meta.get("model_name") or "-",
                product=meta.get("product_code") or "-",
            )
            style = SUCCESS
        else:
            text, style = self.t("partner_check_failed", error=state[1]), DANGER
        self.partner_label.configure(text=text, bootstyle=style)

    def _apply_language(self):
        """Refresh every translatable label/font for the current language."""
        self.root.title(self.t("app_title"))

        for widget, size, bold in self._font_widgets:
            try:
                widget.configure(font=self.font(size, bold))
            except tk.TclError:
                pass

        self.header_label.configure(text=self.t("header"))
        self.language_label.configure(text=self.t("language"))
        self.language_var.set(LANGUAGE_NAMES[self.language])

        self.details_frame.configure(text=self.t("job_details"))
        self.device_name_label.configure(text=self.t("device_name"))
        self.job_number_label.configure(text=self.t("job_number"))
        self.quantity_label.configure(text=self.t("job_quantity"))
        self.model_name_label.configure(text=self.t("model_name"))
        self.product_code_label.configure(text=self.t("product_code"))
        self.role_label_widget.configure(text=self.t("station_role"))
        self.role_menu.configure(values=self._role_display_names())
        self.role_menu.current(STATION_ROLES.index(self.station_role))
        self._render_partner_status()

        self.file_card.configure(text=self.t("log_file"))
        if self.file_path and os.path.isfile(self.file_path):
            self.file_label.configure(text=os.path.basename(self.file_path))
        else:
            self.file_label.configure(text=self.t("no_file"))
        self.browse_button.configure(text=self.t("browse"))

        self.analyze_button.configure(text=self.t("analyze_btn"))

        self._render_mqtt_status()

    # ------------------------------------------------------------------
    # GUI
    # ------------------------------------------------------------------
    def setup_gui(self):
        container = tb.Frame(self.root, padding=24)
        container.pack(fill=BOTH, expand=YES)

        # --- Header: title + language switch + MQTT indicator -------------
        header = tb.Frame(container)
        header.pack(fill=X, pady=(0, 18))

        self.header_label = self._track_font(tb.Label(header), 17, bold=True)
        self.header_label.pack(side=LEFT)

        right_wrap = tb.Frame(header)
        right_wrap.pack(side=RIGHT)

        lang_wrap = tb.Frame(right_wrap)
        lang_wrap.pack(side=TOP, anchor="e", pady=(0, 4))
        self.language_label = self._track_font(tb.Label(lang_wrap), 9)
        self.language_label.pack(side=LEFT, padx=(0, 6))
        self.language_var = tk.StringVar(value=LANGUAGE_NAMES[self.language])
        self.language_menu = self._track_font(
            tb.Combobox(
                lang_wrap,
                textvariable=self.language_var,
                values=list(LANGUAGE_NAMES.values()),
                state="readonly",
                width=8,
            ),
            9,
        )
        self.language_menu.pack(side=LEFT)
        self.language_menu.bind("<<ComboboxSelected>>", self._on_language_selected)

        status_wrap = tb.Frame(right_wrap)
        status_wrap.pack(side=TOP, anchor="e")
        self.mqtt_status_dot = tb.Label(status_wrap, text="●", font=("Segoe UI", 13))
        self.mqtt_status_dot.pack(side=LEFT, padx=(0, 5))
        self.mqtt_status_label = self._track_font(tb.Label(status_wrap), 10)
        self.mqtt_status_label.pack(side=LEFT)

        # --- Job details card -----------------------------------------------
        self.details_frame = self._track_font(tb.Labelframe(container, padding=16), 10)
        details = self.details_frame
        details.pack(fill=X, pady=(0, 16))
        details.columnconfigure(0, weight=1)
        details.columnconfigure(1, weight=1)

        # Device name | Station role
        self.device_name_label = self._track_font(tb.Label(details), 10)
        self.device_name_label.grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.device_name_entry = self._track_font(tb.Entry(details), 10)
        self.device_name_entry.grid(
            row=1, column=0, sticky="ew", pady=(0, 12), padx=(0, 8)
        )
        self.device_name_entry.insert(0, self.device_name)

        self.role_label_widget = self._track_font(tb.Label(details), 10)
        self.role_label_widget.grid(row=0, column=1, sticky="w", pady=(0, 4))
        self.role_menu = self._track_font(
            tb.Combobox(details, state="readonly", values=self._role_display_names()),
            10,
        )
        self.role_menu.grid(row=1, column=1, sticky="ew", pady=(0, 12))
        self.role_menu.bind("<<ComboboxSelected>>", self._on_role_selected)

        # Job number (8 digits) | Job quantity
        self.job_number_label = self._track_font(tb.Label(details), 10)
        self.job_number_label.grid(row=2, column=0, sticky="w", pady=(0, 4))
        job_vcmd = (self.root.register(self._validate_job_number), "%P")
        self.job_number_var = tk.StringVar()
        self.job_number_entry = self._track_font(
            tb.Entry(
                details,
                textvariable=self.job_number_var,
                validate="key",
                validatecommand=job_vcmd,
            ),
            10,
        )
        self.job_number_var.trace_add("write", self._on_job_number_changed)
        self.job_number_entry.grid(
            row=3, column=0, sticky="ew", pady=(0, 12), padx=(0, 8)
        )

        self.quantity_label = self._track_font(tb.Label(details), 10)
        self.quantity_label.grid(row=2, column=1, sticky="w", pady=(0, 4))
        self.quantity_entry = self._track_font(tb.Entry(details), 10)
        self.quantity_entry.grid(row=3, column=1, sticky="ew", pady=(0, 12))

        # Model name | Product code
        self.model_name_label = self._track_font(tb.Label(details), 10)
        self.model_name_label.grid(row=4, column=0, sticky="w", pady=(0, 4))
        self.model_name_entry = self._track_font(tb.Entry(details), 10)
        self.model_name_entry.grid(row=5, column=0, sticky="ew", padx=(0, 8))

        self.product_code_label = self._track_font(tb.Label(details), 10)
        self.product_code_label.grid(row=4, column=1, sticky="w", pady=(0, 4))
        self.product_code_entry = self._track_font(tb.Entry(details), 10)
        self.product_code_entry.grid(row=5, column=1, sticky="ew")

        # Other station's status (two-station mode only)
        self._partner_state = None
        self.partner_label = self._track_font(
            tb.Label(details, bootstyle=SECONDARY, wraplength=520), 9
        )
        self.partner_label.grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))

        # --- Log file card ------------------------------------------------
        self.file_card = self._track_font(tb.Labelframe(container, padding=16), 10)
        self.file_card.pack(fill=X, pady=(0, 16))
        self.file_card.columnconfigure(0, weight=1)

        self.file_label = self._track_font(
            tb.Label(self.file_card, bootstyle=SECONDARY, wraplength=420), 10
        )
        self.file_label.grid(row=0, column=0, sticky="w")

        self.browse_button = tb.Button(
            self.file_card,
            command=self.select_log_file,
            bootstyle=(SECONDARY, OUTLINE),
        )
        self.browse_button.grid(row=0, column=1, padx=(12, 0))

        # --- Primary action --------------------------------------------------
        self.analyze_button = tb.Button(
            container,
            command=self.analyze_and_post,
            bootstyle=SUCCESS,
        )
        self.analyze_button.pack(fill=X, ipady=10)

    @staticmethod
    def _validate_job_number(proposed):
        """Keystroke filter: digits only, max JOB_NUMBER_LENGTH characters."""
        return proposed == "" or (
            proposed.isdigit() and len(proposed) <= JOB_NUMBER_LENGTH
        )

    def _render_mqtt_status(self):
        if mqtt_client.connected:
            self.mqtt_status_dot.configure(bootstyle=SUCCESS)
            self.mqtt_status_label.configure(text=self.t("mqtt_connected"))
        else:
            self.mqtt_status_dot.configure(bootstyle=DANGER)
            self.mqtt_status_label.configure(text=self.t("mqtt_disconnected"))

    def refresh_mqtt_status(self):
        """Poll the shared MQTT client's connection state every 2 s."""
        self._render_mqtt_status()
        self.root.after(2000, self.refresh_mqtt_status)

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------
    def load_config(self):
        self.device_name = ""
        self.last_file_path = ""
        self.language = DEFAULT_LANGUAGE
        self.station_role = DEFAULT_ROLE

        if os.path.exists(self.CONFIG_FILE):
            self.config.read(self.CONFIG_FILE, encoding="utf-8")
            self.device_name = self.config.get("Settings", "device_name", fallback="")
            self.last_file_path = self.config.get(
                "Settings", "last_file_path", fallback=""
            )
            self.language = self.config.get(
                "Settings", "language", fallback=DEFAULT_LANGUAGE
            )
            self.station_role = self.config.get(
                "Settings", "station_role", fallback=DEFAULT_ROLE
            )

        if self.language not in TRANSLATIONS:
            self.language = DEFAULT_LANGUAGE
        if self.station_role not in STATION_ROLES:
            self.station_role = DEFAULT_ROLE

    def save_config(self):
        self.config["Settings"] = {
            "device_name": self.device_name_entry.get(),
            "last_file_path": self.file_path if self.file_path else "",
            "language": self.language,
            "station_role": self.station_role,
        }
        with open(self.CONFIG_FILE, "w", encoding="utf-8") as configfile:
            self.config.write(configfile)

    # ------------------------------------------------------------------
    # File handling
    # ------------------------------------------------------------------
    def select_log_file(self):
        chosen = filedialog.askopenfilename(
            filetypes=[(self.t("log_files_filter"), "*.txt")]
        )
        if chosen:
            self.file_path = chosen
            self.file_label.configure(
                text=os.path.basename(self.file_path), bootstyle=DEFAULT
            )
            self.save_config()

    def load_log_file(self):
        # Runs on a background thread — all Tk calls go through root.after().
        try:
            if os.path.isfile(self.file_path):
                name = os.path.basename(self.file_path)
                self.root.after(
                    0,
                    lambda: self.file_label.configure(text=name, bootstyle=DEFAULT),
                )
            else:
                self.root.after(
                    0,
                    lambda: messagebox.showwarning(
                        self.t("file_not_found_title"),
                        self.t("file_not_found_msg"),
                    ),
                )
        except Exception as e:
            self.root.after(
                0,
                lambda e=e: messagebox.showerror(
                    self.t("error_title"), self.t("load_error_msg", error=e)
                ),
            )

    # ------------------------------------------------------------------
    # Loading modal
    # ------------------------------------------------------------------
    def _show_loading(self, message):
        self.loading_win = tb.Toplevel(self.root)
        self.loading_win.title("")
        self.loading_win.resizable(False, False)
        self.loading_win.transient(self.root)
        self.loading_win.protocol("WM_DELETE_WINDOW", lambda: None)

        frame = tb.Frame(self.loading_win, padding=24)
        frame.pack(fill=BOTH, expand=YES)

        self.loading_label = tb.Label(frame, text=message, font=self.font(10))
        self.loading_label.pack(pady=(0, 12))

        self.loading_bar = tb.Progressbar(
            frame, mode="indeterminate", bootstyle=INFO, length=240
        )
        self.loading_bar.pack()
        self.loading_bar.start(12)

        self.root.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() // 2) - 140
        y = self.root.winfo_y() + (self.root.winfo_height() // 2) - 50
        self.loading_win.geometry(f"280x110+{x}+{y}")
        self.loading_win.grab_set()

        # Don't allow switching language mid-request.
        self.language_menu.configure(state=DISABLED)
        self.role_menu.configure(state=DISABLED)

    def _update_loading(self, message):
        if getattr(self, "loading_win", None) and self.loading_win.winfo_exists():
            self.loading_label.configure(text=message)

    def _hide_loading(self):
        win = getattr(self, "loading_win", None)
        if win and win.winfo_exists():
            self.loading_bar.stop()
            win.grab_release()
            win.destroy()
        self.loading_win = None
        self.language_menu.configure(state="readonly")
        self.role_menu.configure(state="readonly")

    # ------------------------------------------------------------------
    # Analysis + publish
    # ------------------------------------------------------------------
    def read_and_analyze_log(self):
        if not self.file_path:
            messagebox.showwarning(self.t("no_file_title"), self.t("no_file_msg"))
            return None, None

        with open(self.file_path, "r") as log_file:
            log_content = log_file.read()

        programmed_count = log_content.lower().count("programmed")
        verified_count = log_content.lower().count("passed verification")

        return programmed_count, verified_count

    def post_to_database(
        self, job, job_quantity, programmed, verified, storage_bucket, storage_path
    ):
        """
        Publish an analytics.metrics row over MQTT to EMQX (fire-and-forget).
          - output: programmed, verified, job_quantity
          - status: job_order, device, model_name, product_code,
            storage_bucket, storage_path
        """
        row = {
            "tenant_id": TENANT_ID,
            "device_id": DEVICE_ID,
            "resolution": METRICS_RESOLUTION,
            "kind": METRICS_KIND,
            "output": {
                "programmed": programmed,
                "verified": verified,
                "job_quantity": job_quantity,
            },
            "status": {
                "job_order": job,
                "device": self.device_name_entry.get(),
                "model_name": self.model_name_entry.get(),
                "product_code": self.product_code_entry.get(),
                "storage_bucket": storage_bucket,
                "storage_path": storage_path,
            },
        }

        success, error = mqtt_client.publish(row)

        if not success:
            print("MQTT publish failed:", error)
            return False

        return True

    def _show_result(self, title, message, icon, **kwargs):
        show_custom_message(
            title, message, icon, ok_text=self.t("ok"), font=self.font(10), **kwargs
        )

    def analyze_and_post(self):
        self.save_config()

        job = self.job_number_entry.get().strip()
        if not (job.isdigit() and len(job) == JOB_NUMBER_LENGTH):
            messagebox.showwarning(
                self.t("invalid_input_title"), self.t("invalid_job_number")
            )
            self.job_number_entry.focus_set()
            return

        job_quantity = self.quantity_entry.get().strip()
        if not job_quantity.isdigit():
            messagebox.showwarning(
                self.t("invalid_input_title"), self.t("invalid_quantity")
            )
            self.quantity_entry.focus_set()
            return
        job_quantity = int(job_quantity)

        if self.is_split:
            self._start_split_submit(job, job_quantity)
            return

        programmed, verified = self.read_and_analyze_log()

        if programmed is None or verified is None:
            return

        result_message = (
            self.t("res_job", job=job)
            + "\n"
            + self.t("res_qty", qty=job_quantity)
            + "\n"
            + self.t("res_programmed", n=programmed)
            + "\n"
            + self.t("res_verified", n=verified)
            + "\n"
        )

        failure_key = None
        if programmed < job_quantity:
            failure_key = "cond_programmed_low"
        elif verified < job_quantity:
            failure_key = "cond_verified_low"
        elif programmed > job_quantity + 10 or verified > job_quantity + 10:
            failure_key = "cond_diff_high"

        if failure_key:
            result_message += "\n" + self.t(failure_key)
            self._show_result(self.t("cond_not_met_title"), result_message, CLOSE_ICON)
            return

        self._show_loading(self.t("loading_upload"))
        self.analyze_button.configure(state=DISABLED)

        threading.Thread(
            target=self._publish_and_upload_worker,
            args=(job, job_quantity, programmed, verified, result_message),
            daemon=True,
        ).start()

    def _publish_and_upload_worker(
        self, job, job_quantity, programmed, verified, result_message
    ):
        """
        Runs off the main thread. Storage upload first; MQTT publish only if
        the upload succeeded. All Tk work is scheduled via root.after.
        """
        try:
            with open(self.file_path, "rb") as file:
                file_content = file.read()
        except FileNotFoundError:
            error_msg = self.t("file_read_missing", path=self.file_path)
            print(error_msg)
            self.root.after(
                0,
                lambda: self._finish_publish(
                    self.t("file_error_title"), error_msg, None, is_error=True
                ),
            )
            return
        except Exception as e:
            error_msg = self.t(
                "file_read_error", error=f"{type(e).__name__} - {str(e)}"
            )
            print(error_msg)
            self.root.after(
                0,
                lambda: self._finish_publish(
                    self.t("error_title"), error_msg, None, is_error=True
                ),
            )
            return

        success, storage_file_name, error_msg = upload_to_storage(
            file_content, job, job_quantity
        )

        if not success:
            final_message = self.t("upload_failed_msg", job=job, error=error_msg)
            self.root.after(
                0,
                lambda: self._finish_publish(
                    self.t("upload_failed_title"), final_message, CLOSE_ICON
                ),
            )
            return

        result_message += "\n" + self.t("upload_ok")
        self.root.after(0, lambda: self._update_loading(self.t("loading_publish")))

        post_success = self.post_to_database(
            job,
            job_quantity,
            programmed,
            verified,
            SUPABASE_STORAGE_BUCKET,
            storage_file_name,
        )

        if not post_success:
            final_message = f"{result_message}\n\n" + self.t(
                "publish_failed_msg", path=storage_file_name
            )
            self.root.after(
                0,
                lambda: self._finish_publish(
                    self.t("error_title"), final_message, CLOSE_ICON
                ),
            )
            return

        result_message += "\n" + self.t("published")

        lot_success, lot_error = publish_lot_record()
        if lot_success:
            result_message += "\n" + self.t("lot_published")
        else:
            result_message += "\n" + self.t("lot_failed", error=lot_error)

        delete_log_file(self.file_path)
        self.root.after(
            0,
            lambda: self._finish_publish(
                self.t("success_title"), result_message, ACCEPT_ICON, deleted=True
            ),
        )

    # ------------------------------------------------------------------
    # Two-station mode
    # ------------------------------------------------------------------
    def _start_split_submit(self, job, job_quantity):
        # Read every Tk value here on the main thread; the worker never
        # touches widgets directly.
        details = {
            "job_quantity": job_quantity,
            "model_name": self.model_name_entry.get().strip(),
            "product_code": self.product_code_entry.get().strip(),
            "device_name": self.device_name_entry.get().strip(),
        }
        self._show_loading(self.t("loading_part"))
        self.analyze_button.configure(state=DISABLED)
        threading.Thread(
            target=self._split_worker,
            args=(job, self.station_role, details, self.file_path),
            daemon=True,
        ).start()

    def _finish_on_ui(self, title, message, icon, **kwargs):
        self.root.after(
            0, lambda: self._finish_publish(title, message, icon, **kwargs)
        )

    def _split_worker(self, job, role, details, file_path):
        """
        Background thread. Upload this station's half, then merge + publish if
        the other station's half is already there.
        """
        role_name = self.role_label(role)
        partner = other_role(role)
        partner_name = self.role_label(partner)
        qty = details["job_quantity"]
        lines = [
            self.t("res_job", job=job),
            self.t("res_station", role=role_name),
            self.t("res_qty", qty=qty),
        ]

        # 1. This station's log: the local file, or — if it was already
        #    deleted after an earlier submit — the copy in storage.
        reused = False
        try:
            if file_path and os.path.isfile(file_path):
                with open(file_path, "rb") as f:
                    log_bytes = f.read()
            elif pending_part_exists(job, role, "txt"):
                log_bytes = download_pending_log(job, role)
                reused = True
            else:
                self._finish_on_ui(
                    self.t("no_file_title"), self.t("no_file_msg"), None, is_error=True
                )
                return
        except Exception as e:
            self._finish_on_ui(
                self.t("error_title"),
                self.t("file_read_error", error=f"{type(e).__name__} - {e}"),
                None,
                is_error=True,
            )
            return

        count = count_keyword(log_bytes, role)
        lines.append(self.t("res_own_count", n=count))
        if reused:
            lines.append(self.t("reused_part"))

        # Per-station check, same thresholds as the single-PC check.
        failure_key = None
        if count < qty:
            failure_key = "cond_own_low"
        elif count > qty + 10:
            failure_key = "cond_own_high"
        if failure_key:
            msg = "\n".join(lines) + "\n\n" + self.t(failure_key, role=role_name)
            self._finish_on_ui(self.t("cond_not_met_title"), msg, CLOSE_ICON)
            return

        # 2. Upload this station's half.
        meta = {
            **details,
            "job": job,
            "role": role,
            "count": count,
            "device_id": DEVICE_ID,
            "machine_id": MACHINE_ID,
            "app_version": APP_VERSION,
            "submitted_at": _iso_ms_z(datetime.now(timezone.utc)),
        }
        try:
            upload_pending_part(job, role, None if reused else log_bytes, meta)
        except Exception as e:
            self._finish_on_ui(
                self.t("upload_failed_title"),
                self.t("part_failed_msg", error=f"{type(e).__name__} - {e}"),
                CLOSE_ICON,
            )
            return
        lines.append(self.t("part_uploaded", role=role_name))

        # The half is safely in storage now — the local log can go.
        deleted = False
        if not reused:
            delete_log_file(file_path)
            deleted = not os.path.exists(file_path)

        # 3. Has the other station submitted?
        try:
            partner_meta = read_pending_meta(job, partner)
        except Exception as e:
            self._finish_on_ui(
                self.t("merge_failed_title"),
                "\n".join(lines) + "\n\n" + self.t("merge_failed_msg", error=e),
                CLOSE_ICON,
                deleted=deleted,
            )
            return

        if partner_meta is None:
            msg = "\n".join(lines) + "\n\n" + self.t(
                "waiting_msg", role=partner_name, job=job
            )
            self._finish_on_ui(self.t("waiting_title"), msg, ACCEPT_ICON, deleted=deleted)
            return

        # 4. Both halves exist — claim the merge so only one PC publishes.
        self.root.after(0, lambda: self._update_loading(self.t("loading_merge")))
        try:
            try_claim(job)
        except ClaimHeld:
            msg = "\n".join(lines) + "\n\n" + self.t("claimed_msg", job=job)
            self._finish_on_ui(self.t("claimed_title"), msg, ACCEPT_ICON, deleted=deleted)
            return
        except Exception as e:
            self._finish_on_ui(
                self.t("merge_failed_title"),
                "\n".join(lines) + "\n\n" + self.t("merge_failed_msg", error=e),
                CLOSE_ICON,
                deleted=deleted,
            )
            return

        try:
            title, msg, icon = self._merge_and_publish(
                job, role, meta, log_bytes, partner, partner_meta, lines
            )
        except Exception as e:
            release_claim(job)
            title = self.t("merge_failed_title")
            msg = "\n".join(lines) + "\n\n" + self.t(
                "merge_failed_msg", error=f"{type(e).__name__} - {e}"
            )
            icon = CLOSE_ICON
        self._finish_on_ui(title, msg, icon, deleted=deleted)

    def _merge_and_publish(self, job, role, meta, log_bytes, partner, partner_meta, lines):
        """
        Runs while holding the claim. Releases it on every non-success path.
        Returns (title, message, icon).
        """
        # Both stations must agree on the job details.
        mismatches = []
        for key, label_key in (
            ("job_quantity", "job_quantity"),
            ("model_name", "model_name"),
            ("product_code", "product_code"),
        ):
            mine = str(meta.get(key, "")).strip()
            theirs = str(partner_meta.get(key, "")).strip()
            if mine.casefold() != theirs.casefold():
                mismatches.append(
                    self.t(
                        "mismatch_field",
                        field=self.t(label_key),
                        mine=mine,
                        theirs=theirs,
                    )
                )
        if mismatches:
            release_claim(job)
            msg = "\n".join(lines) + "\n\n" + self.t(
                "mismatch_msg", job=job, fields="\n".join(mismatches)
            )
            return self.t("mismatch_title"), msg, CLOSE_ICON

        # Recount the partner's log rather than trusting its .json.
        partner_log = download_pending_log(job, partner)
        if role == ROLE_PROGRAM:
            program_meta, program_log = meta, log_bytes
            verify_meta, verify_log = partner_meta, partner_log
        else:
            program_meta, program_log = partner_meta, partner_log
            verify_meta, verify_log = meta, log_bytes
        programmed = count_keyword(program_log, ROLE_PROGRAM)
        verified = count_keyword(verify_log, ROLE_VERIFY)
        qty = int(meta["job_quantity"])

        lines = lines + [
            "",
            self.t("res_programmed", n=programmed),
            self.t("res_verified", n=verified),
        ]

        failure_key = None
        if programmed < qty:
            failure_key = "cond_programmed_low"
        elif verified < qty:
            failure_key = "cond_verified_low"
        elif programmed > qty + 10 or verified > qty + 10:
            failure_key = "cond_diff_high"
        if failure_key:
            release_claim(job)
            msg = "\n".join(lines) + "\n\n" + self.t(failure_key)
            return self.t("cond_not_met_title"), msg, CLOSE_ICON

        # One merged file in logs/, so downstream still gets a single path.
        merged = build_merged_log(program_meta, program_log, verify_meta, verify_log)
        ok, storage_path, err = upload_to_storage(merged, job, qty)
        if not ok:
            release_claim(job)
            msg = "\n".join(lines) + "\n\n" + self.t("merge_failed_msg", error=err)
            return self.t("merge_failed_title"), msg, CLOSE_ICON
        lines.append(self.t("upload_ok"))

        def station(m):
            return {"device_id": m.get("device_id"), "device_name": m.get("device_name")}

        row = {
            "tenant_id": TENANT_ID,
            "device_id": LINE_DEVICE_ID or program_meta.get("device_id") or DEVICE_ID,
            "resolution": METRICS_RESOLUTION,
            "kind": METRICS_KIND,
            "output": {
                "programmed": programmed,
                "verified": verified,
                "job_quantity": qty,
            },
            "status": {
                "job_order": job,
                "device": program_meta.get("device_name", ""),
                "model_name": program_meta.get("model_name", ""),
                "product_code": program_meta.get("product_code", ""),
                "storage_bucket": SUPABASE_STORAGE_BUCKET,
                "storage_path": storage_path,
                "mode": "two_station",
                "stations": {
                    ROLE_PROGRAM: station(program_meta),
                    ROLE_VERIFY: station(verify_meta),
                },
            },
        }

        success, error = mqtt_client.publish(row)
        if not success:
            print("MQTT publish failed:", error)
            release_claim(job)
            msg = "\n".join(lines) + "\n\n" + self.t("split_publish_failed_msg")
            return self.t("error_title"), msg, CLOSE_ICON

        lines.append(self.t("published"))
        lines.append(self.t("merged_ok", role=self.role_label(partner)))

        lot_success, lot_error = publish_lot_record(program_meta.get("machine_id"))
        if lot_success:
            lines.append(self.t("lot_published"))
        else:
            lines.append(self.t("lot_failed", error=lot_error))

        finish_pending(job)
        return self.t("success_title"), "\n".join(lines), ACCEPT_ICON

    def _finish_publish(self, title, message, icon, deleted=False, is_error=False):
        """Main thread: close loading modal, re-enable button, show result."""
        self._hide_loading()
        self.analyze_button.configure(state=NORMAL)
        if self.is_split:
            self._check_partner()

        if is_error:
            messagebox.showerror(title, message)
        else:
            self._show_result(title, message, icon)
            if deleted:
                messagebox.showinfo(self.t("deleted_title"), self.t("deleted_msg"))


if __name__ == "__main__":
    root = tb.Window(themename="flatly")
    app = LogAnalyzerApp(root)

    def on_close():
        mqtt_client.close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
