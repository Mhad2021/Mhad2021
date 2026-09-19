"""The tracker window: status, the day's totals, and the action buttons.

Tkinter is used deliberately — it ships with Python, so the packaged agent is
around 20MB rather than 120MB, which matters when it is pushed to 200 laptops.
"""
from __future__ import annotations

import contextlib
import logging
import tkinter as tk
from tkinter import messagebox

from tracker.config import APP_NAME, AgentConfig
from tracker.engine import AgentState, TrackerEngine

logger = logging.getLogger(__name__)

BG = "#f1f5f9"
SURFACE = "#ffffff"
TEXT = "#0f172a"
MUTED = "#64748b"
BORDER = "#e2e8f0"

STATE_COLOURS = {
    "active": "#16a34a",
    "idle": "#ea580c",
    "on_break": "#d97706",
    "offline": "#dc2626",
    "clocked_out": "#64748b",
}


def humanize(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


class TrackerWindow:
    """The main window. Hidden to the tray rather than destroyed on close."""

    def __init__(self, engine: TrackerEngine, config: AgentConfig):
        self.engine = engine
        self.config = config
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} — Time Tracker")
        self.root.geometry("440x600")
        self.root.minsize(400, 560)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)

        self._build()
        engine.subscribe(self._on_state)
        self._render(engine.state)
        self._tick_countdown()

    # -- Layout -------------------------------------------------------------
    def _build(self) -> None:
        header = tk.Frame(self.root, bg=TEXT, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(
            header, text=f"🕒 {APP_NAME}", bg=TEXT, fg="white",
            font=("Segoe UI", 13, "bold"),
        ).pack(side="left", padx=18)
        self.name_label = tk.Label(
            header, text=self.config.full_name or "", bg=TEXT, fg="#cbd5e1",
            font=("Segoe UI", 9),
        )
        self.name_label.pack(side="right", padx=18)

        status = tk.Frame(self.root, bg=SURFACE, pady=20)
        status.pack(fill="x", padx=14, pady=(14, 8))
        self.emoji_label = tk.Label(
            status, text="⚪", bg=SURFACE, font=("Segoe UI Emoji", 30)
        )
        self.emoji_label.pack()
        self.state_label = tk.Label(
            status, text="Connecting…", bg=SURFACE, fg=TEXT,
            font=("Segoe UI", 17, "bold"),
        )
        self.state_label.pack(pady=(6, 2))
        self.detail_label = tk.Label(
            status, text="", bg=SURFACE, fg=MUTED, font=("Segoe UI", 9),
            wraplength=380, justify="center",
        )
        self.detail_label.pack()
        self.countdown_label = tk.Label(
            status, text="", bg=SURFACE, fg="#d97706", font=("Segoe UI", 10, "bold")
        )
        self.countdown_label.pack(pady=(6, 0))

        metrics = tk.Frame(self.root, bg=BORDER)
        metrics.pack(fill="x", padx=14, pady=(0, 8))
        self.metric_values: dict[str, tk.Label] = {}
        for column, (key, label) in enumerate(
            [("worked", "Worked"), ("active", "Active"),
             ("idle", "Idle"), ("breaks", "Breaks")]
        ):
            cell = tk.Frame(metrics, bg=SURFACE, padx=10, pady=9)
            cell.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 1), pady=0)
            metrics.grid_columnconfigure(column, weight=1)
            tk.Label(
                cell, text=label.upper(), bg=SURFACE, fg=MUTED,
                font=("Segoe UI", 7, "bold"),
            ).pack()
            value = tk.Label(
                cell, text="—", bg=SURFACE, fg=TEXT, font=("Segoe UI", 12, "bold")
            )
            value.pack()
            self.metric_values[key] = value

        actions = tk.Frame(self.root, bg=BG)
        actions.pack(fill="both", expand=True, padx=14, pady=4)

        self.clock_button = self._button(
            actions, "Clock In", self._toggle_clock, primary=True
        )
        self.clock_button.pack(fill="x", pady=(4, 10), ipady=7)

        tk.Label(
            actions, text="BREAKS", bg=BG, fg=MUTED, font=("Segoe UI", 7, "bold")
        ).pack(anchor="w", pady=(4, 4))

        self.lunch_button = self._button(
            actions, "Start Lunch Break", lambda: self._start_break("lunch")
        )
        self.lunch_button.pack(fill="x", pady=3, ipady=5)

        self.short_button = self._button(
            actions, "Start 10-Minute Break", lambda: self._start_break("short")
        )
        self.short_button.pack(fill="x", pady=3, ipady=5)

        # One button rather than two: only one break can run at a time, so a
        # separate "End Lunch Break" would be disabled whenever this is enabled.
        # The label follows whichever break is actually running.
        self.end_break_button = self._button(
            actions, "End Break", self._end_break, accent="#d97706"
        )
        self.end_break_button.pack(fill="x", pady=3, ipady=5)

        self.allowance_label = tk.Label(
            actions, text="", bg=BG, fg=MUTED, font=("Segoe UI", 8),
            justify="left", wraplength=390,
        )
        self.allowance_label.pack(anchor="w", pady=(10, 0))

        footer = tk.Frame(self.root, bg=BG)
        footer.pack(fill="x", padx=14, pady=(0, 12))
        self.connection_label = tk.Label(
            footer, text="", bg=BG, fg=MUTED, font=("Segoe UI", 8)
        )
        self.connection_label.pack(side="left")
        tk.Button(
            footer, text="What is recorded?", command=self._show_privacy,
            bg=BG, fg=MUTED, font=("Segoe UI", 8, "underline"),
            relief="flat", cursor="hand2", borderwidth=0,
            activebackground=BG, activeforeground=TEXT,
        ).pack(side="right")

    def _button(
        self, parent, text: str, command, primary: bool = False, accent: str = ""
    ) -> tk.Button:
        background = accent or (TEXT if primary else SURFACE)
        foreground = "white" if (primary or accent) else TEXT
        return tk.Button(
            parent, text=text, command=command,
            bg=background, fg=foreground,
            activebackground=background, activeforeground=foreground,
            font=("Segoe UI", 10, "bold" if primary else "normal"),
            relief="flat", cursor="hand2", borderwidth=0,
            highlightthickness=1, highlightbackground=BORDER,
        )

    # -- Actions ------------------------------------------------------------
    def _toggle_clock(self) -> None:
        if self.engine.state.clocked_in:
            if not messagebox.askyesno(
                "Clock out",
                "Clock out and end your working session for today?",
                parent=self.root,
            ):
                return
            ok, message = self.engine.clock_out()
        else:
            ok, message = self.engine.clock_in()
        self._toast(ok, message)

    def _start_break(self, break_type: str) -> None:
        ok, message = self.engine.start_break(break_type)
        self._toast(ok, message)

    def _end_break(self) -> None:
        ok, message = self.engine.end_break()
        self._toast(ok, message)

    def _toast(self, ok: bool, message: str) -> None:
        if ok:
            self.detail_label.config(text=message)
        else:
            messagebox.showwarning("Not allowed", message, parent=self.root)

    def _show_privacy(self) -> None:
        import webbrowser

        webbrowser.open(f"{self.config.server_url.rstrip('/')}/privacy")

    # -- Rendering ----------------------------------------------------------
    def _on_state(self, state: AgentState) -> None:
        # Called from the heartbeat thread; Tk must only be touched on its own.
        # The window may already be torn down; tracking continues regardless.
        with contextlib.suppress(RuntimeError):
            self.root.after(0, lambda: self._render(state))

    def _render(self, state: AgentState) -> None:
        colour = STATE_COLOURS.get(state.state, MUTED)
        self.emoji_label.config(text=state.status_emoji)
        self.state_label.config(text=state.status_label, fg=colour)
        self.name_label.config(text=self.config.full_name or "")

        if state.last_error and not state.connected:
            self.detail_label.config(text=state.last_error)
        else:
            self.detail_label.config(text=state.detail or "")

        self.metric_values["worked"].config(text=humanize(state.worked_seconds))
        self.metric_values["active"].config(text=humanize(state.active_seconds))
        self.metric_values["idle"].config(text=humanize(state.idle_seconds))
        self.metric_values["breaks"].config(text=humanize(state.break_seconds))

        self.clock_button.config(text="Clock Out" if state.clocked_in else "Clock In")

        can_break = state.clocked_in and not state.on_break
        lunch_left = max(
            0, self.config.lunch_breaks_per_day - state.breaks_used.get("lunch", 0)
        )
        short_left = max(
            0, self.config.short_breaks_per_day - state.breaks_used.get("short", 0)
        )

        self.lunch_button.config(
            state="normal" if can_break and lunch_left else "disabled",
            text=f"Start Lunch Break  ({lunch_left} left)",
        )
        self.short_button.config(
            state="normal" if can_break and short_left else "disabled",
            text=f"Start 10-Minute Break  ({short_left} left)",
        )
        self.end_break_button.config(
            state="normal" if state.on_break else "disabled",
            text="End Lunch Break" if state.break_type == "lunch" else "End Break",
        )

        self.allowance_label.config(
            text=(
                f"Lunch {humanize(self.config.lunch_break_max_seconds)} "
                f"· short break {humanize(self.config.short_break_max_seconds)}.\n"
                f"Your team leader is notified if there is no activity for "
                f"{humanize(self.config.idle_threshold_seconds)} without a break running."
            )
        )

        if state.connected and state.last_heartbeat_at:
            stamp = state.last_heartbeat_at.astimezone().strftime("%H:%M:%S")
            self.connection_label.config(text=f"Connected · last sync {stamp}", fg=MUTED)
        else:
            self.connection_label.config(text="Not connected to the server", fg="#dc2626")

    def _tick_countdown(self) -> None:
        """Count a running break down locally between heartbeats."""
        state = self.engine.state
        if state.on_break and state.break_remaining_seconds is not None:
            remaining = state.break_remaining_seconds
            label = "Lunch" if state.break_type == "lunch" else "Break"
            if remaining >= 0:
                self.countdown_label.config(
                    text=f"{label}: {humanize(remaining)} left", fg="#d97706"
                )
            else:
                self.countdown_label.config(
                    text=f"{label}: {humanize(abs(remaining))} over the limit",
                    fg="#dc2626",
                )
            state.break_remaining_seconds = remaining - 1
        else:
            self.countdown_label.config(text="")

        self.root.after(1000, self._tick_countdown)

    # -- Window management --------------------------------------------------
    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.engine.refresh_now()

    def hide(self) -> None:
        """Closing the window hides it — tracking continues in the tray.

        The employee is told this explicitly the first time, because a window
        that vanishes without explanation reads as "the app is off".
        """
        self.root.withdraw()

    def run(self) -> None:
        self.root.mainloop()
