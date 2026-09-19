"""First-run screens: the monitoring disclosure, then enrolment or sign-in."""
from __future__ import annotations

import logging
import threading
import tkinter as tk
from typing import Optional

from tracker.api_client import ApiClient, ApiError
from tracker.config import APP_NAME, AgentConfig

logger = logging.getLogger(__name__)

BG = "#f1f5f9"
SURFACE = "#ffffff"
TEXT = "#0f172a"
MUTED = "#64748b"
BORDER = "#e2e8f0"

DISCLOSURE = """\
{app} records your attendance and whether your work computer is being used.

WHAT IS RECORDED
  •  The time you clock in and clock out
  •  Lunch and short breaks you start and end
  •  How many seconds since your last keyboard or mouse input
  •  Whether your workstation is locked
  •  Whether this laptop is reachable: on, asleep or disconnected
  •  This computer's name, operating system and the tracker version

WHAT IS NOT RECORDED
  •  What you type — only how long since you last typed anything
  •  Your passwords
  •  Messages, emails or chat content
  •  Screenshots or screen recordings
  •  Webcam or microphone
  •  Your files or documents
  •  Websites you visit or applications you open
  •  Your location

WHEN YOUR TEAM LEADER IS NOTIFIED
  •  No keyboard or mouse input for {idle}, with no break running
  •  A break runs past its allowed length
  •  This laptop stops reporting while you are still clocked in

Starting an approved break stops inactivity alerts until it ends. Use the break
buttons whenever you step away and you will not be flagged.
"""


class SetupWindow:
    """Blocks until the laptop is enrolled, or the employee closes the window."""

    def __init__(self, config: AgentConfig):
        self.config = config
        self.client = ApiClient(config)
        self.result: Optional[dict] = None

        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} — Setup")
        self.root.geometry("560x640")
        self.root.configure(bg=BG)

        self.container = tk.Frame(self.root, bg=BG)
        self.container.pack(fill="both", expand=True)

        if config.monitoring_notice_accepted:
            self._show_sign_in()
        else:
            self._show_disclosure()

    def _clear(self) -> None:
        for child in self.container.winfo_children():
            child.destroy()

    # -- Step 1: the disclosure --------------------------------------------
    def _show_disclosure(self) -> None:
        self._clear()

        tk.Label(
            self.container, text="Before you start", bg=BG, fg=TEXT,
            font=("Segoe UI", 15, "bold"),
        ).pack(anchor="w", padx=24, pady=(22, 2))
        tk.Label(
            self.container,
            text="Please read what this software does and does not record.",
            bg=BG, fg=MUTED, font=("Segoe UI", 9),
        ).pack(anchor="w", padx=24, pady=(0, 12))

        frame = tk.Frame(self.container, bg=SURFACE, highlightthickness=1,
                         highlightbackground=BORDER)
        frame.pack(fill="both", expand=True, padx=24)

        scrollbar = tk.Scrollbar(frame)
        scrollbar.pack(side="right", fill="y")
        text = tk.Text(
            frame, wrap="word", bg=SURFACE, fg=TEXT, font=("Segoe UI", 9),
            relief="flat", padx=16, pady=14, yscrollcommand=scrollbar.set,
        )
        text.pack(fill="both", expand=True)
        scrollbar.config(command=text.yview)

        minutes = max(1, self.config.idle_threshold_seconds // 60)
        text.insert(
            "1.0",
            DISCLOSURE.format(app=APP_NAME, idle=f"{minutes} minutes"),
        )
        text.config(state="disabled")

        footer = tk.Frame(self.container, bg=BG)
        footer.pack(fill="x", padx=24, pady=16)

        self.accepted = tk.BooleanVar(value=False)
        tk.Checkbutton(
            footer, text="I have read and understood the above",
            variable=self.accepted, bg=BG, fg=TEXT, font=("Segoe UI", 9),
            activebackground=BG, selectcolor=SURFACE,
            command=lambda: self.continue_button.config(
                state="normal" if self.accepted.get() else "disabled"
            ),
        ).pack(anchor="w", pady=(0, 10))

        self.continue_button = tk.Button(
            footer, text="Continue", command=self._show_sign_in, state="disabled",
            bg=TEXT, fg="white", activebackground=TEXT, activeforeground="white",
            font=("Segoe UI", 10, "bold"), relief="flat", cursor="hand2",
            borderwidth=0,
        )
        self.continue_button.pack(fill="x", ipady=7)

    # -- Step 2: enrol or sign in ------------------------------------------
    def _show_sign_in(self) -> None:
        self._clear()

        tk.Label(
            self.container, text=f"Set up {APP_NAME}", bg=BG, fg=TEXT,
            font=("Segoe UI", 15, "bold"),
        ).pack(anchor="w", padx=24, pady=(26, 2))
        tk.Label(
            self.container,
            text="Sign in with your work account, or use the enrollment code "
                 "your administrator gave you.",
            bg=BG, fg=MUTED, font=("Segoe UI", 9), wraplength=500, justify="left",
        ).pack(anchor="w", padx=24, pady=(0, 16))

        card = tk.Frame(self.container, bg=SURFACE, highlightthickness=1,
                        highlightbackground=BORDER, padx=22, pady=20)
        card.pack(fill="x", padx=24)

        tk.Label(card, text="Server", bg=SURFACE, fg=TEXT,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.server_entry = self._entry(card, self.config.server_url)
        tk.Label(
            card, text="Set by your IT team during installation.",
            bg=SURFACE, fg=MUTED, font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(2, 12))

        tk.Label(card, text="Username", bg=SURFACE, fg=TEXT,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.username_entry = self._entry(card)

        tk.Label(card, text="Password", bg=SURFACE, fg=TEXT,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(10, 0))
        self.password_entry = self._entry(card, show="•")
        self.password_entry.bind("<Return>", lambda _: self._sign_in())

        self.sign_in_button = tk.Button(
            card, text="Sign in", command=self._sign_in,
            bg=TEXT, fg="white", activebackground=TEXT, activeforeground="white",
            font=("Segoe UI", 10, "bold"), relief="flat", cursor="hand2",
            borderwidth=0,
        )
        self.sign_in_button.pack(fill="x", pady=(16, 0), ipady=7)

        separator = tk.Frame(self.container, bg=BG)
        separator.pack(fill="x", padx=24, pady=14)
        tk.Label(separator, text="or use an enrollment code", bg=BG, fg=MUTED,
                 font=("Segoe UI", 8)).pack()

        code_card = tk.Frame(self.container, bg=SURFACE, highlightthickness=1,
                             highlightbackground=BORDER, padx=22, pady=18)
        code_card.pack(fill="x", padx=24)
        tk.Label(code_card, text="Enrollment code", bg=SURFACE, fg=TEXT,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.code_entry = self._entry(code_card)
        self.code_entry.bind("<Return>", lambda _: self._enroll())
        tk.Button(
            code_card, text="Enroll this laptop", command=self._enroll,
            bg=SURFACE, fg=TEXT, font=("Segoe UI", 9), relief="flat",
            cursor="hand2", borderwidth=0, highlightthickness=1,
            highlightbackground=BORDER,
        ).pack(fill="x", pady=(12, 0), ipady=5)

        self.status_label = tk.Label(
            self.container, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9),
            wraplength=500,
        )
        self.status_label.pack(padx=24, pady=14)

    def _entry(self, parent, value: str = "", show: str = "") -> tk.Entry:
        entry = tk.Entry(
            parent, font=("Segoe UI", 10), relief="flat", bg="#f8fafc",
            highlightthickness=1, highlightbackground=BORDER,
            highlightcolor="#2563eb", show=show,
        )
        entry.pack(fill="x", ipady=6, pady=(4, 0))
        if value:
            entry.insert(0, value)
        return entry

    # -- Submission ---------------------------------------------------------
    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.sign_in_button.config(state="disabled" if busy else "normal")
        self.status_label.config(text=message, fg=MUTED)

    def _apply_server_url(self) -> None:
        url = self.server_entry.get().strip()
        if url:
            self.config.server_url = url
            self.client = ApiClient(self.config)

    def _sign_in(self) -> None:
        username = self.username_entry.get().strip()
        password = self.password_entry.get()
        if not username or not password:
            self.status_label.config(text="Enter your username and password", fg="#dc2626")
            return

        self._apply_server_url()
        self._set_busy(True, "Signing in…")
        self._run_async(lambda: self.client.login(username, password))

    def _enroll(self) -> None:
        code = self.code_entry.get().strip()
        if not code:
            self.status_label.config(text="Enter the enrollment code", fg="#dc2626")
            return

        self._apply_server_url()
        self._set_busy(True, "Enrolling this laptop…")
        self._run_async(lambda: self.client.enroll(code))

    def _run_async(self, call) -> None:
        """Network calls run off the Tk thread so the window stays responsive."""

        def worker() -> None:
            try:
                result = call()
            except ApiError as exc:
                # Bind the message now: `exc` is unbound once the except block
                # ends, and the callback runs later on the Tk thread.
                message = exc.message
                self.root.after(0, lambda: self._on_failure(message))
                return
            self.root.after(0, lambda: self._on_success(result))

        threading.Thread(target=worker, daemon=True).start()

    def _on_success(self, result: dict) -> None:
        self.result = result
        try:
            self.client.accept_notice()
        except ApiError:
            logger.warning("Could not record the monitoring acknowledgement")
        self.config.monitoring_notice_accepted = True
        self.config.save()
        self.root.destroy()

    def _on_failure(self, message: str) -> None:
        self._set_busy(False)
        self.status_label.config(text=message, fg="#dc2626")

    def run(self) -> Optional[dict]:
        self.root.mainloop()
        return self.result
