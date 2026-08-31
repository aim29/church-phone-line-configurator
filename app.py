"""
app.py

Desktop GUI for provisioning this church's phone line, on this church's
own Twilio account. Run this, fill in the form, click "Provision".
Nothing needs to keep running afterwards — Twilio hosts everything from
that point on.

Designed for one Twilio account, one phone line. State from a completed
setup is remembered in config.json (next to this file):
  - the Account SID and an encrypted Auth Token, so you don't have to
    retype them every time (see secure_storage.py — the token is
    encrypted with a key derived from this machine, and simply can't be
    decrypted if config.json is copied elsewhere)
  - the current call-flow (mode, labels, digit mapping) and a sha256 of
    each uploaded file, so re-running the app only re-uploads files that
    have actually changed
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import secure_storage
from twilio_backend import ProvisioningError, TwilioBackend, sha256_bytes, sha256_file

# Two different base directories are needed once this is frozen into a
# PyInstaller executable:
#
#   APP_DIR      — where config.json is read/written. This must be next
#                   to the actual .exe/binary so it persists between
#                   runs. `sys.executable` gives that path when frozen;
#                   `__file__` does NOT — under PyInstaller it points
#                   inside a temporary extraction folder that's deleted
#                   when the app closes, which would silently make the
#                   "saved credentials" and hash-based change-detection
#                   features forget everything on every run.
#
#   RESOURCE_DIR — where bundled read-only files (functions/voice.js)
#                   are read from. In --onefile mode these are unpacked
#                   into sys._MEIPASS at startup, not next to the exe,
#                   so this deliberately does NOT use APP_DIR.
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
else:
    APP_DIR = Path(__file__).resolve().parent
    RESOURCE_DIR = APP_DIR

CONFIG_PATH = APP_DIR / "config.json"
FUNCTION_SOURCE_PATH = RESOURCE_DIR / "functions" / "voice.js"

DIGITS = [str(n) for n in range(1, 10)]


def asset_url(domain_name: str, path: str) -> str:
    return f"https://{domain_name}{path}"


class OptionRow(ttk.Frame):
    """One row in the menu-mode option list: digit + label + file picker.

    If `listen_url` is given, this row represents an option that already
    has deployed audio: a Listen button is shown and choosing a
    replacement MP3 becomes optional (leaving it blank keeps the current
    recording).
    """

    def __init__(self, parent, on_remove, initial_digit=None, initial_label="", listen_url=None):
        super().__init__(parent)
        self.file_path: Path | None = None
        self.listen_url = listen_url

        self.digit_var = tk.StringVar(value=initial_digit or DIGITS[0])
        ttk.Combobox(
            self, textvariable=self.digit_var, values=DIGITS, width=3, state="readonly"
        ).grid(row=0, column=0, padx=4)

        self.label_var = tk.StringVar(value=initial_label)
        ttk.Entry(self, textvariable=self.label_var, width=24).grid(row=0, column=1, padx=4)

        placeholder = "Keep current file" if listen_url else "No file chosen"
        self.file_label = ttk.Label(self, text=placeholder, foreground="grey")
        self.file_label.grid(row=0, column=2, padx=4, sticky="w")

        button_text = "Replace MP3..." if listen_url else "Choose MP3..."
        ttk.Button(self, text=button_text, command=self._choose_file).grid(
            row=0, column=3, padx=4
        )

        if listen_url:
            ttk.Button(
                self, text="\u25b6 Listen", command=lambda: webbrowser.open(listen_url)
            ).grid(row=0, column=4, padx=4)

        ttk.Button(self, text="Remove", command=lambda: on_remove(self)).grid(
            row=0, column=5, padx=4
        )

    def _choose_file(self):
        path = filedialog.askopenfilename(filetypes=[("MP3 files", "*.mp3")])
        if path:
            self.file_path = Path(path)
            self.file_label.config(text=self.file_path.name, foreground="black")

    def is_valid(self) -> bool:
        has_label = bool(self.label_var.get().strip())
        has_audio = self.file_path is not None or self.listen_url is not None
        return has_label and has_audio


class ChurchPhoneLineApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Church Phone Line Setup")
        self.geometry("800x760")
        self.option_rows: list[OptionRow] = []
        self.log_queue: queue.Queue[str] = queue.Queue()

        self.existing_config: dict | None = (
            json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else None
        )
        self.saved_account_sid = ""
        self.saved_auth_token = ""
        self.token_decrypt_failed = False
        if self.existing_config:
            self.saved_account_sid = self.existing_config.get("account_sid", "")
            blob = self.existing_config.get("auth_token_encrypted")
            if blob:
                try:
                    self.saved_auth_token = secure_storage.decrypt(blob)
                except secure_storage.DecryptionError:
                    self.token_decrypt_failed = True

        self._build_credentials_section()
        self._build_number_section()
        self._build_recordings_section()
        self._build_mode_section()
        self._build_action_section()
        self._build_log_section()

        self._poll_log_queue()
        self._refresh_for_existing_config()
        if self.existing_config:
            self._rehydrate_call_flow()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_credentials_section(self):
        frame = ttk.LabelFrame(self, text="Twilio account")
        frame.pack(fill="x", padx=10, pady=6)

        ttk.Label(frame, text="Account SID").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        self.account_sid_var = tk.StringVar(value=self.saved_account_sid)
        ttk.Entry(frame, textvariable=self.account_sid_var, width=40).grid(
            row=0, column=1, padx=4, pady=2
        )

        ttk.Label(frame, text="Auth Token").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self.auth_token_var = tk.StringVar(value=self.saved_auth_token)
        ttk.Entry(frame, textvariable=self.auth_token_var, width=40, show="*").grid(
            row=1, column=1, padx=4, pady=2
        )

        note_text = (
            "Saved after your first successful setup, encrypted so it only ever "
            "decrypts on this machine.\nFind your credentials at https://console.twilio.com"
        )
        note_color = "grey"
        if self.token_decrypt_failed:
            note_text = (
                "A saved Auth Token was found but could not be decrypted on this "
                "machine — please re-enter it."
            )
            note_color = "#b35900"
        ttk.Label(frame, text=note_text, foreground=note_color).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=4, pady=(0, 4)
        )

    def _build_number_section(self):
        self.number_frame = ttk.LabelFrame(self, text="Phone number")
        self.number_frame.pack(fill="x", padx=10, pady=6)

        top_row = ttk.Frame(self.number_frame)
        top_row.pack(fill="x", padx=4, pady=2)
        self.existing_line_label = ttk.Label(top_row, text="", foreground="blue")
        self.existing_line_label.pack(side="left")
        self.console_button = ttk.Button(
            top_row, text="Open in Twilio Console", command=self._open_console
        )
        self.forget_button = ttk.Button(
            self.number_frame, text="Forget this line and start over", command=self._forget_config
        )

        self.number_choice_container = ttk.Frame(self.number_frame)
        self.number_choice_container.pack(fill="x", padx=4, pady=2)

        self.number_source_var = tk.StringVar(value="existing")
        ttk.Radiobutton(
            self.number_choice_container, text="Use a number already on this account",
            variable=self.number_source_var, value="existing", command=self._refresh_number_source
        ).grid(row=0, column=0, sticky="w", columnspan=3)
        ttk.Radiobutton(
            self.number_choice_container, text="Buy a new number",
            variable=self.number_source_var, value="new", command=self._refresh_number_source
        ).grid(row=1, column=0, sticky="w", columnspan=3, pady=(4, 0))

        self.existing_row = ttk.Frame(self.number_choice_container)
        self.existing_row.grid(row=2, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Button(
            self.existing_row, text="Load numbers from account", command=self._load_existing_numbers
        ).grid(row=0, column=0, padx=4)
        self.existing_number_var = tk.StringVar()
        self.existing_number_combo = ttk.Combobox(
            self.existing_row, textvariable=self.existing_number_var, width=40, state="readonly"
        )
        self.existing_number_combo.grid(row=0, column=1, padx=4)

        self.new_row = ttk.Frame(self.number_choice_container)
        self.new_row.grid(row=3, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Label(self.new_row, text="Area code (optional, e.g. 0113):").grid(row=0, column=0)
        self.area_code_var = tk.StringVar()
        ttk.Entry(self.new_row, textvariable=self.area_code_var, width=10).grid(
            row=0, column=1, padx=4
        )
        ttk.Button(self.new_row, text="Search", command=self._search_numbers).grid(
            row=0, column=2, padx=4
        )
        self.search_results_var = tk.StringVar()
        self.search_results_combo = ttk.Combobox(
            self.new_row, textvariable=self.search_results_var, width=30, state="readonly"
        )
        self.search_results_combo.grid(row=0, column=3, padx=4)

        self._refresh_number_source()

    def _build_recordings_section(self):
        self.welcome_frame = ttk.LabelFrame(self, text="Welcome message")
        self.welcome_frame.pack(fill="x", padx=10, pady=6)

        self.welcome_mode_var = tk.StringVar(value="upload")
        ttk.Radiobutton(
            self.welcome_frame, text="Upload an MP3", variable=self.welcome_mode_var,
            value="upload", command=self._refresh_welcome_mode,
        ).grid(row=0, column=0, sticky="w", padx=4, pady=(4, 0))
        ttk.Radiobutton(
            self.welcome_frame, text="Type a message (spoken with text-to-speech)",
            variable=self.welcome_mode_var, value="tts", command=self._refresh_welcome_mode,
        ).grid(row=0, column=1, sticky="w", padx=4, pady=(4, 0), columnspan=2)

        # -- Upload sub-section --
        self.welcome_upload_row = ttk.Frame(self.welcome_frame)
        self.welcome_upload_row.grid(row=1, column=0, columnspan=4, sticky="w", padx=4, pady=4)
        ttk.Label(self.welcome_upload_row, text="Welcome message MP3").grid(
            row=0, column=0, sticky="w"
        )
        self.welcome_label = ttk.Label(
            self.welcome_upload_row, text="No file chosen", foreground="grey"
        )
        self.welcome_label.grid(row=0, column=1, sticky="w", padx=4)
        self.welcome_path: Path | None = None
        self.welcome_choose_button = ttk.Button(
            self.welcome_upload_row, text="Choose MP3...", command=self._choose_welcome
        )
        self.welcome_choose_button.grid(row=0, column=2, padx=4)
        self.welcome_listen_button: ttk.Button | None = None
        self.welcome_listen_url: str | None = None

        # -- Text-to-speech sub-section --
        self.welcome_tts_row = ttk.Frame(self.welcome_frame)
        self.welcome_tts_row.grid(row=2, column=0, columnspan=4, sticky="w", padx=4, pady=4)
        ttk.Label(self.welcome_tts_row, text="Message text:").grid(row=0, column=0, sticky="nw")
        self.welcome_tts_text = tk.Text(self.welcome_tts_row, width=70, height=4, wrap="word")
        self.welcome_tts_text.grid(row=0, column=1, padx=4)
        ttk.Label(
            self.welcome_frame,
            text="Spoken using Twilio's free Basic-tier British English voice. There's no "
            "Scottish-accented voice available — Twilio's UK catalogue only offers generic "
            "English (UK) voices.",
            foreground="grey", wraplength=680, justify="left",
        ).grid(row=3, column=0, columnspan=4, sticky="w", padx=4, pady=(0, 4))

        self._refresh_welcome_mode()

    def _refresh_welcome_mode(self):
        if self.welcome_mode_var.get() == "upload":
            self.welcome_upload_row.grid()
            self.welcome_tts_row.grid_remove()
        else:
            self.welcome_tts_row.grid()
            self.welcome_upload_row.grid_remove()

    def _build_mode_section(self):
        frame = ttk.LabelFrame(self, text="Call flow")
        frame.pack(fill="both", expand=True, padx=10, pady=6)

        self.mode_var = tk.StringVar(value="single")
        ttk.Radiobutton(
            frame, text="Single recording", variable=self.mode_var, value="single",
            command=self._refresh_mode
        ).grid(row=0, column=0, sticky="w", padx=4, pady=4)
        ttk.Radiobutton(
            frame, text="Menu (multiple recordings via button press)",
            variable=self.mode_var, value="menu", command=self._refresh_mode
        ).grid(row=0, column=1, sticky="w", padx=4, pady=4)

        self.single_frame = ttk.Frame(frame)
        self.single_frame.grid(row=1, column=0, columnspan=2, sticky="w", padx=4, pady=4)
        ttk.Label(self.single_frame, text="Recording MP3").grid(row=0, column=0, sticky="w")
        self.recording_label = ttk.Label(self.single_frame, text="No file chosen", foreground="grey")
        self.recording_label.grid(row=0, column=1, padx=4)
        self.recording_path: Path | None = None
        self.recording_choose_button = ttk.Button(
            self.single_frame, text="Choose MP3...", command=self._choose_recording
        )
        self.recording_choose_button.grid(row=0, column=2, padx=4)
        self.recording_listen_url: str | None = None

        self.menu_frame = ttk.Frame(frame)
        self.menu_frame.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=4, pady=4)
        self.options_container = ttk.Frame(self.menu_frame)
        self.options_container.pack(fill="both", expand=True)
        ttk.Button(self.menu_frame, text="+ Add option", command=self._add_option_row).pack(
            anchor="w", pady=4
        )

        self.announce_options_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            self.menu_frame,
            text="Read out the menu options automatically (e.g. \"Press 1 for the "
            "Sunday sermon\")",
            variable=self.announce_options_var,
        ).pack(anchor="w", pady=(8, 0))
        ttk.Label(
            self.menu_frame,
            text="Untick this if your welcome message already explains the options, "
            "to avoid repeating them.",
            foreground="grey", wraplength=680, justify="left",
        ).pack(anchor="w", padx=(20, 0))

        self._refresh_mode()

    def _build_action_section(self):
        frame = ttk.Frame(self)
        frame.pack(fill="x", padx=10, pady=6)
        self.provision_button = ttk.Button(
            frame, text="Provision / update phone line", command=self._start_provisioning
        )
        self.provision_button.pack(side="left")

        self.force_reupload_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame, text="Force re-upload of all files (ignore change detection)",
            variable=self.force_reupload_var,
        ).pack(side="left", padx=12)

        self.result_label = ttk.Label(frame, text="", font=("TkDefaultFont", 11, "bold"))
        self.result_label.pack(side="left", padx=12)

    def _build_log_section(self):
        frame = ttk.LabelFrame(self, text="Progress")
        frame.pack(fill="both", expand=True, padx=10, pady=6)
        self.log_text = tk.Text(frame, height=10, state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

    # ------------------------------------------------------------------
    # Rehydrating the UI from a saved config
    # ------------------------------------------------------------------

    def _refresh_for_existing_config(self):
        if self.existing_config:
            number = self.existing_config.get("phone_number", "?")
            self.existing_line_label.config(
                text=f"Existing line found: {number} — provisioning will update its "
                "recordings, not touch the number."
            )
            self.console_button.pack(side="left", padx=8)
            self.forget_button.pack(anchor="w", padx=4, pady=(0, 4))
            self.number_choice_container.pack_forget()
        else:
            self.existing_line_label.config(text="No line set up yet on this machine.")
            self.console_button.pack_forget()
            self.forget_button.pack_forget()
            self.number_choice_container.pack(fill="x", padx=4, pady=2)

    def _rehydrate_call_flow(self):
        """Pre-fill mode, labels and Listen links from the last deployed config."""
        call_config = self.existing_config.get("call_config")
        domain_name = self.existing_config.get("domain_name")
        if not call_config or not domain_name:
            return

        if call_config.get("welcome_tts"):
            self.welcome_mode_var.set("tts")
            self.welcome_tts_text.insert("1.0", call_config["welcome_tts"])
        elif call_config.get("welcome_path"):
            self.welcome_mode_var.set("upload")
            self.welcome_listen_url = asset_url(domain_name, call_config["welcome_path"])
            self.welcome_label.config(text="Currently deployed message", foreground="black")
            self.welcome_choose_button.config(text="Replace MP3...")
            self.welcome_listen_button = ttk.Button(
                self.welcome_upload_row, text="\u25b6 Listen",
                command=lambda: webbrowser.open(self.welcome_listen_url),
            )
            self.welcome_listen_button.grid(row=0, column=3, padx=4)
        self._refresh_welcome_mode()

        self.mode_var.set(call_config.get("mode", "single"))
        self.announce_options_var.set(call_config.get("announce_options", True))

        if call_config.get("mode") == "single" and call_config.get("recording_path"):
            self.recording_listen_url = asset_url(domain_name, call_config["recording_path"])
            self.recording_label.config(text="Currently deployed recording", foreground="black")
            self.recording_choose_button.config(text="Replace MP3...")
            ttk.Button(
                self.single_frame, text="\u25b6 Listen",
                command=lambda: webbrowser.open(self.recording_listen_url),
            ).grid(row=0, column=3, padx=4)

        if call_config.get("mode") == "menu":
            for digit, option in sorted(call_config.get("options", {}).items()):
                listen_url = asset_url(domain_name, option["path"])
                row = OptionRow(
                    self.options_container, self._remove_option_row,
                    initial_digit=digit, initial_label=option.get("label", ""),
                    listen_url=listen_url,
                )
                row.pack(fill="x", pady=2)
                self.option_rows.append(row)

        self._refresh_mode()

    def _open_console(self):
        url = self.existing_config.get("console_url") if self.existing_config else None
        if url:
            webbrowser.open(url)

    def _forget_config(self):
        if not messagebox.askyesno(
            "Forget this line?",
            "This only forgets the number and saved credentials locally — it does "
            "NOT release or delete the number or Twilio service. Use this if you "
            "want to pick a different number, or point this app at a different "
            "Twilio service. Continue?",
        ):
            return
        CONFIG_PATH.unlink(missing_ok=True)
        self.existing_config = None
        self._refresh_for_existing_config()
        messagebox.showinfo("Done", "Local state cleared. Please restart the app.")

    # ------------------------------------------------------------------
    # UI behaviour
    # ------------------------------------------------------------------

    def _refresh_number_source(self):
        if self.number_source_var.get() == "existing":
            self.existing_row.grid()
            self.new_row.grid_remove()
        else:
            self.new_row.grid()
            self.existing_row.grid_remove()

    def _refresh_mode(self):
        if self.mode_var.get() == "single":
            self.single_frame.grid()
            self.menu_frame.grid_remove()
        else:
            self.menu_frame.grid()
            self.single_frame.grid_remove()

    def _choose_welcome(self):
        path = filedialog.askopenfilename(filetypes=[("MP3 files", "*.mp3")])
        if path:
            self.welcome_path = Path(path)
            self.welcome_label.config(text=self.welcome_path.name, foreground="black")

    def _choose_recording(self):
        path = filedialog.askopenfilename(filetypes=[("MP3 files", "*.mp3")])
        if path:
            self.recording_path = Path(path)
            self.recording_label.config(text=self.recording_path.name, foreground="black")

    def _add_option_row(self):
        if len(self.option_rows) >= 9:
            messagebox.showinfo("Limit reached", "Up to 9 options are supported (digits 1-9).")
            return
        row = OptionRow(self.options_container, self._remove_option_row)
        row.pack(fill="x", pady=2)
        self.option_rows.append(row)

    def _remove_option_row(self, row: OptionRow):
        row.destroy()
        self.option_rows.remove(row)

    def _credentials_or_warn(self) -> tuple[str, str] | None:
        sid = self.account_sid_var.get().strip()
        token = self.auth_token_var.get().strip()
        if not sid.startswith("AC") or not token:
            messagebox.showerror(
                "Check credentials", "Enter a valid Account SID and Auth Token first."
            )
            return None
        return sid, token

    def _load_existing_numbers(self):
        creds = self._credentials_or_warn()
        if not creds:
            return
        try:
            backend = TwilioBackend(*creds, log=lambda msg: None)
            numbers = backend.list_existing_numbers()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Could not load numbers", str(e))
            return
        if not numbers:
            messagebox.showinfo("No numbers found", "No voice-capable numbers found on this account.")
            return
        display = [f"{n['phone_number']} ({n['friendly_name']})" for n in numbers]
        self._existing_numbers_lookup = {
            f"{n['phone_number']} ({n['friendly_name']})": n for n in numbers
        }
        self.existing_number_combo["values"] = display
        self.existing_number_combo.current(0)

    def _search_numbers(self):
        creds = self._credentials_or_warn()
        if not creds:
            return
        try:
            backend = TwilioBackend(*creds, log=lambda msg: None)
            results = backend.search_uk_numbers(self.area_code_var.get())
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Search failed", str(e))
            return
        self.search_results_combo["values"] = results
        self.search_results_combo.current(0)

    # ------------------------------------------------------------------
    # Provisioning
    # ------------------------------------------------------------------

    def _validate_form(self) -> str | None:
        if not self._credentials_or_warn():
            return ""  # message already shown
        if not self.existing_config:
            if self.number_source_var.get() == "existing":
                if not self.existing_number_var.get():
                    return "Load and select an existing number first."
            else:
                if not self.search_results_var.get():
                    return "Search for and select a number to buy first."
        if self.welcome_mode_var.get() == "upload":
            if not self.welcome_path and not self.welcome_listen_url:
                return "Choose a welcome message MP3."
        else:
            if not self.welcome_tts_text.get("1.0", "end").strip():
                return "Type the welcome message text."
        if self.mode_var.get() == "single":
            if not self.recording_path and not self.recording_listen_url:
                return "Choose a recording MP3."
        else:
            if not self.option_rows:
                return "Add at least one menu option."
            for row in self.option_rows:
                if not row.is_valid():
                    return "Every menu option needs a label and an MP3 file."
            digits_used = [row.digit_var.get() for row in self.option_rows]
            if len(digits_used) != len(set(digits_used)):
                return "Each menu option must use a different digit."
        return None

    def _start_provisioning(self):
        error = self._validate_form()
        if error == "":
            return  # credentials warning already shown
        elif error:
            messagebox.showerror("Check the form", error)
            return

        if not self.existing_config and self.number_source_var.get() == "new":
            number = self.search_results_var.get()
            if not messagebox.askyesno(
                "Confirm purchase",
                f"This will purchase the number {number} on your Twilio account.\n\n"
                "This is a real charge, billed by Twilio, and recurs monthly until "
                "the number is released:\n"
                "  \u2022 $3.50/month rental for a UK local number\n"
                "  \u2022 $0.01/minute for inbound calls\n\n"
                "Continue with the purchase?",
                icon="warning",
            ):
                return

        self.provision_button.config(state="disabled")
        self.result_label.config(text="")
        self._clear_log()

        form = {
            "account_sid": self.account_sid_var.get().strip(),
            "auth_token": self.auth_token_var.get().strip(),
            "welcome_mode": self.welcome_mode_var.get(),
            "welcome_path": self.welcome_path,
            "welcome_tts": self.welcome_tts_text.get("1.0", "end").strip(),
            "mode": self.mode_var.get(),
            "recording_path": self.recording_path,
            "announce_options": self.announce_options_var.get(),
            "options": [
                (row.digit_var.get(), row.label_var.get().strip(), row.file_path)
                for row in self.option_rows
            ],
            "number_source": self.number_source_var.get(),
            "existing_number": getattr(self, "_existing_numbers_lookup", {}).get(
                self.existing_number_var.get()
            ),
            "new_number": self.search_results_var.get(),
            "force_reupload": self.force_reupload_var.get(),
        }

        thread = threading.Thread(target=self._run_provisioning, args=(form,), daemon=True)
        thread.start()

    def _run_provisioning(self, form: dict):
        def log(msg: str):
            self.log_queue.put(msg)

        try:
            backend = TwilioBackend(form["account_sid"], form["auth_token"], log=log)
            old_hashes = (self.existing_config or {}).get("asset_hashes", {})
            force = form["force_reupload"]

            if self.existing_config:
                service_sid = self.existing_config["service_sid"]
                environment_sid = self.existing_config["environment_sid"]
                phone_sid = self.existing_config["phone_sid"]
                phone_number = self.existing_config["phone_number"]
                log("Updating the existing phone line...")
            else:
                if form["number_source"] == "existing":
                    chosen = form["existing_number"]
                    phone_sid, phone_number = chosen["sid"], chosen["phone_number"]
                    log(f"Using existing number {phone_number}...")
                else:
                    phone_sid, phone_number = backend.buy_number(
                        form["new_number"], "Church Phone Line"
                    )
                service_sid = backend.create_service("Church Phone Line")
                environment_sid, _domain = backend.create_environment(service_sid)

            # --- Function: only upload if the source changed ---
            function_source = FUNCTION_SOURCE_PATH.read_bytes()
            function_hash = sha256_bytes(function_source)
            prev_function_hash = (self.existing_config or {}).get("function_sha256")
            prev_function_version = (self.existing_config or {}).get("function_version_sid")

            if not force and prev_function_hash == function_hash and prev_function_version:
                log("Call handler code unchanged, skipping upload.")
                function_version_sid = prev_function_version
                function_changed = False
            else:
                function_version_sid = backend.upload_function(
                    service_sid, "voice", "/voice", function_source.decode("utf-8")
                )
                function_changed = True

            # --- Assets: only upload changed files ---
            new_hashes: dict[str, dict] = {}
            asset_version_sids: list[str] = []
            any_asset_changed = False

            def resolve_asset(path: str, friendly_name: str, local_file: Path | None) -> str:
                nonlocal any_asset_changed
                previous = old_hashes.get(path)
                if local_file is None:
                    if not previous:
                        raise ProvisioningError(
                            f"No file provided for {path} and no existing version found."
                        )
                    log(f"{path}: keeping current file (unchanged).")
                    new_hashes[path] = previous
                    return previous["version_sid"]

                new_hash = sha256_file(local_file)
                if not force and previous and previous.get("sha256") == new_hash:
                    log(f"{path}: file unchanged, skipping upload.")
                    new_hashes[path] = previous
                    return previous["version_sid"]

                version_sid = backend.upload_asset(service_sid, friendly_name, path, local_file)
                new_hashes[path] = {"sha256": new_hash, "version_sid": version_sid}
                any_asset_changed = True
                return version_sid

            welcome_version = None
            if form["welcome_mode"] == "tts":
                log("Welcome message: using text-to-speech, no file to upload.")
                welcome_fields = {"welcome_tts": form["welcome_tts"]}
            else:
                welcome_version = resolve_asset("/welcome.mp3", "welcome", form["welcome_path"])
                asset_version_sids.append(welcome_version)
                welcome_fields = {"welcome_path": "/welcome.mp3"}

            if form["mode"] == "single":
                recording_version = resolve_asset(
                    "/recording.mp3", "recording", form["recording_path"]
                )
                asset_version_sids.append(recording_version)
                call_config = {
                    "mode": "single",
                    "recording_path": "/recording.mp3",
                    **welcome_fields,
                }
            else:
                options_config = {}
                for digit, label, file_path in form["options"]:
                    asset_path = f"/option-{digit}.mp3"
                    version_sid = resolve_asset(asset_path, f"option-{digit}", file_path)
                    asset_version_sids.append(version_sid)
                    options_config[digit] = {"label": label, "path": asset_path}
                call_config = {
                    "mode": "menu",
                    "announce_options": form["announce_options"],
                    "options": options_config,
                    **welcome_fields,
                }

            needs_build = function_changed or any_asset_changed or not self.existing_config
            if needs_build:
                backend.build_and_deploy(
                    service_sid, environment_sid, asset_version_sids, [function_version_sid]
                )
            else:
                log("No file changes detected — skipping Build/Deploy.")

            prev_call_config = (self.existing_config or {}).get("call_config")
            if call_config != prev_call_config:
                backend.set_config_variable(service_sid, environment_sid, json.dumps(call_config))
            else:
                log("Call-flow configuration unchanged — skipping variable update.")

            env = backend.client.serverless.v1.services(service_sid).environments(
                environment_sid
            ).fetch()
            domain_name = env.domain_name

            if not self.existing_config:
                function_url = f"https://{domain_name}/voice"
                backend.set_number_webhook(phone_sid, function_url)

            CONFIG_PATH.write_text(
                json.dumps(
                    {
                        "account_sid": form["account_sid"],
                        "auth_token_encrypted": secure_storage.encrypt(form["auth_token"]),
                        "phone_sid": phone_sid,
                        "phone_number": phone_number,
                        "service_sid": service_sid,
                        "environment_sid": environment_sid,
                        "domain_name": domain_name,
                        "console_url": TwilioBackend.console_url(service_sid),
                        "function_sha256": function_hash,
                        "function_version_sid": function_version_sid,
                        "call_config": call_config,
                        "asset_hashes": new_hashes,
                    },
                    indent=2,
                )
            )
            self.existing_config = json.loads(CONFIG_PATH.read_text())

            log(f"Done. Phone number: {phone_number}")
            self.log_queue.put(("__SUCCESS__", phone_number))

        except ProvisioningError as e:
            self.log_queue.put(f"ERROR: {e}")
            self.log_queue.put("__FAILURE__")
        except Exception as e:  # noqa: BLE001
            self.log_queue.put(f"UNEXPECTED ERROR: {e}")
            self.log_queue.put("__FAILURE__")

    # ------------------------------------------------------------------
    # Log queue / thread-safe UI updates
    # ------------------------------------------------------------------

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state="disabled")

    def _append_log(self, msg: str):
        self.log_text.config(state="normal")
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state="disabled")

    def _poll_log_queue(self):
        try:
            while True:
                item = self.log_queue.get_nowait()
                if isinstance(item, tuple) and item[0] == "__SUCCESS__":
                    self.result_label.config(text=f"Phone number: {item[1]}", foreground="green")
                    self.provision_button.config(state="normal")
                    self._refresh_for_existing_config()
                elif item == "__FAILURE__":
                    self.provision_button.config(state="normal")
                else:
                    self._append_log(str(item))
        except queue.Empty:
            pass
        self.after(150, self._poll_log_queue)


if __name__ == "__main__":
    ChurchPhoneLineApp().mainloop()
