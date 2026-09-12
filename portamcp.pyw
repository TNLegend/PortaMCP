from __future__ import annotations

import queue
import subprocess
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import messagebox
from tkinter.scrolledtext import ScrolledText

from porta_mcp.platform_support import (
    creation_flags_no_window,
    is_linux,
    is_windows,
    project_venv_dir,
    system_chromium_executable,
    venv_gui_python,
    venv_python,
)

ROOT = Path(__file__).resolve().parent
VENV_DIR = project_venv_dir(ROOT)
VENV_PYTHON = venv_python(VENV_DIR)
VENV_PYTHONW = venv_gui_python(VENV_DIR)
ICON = ROOT / "assets" / "portamcp.png"

BG = "#0B0F16"
CARD = "#111925"
BORDER = "#223044"
TEXT = "#F4F7FB"
MUTED = "#8FA0B7"
ACCENT = "#2DD4BF"


def _bootstrap_python() -> str:
    current = Path(sys.executable)
    if is_windows() and current.name.lower() == "pythonw.exe":
        console_python = current.with_name("python.exe")
        if console_python.exists():
            return str(console_python)
    return str(current)


def environment_ready() -> bool:
    if not VENV_PYTHON.exists():
        return False
    try:
        if is_linux() and system_chromium_executable() is not None:
            check = "import mcp, customtkinter, playwright, websockets, PIL, porta_mcp"
        else:
            check = (
                "from pathlib import Path; import mcp, customtkinter, playwright, websockets, PIL, porta_mcp; "
                "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); "
                "ok=Path(p.chromium.executable_path).is_file(); p.stop(); raise SystemExit(0 if ok else 1)"
            )
        result = subprocess.run(
            [str(VENV_PYTHON), "-c", check],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            creationflags=creation_flags_no_window(),
        )
        return result.returncode == 0
    except Exception:
        return False


def launch_main() -> None:
    executable = VENV_PYTHONW if VENV_PYTHONW.exists() else VENV_PYTHON
    if not executable.exists():
        raise RuntimeError("The PortaMCP environment is not installed yet.")
    subprocess.Popen(
        [str(executable), "-m", "porta_mcp.control_center"],
        cwd=str(ROOT),
        creationflags=creation_flags_no_window(),
    )


class Bootstrap(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("PortaMCP Setup")
        self.geometry("700x470")
        self.resizable(False, False)
        self.configure(bg=BG)
        self.output: queue.Queue[str] = queue.Queue()
        self.proc: subprocess.Popen[str] | None = None
        self._icon = None
        if ICON.exists():
            try:
                self._icon = tk.PhotoImage(file=str(ICON))
                self.iconphoto(True, self._icon)
            except Exception:
                pass
        self._build()
        self.after(300, self.start_install)
        self.after(120, self.poll_output)

    def _build(self) -> None:
        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="both", expand=True, padx=34, pady=30)

        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x")
        mark = tk.Label(header, text="P", bg=ACCENT, fg="#041B17", font=("Segoe UI", 19, "bold"), width=2, height=1)
        mark.pack(side="left", padx=(0, 13))
        title_wrap = tk.Frame(header, bg=BG)
        title_wrap.pack(side="left")
        tk.Label(title_wrap, text="PortaMCP", bg=BG, fg=TEXT, font=("Segoe UI", 22, "bold")).pack(anchor="w")
        tk.Label(title_wrap, text="FIRST-RUN ENVIRONMENT SETUP", bg=BG, fg=MUTED, font=("Segoe UI", 8, "bold")).pack(anchor="w")

        card = tk.Frame(outer, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="both", expand=True, pady=(26, 0))
        body = tk.Frame(card, bg=CARD)
        body.pack(fill="both", expand=True, padx=22, pady=20)

        self.status = tk.Label(body, text="Preparing the isolated Python environment...", bg=CARD, fg=TEXT, font=("Segoe UI", 13, "bold"), anchor="w")
        self.status.pack(fill="x")
        tk.Label(
            body,
            text="PortaMCP will create .venv, install its dependencies, and install Playwright Chromium. No separate setup script is required.",
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 9),
            justify="left",
            wraplength=610,
            anchor="w",
        ).pack(fill="x", pady=(6, 14))

        self.canvas = tk.Canvas(body, height=7, bg="#182333", highlightthickness=0)
        self.canvas.pack(fill="x")
        self.bar = self.canvas.create_rectangle(0, 0, 90, 7, fill=ACCENT, outline="")
        self.phase = 0
        self.animate()

        self.log = ScrolledText(body, height=10, bg="#080C12", fg="#B8C8DA", insertbackground=TEXT, relief="flat", font=("Cascadia Mono", 8), wrap="word")
        self.log.pack(fill="both", expand=True, pady=(14, 0))
        self.log.insert("end", "Starting PortaMCP setup...\n")
        self.log.configure(state="disabled")

        footer = tk.Frame(outer, bg=BG)
        footer.pack(fill="x", pady=(12, 0))
        tk.Label(footer, text="Developed by TNLegend", bg=BG, fg="#66758A", font=("Segoe UI", 8)).pack(side="left")
        self.action = tk.Button(
            footer,
            text="Installing...",
            state="disabled",
            bg="#1A2937",
            fg="#AAB6C7",
            activebackground="#24364B",
            activeforeground=TEXT,
            relief="flat",
            padx=18,
            pady=7,
            font=("Segoe UI", 9, "bold"),
            command=self.finish_launch,
        )
        self.action.pack(side="right")

    def animate(self) -> None:
        if self.proc is not None and self.proc.poll() is not None:
            return
        width = max(self.canvas.winfo_width(), 600)
        segment = max(80, width // 5)
        x = (self.phase * 7) % (width + segment) - segment
        self.canvas.coords(self.bar, x, 0, x + segment, 7)
        self.phase += 1
        self.after(35, self.animate)

    def start_install(self) -> None:
        try:
            self.proc = subprocess.Popen(
                [_bootstrap_python(), "-u", "-m", "porta_mcp.installer"],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creation_flags_no_window(),
            )
            threading.Thread(target=self.reader, daemon=True).start()
        except Exception as exc:
            self.failed(str(exc))

    def reader(self) -> None:
        assert self.proc is not None
        if self.proc.stdout is not None:
            for line in iter(self.proc.stdout.readline, ""):
                self.output.put(line)
        code = self.proc.wait()
        self.output.put(f"\n[setup exited with code {code}]\n")
        self.after(0, lambda: self.completed(code))

    def poll_output(self) -> None:
        try:
            while True:
                line = self.output.get_nowait()
                self.log.configure(state="normal")
                self.log.insert("end", line)
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(120, self.poll_output)

    def completed(self, code: int) -> None:
        if code == 0 and environment_ready():
            self.status.configure(text="Environment ready. Launching PortaMCP...", fg="#8FF0B9")
            self.action.configure(text="Launch PortaMCP", state="normal", bg=ACCENT, fg="#041B17")
            self.after(900, self.finish_launch)
        else:
            self.failed(f"Installation did not complete successfully (exit code {code}). Review the log above.")

    def failed(self, text: str) -> None:
        self.status.configure(text="Setup needs attention", fg="#FFB6BA")
        self.log.configure(state="normal")
        self.log.insert("end", f"\nERROR: {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        self.action.configure(text="Retry", state="normal", bg="#5F2026", fg="#FFD9DC", command=self.retry)

    def retry(self) -> None:
        self.action.configure(text="Installing...", state="disabled", bg="#1A2937", fg="#AAB6C7")
        self.status.configure(text="Retrying environment setup...", fg=TEXT)
        self.start_install()
        self.animate()

    def finish_launch(self) -> None:
        try:
            launch_main()
            self.destroy()
        except Exception as exc:
            messagebox.showerror("PortaMCP", str(exc))


if __name__ == "__main__":
    if environment_ready():
        launch_main()
    else:
        Bootstrap().mainloop()
