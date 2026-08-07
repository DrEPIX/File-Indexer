"""Onboarding and searchable in-app help for File Indexer V1."""

from __future__ import annotations

from dataclasses import dataclass
from tkinter import BOTH, END, LEFT, StringVar, Text, Toplevel, ttk
from typing import Any

from .appearance import GradientHeader, PlaceholderEntry, Theme


@dataclass(frozen=True, slots=True)
class TutorialPage:
    eyebrow: str
    title: str
    body: str
    tips: tuple[str, ...]


PAGES = (
    TutorialPage(
        "WELCOME",
        "Your files, finally findable",
        "File Indexer builds a private, searchable catalog of your images, videos, audio, and documents. Your originals remain untouched.",
        ("Everything stays local by default", "Duplicate bytes become one asset", "Close anytime—scans resume safely"),
    ),
    TutorialPage(
        "STEP 1",
        "Choose what belongs",
        "Add one or more library folders. V1 walks subfolders, extracts metadata, and stores thumbnails in a separate cache.",
        ("Use Add & Scan Folder in Library", "Manage permanent roots in Settings", "Cancel without corrupting the index"),
    ),
    TutorialPage(
        "STEP 2",
        "Search like you remember it",
        "Start with ordinary words, then narrow with readable filters. Search looks across names, document text, tags, places, and analyzer labels.",
        ("type:image after:2024", "camera:Canon has:gps", "user.label:favorite"),
    ),
    TutorialPage(
        "STEP 3",
        "Make V1 yours",
        "Choose a theme, text size, density, and motion preference. Human labels always outrank machine suggestions.",
        ("Open Style in the header", "Use Analyzers only when you want them", "Reopen this tour from Help anytime"),
    ),
)


HELP_TOPICS: dict[str, tuple[str, str]] = {
    "Getting started": (
        "Add your first folder",
        "Open Library and choose Add & Scan Folder. V1 remembers the folder and starts indexing in the background. The status bar shows the active stage and Cancel stops at a safe boundary.",
    ),
    "Search basics": (
        "Find by words and filters",
        "Type ordinary words to search names, labels, tags, and extracted document text. Add filters such as type:image, after:2024, before:2026, camera:Canon, ext:png, or has:gps.",
    ),
    "Analyzer labels": (
        "Search anything a plugin emits",
        "Analyzer namespaces become filters automatically. If an analyzer emits color.dominant=blue, search color.dominant:blue. The core never hardcodes a taxonomy.",
    ),
    "Privacy and originals": (
        "Local and read-only by design",
        "Original files are never modified. Thumbnails and other derivatives live in the configured preview cache. Network access is denied unless an analyzer declares it and configuration explicitly allows it.",
    ),
    "Duplicates and moves": (
        "Content identity, not path identity",
        "Two byte-identical files are one asset with multiple file paths. Renaming or moving a file does not create a new asset after the next scan.",
    ),
    "Keyboard and navigation": (
        "Move quickly",
        "Press Ctrl+K to focus search, Ctrl+L for Library, Ctrl+, for Settings, F1 for Help, and Escape to clear the active search. Double-click a result to open it.",
    ),
}


class TutorialDialog(Toplevel):
    """Four-step, keyboard-friendly onboarding tour."""

    def __init__(self, parent: Any, theme: Theme, scale: float, on_finish: Any) -> None:
        super().__init__(parent)
        self.theme = theme
        self.scale = scale
        self.on_finish = on_finish
        self.index = 0
        self.title("Welcome to File Indexer V1")
        self.geometry(f"{round(720 * scale)}x{round(520 * scale)}")
        self.minsize(620, 450)
        self.configure(background=theme.bg)
        self.transient(parent)
        self.grab_set()

        self.header = GradientHeader(self, theme, scale=scale, reduced_motion=True)
        self.header.configure(height=round(92 * scale))
        self.header.pack(fill="x")
        self.header.transition("Quick tour")

        self.body = ttk.Frame(self, padding=(round(42 * scale), round(26 * scale)))
        self.body.pack(fill=BOTH, expand=True)
        self.eyebrow = ttk.Label(self.body, style="Eyebrow.TLabel")
        self.eyebrow.pack(anchor="w")
        self.heading = ttk.Label(self.body, style="TutorialTitle.TLabel", wraplength=600)
        self.heading.pack(anchor="w", pady=(8, 12))
        self.copy = ttk.Label(self.body, style="Body.TLabel", wraplength=610, justify="left")
        self.copy.pack(anchor="w")
        self.tips = ttk.Frame(self.body, style="Raised.TFrame", padding=16)
        self.tips.pack(fill="x", pady=22)

        footer = ttk.Frame(self, padding=(round(34 * scale), 0, round(34 * scale), round(26 * scale)))
        footer.pack(fill="x")
        self.progress = ttk.Label(footer, style="Muted.TLabel")
        self.progress.pack(side=LEFT)
        self.skip = ttk.Button(footer, text="Skip tour", command=self.finish)
        self.skip.pack(side="right")
        self.next = ttk.Button(footer, text="Continue", style="Accent.TButton", command=self.advance)
        self.next.pack(side="right", padx=8)
        self.back = ttk.Button(footer, text="Back", command=self.go_back)
        self.back.pack(side="right")
        self.bind("<Escape>", lambda _: self.finish())
        self.bind("<Right>", lambda _: self.advance())
        self.bind("<Left>", lambda _: self.go_back())
        self.protocol("WM_DELETE_WINDOW", self.finish)
        self.render()
        self.after(80, self._center)

    def _center(self) -> None:
        self.update_idletasks()
        parent = self.master
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_width()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_height()) // 2)
        self.geometry(f"+{x}+{y}")

    def render(self) -> None:
        page = PAGES[self.index]
        self.eyebrow.configure(text=page.eyebrow)
        self.heading.configure(text=page.title)
        self.copy.configure(text=page.body)
        for child in self.tips.winfo_children():
            child.destroy()
        for tip in page.tips:
            ttk.Label(self.tips, text=f"  •  {tip}", style="Raised.TLabel").pack(anchor="w", pady=3)
        dots = "  ".join("●" if index == self.index else "○" for index in range(len(PAGES)))
        self.progress.configure(text=dots)
        self.back.configure(state="normal" if self.index else "disabled")
        self.next.configure(text="Start exploring" if self.index == len(PAGES) - 1 else "Continue")

    def advance(self) -> None:
        if self.index == len(PAGES) - 1:
            self.finish()
            return
        self.index += 1
        self.render()

    def go_back(self) -> None:
        if self.index:
            self.index -= 1
            self.render()

    def finish(self) -> None:
        self.grab_release()
        self.destroy()
        self.on_finish()


class HelpCenterDialog(Toplevel):
    """Searchable help center so query syntax is available at point of use."""

    def __init__(self, parent: Any, theme: Theme, scale: float, on_tour: Any) -> None:
        super().__init__(parent)
        self.theme = theme
        self.scale = scale
        self.on_tour = on_tour
        self.query = StringVar()
        self.title("File Indexer Help")
        self.geometry(f"{round(820 * scale)}x{round(590 * scale)}")
        self.minsize(680, 480)
        self.configure(background=theme.bg)
        self.transient(parent)

        frame = ttk.Frame(self, padding=24)
        frame.pack(fill=BOTH, expand=True)
        ttk.Label(frame, text="Help center", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame, text="Search workflows, privacy, and query syntax.", style="Muted.TLabel").pack(anchor="w", pady=(2, 14))
        search = PlaceholderEntry(frame, self.query, "Search help…")
        search.pack(fill="x")
        search.bind("<KeyRelease>", lambda _: self.refresh())

        content = ttk.Panedwindow(frame, orient="horizontal")
        content.pack(fill=BOTH, expand=True, pady=14)
        left = ttk.Frame(content, style="Panel.TFrame", padding=8)
        right = ttk.Frame(content, style="Panel.TFrame", padding=18)
        content.add(left, weight=2)
        content.add(right, weight=5)
        self.topics = ttk.Treeview(left, show="tree", selectmode="browse")
        self.topics.pack(fill=BOTH, expand=True)
        self.topics.bind("<<TreeviewSelect>>", lambda _: self.show_selected())
        self.article_title = ttk.Label(right, style="Subtitle.TLabel", wraplength=500)
        self.article_title.pack(anchor="w")
        self.article = ttk.Label(right, style="Body.TLabel", wraplength=500, justify="left")
        self.article.pack(anchor="w", pady=(10, 0))

        footer = ttk.Frame(frame)
        footer.pack(fill="x")
        ttk.Button(footer, text="Replay quick tour", command=self._replay).pack(side=LEFT)
        ttk.Button(footer, text="Close", style="Accent.TButton", command=self.destroy).pack(side="right")
        self.bind("<Escape>", lambda _: self.destroy())
        self.refresh()

    def refresh(self) -> None:
        raw = "" if self.query.get() == "Search help…" else self.query.get().lower().strip()
        self.topics.delete(*self.topics.get_children())
        for key, (title, body) in HELP_TOPICS.items():
            if not raw or raw in key.lower() or raw in title.lower() or raw in body.lower():
                self.topics.insert("", END, iid=key, text=key)
        children = self.topics.get_children()
        if children:
            self.topics.selection_set(children[0])
            self.show_selected()
        else:
            self.article_title.configure(text="No help topics found")
            self.article.configure(text="Try a broader word such as search, privacy, folder, or duplicate.")

    def show_selected(self) -> None:
        selection = self.topics.selection()
        if not selection:
            return
        title, body = HELP_TOPICS[selection[0]]
        self.article_title.configure(text=title)
        self.article.configure(text=body)

    def _replay(self) -> None:
        self.destroy()
        self.on_tour()
