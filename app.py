import asyncio
import json
import queue
import subprocess
import sys
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from tgsearch.backend import Backend
from tgsearch.engine import Contact, Evidence, SearchOptions
from tgsearch.extraction import PRESETS, ROLE_FILTERS
from tgsearch.history import ContactHistory
from tgsearch.storage import PRIVATE, RESULTS, export_csv, export_txt, load_config, prepare_storage

BG = "#f2f5f8"
INK = "#172d43"
MUTED = "#62768a"


def open_path(path):
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform == "win32":
        import os
        os.startfile(str(path))
    else:
        subprocess.Popen(["xdg-open", str(path)])


class Application(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Telegram Контакты")
        self.geometry(f"1200x{min(850, self.winfo_screenheight() - 90)}")
        self.minsize(1080, 740)
        self.configure(bg=BG)
        self.backend = Backend()
        self.config_data = load_config()
        self.history = ContactHistory(PRIVATE / "contacts.sqlite3", PRIVATE / "reports")
        self.authorized = False
        self.running = False
        self.auth_busy = False
        self.closing = False
        self.auth_stage = "phone"
        self.contacts = {}
        self.last_path = None
        self.close_started = False
        self.qr_window = None
        self.restored_results = False
        self.build_style()
        self.build_ui()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(100, self.poll)
        if self.config_data.get("api_id"):
            self.auth_state.set("Проверяю подключение к Telegram…")
            self.set_auth_busy(True)
            self.backend.submit(self.backend.restore(), "restored")

    def build_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        self.option_add("*Font", ("Helvetica Neue", 12))
        style.configure(".", font=("Helvetica Neue", 12), background=BG, foreground=INK)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG)
        style.configure("Hint.TLabel", foreground=MUTED, font=("Helvetica Neue", 10))
        style.configure("TButton", padding=(10, 7))
        style.configure("Primary.TButton", background="#2279ad", foreground="white", padding=(16, 9))
        style.map("Primary.TButton", background=[("disabled", "#a6b7c5"), ("active", "#17628f")])
        style.configure("TEntry", padding=5, fieldbackground="white")
        style.configure("TCombobox", padding=5, fieldbackground="white")
        style.configure("Treeview", rowheight=29, background="white", fieldbackground="white", font=("Helvetica Neue", 11))
        style.configure("Treeview.Heading", font=("Helvetica Neue", 11, "bold"), padding=(6, 7))
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 9))

    def build_ui(self):
        header = tk.Frame(self, bg=INK, padx=24, pady=15)
        header.pack(fill="x")
        tk.Label(header, text="Telegram Контакты", bg=INK, fg="white", font=("Helvetica Neue", 23, "bold")).pack(anchor="w")
        tk.Label(header, text="Публичные контакты сообществ · фильтры по теме · один TXT со всеми никами", bg=INK, fg="#c1d5e3", font=("Helvetica Neue", 11)).pack(anchor="w", pady=(4, 0))
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=18, pady=(12, 5))
        self.search_tab = ttk.Frame(self.tabs, padding=(12, 12))
        self.login_tab = ttk.Frame(self.tabs, padding=24)
        self.tabs.add(self.search_tab, text="Поиск контактов")
        self.tabs.add(self.login_tab, text="Вход в Telegram")
        self.build_login()
        self.build_search()
        ttk.Label(self, text="Ники проверяются на существование. Роли из текста — подсказка; скрытые владельцы и полный охват Telegram недоступны.", style="Hint.TLabel").pack(anchor="w", padx=24, pady=(4, 10))
        self.tabs.select(self.login_tab)

    def build_login(self):
        panel = ttk.Frame(self.login_tab)
        panel.pack(anchor="n", fill="x")
        ttk.Label(panel, text="Подключите свой Telegram", font=("Helvetica Neue", 21, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(panel, text="Код и пароль вводятся только в этом локальном окне. После входа сессия сохраняется на компьютере.", wraplength=900).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 20))
        self.phone = tk.StringVar(value=self.config_data.get("phone", ""))
        self.code = tk.StringVar()
        self.password = tk.StringVar()
        self.api_id = tk.StringVar(value=str(self.config_data.get("api_id", "")))
        self.api_hash = tk.StringVar(value=self.config_data.get("api_hash", ""))
        self.auth_state = tk.StringVar(value="Введите номер телефона и запросите код входа.")
        self.phone_entry = self.login_entry(panel, 2, "Номер телефона", self.phone)
        self.request_button = ttk.Button(panel, text="1. Получить код", command=self.request_code, style="Primary.TButton")
        self.request_button.grid(row=3, column=1, sticky="w", pady=(3, 15))
        self.qr_button = ttk.Button(panel, text="Войти по QR", command=self.start_qr_login)
        self.qr_button.grid(row=3, column=2, sticky="w", padx=(16, 0), pady=(3, 15))
        self.code_entry = self.login_entry(panel, 4, "Код из Telegram", self.code)
        self.code_button = ttk.Button(panel, text="2. Подтвердить код", command=self.sign_in)
        self.code_button.grid(row=5, column=1, sticky="w", pady=(3, 15))
        self.password_entry = self.login_entry(panel, 6, "Пароль 2FA (если включён)", self.password, show="•")
        self.password_button = ttk.Button(panel, text="3. Подтвердить пароль", command=self.password_sign_in)
        self.password_button.grid(row=7, column=1, sticky="w", pady=(3, 12))
        ttk.Label(panel, textvariable=self.auth_state, wraplength=900, foreground="#186e8c").grid(row=8, column=0, columnspan=2, sticky="w", pady=(8, 18))
        details = ttk.LabelFrame(panel, text="API-настройки — сохранены только на этом компьютере", padding=12)
        details.grid(row=9, column=0, columnspan=2, sticky="ew")
        ttk.Label(details, text="API ID").grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Entry(details, textvariable=self.api_id, width=14).grid(row=0, column=1, sticky="w", padx=(0, 25))
        ttk.Label(details, text="API Hash").grid(row=0, column=2, sticky="w", padx=(0, 12))
        ttk.Entry(details, textvariable=self.api_hash, width=40, show="•").grid(row=0, column=3, sticky="w")
        self.logout_button = ttk.Button(panel, text="Выйти из аккаунта на этом компьютере", command=self.logout)
        self.logout_button.grid(row=10, column=0, columnspan=2, sticky="w", pady=15)
        self.set_auth_busy(False)

    def login_entry(self, parent, row, label, variable, **kwargs):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 20), pady=5)
        entry = ttk.Entry(parent, textvariable=variable, width=38, **kwargs)
        entry.grid(row=row, column=1, sticky="w", pady=5)
        return entry

    def build_search(self):
        self.search_tab.columnconfigure(1, weight=1)
        self.search_tab.rowconfigure(0, weight=1)
        left_panel = ttk.Frame(self.search_tab, width=355)
        left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 20))
        left_panel.pack_propagate(False)
        scroller = ttk.Frame(left_panel)
        scroller.pack(fill="both", expand=True)
        canvas = tk.Canvas(scroller, bg=BG, highlightthickness=0, width=335)
        filter_scroll = ttk.Scrollbar(scroller, orient="vertical", command=canvas.yview)
        filter_scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=filter_scroll.set)
        left = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=left, anchor="nw")
        left.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        right = ttk.Frame(self.search_tab)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        self.topic = tk.StringVar(value="Строители")
        self.city = tk.StringVar()
        self.roles = tk.StringVar(value=next(iter(ROLE_FILTERS)))
        self.chat_type = tk.StringVar(value="Все")
        self.max_chats = tk.StringVar(value="50")
        self.posts = tk.StringVar(value="30")
        self.min_members = tk.StringVar(value="0")
        self.admins = tk.BooleanVar(value=True)
        self.bots = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Для поиска войдите в Telegram")
        self.stats = tk.StringVar(value=f"Новых: 0 · Найдено: 0 · В базе: {self.history.total()}")
        self.filter_widgets = []
        ttk.Label(left, text="Настройки поиска", font=("Helvetica Neue", 16, "bold")).pack(anchor="w", pady=(0, 10))
        self.field_label(left, "Тема")
        combo = self.combo(left, self.topic, list(PRESETS))
        combo.bind("<<ComboboxSelected>>", self.change_topic)
        self.field_label(left, "Поисковые слова через запятую")
        self.keywords = self.text_input(left, 3)
        self.keywords.insert("1.0", PRESETS[self.topic.get()])
        self.field_label(left, "Город / регион (необязательно)")
        entry = ttk.Entry(left, textvariable=self.city)
        entry.pack(fill="x")
        self.filter_widgets.append(entry)
        self.field_label(left, "Кого искать")
        self.combo(left, self.roles, list(ROLE_FILTERS))
        grid = ttk.Frame(left)
        grid.pack(fill="x", pady=(8, 0))
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        ttk.Label(grid, text="Сообщества").grid(row=0, column=0, sticky="w")
        ttk.Label(grid, text="Лимит сообществ").grid(row=0, column=1, sticky="w", padx=(10, 0))
        types_combo = ttk.Combobox(grid, textvariable=self.chat_type, values=["Все", "Группы", "Каналы"], state="readonly", width=12)
        types_combo.grid(row=1, column=0, sticky="ew", pady=3)
        limit = ttk.Spinbox(grid, from_=1, to=300, textvariable=self.max_chats, width=10)
        limit.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=3)
        ttk.Label(grid, text="Постов на канал").grid(row=2, column=0, sticky="w", pady=(7, 0))
        ttk.Label(grid, text="Мин. участников").grid(row=2, column=1, sticky="w", padx=(10, 0), pady=(7, 0))
        posts = ttk.Spinbox(grid, from_=0, to=100, textvariable=self.posts, width=10)
        posts.grid(row=3, column=0, sticky="ew", pady=3)
        members = ttk.Spinbox(grid, from_=0, to=10000000, textvariable=self.min_members, width=10)
        members.grid(row=3, column=1, sticky="ew", padx=(10, 0), pady=3)
        self.filter_widgets.extend([types_combo, limit, posts, members])
        for label, variable in [("Проверять доступных администраторов", self.admins), ("Включать контактных ботов", self.bots)]:
            widget = ttk.Checkbutton(left, text=label, variable=variable)
            widget.pack(anchor="w", pady=(7, 0))
            self.filter_widgets.append(widget)
        self.field_label(left, "Свои группы / каналы (необязательно)")
        self.sources = self.text_input(left, 2)
        ttk.Label(left, text="@канал или t.me/канал, через запятую.\nГород проверяется по названию и описанию.", style="Hint.TLabel").pack(anchor="w", pady=(5, 8))
        actions = ttk.Frame(left_panel)
        actions.pack(fill="x", pady=(5, 0))
        self.start_button = ttk.Button(actions, text="Начать поиск", style="Primary.TButton", command=self.start_search, state="disabled")
        self.start_button.pack(side="left", fill="x", expand=True)
        self.stop_button = ttk.Button(actions, text="Стоп", command=self.stop_search, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Label(right, textvariable=self.stats, font=("Helvetica Neue", 16, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Label(right, textvariable=self.status, wraplength=650, style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 10))
        table_frame = ttk.Frame(right)
        table_frame.grid(row=2, column=0, sticky="nsew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.table = ttk.Treeview(table_frame, columns=("username", "state", "role", "source"), show="headings", selectmode="browse", height=7)
        for name, label, width in [("username", "Ник", 145), ("state", "Выгрузка", 85), ("role", "Роль по источнику", 200), ("source", "Группа / канал", 180)]:
            self.table.heading(name, text=label)
            self.table.column(name, width=width, minwidth=90)
        self.table.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.bind("<<TreeviewSelect>>", self.show_evidence)
        self.table.bind("<Double-1>", self.open_contact)
        ttk.Label(right, text="Источник и подтверждение выбранного контакта", style="Hint.TLabel").grid(row=3, column=0, sticky="w", pady=(10, 4))
        self.evidence = self.output_text(right, 4)
        self.evidence.grid(row=4, column=0, sticky="ew")
        self.progress = ttk.Progressbar(right, mode="indeterminate")
        self.progress.grid(row=5, column=0, sticky="ew", pady=(12, 8))
        self.log_text = self.output_text(right, 6)
        self.log_text.grid(row=6, column=0, sticky="ew")
        buttons = ttk.Frame(right)
        buttons.grid(row=7, column=0, sticky="ew", pady=(10, 0))
        self.open_button = ttk.Button(buttons, text="Открыть TXT", command=self.open_result, state="disabled")
        self.open_button.pack(side="left")
        self.save_button = ttk.Button(buttons, text="Сохранить TXT как…", command=self.save_as, state="disabled")
        self.save_button.pack(side="left", padx=6)
        self.csv_button = ttk.Button(buttons, text="Источники CSV", command=self.save_csv, state="disabled")
        self.csv_button.pack(side="left")
        self.history_button = ttk.Button(buttons, text="Вся база TXT", command=self.save_history)
        self.history_button.pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Папка", command=lambda: open_path(RESULTS), width=6).pack(side="left", padx=(6, 0))
        self.add_log(f"В истории {self.history.total()} аккаунтов. TXT и CSV нового поиска содержат только новые контакты.")
        self.restore_search_settings()

    def restore_search_settings(self):
        try:
            settings = json.loads((PRIVATE / "search_settings.json").read_text(encoding="utf-8"))
            for field in ("topic", "city", "roles", "chat_type", "max_chats", "posts", "min_members"):
                if field in settings:
                    getattr(self, field).set(settings[field])
            self.admins.set(settings.get("check_admins", True))
            self.bots.set(settings.get("include_bots", False))
            for field in ("keywords", "sources"):
                if field in settings:
                    widget = getattr(self, field)
                    widget.delete("1.0", "end")
                    widget.insert("1.0", settings[field])
        except (OSError, ValueError, TypeError):
            return

    def restore_last_search(self):
        if self.restored_results:
            return
        self.restored_results = True
        try:
            pointer = json.loads((PRIVATE / "last_search.json").read_text(encoding="utf-8"))
            report_path = Path(pointer["report"]).resolve()
            if not report_path.is_relative_to((PRIVATE / "reports").resolve()):
                return
            report = json.loads(report_path.read_text(encoding="utf-8"))
            for row in report["contacts"]:
                contact = Contact(row["username"], row["user_id"], row.get("bot", False),
                                  [Evidence(**item) for item in row["evidence"]], row.get("is_new", True))
                self.handle_event("contact", contact)
            self.last_path = Path(report["file"])
            self.handle_event("stats", report["stats"])
            self.set_running(False)
            self.status.set(f"Открыта прошлая выгрузка. В общей истории: {self.history.total()} аккаунтов. Следующий поиск сохранит только новые.")
        except (OSError, ValueError, KeyError, TypeError):
            return

    def field_label(self, parent, text):
        ttk.Label(parent, text=text).pack(anchor="w", pady=(8, 4))

    def combo(self, parent, variable, values):
        combo = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly", width=35)
        combo.pack(fill="x")
        self.filter_widgets.append(combo)
        return combo

    def text_input(self, parent, height):
        widget = tk.Text(parent, height=height, width=35, wrap="word", relief="solid", borderwidth=1,
                         highlightthickness=0, padx=7, pady=5, bg="white", fg=INK, font=("Helvetica Neue", 11))
        widget.pack(fill="x")
        self.filter_widgets.append(widget)
        return widget

    def output_text(self, parent, height):
        return tk.Text(parent, height=height, wrap="word", state="disabled", bg="white", fg=INK,
                       relief="flat", padx=10, pady=8, font=("Helvetica Neue", 10), width=50)

    def change_topic(self, _event=None):
        self.keywords.delete("1.0", "end")
        self.keywords.insert("1.0", PRESETS[self.topic.get()])

    def add_log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{datetime.now():%H:%M:%S}  {text}\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > 500:
            self.log_text.delete("1.0", "100.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def set_auth_busy(self, busy):
        self.auth_busy = busy
        self.request_button.configure(state="disabled" if busy or self.authorized or self.running else "normal")
        self.qr_button.configure(state="disabled" if busy or self.authorized or self.running else "normal")
        self.code_button.configure(state="normal" if not busy and self.auth_stage == "code" and not self.authorized else "disabled")
        self.password_button.configure(state="normal" if not busy and self.auth_stage == "password" and not self.authorized else "disabled")
        self.logout_button.configure(state="normal" if self.authorized and not busy and not self.running else "disabled")

    def request_code(self):
        if self.auth_busy:
            return
        self.set_auth_busy(True)
        self.auth_state.set("Запрашиваю код. Он может прийти в приложение Telegram или другим способом, выбранным Telegram…")
        self.backend.submit(self.backend.request_code(self.phone.get().strip(), self.api_id.get().strip(), self.api_hash.get().strip()), "code_sent")

    def sign_in(self):
        self.set_auth_busy(True)
        self.backend.submit(self.backend.sign_in(self.code.get().strip()), "signed_in")

    def start_qr_login(self):
        if self.auth_busy or self.authorized or self.running:
            return
        self.set_auth_busy(True)
        self.auth_state.set("Создаю QR-код для входа. Запрос SMS не выполняется.")
        window = tk.Toplevel(self)
        self.qr_window = window
        window.title("Вход по QR — Telegram")
        window.configure(bg=BG)
        window.transient(self)
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", self.cancel_qr_login)
        ttk.Label(window, text="Войти через Telegram на телефоне", font=("Helvetica Neue", 16, "bold")).pack(padx=22, pady=(20, 12))
        ttk.Label(window, text="Telegram → Настройки → Устройства → Подключить устройство.\nНаведите камеру на QR-код и подтвердите вход.", wraplength=380, justify="center").pack(padx=22, pady=(0, 14))
        self.qr_canvas = tk.Canvas(window, width=280, height=280, bg="white", highlightthickness=0)
        self.qr_canvas.pack(padx=22)
        self.qr_canvas.create_text(140, 140, text="Получаю QR-код…", fill=INK)
        self.qr_state = tk.StringVar(value="Соединение с Telegram…")
        ttk.Label(window, textvariable=self.qr_state, wraplength=380, justify="center").pack(padx=22, pady=12)
        ttk.Button(window, text="Отменить", command=self.cancel_qr_login).pack(pady=(0, 20))
        window.grab_set()
        self.backend.submit(self.backend.qr_login(self.api_id.get().strip(), self.api_hash.get().strip()), "qr_finished")

    def cancel_qr_login(self):
        self.backend.cancel_qr()
        self.close_qr_window()
        self.auth_state.set("Отменяю вход по QR…")

    def close_qr_window(self):
        if self.qr_window is not None:
            self.qr_window.grab_release()
            self.qr_window.destroy()
            self.qr_window = None

    def show_qr_code(self, data):
        if self.qr_window is None:
            return
        import qrcode
        qr = qrcode.QRCode(border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(data["url"])
        qr.make(fit=True)
        matrix = qr.get_matrix()
        unit = max(1, 280 // len(matrix))
        offset = (280 - unit * len(matrix)) // 2
        self.qr_canvas.delete("all")
        for row, values in enumerate(matrix):
            for col, filled in enumerate(values):
                if filled:
                    x, y = offset + col * unit, offset + row * unit
                    self.qr_canvas.create_rectangle(x, y, x + unit, y + unit, fill="black", outline="")
        self.qr_state.set("Отсканируйте код и подтвердите вход на телефоне. При истечении срока QR обновится автоматически.")

    def password_sign_in(self):
        password = self.password.get()
        self.password.set("")
        self.set_auth_busy(True)
        self.backend.submit(self.backend.password_sign_in(password), "signed_in")

    def connected(self, identity):
        self.authorized = True
        self.auth_state.set(f"Подключён аккаунт {identity}. Можно начинать поиск.")
        self.status.set(f"Telegram подключён: {identity}")
        self.start_button.configure(state="normal")
        self.code.set("")
        self.password.set("")
        self.set_auth_busy(False)
        self.tabs.select(self.search_tab)
        self.restore_last_search()

    def logout(self):
        self.set_auth_busy(True)
        self.backend.submit(self.backend.logout(), "logged_out")

    def start_search(self):
        if self.running:
            return
        try:
            options = SearchOptions(self.keywords.get("1.0", "end").strip(), ROLE_FILTERS[self.roles.get()],
                                    self.city.get().strip(), self.chat_type.get(), int(self.max_chats.get()),
                                    int(self.posts.get()), int(self.min_members.get()), self.admins.get(),
                                    self.bots.get(), self.sources.get("1.0", "end").strip())
            options.validate()
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Настройки поиска", str(exc), parent=self)
            return
        self.contacts.clear()
        self.last_path = None
        self.table.delete(*self.table.get_children())
        self.evidence.configure(state="normal")
        self.evidence.delete("1.0", "end")
        self.evidence.configure(state="disabled")
        self.stats.set(f"Новых: 0 · Найдено: 0 · В базе: {self.history.total()}")
        self.set_running(True)
        self.backend.submit(self.backend.search(options), "finished")

    def set_running(self, running):
        self.running = running
        self.start_button.configure(state="disabled" if running or not self.authorized else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        for widget in self.filter_widgets:
            widget.configure(state="disabled" if running else ("readonly" if isinstance(widget, ttk.Combobox) else "normal"))
        for button in (self.open_button, self.save_button, self.csv_button):
            button.configure(state="disabled" if running or self.last_path is None else "normal")
        self.history_button.configure(state="disabled" if running else "normal")
        self.set_auth_busy(self.auth_busy)
        if running:
            self.progress.start(12)
            self.status.set("Идёт поиск. В TXT сохраняются только новые аккаунты; история общая для всех тем.")
        else:
            self.progress.stop()

    def stop_search(self):
        self.status.set("Останавливаю поиск и сохраняю найденные ники…")
        self.stop_button.configure(state="disabled")
        self.backend.stop_search()

    def poll(self):
        try:
            while True:
                kind, value = self.backend.events.get_nowait()
                self.handle_event(kind, value)
        except queue.Empty:
            pass
        if not self.closing:
            self.after(100, self.poll)

    def handle_event(self, kind, value):
        if kind == "restored":
            self.set_auth_busy(False)
            if value:
                self.connected(value)
            else:
                self.auth_state.set("API-настройки готовы. Введите номер телефона и запросите код входа.")
        elif kind == "code_sent":
            if value.get("identity"):
                self.connected(value["identity"])
            else:
                self.auth_stage = "code"
                self.auth_state.set(value.get("delivery_message", "Код отправлен. Введите его в поле «Код из Telegram»."))
                self.set_auth_busy(False)
                self.code_entry.focus_set()
        elif kind == "qr_code":
            self.show_qr_code(value)
        elif kind in {"signed_in", "qr_finished"}:
            if kind == "qr_finished":
                self.close_qr_window()
                if value.get("cancelled") or value.get("expired"):
                    self.auth_state.set("Вход по QR отменён. Можно нажать «Войти по QR» ещё раз.")
                    self.set_auth_busy(False)
                    return
            if value.get("password_required"):
                self.auth_stage = "password"
                self.auth_state.set("Включена двухэтапная проверка. Введите свой облачный пароль Telegram.")
                self.set_auth_busy(False)
                self.password_entry.focus_set()
            else:
                self.connected(value["identity"])
        elif kind == "logged_out":
            self.authorized = False
            self.auth_stage = "phone"
            self.start_button.configure(state="disabled")
            self.auth_state.set("Вы вышли из аккаунта. Для нового поиска войдите снова.")
            self.status.set("Для поиска войдите в Telegram")
            self.set_auth_busy(False)
        elif kind == "log":
            self.add_log(value)
        elif kind == "stats":
            if "new" in value:
                self.stats.set(f"Новых: {value['new']} · Найдено: {value['contacts']} · Сообществ: {value['scanned']}")
            else:
                self.stats.set(f"В прошлой выгрузке: {value['contacts']} · Сообществ: {value['scanned']}")
        elif kind == "contact":
            self.contacts[value.user_id] = value
            roles = list(dict.fromkeys(e.role_label for e in value.evidence))
            values = ("@" + value.username + (" [бот]" if value.bot else ""),
                      "В TXT" if value.is_new else "В базе", "; ".join(roles), value.evidence[0].title)
            iid = str(value.user_id)
            if self.table.exists(iid):
                self.table.item(iid, values=values)
            else:
                self.table.insert("", "end", iid=iid, values=values)
        elif kind == "finished":
            if value is None:
                self.set_running(False)
                self.status.set("Поиск остановлен до начала проверки сообществ.")
                if self.closing:
                    self.finish_close()
                return
            self.last_path = value.path
            self.set_running(False)
            prefix = "Остановлено" if value.cancelled else "Готово"
            self.status.set(value.reason or f"{prefix}. Новых: {len(value.new_contacts)}, уже были: {len(value.contacts) - len(value.new_contacts)}. В базе: {self.history.total()}. TXT содержит только новые.")
            if value.reason:
                messagebox.showinfo("Ограничение Telegram", value.reason, parent=self)
            if self.closing:
                self.finish_close()
        elif kind == "error":
            action, message = value
            if action == "qr_finished":
                self.close_qr_window()
            if action == "finished":
                engine = self.backend.engine
                if engine and engine.path:
                    self.last_path = engine.path
                self.set_running(False)
                self.status.set(message)
                self.add_log(message)
                if self.closing:
                    self.finish_close()
                    return
            else:
                self.set_auth_busy(False)
                self.auth_state.set(message)
            if action != "restored":
                messagebox.showerror("Telegram Контакты", message, parent=self)

    def show_evidence(self, _event=None):
        selection = self.table.selection()
        if not selection:
            return
        contact = self.contacts[int(selection[0])]
        text = "\n\n".join(f"{e.role_label} · {e.title}\n{e.source}\n{e.excerpt}" for e in contact.evidence)
        self.evidence.configure(state="normal")
        self.evidence.delete("1.0", "end")
        self.evidence.insert("1.0", text)
        self.evidence.configure(state="disabled")

    def open_contact(self, _event=None):
        selection = self.table.selection()
        if selection:
            open_path(f"https://t.me/{self.contacts[int(selection[0])].username}")

    def open_result(self):
        if self.last_path:
            open_path(self.last_path)

    def save_as(self):
        path = filedialog.asksaveasfilename(parent=self, title="Сохранить только новые контакты этого поиска", defaultextension=".txt", initialfile="Новые_ники_Telegram.txt", initialdir=str(RESULTS), filetypes=[("Текстовый файл", "*.txt")])
        if path:
            try:
                export_txt(Path(path), (c for c in self.contacts.values() if c.is_new))
            except OSError as exc:
                messagebox.showerror("Сохранение", str(exc), parent=self)
            else:
                self.add_log(f"Список сохранён: {path}")

    def save_csv(self):
        path = filedialog.asksaveasfilename(parent=self, title="Сохранить источники", defaultextension=".csv", initialfile="Источники_Telegram.csv", initialdir=str(RESULTS), filetypes=[("CSV", "*.csv")])
        if path:
            try:
                export_csv(path, (c for c in self.contacts.values() if c.is_new))
            except OSError as exc:
                messagebox.showerror("Сохранение", str(exc), parent=self)
            else:
                self.add_log(f"Источники сохранены: {path}")

    def save_history(self):
        path = filedialog.asksaveasfilename(parent=self, title="Сохранить всю накопленную базу контактов", defaultextension=".txt", initialfile="Все_контакты_Telegram.txt", initialdir=str(RESULTS), filetypes=[("Текстовый файл", "*.txt")])
        if path:
            try:
                count = self.history.export_txt(Path(path))
            except OSError as exc:
                messagebox.showerror("Сохранение", str(exc), parent=self)
            else:
                self.add_log(f"Вся база сохранена: {count} ников. {path}")

    def close(self):
        self.closing = True
        if self.running:
            self.stop_search()
            self.after(100, self.wait_close)
        else:
            self.finish_close()

    def wait_close(self):
        self.poll()
        if self.running:
            self.after(100, self.wait_close)

    def finish_close(self):
        if self.close_started:
            return
        self.close_started = True
        future = asyncio.run_coroutine_threadsafe(self.backend.shutdown(), self.backend.loop)
        self.after(100, lambda: self.wait_disconnect(future))

    def wait_disconnect(self, future, tries=0):
        if future.done() or tries >= 30:
            self.backend.loop.call_soon_threadsafe(self.backend.loop.stop)
            self.destroy()
        else:
            self.after(100, lambda: self.wait_disconnect(future, tries + 1))


def main():
    prepare_storage()
    # The desktop shortcut can be clicked more than once; never open the same SQLite
    # Telegram session concurrently in two processes.
    lock = (PRIVATE / "app.lock").open("a+")
    if sys.platform != "win32":
        import fcntl
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
    Application().mainloop()


if __name__ == "__main__":
    main()
