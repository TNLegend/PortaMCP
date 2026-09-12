from __future__ import annotations

import queue
import subprocess
import threading
import time
import webbrowser
from collections import deque
from tkinter import filedialog, messagebox
from tkinter import font as tkfont
from typing import Any

import customtkinter as ctk
from PIL import Image, ImageDraw

from . import control_center_backend as backend

APP_NAME = "PortaMCP"
APP_SUBTITLE = "Universal MCP Control Center"
APP_VERSION = "0.6.0"
SUPPORT_URL = "https://buymeacoffee.com/tnlegend"

BG = "#081321"
SIDEBAR = "#060F1C"
CARD = "#0D1C30"
CARD_ALT = "#0A1728"
BORDER = "#213A57"
TEXT = "#F1F6FD"
MUTED = "#9AADC4"
ACCENT = "#38BDF8"
ACCENT_HOVER = "#67D1FA"
BLUE = "#2563EB"
BLUE_HOVER = "#2D6BDF"
DANGER = "#F05261"
DANGER_HOVER = "#D83F50"
WARN = "#F5B942"
GOOD = "#34D399"
CONTROL = "#132B46"
CONTROL_HOVER = "#1B3D60"
CONTROL_BORDER = "#305475"
DISABLED_TEXT = "#7E96B3"
SUPPORT = "#7DD3FC"
SUPPORT_HOVER = "#A6E3FF"
SUPPORT_TEXT = "#08243D"
BUTTON_RADIUS = 8
BUTTON_STYLES = {
    "primary": (BLUE, BLUE_HOVER, "#FFFFFF", BLUE),
    "blue": (BLUE, BLUE_HOVER, "#FFFFFF", BLUE),
    "secondary": (CONTROL, CONTROL_HOVER, TEXT, CONTROL_BORDER),
    "ghost": (CARD_ALT, CONTROL, "#C7D8EB", BORDER),
    "danger": ("#301C2B", "#452437", "#FFA9B2", "#7F354E"),
    "support": (SUPPORT, SUPPORT_HOVER, SUPPORT_TEXT, SUPPORT),
}

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class PortaMCPApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__(fg_color=BG)
        # Resolve installed families through Tk, on both Windows and Linux.
        families = set(tkfont.families(self))
        self.ui_font = next(
            (name for name in ("Segoe UI", "Inter", "Ubuntu", "DejaVu Sans") if name in families),
            tkfont.nametofont("TkDefaultFont").actual("family"),
        )
        self.mono_font = next(
            (name for name in ("Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Liberation Mono") if name in families),
            tkfont.nametofont("TkFixedFont").actual("family"),
        )
        # Draw the same crisp cup on both platforms, independent of emoji fonts.
        cup = Image.new("RGBA", (64, 64))
        draw = ImageDraw.Draw(cup)
        draw.rounded_rectangle((12, 22, 44, 49), radius=6, outline=SUPPORT_TEXT, width=4)
        draw.arc((36, 25, 56, 43), -90, 90, fill=SUPPORT_TEXT, width=4)
        draw.line((10, 55, 48, 55), fill=SUPPORT_TEXT, width=4)
        for x in (21, 34):
            draw.line((x, 8, x, 16), fill=SUPPORT_TEXT, width=3)
        self._support_icon = ctk.CTkImage(cup, size=(20, 20))
        self._support_large_icon = ctk.CTkImage(cup, size=(42, 42))
        backend.ensure_runtime_layout()

        self.title(f"{APP_NAME} Control Center")
        self.geometry("1380x860")
        self.minsize(1160, 740)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.icon_image = None
        icon_path = backend.PROJECT_ROOT / "assets" / "portamcp.png"
        if icon_path.exists():
            try:
                self.icon_image = ctk.CTkImage(Image.open(icon_path), size=(34, 34))
                # Tk window icon, independent of CTkImage scaling.
                import tkinter as tk

                raw = tk.PhotoImage(file=str(icon_path))
                self._window_icon = raw
                self.iconphoto(True, raw)
            except Exception:
                self.icon_image = None

        self.config_data = backend.read_config()
        self.ui_state = backend.read_ui_state()
        self.profile = str(self.ui_state.get("profile", "local"))
        if self.profile not in backend.PROFILE_LABELS:
            self.profile = "local"
        self.preset = str(self.ui_state.get("preset", "readonly"))
        if self.preset not in backend.PRESET_LABELS:
            self.preset = "custom"

        self.current_page = "dashboard"
        self.server_process: subprocess.Popen[str] | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()
        self._server_log_buffer: deque[str] = deque(
            ["PortaMCP Control Center ready.\n"],
            maxlen=2000,
        )
        self.install_process: subprocess.Popen[str] | None = None
        self._dirty = False
        self._last_server_signature = ""
        self._runtime_refresh_job: str | None = None
        self._page_cache: dict[str, ctk.CTkScrollableFrame] = {}
        self._active_page_frame: ctk.CTkScrollableFrame | None = None
        self._building_page: str | None = None
        self._dependency_check_running = False
        self._dependency_check_last = 0.0
        self._dependency_installed: bool | None = None
        self._support_popup: ctk.CTkToplevel | None = None

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self.content = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.grid_propagate(False)
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(1, weight=1)

        self._build_topbar()
        self.page_host = ctk.CTkFrame(self.content, fg_color=BG, corner_radius=0)
        self.page_host.grid(row=1, column=0, sticky="nsew", padx=28, pady=(8, 26))
        self.page_host.grid_propagate(False)
        self.page_host.grid_columnconfigure(0, weight=1)
        self.page_host.grid_rowconfigure(0, weight=1)

        self.show_page("dashboard")
        self.after(250, self._poll_logs)
        self._schedule_runtime_refresh(500)
        self.after(650, self._show_support_popup)

    # ---------- shell ----------
    def _build_sidebar(self) -> None:
        self.sidebar = ctk.CTkFrame(
            self,
            width=224,
            fg_color=SIDEBAR,
            corner_radius=0,
            border_width=0,
        )
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_columnconfigure(0, weight=1)
        self.sidebar.grid_rowconfigure(6, weight=1)

        brand = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="ew", padx=20, pady=(24, 24))
        brand.grid_columnconfigure(1, weight=1)

        if self.icon_image:
            ctk.CTkLabel(brand, text="", image=self.icon_image).grid(row=0, column=0, rowspan=2, padx=(0, 12))
        else:
            mark = ctk.CTkLabel(
                brand,
                text="P",
                width=38,
                height=40,
                corner_radius=10,
                fg_color=ACCENT,
                text_color="#061423",
                font=ctk.CTkFont(self.ui_font, 20, "bold"),
            )
            mark.grid(row=0, column=0, rowspan=2, padx=(0, 12))

        ctk.CTkLabel(
            brand,
            text=APP_NAME,
            text_color=TEXT,
            font=ctk.CTkFont(self.ui_font, 21, "bold"),
            anchor="w",
        ).grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(
            brand,
            text="Control center",
            text_color=MUTED,
            font=ctk.CTkFont(self.ui_font, 11, "bold"),
            anchor="w",
        ).grid(row=1, column=1, sticky="w", pady=(1, 0))

        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        nav = [
            ("dashboard", "Overview"),
            ("connection", "Connection & Auth"),
            ("security", "Security Profiles"),
            ("activity", "Activity & Logs"),
            ("setup", "Setup & Integrations"),
        ]
        for idx, (key, label) in enumerate(nav, start=1):
            btn = ctk.CTkButton(
                self.sidebar,
                text=label,
                height=48,
                corner_radius=BUTTON_RADIUS,
                fg_color="transparent",
                hover_color="#132C48",
                text_color=MUTED,
                anchor="w",
                font=ctk.CTkFont(self.ui_font, 13, "normal"),
                command=lambda k=key: self.show_page(k),
            )
            btn.grid(row=idx, column=0, sticky="ew", padx=14, pady=4)
            self.nav_buttons[key] = btn

        emergency_wrap = ctk.CTkFrame(
            self.sidebar,
            fg_color=CARD_ALT,
            corner_radius=12,
            border_width=1,
            border_color=BORDER,
        )
        emergency_wrap.grid(row=7, column=0, sticky="ew", padx=14, pady=(24, 8))
        ctk.CTkLabel(
            emergency_wrap,
            text="Safety control",
            text_color=MUTED,
            font=ctk.CTkFont(self.ui_font, 11, "bold"),
        ).pack(anchor="w", padx=13, pady=(10, 3))
        self.sidebar_emergency_btn = self._button(
            emergency_wrap, "Emergency Deny", self.toggle_emergency, "danger", 170,
        )
        self.sidebar_emergency_btn.pack(fill="x", padx=9, pady=(3, 9))

        footer = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        footer.grid(row=9, column=0, sticky="sew", padx=22, pady=20)
        ctk.CTkLabel(
            footer,
            text="Developed by TNLegend",
            text_color=MUTED,
            font=ctk.CTkFont(self.ui_font, 12),
            anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            footer,
            text=f"v{APP_VERSION}  |  Client-neutral MCP",
            text_color="#728BA8",
            font=ctk.CTkFont(self.ui_font, 11),
            anchor="w",
        ).pack(anchor="w", pady=(3, 0))

    def _build_topbar(self) -> None:
        top = ctk.CTkFrame(self.content, fg_color="transparent", height=86)
        top.grid(row=0, column=0, sticky="ew", padx=30, pady=(20, 0))
        top.grid_columnconfigure(0, weight=1)

        left = ctk.CTkFrame(top, fg_color="transparent")
        left.grid(row=0, column=0, sticky="ew")
        self.page_title = ctk.CTkLabel(
            left,
            text="Overview",
            text_color=TEXT,
            font=ctk.CTkFont(self.ui_font, 27, "bold"),
        )
        self.page_title.pack(anchor="w")
        self.page_subtitle = ctk.CTkLabel(
            top,
            text="Manage your MCP runtime from one place.",
            text_color=MUTED,
            font=ctk.CTkFont(self.ui_font, 13),
        )
        self.page_subtitle.configure(wraplength=500, justify="left")
        self.page_subtitle.configure(anchor="w")
        self.page_subtitle.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 10))
        self._wrap_to_width(self.page_subtitle)

        right = ctk.CTkFrame(top, fg_color="transparent")
        right.grid(row=0, column=1, sticky="e")
        self.support_button = self._button(
            right, "Support PortaMCP", self._open_support_page, "support", 186,
        )
        self.support_button.configure(image=self._support_icon, height=40)
        self.support_button.pack(side="left", padx=(0, 10))
        self.version_pill = ctk.CTkLabel(
            right,
            text=f"  v{APP_VERSION}  ",
            height=28,
            corner_radius=10,
            fg_color=CARD_ALT,
            text_color=MUTED,
            font=ctk.CTkFont(self.mono_font, 11, "bold"),
        )
        # Version is shown in the sidebar footer.
        self.dirty_badge = ctk.CTkLabel(
            top,
            text="",
            width=0,
            text_color=WARN,
            font=ctk.CTkFont(self.ui_font, 12, "bold"),
        )
        self.dirty_badge.grid(row=2, column=0, columnspan=2, sticky="w")
        self.dirty_badge.grid_remove()
        self.status_pill = ctk.CTkLabel(
            right,
            text="  CHECKING SYSTEM  ",
            height=32,
            corner_radius=BUTTON_RADIUS,
            fg_color=CARD_ALT,
            text_color=MUTED,
            border_width=1,
            border_color=BORDER,
            font=ctk.CTkFont(self.ui_font, 11, "bold"),
        )
        self.status_pill.pack(side="left")

    def _open_support_page(self) -> None:
        try:
            webbrowser.open_new_tab(SUPPORT_URL)
        except Exception as exc:
            messagebox.showerror("PortaMCP", f"Could not open the support page:\n{exc}")
            return
        self._animate_support_button()

    def _animate_support_button(self) -> None:
        button = getattr(self, "support_button", None)
        if button is None or not button.winfo_exists():
            return
        button.configure(text="Support page opened")

        def restore() -> None:
            if button.winfo_exists():
                button.configure(text="Support PortaMCP")

        self.after(1300, restore)

    def _show_support_popup(self) -> None:
        existing = self._support_popup
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_force()
            return

        popup = ctk.CTkToplevel(self, fg_color=BG)
        self._support_popup = popup
        popup.title("Support PortaMCP")
        popup.geometry("590x490")
        popup.resizable(False, False)
        popup.transient(self)
        popup.protocol("WM_DELETE_WINDOW", self._close_support_popup)
        popup.grid_columnconfigure(0, weight=1)
        popup.grid_rowconfigure(0, weight=1)

        card = ctk.CTkFrame(
            popup,
            fg_color=CARD,
            corner_radius=18,
            border_width=1,
            border_color=BORDER,
        )
        card.grid(row=0, column=0, sticky="nsew", padx=18, pady=18)
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            card, text="", image=self._support_large_icon,
            width=72, height=72, corner_radius=16, fg_color=SUPPORT,
        ).grid(row=0, column=0, pady=(28, 18))
        ctk.CTkLabel(
            card,
            text="Help PortaMCP keep growing",
            text_color=TEXT,
            font=ctk.CTkFont(self.ui_font, 23, "bold"),
        ).grid(row=1, column=0, padx=24, pady=(0, 8))
        ctk.CTkLabel(
            card,
            text=(
                "If PortaMCP helps you, consider supporting its development. "
                "Donations help fund new tools, cross-platform testing, maintenance, "
                "and future improvements."
            ),
            text_color="#B9CCE2",
            font=ctk.CTkFont(self.ui_font, 13),
            justify="center",
            wraplength=470,
        ).grid(row=2, column=0, padx=30, pady=(0, 10))
        ctk.CTkLabel(
            card,
            text="Support is optional - closing this window does not limit PortaMCP.",
            text_color=MUTED,
            font=ctk.CTkFont(self.ui_font, 11),
        ).grid(row=3, column=0, padx=24, pady=(0, 18))

        self._support_feedback = ctk.CTkLabel(
            card,
            text="",
            height=28,
            text_color=MUTED,
            font=ctk.CTkFont(self.ui_font, 12),
        )
        self._support_feedback.grid(row=4, column=0, padx=20, pady=(0, 2))

        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.grid(row=5, column=0, pady=(4, 28))
        self._support_donate_button = self._button(
            actions, "Buy me a coffee", self._donate_from_popup, "support", 204,
        )
        self._support_donate_button.configure(image=self._support_icon)
        self._support_donate_button.pack(side="left", padx=(0, 12))
        self._support_close_button = self._button(
            actions, "Maybe later", self._close_support_popup, "ghost", 132,
        )
        self._support_close_button.pack(side="left")

        popup.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - popup.winfo_width()) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - popup.winfo_height()) // 2)
        popup.geometry(f"+{x}+{y}")
        popup.lift()
        popup.focus_force()
        try:
            popup.grab_set()
        except Exception:
            pass

    def _donate_from_popup(self) -> None:
        try:
            webbrowser.open_new_tab(SUPPORT_URL)
        except Exception as exc:
            messagebox.showerror("PortaMCP", f"Could not open the support page:\n{exc}")
            return
        feedback = getattr(self, "_support_feedback", None)
        if feedback is not None and feedback.winfo_exists():
            feedback.configure(text="Support page opened in your browser.")
        button = getattr(self, "_support_donate_button", None)
        if button is not None and button.winfo_exists():
            button.configure(text="Page opened", state="disabled")
        self._animate_support_button()
        self.after(850, self._destroy_support_popup)

    def _close_support_popup(self) -> None:
        self._destroy_support_popup()

    def _destroy_support_popup(self) -> None:
        popup = self._support_popup
        self._support_popup = None
        if popup is not None and popup.winfo_exists():
            try:
                popup.grab_release()
            except Exception:
                pass
            popup.destroy()

    # ---------- routing ----------
    def show_page(self, page: str) -> None:
        titles = {
            "dashboard": ("Overview", "Runtime health, connection details and quick controls."),
            "connection": ("Connection & Auth", "Choose how clients connect and configure your public tunnel endpoint."),
            "security": ("Security Profiles", "Choose capabilities and explicitly grant filesystem scopes."),
            "activity": ("Activity & Logs", "Inspect server output and the local audit trail."),
            "setup": ("Setup & Integrations", "Dependencies, browser bridge and local project utilities."),
        }
        builders = {
            "dashboard": self._page_dashboard,
            "connection": self._page_connection,
            "security": self._page_security,
            "activity": self._page_activity,
            "setup": self._page_setup,
        }
        if page not in titles:
            return

        self.current_page = page
        title, subtitle = titles[page]
        self.page_title.configure(text=title)
        self.page_subtitle.configure(text=subtitle)
        for key, button in self.nav_buttons.items():
            if key == page:
                button.configure(
                    fg_color="#132C48",
                    text_color=TEXT,
                    font=ctk.CTkFont(self.ui_font, 13, "bold"),
                    border_width=1,
                    border_color="#285B8C",
                )
            else:
                button.configure(
                    fg_color="transparent",
                    text_color=MUTED,
                    font=ctk.CTkFont(self.ui_font, 13, "normal"),
                    border_width=0,
                )

        if self._active_page_frame is not None:
            self._active_page_frame.grid_remove()

        first_build = page not in self._page_cache
        if first_build:
            self._building_page = page
            builders[page]()
            self._building_page = None

        frame = self._page_cache[page]
        frame.grid(row=0, column=0, sticky="nsew")
        self._active_page_frame = frame
        self._on_page_shown(page, first_build)

    def _on_page_shown(self, page: str, first_build: bool) -> None:
        if page == "activity":
            self.after_idle(self.refresh_audit)
            self.after_idle(self.refresh_diagnostics)
        elif page == "setup":
            self._refresh_dependency_status(force=first_build)
        elif page == "dashboard":
            self._schedule_runtime_refresh(0)

    # ---------- reusable UI ----------
    @staticmethod
    def _wrap_to_width(label: ctk.CTkLabel) -> None:
        """Wrap copy to its allocated width without changing the grid's size."""
        def resize(_event) -> None:
            # CTk binds both its canvas and internal Tk label. Their event
            # widths differ; use the outer allocation to avoid resize loops.
            # winfo reports physical pixels; CTk wraplength uses scaled units.
            width = max(80, label._reverse_widget_scaling(label.winfo_width()) - 8)
            if abs(float(label.cget("wraplength")) - width) > 2:
                label.configure(wraplength=width)

        label.bind("<Configure>", resize, add="+")

    def _scroll_page(self) -> ctk.CTkScrollableFrame:
        frame = ctk.CTkScrollableFrame(
            self.page_host,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_button_color="#294461",
            scrollbar_button_hover_color="#3A6086",
        )
        frame.grid(row=0, column=0, sticky="nsew")
        frame.grid_columnconfigure(0, weight=1)
        if self._building_page is not None:
            self._page_cache[self._building_page] = frame
        return frame

    def _card(self, parent, title: str, subtitle: str = "") -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=10, border_width=1, border_color=BORDER)
        card.grid_columnconfigure(0, weight=1)
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=20, pady=(20, 14))
        ctk.CTkLabel(head, text=title, text_color=TEXT, font=ctk.CTkFont(self.ui_font, 16, "bold")).pack(anchor="w")
        if subtitle:
            subtitle_label = ctk.CTkLabel(
                head,
                text=subtitle,
                text_color=MUTED,
                justify="left",
                anchor="w",
                wraplength=650,
                font=ctk.CTkFont(self.ui_font, 12),
            )
            subtitle_label.pack(anchor="w", pady=(5, 0), fill="x")
            self._wrap_to_width(subtitle_label)
        return card

    def _metric_card(self, parent, row: int, col: int, label: str, value: str, accent: str = ACCENT) -> tuple[ctk.CTkFrame, ctk.CTkLabel]:
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=10, border_width=1, border_color=BORDER)
        card.grid(
            row=row,
            column=col,
            sticky="nsew",
            padx=(0 if col == 0 else 7, 0 if col == 1 else 7),
            pady=(0 if row == 0 else 7, 7 if row == 0 else 0),
        )
        ctk.CTkFrame(card, width=4, fg_color=accent, corner_radius=2).pack(side="left", fill="y", padx=(0, 12), pady=12)
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(side="left", fill="both", expand=True, pady=16, padx=(0, 14))
        ctk.CTkLabel(body, text=label.upper(), text_color=MUTED, font=ctk.CTkFont(self.ui_font, 11, "bold")).pack(anchor="w")
        val = ctk.CTkLabel(body, text=value, text_color=TEXT, font=ctk.CTkFont(self.ui_font, 16, "bold"), anchor="w")
        val.pack(anchor="w", pady=(5, 0))
        return card, val

    def _overview_status_card(
        self,
        parent,
        col: int,
        label: str,
        value: str,
        code: str,
        detail: str,
    ) -> tuple[ctk.CTkFrame, ctk.CTkLabel, ctk.CTkFrame, ctk.CTkLabel]:
        card = ctk.CTkFrame(
            parent,
            fg_color=CARD,
            corner_radius=10,
            border_width=1,
            border_color=BORDER,
        )
        card.grid(
            row=0,
            column=col,
            sticky="nsew",
            padx=(0 if col == 0 else 6, 0 if col == 3 else 6),
        )
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            card,
            text=label,
            text_color=MUTED,
            font=ctk.CTkFont(self.ui_font, 12),
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=18, pady=(16, 0))

        value_row = ctk.CTkFrame(card, fg_color="transparent")
        value_row.grid(row=1, column=0, sticky="ew", padx=18, pady=(5, 0))
        value_label = ctk.CTkLabel(
            value_row,
            text=value,
            text_color=TEXT,
            font=ctk.CTkFont(self.ui_font, 20, "bold"),
            anchor="w",
        )
        value_label.pack(side="left")

        dot = ctk.CTkFrame(
            value_row,
            width=7,
            height=7,
            corner_radius=4,
            fg_color=GOOD,
        )
        dot.pack(side="right", padx=(8, 0))
        dot.pack_propagate(False)

        detail_label = ctk.CTkLabel(
            card,
            text=detail,
            text_color=MUTED,
            font=ctk.CTkFont(self.ui_font, 11),
            anchor="w",
        )
        detail_label.grid(
            row=2,
            column=0,
            sticky="ew",
            padx=18,
            pady=(8, 16),
        )
        self._wrap_to_width(detail_label)

        return card, value_label, dot, detail_label


    def _button(self, parent, text: str, command, kind: str = "primary", width: int = 120) -> ctk.CTkButton:
        fg, hover, text_color, border = BUTTON_STYLES[kind]
        font = ctk.CTkFont(self.ui_font, 13, "bold")
        width = max(width, font.measure(text) + 32)
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            width=width,
            height=44,
            corner_radius=BUTTON_RADIUS,
            fg_color=fg,
            hover_color=hover,
            text_color=text_color,
            text_color_disabled=SUPPORT_TEXT if kind == "support" else DISABLED_TEXT,
            border_width=1,
            border_color=border,
            font=font,
        )

    # ---------- overview ----------
    def _page_dashboard(self) -> None:
        page = self._scroll_page()
        page.grid_columnconfigure(0, weight=1)

        port = int(self.config_data.get("port", 8765))
        emergency = backend.emergency_active()
        enabled_count = sum(
            1
            for key in backend.FEATURE_KEYS
            if backend.effective_feature_enabled(self.config_data, key)
        )
        scope_count = len(self.config_data.get("allowed_roots", []))

        metrics = ctk.CTkFrame(page, fg_color="transparent")
        metrics.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        for col in range(4):
            metrics.grid_columnconfigure(col, weight=1, uniform="overview-status")

        (
            _,
            self.metric_server,
            self.metric_server_dot,
            self.metric_server_detail,
        ) = self._overview_status_card(
            metrics,
            0,
            "Server",
            "Checking...",
            "SV",
            f"Port {port}",
        )
        (
            _,
            self.metric_auth,
            self.metric_auth_dot,
            self.metric_auth_detail,
        ) = self._overview_status_card(
            metrics,
            1,
            "Authentication",
            backend.PROFILE_LABELS[self.profile],
            "AU",
            "Client authentication profile",
        )
        (
            _,
            self.metric_security,
            self.metric_security_dot,
            self.metric_security_detail,
        ) = self._overview_status_card(
            metrics,
            2,
            "Security profile",
            backend.PRESET_LABELS.get(self.preset, "Custom"),
            "SC",
            f"{enabled_count} capabilities enabled",
        )
        (
            _,
            self.metric_emergency,
            self.metric_emergency_dot,
            self.metric_emergency_detail,
        ) = self._overview_status_card(
            metrics,
            3,
            "Emergency status",
            "DENY ACTIVE" if emergency else "Normal",
            "EM",
            "Restrictions active" if emergency else "No active restrictions",
        )

        runtime = ctk.CTkFrame(
            page,
            fg_color=CARD,
            corner_radius=16,
            border_width=1,
            border_color=BORDER,
        )
        runtime.grid(row=1, column=0, sticky="ew", pady=(0, 18))
        runtime.grid_columnconfigure(0, weight=1)

        runtime_head = ctk.CTkFrame(runtime, fg_color="transparent")
        runtime_head.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 7))
        runtime_head.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            runtime_head,
            text="Runtime controls",
            text_color=TEXT,
            font=ctk.CTkFont(self.ui_font, 15, "bold"),
        ).grid(row=0, column=1, sticky="sw")
        ctk.CTkLabel(
            runtime_head,
            text="Start, restart, recover, or stop PortaMCP cleanly.",
            text_color=MUTED,
            font=ctk.CTkFont(self.ui_font, 11),
        ).grid(row=1, column=1, sticky="nw", pady=(2, 0))

        profile_wrap = ctk.CTkFrame(
            runtime_head,
            fg_color=CARD_ALT,
            corner_radius=10,
            border_width=1,
            border_color=BORDER,
        )
        profile_wrap.grid(row=0, column=2, rowspan=2, sticky="e")
        ctk.CTkLabel(
            profile_wrap,
            text="PROFILE",
            text_color=MUTED,
            font=ctk.CTkFont(self.ui_font, 11, "bold"),
        ).pack(side="left", padx=(12, 8), pady=8)
        self.runtime_profile_label = ctk.CTkLabel(
            profile_wrap,
            text=backend.PROFILE_LABELS[self.profile],
            text_color=TEXT,
            font=ctk.CTkFont(self.ui_font, 12, "bold"),
        )
        self.runtime_profile_label.pack(side="left", padx=(0, 12), pady=8)

        body = ctk.CTkFrame(runtime, fg_color="transparent")
        body.grid(row=1, column=0, sticky="ew", padx=20, pady=(12, 19))
        self._button(body, "Start server", self.start_server, "primary", 145).pack(side="left")
        self._button(body, "Restart", self.restart_server, "secondary", 120).pack(side="left", padx=9)
        self._button(body, "Recover public", self.recover_public_connection, "secondary", 145).pack(side="left", padx=(0, 9))
        self._button(body, "Stop", self.stop_server, "ghost", 105).pack(side="left")
        self._button(body, "Emergency deny", self.activate_emergency, "danger", 155).pack(side="right")

        lower = ctk.CTkFrame(page, fg_color="transparent")
        lower.grid(row=2, column=0, sticky="nsew")
        lower.grid_columnconfigure(0, weight=1, uniform="overview-lower")
        lower.grid_columnconfigure(1, weight=1, uniform="overview-lower")

        connection = self._card(
            lower,
            "Connection summary",
            "Current client profile, endpoint, tunnel, and filesystem scope.",
        )
        connection.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        content = ctk.CTkFrame(connection, fg_color="transparent")
        content.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 18))
        content.grid_columnconfigure(0, weight=1)

        summary_rows = [
            ("PROFILE", backend.PROFILE_LABELS[self.profile]),
            ("MCP ENDPOINT", backend.endpoint_for(self.profile, self.config_data)),
            (
                "PUBLIC BASE",
                self.config_data.get("public_base_url") or "Not configured",
            ),
            (
                "FILESYSTEM SCOPES",
                f"{scope_count} granted" if scope_count else "None granted",
            ),
        ]
        self._summary_value_labels: dict[str, ctk.CTkLabel] = {}
        for idx, (label, value) in enumerate(summary_rows):
            item = ctk.CTkFrame(
                content,
                fg_color=CARD_ALT,
                corner_radius=10,
                border_width=1,
                border_color="#1B304A",
            )
            item.grid(row=idx, column=0, sticky="ew", pady=(0, 8))
            item.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                item,
                text=label,
                width=120,
                text_color=MUTED,
                font=ctk.CTkFont(self.ui_font, 11, "bold"),
                anchor="w",
            ).grid(row=0, column=0, sticky="w", padx=12, pady=(8, 0))
            value_label = ctk.CTkLabel(
                item,
                text=str(value),
                text_color=TEXT,
                font=ctk.CTkFont(
                    self.mono_font if label in {"MCP ENDPOINT", "PUBLIC BASE"} else self.ui_font,
                    12,
                ),
                anchor="w",
                justify="left",
                wraplength=420,
            )
            value_label.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 10))
            self._wrap_to_width(value_label)
            self._summary_value_labels[label] = value_label
            if label == "MCP ENDPOINT":
                self._button(
                    item,
                    "Copy",
                    self.copy_endpoint,
                    "ghost",
                    62,
                ).grid(row=0, column=1, rowspan=2, padx=(0, 8), pady=6)

        policy = self._card(
            lower,
            "Active capability policy",
            "Live view of the capabilities currently granted to this profile.",
        )
        policy.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        policy_top = ctk.CTkFrame(policy, fg_color="transparent")
        policy_top.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 8))
        policy_top.grid_columnconfigure(0, weight=1)
        self.policy_count_label = ctk.CTkLabel(
            policy_top,
            text=f"{enabled_count} / {len(backend.FEATURE_KEYS)} enabled",
            text_color=ACCENT,
            font=ctk.CTkFont(self.mono_font, 11, "bold"),
        )
        self.policy_count_label.grid(row=0, column=0, sticky="w")

        grid = ctk.CTkFrame(policy, fg_color="transparent")
        grid.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 16))
        grid.grid_columnconfigure(0, weight=1)

        labels = [
            ("File writes", "allow_fs_write"),
            ("Shell", "allow_shell"),
            ("Process launch", "allow_process_start"),
            ("Process kill", "allow_process_kill"),
            ("Destructive FS", "allow_destructive_fs"),
            ("Input control", "allow_input_control"),
            ("UI automation", "allow_ui_automation"),
            ("Browser control", "allow_browser_control"),
            ("Admin commands", "allow_admin_commands"),
        ]
        self._policy_widgets: dict[str, tuple[ctk.CTkFrame, ctk.CTkLabel]] = {}
        for idx, (label, key) in enumerate(labels):
            supported = backend.feature_supported(key)
            enabled = backend.effective_feature_enabled(self.config_data, key)
            item = ctk.CTkFrame(
                grid,
                fg_color="transparent",
                corner_radius=0,
                border_width=0,
            )
            item.grid(
                row=idx,
                column=0,
                sticky="ew",
                padx=4,
                pady=2,
            )
            dot = ctk.CTkFrame(
                item,
                width=7,
                height=7,
                corner_radius=4,
                fg_color=(ACCENT if enabled else ("#526C8A" if supported else "#33485F")),
            )
            dot.pack(side="left", padx=(11, 8), pady=5)
            dot.pack_propagate(False)
            ctk.CTkLabel(
                item,
                text=label,
                text_color=TEXT if enabled else MUTED,
                font=ctk.CTkFont(self.ui_font, 11),
            ).pack(side="left", pady=2)
            state = ctk.CTkLabel(
                item,
                text="ON" if enabled else ("OFF" if supported else "N/A"),
                text_color=ACCENT if enabled else MUTED,
                font=ctk.CTkFont(self.mono_font, 11, "bold"),
            )
            state.pack(side="right", padx=(8, 10), pady=2)
            self._policy_widgets[key] = (dot, state)

    # ---------- connection ----------
    def _page_connection(self) -> None:
        page = self._scroll_page()

        auth = self._card(page, "Authentication profile", "Choose how MCP clients authenticate. No Auth is intentionally restricted to localhost.")
        auth.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        body = ctk.CTkFrame(auth, fg_color="transparent")
        body.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 18))
        self.profile_segment = ctk.CTkSegmentedButton(
            body,
            values=["Local / No Auth", "Bearer Token", "OAuth"],
            command=self._profile_selected,
            selected_color=BLUE,
            selected_hover_color=BLUE_HOVER,
            unselected_color=CONTROL,
            unselected_hover_color=CONTROL_HOVER,
            text_color=TEXT,
            font=ctk.CTkFont(self.ui_font, 13, "bold"),
            height=40,
        )
        self.profile_segment.pack(anchor="w", fill="x")
        self.profile_segment.set(backend.PROFILE_LABELS[self.profile])
        self.auth_explainer = ctk.CTkLabel(body, text="", text_color=MUTED, justify="left", wraplength=650, font=ctk.CTkFont(self.ui_font, 12))
        self.auth_explainer.pack(anchor="w", pady=(12, 0))

        self.secret_card = self._card(page, "Credentials")
        self.secret_card.grid(row=1, column=0, sticky="ew", pady=(0, 16))
        self.secret_body = ctk.CTkFrame(self.secret_card, fg_color="transparent")
        self.secret_body.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 18))
        self.secret_body.grid_columnconfigure(0, weight=1)

        tunnel = self._card(page, "Public endpoint / tunnel", "Paste the HTTPS base URL supplied by Tailscale Funnel, Cloudflare Tunnel, ngrok, or your own reverse proxy. PortaMCP adds /mcp automatically.")
        tunnel.grid(row=2, column=0, sticky="ew", pady=(0, 16))
        t = ctk.CTkFrame(tunnel, fg_color="transparent")
        t.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 18))
        t.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(t, text="Provider", text_color=MUTED, font=ctk.CTkFont(self.ui_font, 12, "bold")).grid(row=0, column=0, sticky="w", padx=(0, 12), pady=6)
        self.tunnel_provider = ctk.CTkOptionMenu(
            t,
            values=["Manual / custom HTTPS", "Tailscale", "Cloudflare Tunnel", "ngrok", "Other"],
            fg_color=CONTROL,
            button_color=CONTROL,
            button_hover_color=CONTROL_HOVER,
            width=230,
            height=40,
            corner_radius=BUTTON_RADIUS,
            text_color=TEXT,
            font=ctk.CTkFont(self.ui_font, 13),
            dropdown_fg_color=CARD,
            dropdown_hover_color=CONTROL_HOVER,
            dropdown_text_color=TEXT,
            dropdown_font=ctk.CTkFont(self.ui_font, 13),
        )
        self.tunnel_provider.grid(row=0, column=1, sticky="w", pady=6)
        self.tunnel_provider.set(str(self.ui_state.get("tunnel_provider", "Manual / custom HTTPS")))

        ctk.CTkLabel(t, text="Base URL", text_color=MUTED, font=ctk.CTkFont(self.ui_font, 12, "bold")).grid(row=1, column=0, sticky="w", padx=(0, 12), pady=6)
        self.tunnel_entry = ctk.CTkEntry(
            t,
            placeholder_text="https://your-host.example.com",
            fg_color="#081321",
            border_color=BORDER,
            text_color=TEXT,
            height=40,
            font=ctk.CTkFont(self.mono_font, 12),
        )
        self.tunnel_entry.grid(row=1, column=1, sticky="ew", pady=6)
        self.tunnel_entry.insert(0, str(self.config_data.get("public_base_url", "")))
        self._button(t, "Detect Tailscale", self.detect_tailscale, "ghost", 130).grid(row=1, column=2, padx=(10, 0))
        self._button(t, "Save endpoint", self.save_connection, "primary", 120).grid(row=1, column=3, padx=(8, 0))

        self.endpoint_preview = ctk.CTkLabel(
            t,
            text=f"MCP URL  {backend.endpoint_for(self.profile, self.config_data)}",
            text_color=TEXT,
            fg_color=CARD_ALT,
            corner_radius=8,
            anchor="w",
            padx=12,
            height=36,
            wraplength=650,
            justify="left",
            font=ctk.CTkFont(self.mono_font, 12),
        )
        self.endpoint_preview.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        self._wrap_to_width(self.endpoint_preview)

        server = self._card(page, "Local listener", "The MCP server binds locally; a tunnel/reverse proxy can publish it without changing this address.")
        server.grid(row=3, column=0, sticky="ew")
        s = ctk.CTkFrame(server, fg_color="transparent")
        s.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 18))
        ctk.CTkLabel(s, text="Host", text_color=MUTED, font=ctk.CTkFont(self.ui_font, 12, "bold")).grid(row=0, column=0, sticky="w")
        self.host_entry = ctk.CTkEntry(s, width=180, height=40, fg_color="#081321", border_color=BORDER, font=ctk.CTkFont(self.mono_font, 12))
        self.host_entry.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.host_entry.insert(0, str(self.config_data.get("host", "127.0.0.1")))
        ctk.CTkLabel(s, text="Port", text_color=MUTED, font=ctk.CTkFont(self.ui_font, 12, "bold")).grid(row=0, column=1, sticky="w", padx=(18, 0))
        self.port_entry = ctk.CTkEntry(s, width=110, height=40, fg_color="#081321", border_color=BORDER, font=ctk.CTkFont(self.mono_font, 12))
        self.port_entry.grid(row=1, column=1, sticky="w", padx=(18, 0), pady=(4, 0))
        self.port_entry.insert(0, str(self.config_data.get("port", 8765)))
        self._button(s, "Save listener", self.save_connection, "secondary", 118).grid(row=1, column=2, padx=(18, 0), pady=(4, 0))

        self._render_auth_details()

    def _profile_selected(self, label: str) -> None:
        reverse = {v: k for k, v in backend.PROFILE_LABELS.items()}
        self.profile = reverse[label]
        self.ui_state["profile"] = self.profile
        backend.write_ui_state(self.ui_state)
        self._mark_dirty(True)
        if hasattr(self, "secret_body"):
            self._render_auth_details()
        if hasattr(self, "endpoint_preview"):
            self.endpoint_preview.configure(text=f"MCP URL  {backend.endpoint_for(self.profile, self.config_data)}")

    def _render_auth_details(self) -> None:
        for widget in self.secret_body.winfo_children():
            widget.destroy()
        if self.profile == "local":
            self.auth_explainer.configure(
                text="Local / No Auth is the frictionless option for software on this PC. PortaMCP forces this mode to 127.0.0.1 so it cannot be exposed unauthenticated by changing the config host."
            )
            ctk.CTkLabel(
                self.secret_body,
                text="No credential required. The endpoint is loopback-only by design.",
                text_color="#C7D8EB",
                font=ctk.CTkFont(self.ui_font, 13),
                anchor="w",
            ).grid(row=0, column=0, sticky="ew")
            return

        if self.profile == "bearer":
            self.auth_explainer.configure(text="Bearer mode protects /mcp with a generated token. Copy it into any client that supports Authorization: Bearer headers.")
            label = "Bearer token"
            value = backend.bearer_token()
            rotate = self.rotate_bearer
        else:
            self.auth_explainer.configure(text="OAuth mode is best for hosted clients. It uses PortaMCP's self-hosted OAuth flow and requires a public HTTPS endpoint/tunnel.")
            label = "OAuth owner password"
            value = backend.oauth_password()
            rotate = self.rotate_oauth

        ctk.CTkLabel(self.secret_body, text=label, text_color=MUTED, font=ctk.CTkFont(self.ui_font, 12, "bold"), anchor="w").grid(row=0, column=0, sticky="w")
        row = ctk.CTkFrame(self.secret_body, fg_color="transparent")
        row.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        row.grid_columnconfigure(0, weight=1)
        self.secret_entry = ctk.CTkEntry(
            row,
            fg_color="#081321",
            border_color=BORDER,
            text_color=TEXT,
            font=ctk.CTkFont(self.mono_font, 12),
            show="*",
            height=40,
        )
        self.secret_entry.grid(row=0, column=0, sticky="ew")
        self.secret_entry.insert(0, value)
        self.secret_entry.configure(state="readonly")
        self.secret_visible = False
        self.secret_toggle_button = self._button(row, "Reveal", self.toggle_secret, "ghost", 78)
        self.secret_toggle_button.grid(row=0, column=1, padx=(8, 0))
        self._button(row, "Copy", lambda: self.copy_text(value, f"{label} copied"), "secondary", 70).grid(row=0, column=2, padx=(8, 0))
        self._button(row, "Rotate", rotate, "ghost", 76).grid(row=0, column=3, padx=(8, 0))
        if self.profile == "oauth":
            self._button(row, "Reset OAuth clients", self.reset_oauth, "ghost", 140).grid(row=0, column=4, padx=(8, 0))

    def toggle_secret(self) -> None:
        self.secret_visible = not self.secret_visible
        self.secret_entry.configure(show="" if self.secret_visible else "*")
        self.secret_toggle_button.configure(text="Hide" if self.secret_visible else "Reveal")

    def rotate_bearer(self) -> None:
        if not messagebox.askyesno("Rotate bearer token", "Existing clients using the old token will stop authenticating. Rotate it now?"):
            return
        backend.rotate_bearer_token()
        self._render_auth_details()
        self._mark_dirty(True)
        self.toast("Bearer token rotated. Restart the server to apply it.")

    def rotate_oauth(self) -> None:
        if not messagebox.askyesno("Rotate OAuth password", "Rotate the owner password used to approve OAuth authorization requests?"):
            return
        backend.rotate_oauth_password()
        self._render_auth_details()
        self.toast("OAuth owner password rotated.")

    def reset_oauth(self) -> None:
        processes = backend.server_processes()
        if processes:
            prompt = (
                "A PortaMCP server is currently running. Resetting OAuth while it is live would leave in-memory "
                "credentials active. Stop the server and reset stored OAuth clients/tokens now?"
            )
        else:
            prompt = "This revokes stored OAuth clients/tokens. Connected clients will need to authorize again. Continue?"
        if not messagebox.askyesno("Reset OAuth state", prompt):
            return
        if processes:
            self.stop_server(ask=False)
            if backend.server_processes():
                messagebox.showerror("Reset OAuth state", "Could not stop the running PortaMCP server. OAuth state was not reset.")
                return
        backend.reset_oauth_state()
        self.toast("OAuth state reset. Restart PortaMCP to authorize clients again.")
        messagebox.showinfo(
            "OAuth reset complete",
            "Stored OAuth clients and tokens were reset. Restart PortaMCP, then connected clients must authorize again.",
        )

    def detect_tailscale(self) -> None:
        url = backend.detect_tailscale_url()
        if not url:
            messagebox.showinfo("Tailscale", "No Tailscale DNS hostname was detected. You can still paste any HTTPS tunnel URL manually.")
            return
        self.tunnel_entry.delete(0, "end")
        self.tunnel_entry.insert(0, url)
        self.tunnel_provider.set("Tailscale")
        self.toast("Tailscale hostname detected. Save the endpoint when ready.")

    def save_connection(self) -> None:
        try:
            port = int(self.port_entry.get().strip())
            if not 1 <= port <= 65535:
                raise ValueError("Port must be between 1 and 65535.")
            host = self.host_entry.get().strip() or "127.0.0.1"
            cfg = dict(self.config_data)
            cfg["host"] = host
            cfg["port"] = port
            cfg["transport"] = "streamable-http"
            cfg = backend.configure_public_url(cfg, self.tunnel_entry.get())
            backend.write_config(cfg)
            self.config_data = cfg
            self.ui_state["profile"] = self.profile
            self.ui_state["tunnel_provider"] = self.tunnel_provider.get()
            backend.write_ui_state(self.ui_state)
            self.endpoint_preview.configure(text=f"MCP URL  {backend.endpoint_for(self.profile, self.config_data)}")
            self._mark_dirty(True)
            self.toast("Connection settings saved. Restart PortaMCP to apply changes.")
        except Exception as exc:
            messagebox.showerror("Connection settings", str(exc))

    # ---------- security ----------
    def _page_security(self) -> None:
        page = self._scroll_page()
        preset = self._card(page, "Security preset", "Presets change capabilities and limits only. They never grant folders or drives automatically.")
        preset.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        p = ctk.CTkFrame(preset, fg_color="transparent")
        p.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 18))
        self.preset_segment = ctk.CTkSegmentedButton(
            p,
            values=["Full Control", "Balanced", "Read Only", "Custom"],
            command=self._preset_selected,
            selected_color=BLUE,
            selected_hover_color=BLUE_HOVER,
            unselected_color=CONTROL,
            unselected_hover_color=CONTROL_HOVER,
            text_color=TEXT,
            height=40,
            font=ctk.CTkFont(self.ui_font, 13, "bold"),
        )
        self.preset_segment.pack(fill="x")
        self.preset_segment.set(backend.PRESET_LABELS.get(self.preset, "Custom"))
        self.preset_desc = ctk.CTkLabel(p, text="", text_color=MUTED, justify="left", wraplength=650, font=ctk.CTkFont(self.ui_font, 12))
        self.preset_desc.pack(anchor="w", pady=(12, 0))
        self._update_preset_desc()

        roots = self._card(page, "Filesystem scopes", "Start with no granted paths. Add only the folders you want PortaMCP to access. Denied paths always win.")
        roots.grid(row=1, column=0, sticky="ew", pady=(0, 16))
        r = ctk.CTkFrame(roots, fg_color="transparent")
        r.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 18))
        r.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkLabel(r, text="ALLOWED SCOPES", text_color=MUTED, font=ctk.CTkFont(self.ui_font, 11, "bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(r, text="DENIED PATHS", text_color=MUTED, font=ctk.CTkFont(self.ui_font, 11, "bold")).grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.allowed_roots_box = ctk.CTkTextbox(r, height=112, fg_color="#081321", border_width=1, border_color=BORDER, font=ctk.CTkFont(self.mono_font, 12))
        self.allowed_roots_box.grid(row=1, column=0, sticky="ew", pady=(5, 6), padx=(0, 6))
        self.allowed_roots_box.insert("1.0", "\n".join(map(str, self.config_data.get("allowed_roots", []))))
        self.denied_roots_box = ctk.CTkTextbox(r, height=112, fg_color="#081321", border_width=1, border_color=BORDER, font=ctk.CTkFont(self.mono_font, 12))
        self.denied_roots_box.grid(row=1, column=1, sticky="ew", pady=(5, 6), padx=(6, 0))
        self.denied_roots_box.insert("1.0", "\n".join(map(str, self.config_data.get("denied_roots", []))))
        self._button(r, "Add folder scope", self.add_allowed_root, "ghost", 132).grid(row=2, column=0, sticky="w")
        self._button(r, "Reset denied defaults", self.reset_denied_roots, "ghost", 148).grid(row=2, column=1, sticky="w", padx=(12, 0))
        ctk.CTkLabel(
            r,
            text="Scopes are selected from this computer at runtime. PortaMCP never grants a detected volume automatically.",
            text_color=MUTED,
            font=ctk.CTkFont(self.ui_font, 11),
            anchor="w",
            justify="left",
            wraplength=660,
        ).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ctk.CTkLabel(
            r,
            text="Important: filesystem scopes constrain structured file operations and shell/process working directories. Once shell or process launch is enabled, commands/programs may access resources beyond those paths. Enable them only for trusted clients.",
            text_color="#D6B76D",
            justify="left",
            anchor="w",
            wraplength=660,
            font=ctk.CTkFont(self.ui_font, 11),
        ).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        capabilities = self._card(page, "Capabilities", "These switches are enforced by the MCP policy layer, not just hidden in the UI.")
        capabilities.grid(row=2, column=0, sticky="ew", pady=(0, 16))
        cap = ctk.CTkFrame(capabilities, fg_color="transparent")
        cap.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 18))
        for col in range(2):
            cap.grid_columnconfigure(col, weight=1)
        feature_labels = [
            ("Filesystem writes", "allow_fs_write", "Create, edit, copy and move files."),
            ("Shell execution", "allow_shell", "Run PowerShell on Windows or /bin/sh commands on Linux."),
            ("Process launch", "allow_process_start", "Start programs from an allowed working directory."),
            ("Process termination", "allow_process_kill", "Terminate explicitly targeted processes."),
            ("Destructive filesystem", "allow_destructive_fs", "Delete and destructive overwrite operations."),
            ("Mouse & keyboard input", "allow_input_control", "Raw desktop input control."),
            ("Structured UI automation", "allow_ui_automation", "Semantic desktop UI actions: Windows UI Automation on Windows, AT-SPI on supported Linux sessions."),
            ("Browser control", "allow_browser_control", "Managed browser and current-Chrome actions."),
            ("Administrative commands", "allow_admin_commands", "High-risk elevated/admin command patterns."),
        ]
        self.feature_vars: dict[str, ctk.BooleanVar] = {}
        for idx, (label, key, desc) in enumerate(feature_labels):
            item = ctk.CTkFrame(cap, fg_color=CARD_ALT, corner_radius=9)
            item.grid(row=idx // 2, column=idx % 2, sticky="nsew", padx=(0 if idx % 2 == 0 else 6, 6 if idx % 2 == 0 else 0), pady=5)
            supported = backend.feature_supported(key)
            var = ctk.BooleanVar(
                value=backend.effective_feature_enabled(self.config_data, key)
            )
            self.feature_vars[key] = var
            switch = ctk.CTkSwitch(
                item,
                text=label,
                variable=var,
                progress_color=ACCENT,
                button_color="#EFF6FF",
                button_hover_color="#FFFFFF",
                text_color=TEXT if supported else "#728BA8",
                font=ctk.CTkFont(self.ui_font, 13, "bold"),
                command=self._customized_security,
                state="normal" if supported else "disabled",
            )
            switch.pack(anchor="w", padx=12, pady=(11, 2))
            if not supported:
                desc = desc + " Unavailable on this platform/session."
            description = ctk.CTkLabel(item, text=desc, text_color=MUTED, font=ctk.CTkFont(self.ui_font, 12), anchor="w", justify="left", wraplength=320)
            description.pack(fill="x", padx=12, pady=(0, 14))
            self._wrap_to_width(description)

        advanced = self._card(page, "Limits & runtime policy", "Adjust payload ceilings and command timeout. Conservative defaults are recommended for remote use.")
        advanced.grid(row=3, column=0, sticky="ew")
        a = ctk.CTkFrame(advanced, fg_color="transparent")
        a.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 18))
        fields = [
            ("Read limit (MB)", "max_read_bytes", 1_000_000),
            ("Write limit (MB)", "max_write_bytes", 1_000_000),
            ("Command output (MB)", "max_command_output_bytes", 1_000_000),
            ("Command timeout (sec)", "command_timeout_seconds", 1),
        ]
        self.limit_entries: dict[str, tuple[ctk.CTkEntry, int]] = {}
        for idx, (label, key, divisor) in enumerate(fields):
            ctk.CTkLabel(a, text=label, text_color=MUTED, font=ctk.CTkFont(self.ui_font, 11, "bold")).grid(row=0, column=idx, sticky="w", padx=(0 if idx == 0 else 10, 0))
            entry = ctk.CTkEntry(a, width=150, height=40, fg_color="#081321", border_color=BORDER)
            entry.grid(row=1, column=idx, sticky="ew", padx=(0 if idx == 0 else 10, 0), pady=(5, 0))
            raw = int(self.config_data.get(key, 0))
            entry.insert(0, str(raw / divisor if divisor != 1 else raw).rstrip("0").rstrip(".") if divisor != 1 else str(raw))
            self.limit_entries[key] = (entry, divisor)
            a.grid_columnconfigure(idx, weight=1)
        self._button(a, "Save security policy", self.save_security, "primary", 150).grid(row=2, column=0, columnspan=4, sticky="e", pady=(14, 0))

    def _refresh_security_page_from_config(self) -> None:
        if hasattr(self, "preset_segment"):
            self.preset_segment.set(backend.PRESET_LABELS.get(self.preset, "Custom"))
        self._update_preset_desc()

        if hasattr(self, "allowed_roots_box"):
            self.allowed_roots_box.delete("1.0", "end")
            self.allowed_roots_box.insert(
                "1.0",
                "\n".join(map(str, self.config_data.get("allowed_roots", []))),
            )
        if hasattr(self, "denied_roots_box"):
            self.denied_roots_box.delete("1.0", "end")
            self.denied_roots_box.insert(
                "1.0",
                "\n".join(map(str, self.config_data.get("denied_roots", []))),
            )
        if hasattr(self, "feature_vars"):
            for key, var in self.feature_vars.items():
                var.set(backend.effective_feature_enabled(self.config_data, key))
        if hasattr(self, "limit_entries"):
            for key, (entry, divisor) in self.limit_entries.items():
                raw = int(self.config_data.get(key, 0))
                value = (
                    str(raw / divisor).rstrip("0").rstrip(".")
                    if divisor != 1
                    else str(raw)
                )
                entry.delete(0, "end")
                entry.insert(0, value)

    def _preset_selected(self, label: str) -> None:
        reverse = {v: k for k, v in backend.PRESET_LABELS.items()}
        preset = reverse[label]
        self.preset = preset
        if preset != "custom":
            self.config_data = backend.apply_preset(self.config_data, preset)
            backend.write_config(self.config_data)
            self.ui_state["preset"] = preset
            backend.write_ui_state(self.ui_state)
            self._mark_dirty(True)
            self._refresh_security_page_from_config()
            self.toast(f"{label} preset applied. Restart PortaMCP to apply it.")
        else:
            self.ui_state["preset"] = "custom"
            backend.write_ui_state(self.ui_state)
            self._update_preset_desc()

    def _update_preset_desc(self) -> None:
        descriptions = {
            "full": "Maximum capabilities. Filesystem scopes remain exactly what you grant below; no drive or folder is added automatically. Administrative command patterns remain off by default.",
            "balanced": "Practical automation mode: file writes, shell, UI and browser control are enabled, while process launch/termination and destructive filesystem operations stay blocked. Scopes remain manual.",
            "readonly": "Safe default. Mutating filesystem actions, shell, process launch/termination, input, UI actions and browser control are disabled at policy level. Add scopes only if you want file observation.",
            "custom": "Fine-grained capability mode. Filesystem scopes are always edited separately below.",
        }
        if hasattr(self, "preset_desc"):
            self.preset_desc.configure(text=descriptions[self.preset])

    def _customized_security(self) -> None:
        if self.preset != "custom":
            self.preset = "custom"
            self.ui_state["preset"] = "custom"
            backend.write_ui_state(self.ui_state)
            if hasattr(self, "preset_segment"):
                self.preset_segment.set("Custom")
            self._update_preset_desc()

    def add_allowed_root(self) -> None:
        path = filedialog.askdirectory(title="Choose a filesystem scope")
        if not path:
            return
        current = self.allowed_roots_box.get("1.0", "end").strip()
        lines = [x.strip() for x in current.splitlines() if x.strip()]
        if path not in lines:
            lines.append(path)
        self.allowed_roots_box.delete("1.0", "end")
        self.allowed_roots_box.insert("1.0", "\n".join(lines))
        self._mark_dirty(True)

    def reset_denied_roots(self) -> None:
        defaults = backend.reset_denied_roots()
        self.denied_roots_box.delete("1.0", "end")
        self.denied_roots_box.insert("1.0", "\n".join(defaults))
        self._mark_dirty(True)
        self.toast("Denied paths reset to portable safe defaults. Save the security policy to apply them.")

    def save_security(self) -> None:
        try:
            cfg = dict(self.config_data)
            cfg["allowed_roots"] = backend.validate_roots(self.allowed_roots_box.get("1.0", "end").splitlines())
            cfg["denied_roots"] = [x.strip() for x in self.denied_roots_box.get("1.0", "end").splitlines() if x.strip()]
            for key, var in self.feature_vars.items():
                cfg[key] = bool(var.get())
            for key, (entry, divisor) in self.limit_entries.items():
                value = float(entry.get().strip())
                if value <= 0:
                    raise ValueError(f"{key} must be greater than zero")
                cfg[key] = int(value * divisor)
            self.config_data = cfg
            backend.write_config(cfg)
            backend.write_ui_state(self.ui_state)
            self._mark_dirty(True)
            self.toast("Security policy saved. Restart PortaMCP to apply it.")
        except Exception as exc:
            messagebox.showerror("Security policy", str(exc))

    # ---------- logs ----------
    def _page_activity(self) -> None:
        page = self._scroll_page()
        server = self._card(page, "Server output", "Live stdout/stderr is captured when PortaMCP is launched from this control center.")
        server.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        self.server_log_box = ctk.CTkTextbox(server, height=270, fg_color="#060F1C", border_width=0, text_color="#C7D8EB", font=ctk.CTkFont(self.mono_font, 11))
        self.server_log_box.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))
        self.server_log_box.insert("end", "".join(self._server_log_buffer))
        self.server_log_box.configure(state="disabled")
        controls = ctk.CTkFrame(server, fg_color="transparent")
        controls.grid(row=2, column=0, sticky="e", padx=12, pady=(0, 12))
        self._button(controls, "Clear", self.clear_server_log, "ghost", 76).pack(side="left")

        audit = self._card(page, "Audit trail", "Local JSONL audit records emitted by policy-protected tools.")
        audit.grid(row=1, column=0, sticky="ew")
        self.audit_box = ctk.CTkTextbox(audit, height=300, fg_color="#060F1C", border_width=0, text_color="#B3C7DF", font=ctk.CTkFont(self.mono_font, 11))
        self.audit_box.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))
        self.refresh_audit()
        controls2 = ctk.CTkFrame(audit, fg_color="transparent")
        controls2.grid(row=2, column=0, sticky="e", padx=12, pady=(0, 12))
        self._button(controls2, "Refresh", self.refresh_audit, "secondary", 86).pack(side="left")
        self._button(controls2, "Open audit file", lambda: backend.open_path(backend.AUDIT_PATH), "ghost", 112).pack(side="left", padx=(8, 0))

        diagnostics = self._card(
            page,
            "Transport diagnostics",
            "Rotating MCP transport status plus persisted SDK/Uvicorn errors. Credentials and Authorization headers are not logged.",
        )
        diagnostics.grid(row=2, column=0, sticky="ew", pady=(16, 0))
        self.diagnostics_box = ctk.CTkTextbox(
            diagnostics,
            height=300,
            fg_color="#060F1C",
            border_width=0,
            text_color="#B3C7DF",
            font=ctk.CTkFont(self.mono_font, 11),
        )
        self.diagnostics_box.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))
        self.refresh_diagnostics()
        controls3 = ctk.CTkFrame(diagnostics, fg_color="transparent")
        controls3.grid(row=2, column=0, sticky="e", padx=12, pady=(0, 12))
        self._button(controls3, "Refresh", self.refresh_diagnostics, "secondary", 86).pack(side="left")
        self._button(
            controls3,
            "Open runtime folder",
            lambda: backend.open_path(backend.RUNTIME_DIR),
            "ghost",
            132,
        ).pack(side="left", padx=(8, 0))

    def clear_server_log(self) -> None:
        self._server_log_buffer.clear()
        if not hasattr(self, "server_log_box"):
            return
        self.server_log_box.configure(state="normal")
        self.server_log_box.delete("1.0", "end")
        self.server_log_box.configure(state="disabled")

    def refresh_audit(self) -> None:
        if not hasattr(self, "audit_box"):
            return
        self.audit_box.configure(state="normal")
        self.audit_box.delete("1.0", "end")
        self.audit_box.insert("end", backend.tail_audit())
        self.audit_box.configure(state="disabled")
        self.audit_box.see("end")

    def refresh_diagnostics(self) -> None:
        if not hasattr(self, "diagnostics_box"):
            return
        self.diagnostics_box.configure(state="normal")
        self.diagnostics_box.delete("1.0", "end")
        self.diagnostics_box.insert("end", backend.tail_runtime_diagnostics())
        self.diagnostics_box.configure(state="disabled")
        self.diagnostics_box.see("end")

    # ---------- setup ----------
    def _page_setup(self) -> None:
        page = self._scroll_page()
        deps = self._card(page, "Installation & dependencies", "PortaMCP uses an isolated .venv. Repair/update refreshes the environment and Playwright Chromium directly from the app.")
        deps.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        d = ctk.CTkFrame(deps, fg_color="transparent")
        d.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 18))
        d.grid_columnconfigure(0, weight=1)
        self.dependency_status_label = ctk.CTkLabel(
            d,
            text="Checking dependencies...",
            text_color=MUTED,
            font=ctk.CTkFont(self.ui_font, 12, "bold"),
        )
        self.dependency_status_label.grid(row=0, column=0, sticky="w")
        self._button(d, "Repair / update dependencies", self.run_install, "secondary", 185).grid(row=0, column=1, sticky="e")
        self.install_progress = ctk.CTkProgressBar(d, mode="determinate", progress_color=ACCENT, fg_color=CONTROL)
        self.install_progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self.install_progress.set(0)
        self.install_log = ctk.CTkTextbox(d, height=150, fg_color="#060F1C", border_width=0, text_color="#B3C7DF", font=ctk.CTkFont(self.mono_font, 11))
        self.install_log.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.install_log.insert("end", "Ready.\n")

        browser = self._card(page, "Chrome bridge", "The optional unpacked extension lets PortaMCP inspect and control tabs in the user's existing Chrome profile through Chrome's debugger API.")
        browser.grid(row=1, column=0, sticky="ew", pady=(0, 16))
        b = ctk.CTkFrame(browser, fg_color="transparent")
        b.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 18))
        status = backend.chrome_extension_status()
        if status["manifest_exists"] and status["bridge_config_exists"]:
            bridge_text, bridge_color = "Bridge runtime config ready", GOOD
        elif status["manifest_exists"]:
            bridge_text, bridge_color = "Extension ready | start a server once to generate bridge config", WARN
        else:
            bridge_text, bridge_color = "Chrome extension files are missing", DANGER
        ctk.CTkLabel(
            b,
            text=bridge_text,
            text_color=bridge_color,
            font=ctk.CTkFont(self.ui_font, 13, "bold"),
        ).pack(anchor="w", pady=(0, 12))
        self._button(b, "Open extension folder", lambda: backend.open_path(backend.CHROME_EXTENSION_DIR), "ghost", 145).pack(side="right")
        self._button(b, "Open chrome://extensions", lambda: backend.open_url("chrome://extensions/"), "secondary", 155).pack(side="right", padx=8)

        project = self._card(page, "Project & configuration", "Useful local paths. Secrets remain in .portamcp and should never be committed.")
        project.grid(row=2, column=0, sticky="ew")
        p = ctk.CTkFrame(project, fg_color="transparent")
        p.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 18))
        p.grid_columnconfigure(1, weight=1)
        rows = [
            ("Project", str(backend.PROJECT_ROOT)),
            ("Config", str(backend.CONFIG_PATH)),
            ("Runtime state", str(backend.RUNTIME_DIR)),
            ("Python", str(backend.VENV_PYTHON)),
        ]
        for idx, (label, value) in enumerate(rows):
            ctk.CTkLabel(p, text=label, text_color=MUTED, width=110, anchor="w", font=ctk.CTkFont(self.ui_font, 12, "bold")).grid(row=idx, column=0, sticky="w", pady=4)
            ctk.CTkLabel(
                p,
                text=value,
                text_color=TEXT,
                anchor="w",
                justify="left",
                wraplength=430,
                font=ctk.CTkFont(self.mono_font, 11),
            ).grid(row=idx, column=1, sticky="ew", pady=4)
        self._button(p, "Open project", lambda: backend.open_path(backend.PROJECT_ROOT), "ghost", 106).grid(row=0, column=2, rowspan=2, padx=(12, 0))
        self._button(p, "Open config", lambda: backend.open_path(backend.CONFIG_PATH), "ghost", 106).grid(row=2, column=2, rowspan=2, padx=(12, 0))

    def _refresh_dependency_status(self, force: bool = False) -> None:
        if not hasattr(self, "dependency_status_label"):
            return
        now = time.monotonic()
        if (
            not force
            and self._dependency_installed is not None
            and now - self._dependency_check_last < 60.0
        ):
            self._apply_dependency_status(self._dependency_installed)
            return
        if self._dependency_check_running:
            return

        self._dependency_check_running = True
        self.dependency_status_label.configure(
            text="Checking dependencies...",
            text_color=MUTED,
        )

        def worker() -> None:
            installed = backend.installed()
            self.after(
                0,
                lambda value=installed: self._dependency_check_finished(value),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _dependency_check_finished(self, installed: bool) -> None:
        self._dependency_check_running = False
        self._dependency_installed = bool(installed)
        self._dependency_check_last = time.monotonic()
        self._apply_dependency_status(bool(installed))

    def _apply_dependency_status(self, installed: bool) -> None:
        if not hasattr(self, "dependency_status_label"):
            return
        self.dependency_status_label.configure(
            text="Dependencies ready" if installed else "Installation incomplete",
            text_color=GOOD if installed else WARN,
        )

    # ---------- lifecycle ----------
    def start_server(self) -> None:
        try:
            self.config_data = backend.read_config()
            existing = backend.server_processes()
            if existing:
                pids = ", ".join(str(x["pid"]) for x in existing)
                messagebox.showinfo("PortaMCP already running", f"A PortaMCP server process is already running (PID {pids}). Use Restart if you want to relaunch it with the selected profile.")
                return
            port = int(self.config_data.get("port", 8765))
            if backend.port_open("127.0.0.1", port):
                raise RuntimeError(f"Port {port} is already in use by another process.")
            cmd, env = backend.build_server_command(self.profile, self.config_data)
            self._append_log(f"\n$ {' '.join(cmd)}\n")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.server_process = subprocess.Popen(
                cmd,
                cwd=str(backend.PROJECT_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=flags,
            )
            threading.Thread(target=self._read_process_output, args=(self.server_process, "server"), daemon=True).start()
            self._mark_dirty(False)
            self._schedule_runtime_refresh(700)
        except Exception as exc:
            messagebox.showerror("Start PortaMCP", str(exc))

    def stop_server(self, ask: bool = True) -> None:
        processes = backend.server_processes()
        if ask and processes and not messagebox.askyesno("Stop PortaMCP", f"Stop {len(processes)} PortaMCP server process(es)?"):
            return
        killed = backend.stop_server_processes()
        if self.server_process is not None:
            try:
                if self.server_process.poll() is None:
                    try:
                        self.server_process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        self.server_process.terminate()
            except Exception:
                pass
            self.server_process = None
        self._append_log(f"Stopped PortaMCP process(es): {killed or 'none found'}\n")
        self._schedule_runtime_refresh(400)

    def restart_server(self) -> None:
        processes = backend.server_processes()
        if processes and not messagebox.askyesno("Restart PortaMCP", "Restart the running server with the currently selected profile and saved configuration?"):
            return
        self.stop_server(ask=False)
        self.after(600, self.start_server)

    def recover_public_connection(self) -> None:
        if getattr(self, "_public_recovery_running", False):
            self.toast("Public connection recovery is already running.")
            return
        try:
            cfg = backend.read_config()
            if not backend.is_tailscale_public_config(cfg):
                messagebox.showinfo(
                    "Recover public connection",
                    "The configured public endpoint is not this machine's Tailscale Funnel URL. "
                    "Use your tunnel provider's own restart controls instead.",
                )
                return
        except Exception as exc:
            messagebox.showerror("Recover public connection", str(exc))
            return

        if not messagebox.askyesno(
            "Recover public connection",
            "Reset and reopen the Tailscale Funnel, then restart PortaMCP?\n\n"
            "OAuth clients, tokens, and owner password will be preserved.",
        ):
            return

        self._public_recovery_running = True
        self._append_log("\nRecovering public connection: Funnel reset -> Funnel reopen -> server restart...\n")
        self.toast("Recovering public connection...")

        def worker() -> None:
            try:
                result = backend.recover_tailscale_funnel(cfg)
                killed = backend.stop_server_processes()
                self.log_queue.put(
                    f"[recovery] Funnel active on port {result['port']}; stopped server process(es): "
                    f"{killed or 'none found'}\n"
                )
                self.after(0, self._finish_public_recovery)
            except backend.TailscaleOperatorRequired as exc:
                message = str(exc)
                self.after(
                    0,
                    lambda value=message, config=cfg: self._offer_tailscale_operator_setup(
                        config, value
                    ),
                )
            except Exception as exc:
                message = str(exc)
                self.after(0, lambda value=message: self._public_recovery_failed(value))

        threading.Thread(target=worker, daemon=True).start()

    def _offer_tailscale_operator_setup(
        self, cfg: dict[str, Any], original_message: str
    ) -> None:
        command = backend.tailscale_operator_setup_command()
        if not messagebox.askyesno(
            "Tailscale permission required",
            "Linux requires one-time permission for your user to manage Tailscale "
            "Serve/Funnel settings.\n\n"
            "Grant that permission now? A system authentication dialog may appear.\n\n"
            f"Equivalent terminal command:\n{command}",
        ):
            self._public_recovery_running = False
            self._append_log(
                "[recovery] Linux Tailscale operator permission is required.\n"
            )
            messagebox.showinfo(
                "Tailscale permission required",
                "Run this once in a terminal, then retry Recover public:\n\n"
                + command,
            )
            return

        self._append_log(
            "[recovery] Requesting one-time Linux Tailscale operator permission...\n"
        )
        self.toast("Waiting for system authorization...")

        def worker() -> None:
            try:
                operator = backend.enable_tailscale_operator()
                self.log_queue.put(
                    f"[recovery] Tailscale operator enabled for {operator['operator']}.\n"
                )
                result = backend.recover_tailscale_funnel(cfg)
                killed = backend.stop_server_processes()
                self.log_queue.put(
                    f"[recovery] Funnel active on port {result['port']}; stopped server process(es): "
                    f"{killed or 'none found'}\n"
                )
                self.after(0, self._finish_public_recovery)
            except Exception as exc:
                message = str(exc)
                self.after(0, lambda value=message: self._public_recovery_failed(value))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_public_recovery(self) -> None:
        self.server_process = None
        self._public_recovery_running = False
        self.start_server()
        self.toast("Public connection recovered. Reconnect the MCP client if needed.")

    def _public_recovery_failed(self, message: str) -> None:
        self._public_recovery_running = False
        self._append_log(f"[recovery] FAILED: {message}\n")
        messagebox.showerror("Recover public connection", message)

    def _read_process_output(self, proc: subprocess.Popen[str], source: str) -> None:
        if proc.stdout is None:
            return
        for line in iter(proc.stdout.readline, ""):
            self.log_queue.put(f"[{source}] {line}")
        code = proc.wait()
        self.log_queue.put(f"[{source}] process exited with code {code}\n")

    def _poll_logs(self) -> None:
        try:
            while True:
                line = self.log_queue.get_nowait()
                self._append_log(line)
                if self.current_page == "setup" and hasattr(self, "install_log") and line.startswith("[install]"):
                    self.install_log.insert("end", line)
                    self.install_log.see("end")
        except queue.Empty:
            pass
        self.after(250, self._poll_logs)

    def _append_log(self, text: str) -> None:
        self._server_log_buffer.append(text)
        if hasattr(self, "server_log_box") and self.current_page == "activity":
            self.server_log_box.configure(state="normal")
            self.server_log_box.insert("end", text)
            self.server_log_box.see("end")
            self.server_log_box.configure(state="disabled")

    def run_install(self) -> None:
        if self.install_process is not None and self.install_process.poll() is None:
            messagebox.showinfo("Installer", "Installation is already running.")
            return
        if backend.server_processes():
            if not messagebox.askyesno("Update dependencies", "A PortaMCP server is running. Stop it before updating the editable environment?"):
                return
            self.stop_server(ask=False)
        try:
            if hasattr(self, "install_progress"):
                self.install_progress.configure(mode="indeterminate")
                self.install_progress.start()
            cmd = backend.install_command()
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.install_process = subprocess.Popen(
                cmd,
                cwd=str(backend.PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=flags,
            )
            threading.Thread(target=self._watch_install, daemon=True).start()
        except Exception as exc:
            if hasattr(self, "install_progress"):
                self.install_progress.stop()
            messagebox.showerror("Installer", str(exc))

    def _watch_install(self) -> None:
        proc = self.install_process
        if proc is None or proc.stdout is None:
            return
        for line in iter(proc.stdout.readline, ""):
            self.log_queue.put(f"[install] {line}")
        code = proc.wait()
        self.log_queue.put(f"[install] completed with code {code}\n")
        self.after(0, lambda: self._install_finished(code))

    def _install_finished(self, code: int) -> None:
        if hasattr(self, "install_progress"):
            self.install_progress.stop()
            self.install_progress.configure(mode="determinate")
            self.install_progress.set(1 if code == 0 else 0)
        if code == 0:
            self.ui_state["first_run_complete"] = True
            backend.write_ui_state(self.ui_state)
            self._refresh_dependency_status(force=True)
            self.toast("Dependencies are ready.")
        else:
            self._refresh_dependency_status(force=True)
            messagebox.showerror("Installer", f"Installation exited with code {code}. Check the setup log.")

    # ---------- emergency ----------
    def activate_emergency(self) -> None:
        if backend.emergency_active():
            self.toast("Emergency deny is already active.")
            return
        backend.set_emergency(True)
        self._schedule_runtime_refresh(0)
        self.toast("Emergency deny activated. Policy-protected actions are blocked.")

    def toggle_emergency(self) -> None:
        if backend.emergency_active():
            if not messagebox.askyesno("Resume PortaMCP", "Clear emergency deny and allow policy-permitted actions again?"):
                return
            backend.set_emergency(False)
            self.toast("Emergency deny cleared.")
        else:
            backend.set_emergency(True)
            self.toast("Emergency deny activated.")
        self._schedule_runtime_refresh(0)

    # ---------- runtime state ----------
    def _schedule_runtime_refresh(self, delay_ms: int = 1500) -> None:
        if self._runtime_refresh_job is not None:
            try:
                self.after_cancel(self._runtime_refresh_job)
            except Exception:
                pass
        self._runtime_refresh_job = self.after(delay_ms, self._refresh_runtime_state)

    def _refresh_runtime_state(self) -> None:
        self._runtime_refresh_job = None
        try:
            processes = backend.server_processes()
            running = bool(processes)
            port = int(self.config_data.get("port", 8765))
            healthy = backend.server_health(port, timeout=0.25) if running else False
            emergency = backend.emergency_active()
            if running and healthy:
                self.status_pill.configure(
                    text="  SYSTEM HEALTHY  ",
                    fg_color="#092B32",
                    text_color="#5EEAC5",
                    border_color="#15504D",
                )
            elif running:
                self.status_pill.configure(
                    text="  SYSTEM DEGRADED  ",
                    fg_color="#241C0B",
                    text_color="#F6C96C",
                    border_color="#5F4A1F",
                )
            else:
                self.status_pill.configure(
                    text="  SYSTEM OFFLINE  ",
                    fg_color=CARD_ALT,
                    text_color=MUTED,
                    border_color=BORDER,
                )

            if emergency:
                self.sidebar_emergency_btn.configure(
                    text="Resume from deny",
                    fg_color="#2A1B0D",
                    hover_color="#3A260F",
                    text_color="#FFD37A",
                    border_color="#765523",
                )
            else:
                self.sidebar_emergency_btn.configure(
                    text="Emergency Deny",
                    fg_color=BUTTON_STYLES["danger"][0],
                    hover_color=BUTTON_STYLES["danger"][1],
                    text_color=BUTTON_STYLES["danger"][2],
                    border_color=BUTTON_STYLES["danger"][3],
                )

            if self.current_page == "dashboard":
                enabled_count = sum(
                    1
                    for key in backend.FEATURE_KEYS
                    if backend.effective_feature_enabled(self.config_data, key)
                )
                scope_count = len(self.config_data.get("allowed_roots", []))

                if hasattr(self, "metric_server"):
                    server_text = (
                        "Running"
                        if running and healthy
                        else ("Degraded" if running else "Offline")
                    )
                    self.metric_server.configure(text=server_text, text_color=TEXT)
                    if hasattr(self, "metric_server_dot"):
                        self.metric_server_dot.configure(
                            fg_color=(
                                GOOD
                                if running and healthy
                                else (WARN if running else "#526C8A")
                            )
                        )
                    if hasattr(self, "metric_server_detail"):
                        if running:
                            pid_text = ", ".join(str(x["pid"]) for x in processes)
                            self.metric_server_detail.configure(
                                text=f"Port {port}  |  PID {pid_text}"
                            )
                        else:
                            self.metric_server_detail.configure(text=f"Port {port}")

                if hasattr(self, "metric_auth"):
                    self.metric_auth.configure(
                        text=backend.PROFILE_LABELS[self.profile],
                        text_color=TEXT,
                    )
                    if hasattr(self, "metric_auth_dot"):
                        self.metric_auth_dot.configure(fg_color=GOOD)
                    if hasattr(self, "metric_auth_detail"):
                        self.metric_auth_detail.configure(
                            text="Client authentication profile"
                        )

                if hasattr(self, "metric_security"):
                    self.metric_security.configure(
                        text=backend.PRESET_LABELS.get(self.preset, "Custom"),
                        text_color=TEXT,
                    )
                    if hasattr(self, "metric_security_dot"):
                        self.metric_security_dot.configure(fg_color=GOOD)
                    if hasattr(self, "metric_security_detail"):
                        self.metric_security_detail.configure(
                            text=f"{enabled_count} capabilities enabled"
                        )

                if hasattr(self, "metric_emergency"):
                    self.metric_emergency.configure(
                        text="DENY ACTIVE" if emergency else "Normal",
                        text_color="#FF8B96" if emergency else TEXT,
                    )
                    if hasattr(self, "metric_emergency_dot"):
                        self.metric_emergency_dot.configure(
                            fg_color=DANGER if emergency else GOOD
                        )
                    if hasattr(self, "metric_emergency_detail"):
                        self.metric_emergency_detail.configure(
                            text=(
                                "Restrictions active"
                                if emergency
                                else "No active restrictions"
                            )
                        )

                if hasattr(self, "runtime_profile_label"):
                    self.runtime_profile_label.configure(
                        text=backend.PROFILE_LABELS[self.profile]
                    )

                if hasattr(self, "_summary_value_labels"):
                    values = {
                        "PROFILE": backend.PROFILE_LABELS[self.profile],
                        "MCP ENDPOINT": backend.endpoint_for(
                            self.profile, self.config_data
                        ),
                        "PUBLIC BASE": (
                            self.config_data.get("public_base_url")
                            or "Not configured"
                        ),
                        "FILESYSTEM SCOPES": (
                            f"{scope_count} granted"
                            if scope_count
                            else "None granted"
                        ),
                    }
                    for key, value in values.items():
                        label = self._summary_value_labels.get(key)
                        if label is not None:
                            label.configure(text=str(value))

                if hasattr(self, "policy_count_label"):
                    self.policy_count_label.configure(
                        text=f"{enabled_count} / {len(backend.FEATURE_KEYS)} enabled"
                    )

                if hasattr(self, "_policy_widgets"):
                    for key, (dot, state) in self._policy_widgets.items():
                        supported = backend.feature_supported(key)
                        enabled = backend.effective_feature_enabled(
                            self.config_data, key
                        )
                        dot.configure(
                            fg_color=(
                                ACCENT
                                if enabled
                                else ("#526C8A" if supported else "#33485F")
                            )
                        )
                        state.configure(
                            text="ON" if enabled else ("OFF" if supported else "N/A"),
                            text_color=(
                                ACCENT
                                if enabled
                                else MUTED
                            ),
                        )
        except Exception:
            pass
        self._schedule_runtime_refresh()

    # ---------- misc ----------
    def copy_text(self, text: str, notice: str = "Copied") -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()
        self.toast(notice)

    def copy_endpoint(self) -> None:
        self.copy_text(backend.endpoint_for(self.profile, self.config_data), "MCP endpoint copied")

    def _mark_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        self.dirty_badge.configure(text="Restart required to apply saved changes" if dirty else "")
        if dirty:
            self.dirty_badge.grid()
        else:
            self.dirty_badge.grid_remove()

    def toast(self, text: str) -> None:
        # Lightweight in-app toast, no modal interruption.
        if hasattr(self, "_toast") and self._toast.winfo_exists():
            self._toast.destroy()
        toast = ctk.CTkLabel(
            self,
            text=text,
            fg_color="#163251",
            text_color=TEXT,
            corner_radius=9,
            padx=14,
            pady=8,
            font=ctk.CTkFont(self.ui_font, 12),
        )
        self._toast = toast
        toast.place(relx=0.98, rely=0.96, anchor="se")
        self.after(3000, lambda widget=toast: widget.destroy() if widget.winfo_exists() else None)

    def _on_close(self) -> None:
        # Closing the control center does not implicitly kill a server the user intentionally started.
        if self.install_process is not None and self.install_process.poll() is None:
            if not messagebox.askyesno("Installation running", "Dependency installation is still running. Close the control center anyway?"):
                return
        self.destroy()


def main() -> None:
    app = PortaMCPApp()
    app.mainloop()


if __name__ == "__main__":
    main()
