"""
wheel2pad — окно программы: одно приложение с двумя вкладками.

  «Работа программы»  — то, что раньше было чёрным окном консоли: состояние
                            крупно, живая строка «руль / газ / тормоз», журнал
                            сообщений, поле ответа мастеру калибровки («Далее» =
                            Enter), кнопки «Перекалибровать руль» и «Путь для HidHide»;
  «Настройка кнопок»  — в духе x360ce: что приходит с руля, что получает игра
                            (нарисованный геймпад Xbox 360), какая кнопка руля
                            заменяет каждую кнопку Xbox, ползунки чувствительности.

Над вкладками всегда видно, какой руль подключён и в каком он режиме
(X-Input / D-Input). При запуске эмуляции всплывает подсказка, что программу
нельзя закрывать во время игры, а при закрытии — просим подтвердить.

Руль, калибровка и эмуляция работают в фоновом потоке (core.gui_worker), окно —
в главном; общаются через core.Bridge (см. его описание). Ядро передаётся
параметром core, а не импортируется: при запуске wheel2pad.py скриптом это была
бы вторая копия модуля. Что получает игра, окно не пересчитывает, а берёт из
снимка эмуляции — поэтому «Настройка» показывает ровно то, что уходит в игру.
"""
import copy
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

TICK_MS = 15        # опрос моста: журнал, состояние, события руля
REDRAW_EVERY = 2    # перерисовка живых значений — каждый 2-й опрос (~30 кадров/с)

# ---------------------------------------------------------------------------
# Тема «чай»: цвета иконки (кремовая чашка, коралловое блюдце, сливовый контур,
# шоколадный чай), но насыщеннее — контрастно и ярко, без кислотных оттенков.
# ---------------------------------------------------------------------------
BG = "#fbf6ef"        # фон окна — сливки
PANEL = "#f3e9dd"     # неактивные вкладки, подложки
CARD = "#ffffff"      # поля ввода, журнал
TEXT = "#2e1f16"      # текст — крепкий чай
MUTED = "#7d6a5c"     # подсказки
PLUM = "#7a1f45"      # главный акцент — контур чашки
PLUM_HI = "#9b2a5a"   # он же под мышкой
CORAL = "#f0705f"     # нажато / полоски — блюдце
ORANGE = "#e8862a"    # курки газа и тормоза — корица
ROSE = "#e7a1ae"      # рамки — розовый бок чашки
LINE = "#e6d3c3"
GREEN, AMBER, RED = "#2e9e5b", "#e09a1f", "#d0452b"

ON, OFF = CORAL, "#efe6db"
ABXY = {"A": ("#2e9e5b", "#d5ecdd"), "B": ("#d0452b", "#f6d6cf"),
        "X": ("#2f6fd8", "#d6e2f7"), "Y": ("#e8b100", "#f8ecc4")}
# Где на картинке assets/gamepad.png центры органов управления: (x, y, радиус).
GAMEPAD_SPOTS = {"LS": (71.5, 51.5, 22), "DPAD": (118, 105, 28), "RS": (214.5, 105, 22),
                 "Y": (261.5, 31, 11), "X": (237.5, 54, 11), "B": (287, 56, 11), "A": (263, 79, 11),
                 "BACK": (134, 55, 7), "START": (203, 55, 7)}
MODE_BANNERS = {
    "X-Input": ("#fde4e1", "Режим руля: X-Input — обычный, включается сам при каждом подключении USB. "
                          "Вибрация есть; лепестки L2/R2 дублируют тормоз и газ."),
    "D-Input": ("#fff0cf", "Режим руля: D-Input — все 4 лепестка кнопками, но нет вибрации, а газ и "
                          "тормоз на одной оси. После переподключения USB руль вернётся в X-Input."),
}
# (значок, цвет) для состояний рабочего потока
STATES = {"start": ("…", MUTED), "waiting": ("⏳", AMBER), "calibrating": ("✎", PLUM),
          "emulating": ("✔", GREEN), "stopped": ("✖", RED), "error": ("✖", RED)}

# Шаги мастера калибровки для «установщика»: (этап, заголовок, по какой фразе мастера
# узнать шаг, подсказка). Сам мастер — core.calibrate, окно лишь читает его консоль.
WIZARD = [
    ("Режим", "Режим руля", "калибровать в этом режиме",
     "Обычный режим X-Input — то, что нужно: просто «Далее». Зажатая на 3 секунды кнопка MODE "
     "переключит руль в D-Input — подробности в консоли."),
    ("Руль", "Руль по центру", "Отпустите педали",
     "Педали не трогайте — программа запомнит, как руль выглядит в покое."),
    ("Руль", "Руль влево", "ДО УПОРА ВЛЕВО", "Поверните до упора и держите, пока не нажмёте «Далее»."),
    ("Руль", "Руль вправо", "ДО УПОРА ВПРАВО", "Теперь до упора в другую сторону — и держите."),
    ("Руль", "Руль в центр", "Верните руль в центр", "Отпустите руль ровно."),
    ("Педали", "Газ", "Выжмите ГАЗ", "Нажмите до пола, отпустите, потом «Далее»."),
    ("Педали", "Тормоз", "Выжмите ТОРМОЗ", "Нажмите до пола, отпустите, потом «Далее»."),
    ("Педали", "Сцепление", "СЦЕПЛЕНИЯ", "У Artplays V-1200 сцепления нет — просто «Далее»."),
    ("Кнопки", "Кнопки", None,
     "Нажмите на руле подсказанную кнопку или «Далее», чтобы пропустить. Проще всего — "
     "пропустить все, а потом на вкладке «Настройка кнопок» нажать «Авто для FH4»."),
    ("Готово", "Готово", "настройки сохранены", "Сейчас откроется вкладка «Настройка кнопок»."),
]
BUTTONS_STEP = 8
STAGES = ["Режим", "Руль", "Педали", "Кнопки", "Готово"]

# Как показывать кнопки Xbox людям: в скобках — какой лепесток руля Artplays им
# соответствует (по схеме руля: L1/R1 и L2/R2), крестовина — стрелками.
XBOX_DISPLAY = {"LB": "LB (L1)", "RB": "RB (R1)", "LS": "LS (L2)", "RS": "RS (R2)",
                "BACK": "BACK (View)", "START": "START (Menu)",
                "DPAD_UP": "Крестовина ↑", "DPAD_DOWN": "Крестовина ↓",
                "DPAD_LEFT": "Крестовина ←", "DPAD_RIGHT": "Крестовина →"}
XBOX_EXPLAIN = {"LB": "левая кнопка-плечо; на руле — лепесток L1",
                "RB": "правая кнопка-плечо; на руле — лепесток R1",
                "LS": "нажатие левого стика; на руле — лепесток L2, но только в режиме D-Input "
                      "(в обычном X-Input L2 — это тормоз)",
                "RS": "нажатие правого стика; на руле — лепесток R2, но только в режиме D-Input "
                      "(в обычном X-Input R2 — это газ)",
                "BACK": "маленькая кнопка View слева в центре",
                "START": "маленькая кнопка Menu справа в центре",
                "DPAD_UP": "крестовина вверх", "DPAD_DOWN": "крестовина вниз",
                "DPAD_LEFT": "крестовина влево", "DPAD_RIGHT": "крестовина вправо"}


def xbox_name(name):
    return XBOX_DISPLAY.get(name, name)

# (ключ в settings, подпись, минимум, максимум, подсказка при наведении)
SLIDERS = [
    ("steer_range", "Ход руля до полного стика", 0.1, 1.0,
     "Какую часть поворота от центра до упора нужно сделать, чтобы стик ушёл до конца.\n"
     "1.00 — только на упоре. Руль в режиме 900° — попробуйте 0.30–0.50."),
    ("steer_deadzone", "Мёртвая зона руля в центре", 0.0, 0.2,
     "Маленькие повороты около центра игнорируются — машина не виляет."),
    ("steer_anti_deadzone", "Компенсация мёртвой зоны игры", 0.0, 0.3,
     "Если в игре мелкие повороты «не доходят» — добавьте 0.10–0.20."),
    ("steer_gamma", "Кривая руля (больше 1 — мягче в центре)", 0.5, 2.5,
     "Больше 1 — руль спокойнее около центра и резче к краям; меньше 1 — наоборот."),
    ("pedal_deadzone", "Мёртвая зона педалей (отпущено = 0 %)", 0.0, 0.2,
     "Чтобы отпущенная педаль давала ровно 0 %, даже если чуть «дрожит»."),
    ("pedal_top_deadzone", "Запас педалей до 100 %", 0.0, 0.2,
     "Чтобы педаль в пол гарантированно давала 100 %."),
]


def asset(name):
    """Файл из assets: в exe его кладёт сборка (--add-data) во временную папку
    sys._MEIPASS, без exe — рядом со скриптом."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets" / name


def apply_theme(root):
    """Тема ttk «clam» (только её можно перекрасить) в цветах иконки."""
    st = ttk.Style(root)
    st.theme_use("clam")
    root.configure(bg=BG)
    root.option_add("*TCombobox*Listbox.background", CARD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", CORAL)
    root.option_add("*TCombobox*Listbox.selectForeground", "white")
    root.option_add("*TCombobox*Listbox.font", ("Segoe UI", 9))
    st.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 9), bordercolor=LINE,
                 lightcolor=BG, darkcolor=BG, troughcolor=PANEL, focuscolor=ROSE,
                 selectbackground=CORAL, selectforeground="white")
    st.configure("TLabel", background=BG, foreground=TEXT)
    st.configure("Bold.TLabel", font=("Segoe UI", 9, "bold"))
    st.configure("Big.TLabel", font=("Segoe UI", 12, "bold"))
    st.configure("Title.TLabel", font=("Segoe UI", 14, "bold"), foreground=PLUM)
    st.configure("Hint.TLabel", foreground=MUTED)
    st.configure("TLabelframe", background=BG, bordercolor=ROSE, relief="solid", borderwidth=1)
    st.configure("TLabelframe.Label", background=BG, foreground=PLUM, font=("Segoe UI", 9, "bold"))
    st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 4, 0, 0))
    st.configure("TNotebook.Tab", background=PANEL, foreground=TEXT, padding=(18, 7),
                 font=("Segoe UI", 10, "bold"), bordercolor=LINE, lightcolor=PANEL)
    st.map("TNotebook.Tab", background=[("selected", PLUM), ("active", "#f7d9d4")],
           foreground=[("selected", "white")], lightcolor=[("selected", PLUM)])
    st.configure("TButton", background=CARD, foreground=TEXT, bordercolor=ROSE, lightcolor=CARD,
                 darkcolor=CARD, padding=(10, 4), focuscolor=CORAL)
    st.map("TButton", background=[("pressed", ROSE), ("active", "#fde4e1")])
    st.configure("Accent.TButton", background=PLUM, foreground="white", bordercolor=PLUM,
                 lightcolor=PLUM, darkcolor=PLUM, font=("Segoe UI", 10, "bold"), padding=(14, 5))
    st.map("Accent.TButton", background=[("pressed", "#5c1734"), ("active", PLUM_HI)],
           lightcolor=[("active", PLUM_HI)], darkcolor=[("active", PLUM_HI)])
    st.configure("TEntry", fieldbackground=CARD, bordercolor=ROSE, lightcolor=CARD, insertcolor=TEXT)
    st.map("TEntry", bordercolor=[("focus", PLUM)])
    st.configure("TCombobox", fieldbackground=CARD, background=CARD, bordercolor=ROSE,
                 lightcolor=CARD, darkcolor=CARD, arrowcolor=PLUM)
    st.map("TCombobox", fieldbackground=[("readonly", CARD)], background=[("active", "#fde4e1")],
           selectbackground=[("readonly", CARD)], selectforeground=[("readonly", TEXT)],
           bordercolor=[("focus", PLUM)])
    st.configure("Horizontal.TScale", background=CORAL, troughcolor="#efe2d4", bordercolor=ROSE,
                 lightcolor=CORAL, darkcolor=RED)
    st.map("Horizontal.TScale", background=[("active", RED)])
    st.configure("TCheckbutton", background=BG, foreground=TEXT, indicatorbackground=CARD,
                 indicatorforeground=PLUM, bordercolor=ROSE)
    st.map("TCheckbutton", background=[("active", BG)], indicatorbackground=[("selected", CARD)])
    st.configure("Vertical.TScrollbar", background=PANEL, troughcolor=BG, bordercolor=LINE,
                 arrowcolor=PLUM, lightcolor=PANEL, darkcolor=PANEL)
    st.configure("Tea.Horizontal.TProgressbar", background=CORAL, troughcolor="#efe2d4",
                 bordercolor=ROSE, lightcolor=CORAL, darkcolor=RED, thickness=12)


class Tip:
    """Подсказка при наведении мыши — объясняет, что делает кнопка или ползунок."""

    def __init__(self, widget, text):
        self.w, self.text, self.win, self.job = widget, text, None, None
        widget.bind("<Enter>", lambda e: self.schedule(), add="+")
        widget.bind("<Leave>", lambda e: self.hide(), add="+")
        widget.bind("<ButtonPress>", lambda e: self.hide(), add="+")

    def schedule(self):
        self.job = self.w.after(450, self.show)

    def show(self):
        if self.win or not self.w.winfo_exists():
            return
        x, y = self.w.winfo_rootx() + 12, self.w.winfo_rooty() + self.w.winfo_height() + 4
        self.win = tk.Toplevel(self.w)
        self.win.wm_overrideredirect(True)
        self.win.geometry(f"+{x}+{y}")
        tk.Label(self.win, text=self.text, justify="left", bg="#fff7e8", fg=TEXT, padx=8, pady=5,
                 font=("Segoe UI", 9), highlightthickness=1, highlightbackground=ROSE).pack()

    def hide(self):
        if self.job:
            self.w.after_cancel(self.job)
            self.job = None
        if self.win:
            self.win.destroy()
            self.win = None


class Tea:
    """Анимированная чашка чая (assets/tea.gif — уменьшенная icon.gif)."""

    def __init__(self, parent, bg=BG):
        self.frames = []
        try:
            while True:  # Tk читает кадры GIF по одному: «gif -index N»
                self.frames.append(tk.PhotoImage(file=str(asset("tea.gif")),
                                                 format=f"gif -index {len(self.frames)}"))
        except tk.TclError:
            pass
        self.label = tk.Label(parent, bg=bg, bd=0, image=self.frames[0] if self.frames else "")
        self.i = 0
        if len(self.frames) > 1:
            self.label.after(200, self.next)

    def next(self):
        if not self.label.winfo_exists():
            return
        self.i = (self.i + 1) % len(self.frames)
        self.label.config(image=self.frames[self.i])
        self.label.after(200, self.next)

NO_LABELS = {"buttons": {}, "axes": {}, "hidden_axes": set()}
# Короткие подписи для лампочек кнопок (полные — в выпадающих списках).
SHORT = {"крестовина вверх": "крест. ↑", "крестовина вниз": "крест. ↓",
         "крестовина влево": "крест. ←", "крестовина вправо": "крест. →"}


def _same(a, b):
    """Один и тот же источник? Для оси важно ещё направление (+/−)."""
    if a is None or b is None:
        return a is b
    if (a["type"], a["index"], a.get("value")) != (b["type"], b["index"], b.get("value")):
        return False
    if a["type"] == "axis":
        return (a["full"] - a["rest"] > 0) == (b["full"] - b["rest"] > 0)
    return True


class SnapJoy:
    """«Руль» для окна: те же методы, что у pygame.Joystick, но значения — из
    последнего снимка эмуляции. Так core.read_axes, core.safe_axis и
    core.ControlDetector работают в окне без доступа к pygame."""

    def __init__(self, bridge, snap):
        self.bridge, self.last = bridge, snap

    def _s(self):
        self.last = self.bridge.snap or self.last
        return self.last

    def get_name(self): return self._s()["name"]
    def get_instance_id(self): return self._s()["instance_id"]
    def get_numaxes(self): return len(self._s()["axes"])
    def get_numbuttons(self): return len(self._s()["buttons"])
    def get_numhats(self): return len(self._s()["hats"])
    def get_axis(self, i): return self._s()["axes"][i]
    def get_button(self, i): return self._s()["buttons"][i]
    def get_hat(self, i): return self._s()["hats"][i]


class LogWriter:
    """sys.stdout/stderr рабочего потока → очередь → журнал на вкладке «Работа»."""

    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(s)
        return len(s)

    def flush(self):
        pass


# ======================================================================== окно
class App:
    def __init__(self, core, args):
        self.core = core
        self.bridge = core.Bridge()
        core.BRIDGE = self.bridge
        self.log_q = queue.Queue()
        sys.stdout = sys.stderr = LogWriter(self.log_q)
        self.tip_shown = False
        self.last_state = None
        self.ticks = 0

        try:  # чёткий текст на мониторах с масштабом больше 100 %
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
        self.root = tk.Tk()
        self.root.title(f"wheel2pad {core.VERSION} — руль как геймпад Xbox 360")
        self.set_icon()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Return>", self.on_return)
        self.root.bind("<Escape>", lambda e: self.settings.cancel_capture())
        apply_theme(self.root)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        self.build_header()
        self.nb = ttk.Notebook(self.root)
        self.nb.grid(row=2, column=0, sticky="nsew", padx=10, pady=(6, 10))
        self.work = WorkTab(self.nb, self)
        self.settings = SettingsTab(self.nb, self)
        self.nb.add(self.work.frame, text="Работа программы")
        self.nb.add(self.settings.frame, text="Настройка кнопок")
        if args.edit:
            self.nb.select(1)

        self.worker = threading.Thread(target=core.gui_worker, args=(args,), daemon=True)
        self.worker.start()
        self.center_on_screen()
        self.root.after(TICK_MS, self.tick)

    def set_icon(self):
        """Иконка программы вместо пера Tk."""
        try:
            self.root.iconbitmap(default=str(asset("wheel2pad.ico")))
        except tk.TclError:
            pass  # нет файла — останется стандартная иконка, это не страшно

    def center_on_screen(self):
        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        x = max((self.root.winfo_screenwidth() - w) // 2, 0)
        y = max((self.root.winfo_screenheight() - h) // 3, 0)
        self.root.geometry(f"+{x}+{y}")

    def build_header(self):
        # Сливовая полоса сверху — «шапка» в цвет контура чашки.
        tk.Frame(self.root, bg=PLUM, height=5).grid(row=0, column=0, sticky="ew")
        top = ttk.Frame(self.root, padding=(10, 8, 10, 0))
        top.grid(row=1, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        self.device_lbl = ttk.Label(top, text="Руль: ищу…", style="Title.TLabel")
        self.device_lbl.grid(row=0, column=0, sticky="w")
        self.banner = tk.Label(top, anchor="w", justify="left", padx=10, pady=5, wraplength=1100,
                               fg=TEXT, font=("Segoe UI", 9, "bold"))
        self.banner.grid(row=1, column=0, sticky="ew", pady=(4, 0))

    def update_header(self):
        b = self.bridge
        snap, cfg = b.snap, b.cfg
        name = snap["name"] if snap else (b.wanted or (cfg or {}).get("device", {}).get("name"))
        code = b.state[0]
        if snap:
            self.device_lbl.config(text=f"Руль: «{name}»")
            mode = self.core.wheel_mode(name)
            bg, text = MODE_BANNERS.get(mode, (PANEL, "Это не Artplays V-1200 — названия кнопок "
                                                      "руля неизвестны, показаны номера."))
        elif code == "calibrating":
            self.device_lbl.config(text="Руль: калибровка")
            bg, text = "#f0e2ef", ("Идёт калибровка — шаги крупно на вкладке "
                                   "«Работа программы». Режим руля мастер покажет сам.")
        elif b.other_mode:
            have, need = self.core.wheel_mode(b.other_mode), self.core.wheel_mode(name)
            self.device_lbl.config(text=f"Руль: «{b.other_mode}» — не тот режим")
            bg, text = "#fff0cf", (f"Руль сейчас в режиме {have}, а настройки сделаны для {need}. "
                                   "Что сделать — написано в консоли на вкладке «Работа программы».")
        else:
            self.device_lbl.config(text=f"Руль: «{name}» — не подключён" if name else "Руль: не найден")
            bg, text = "#fbd9d2", ("Руля нет. Если на нём зажимали MODE — это смена режима "
                                   "(X-Input ↔ D-Input); что делать — написано в консоли.")
        if self.banner.cget("text") != text:
            self.banner.config(bg=bg, text=text)

    # ---------------------------------------------------------------- таймер
    def tick(self):
        try:
            self._tick()
        finally:
            try:
                self.root.after(TICK_MS, self.tick)
            except tk.TclError:
                pass  # окно уже закрыли

    def _tick(self):
        self.work.pump_log()
        b = self.bridge
        while True:  # ответы рабочего потока на submit() (например, «Авто»)
            try:
                cb, res = b.results.get_nowait()
            except queue.Empty:
                break
            cb(res)
        if b.state != self.last_state:
            self.last_state = b.state
            self.work.set_state(*b.state)
            if b.state[0] == "calibrating":
                self.nb.select(0)  # мастер калибровки — на вкладке «Работа»
            if b.state[0] == "emulating" and not self.tip_shown:
                self.tip_shown = True
                self.show_tip()
        if b.show_settings:
            b.show_settings = False
            self.nb.select(1)
        self.settings.tick()
        self.ticks += 1
        if self.ticks % REDRAW_EVERY == 0:
            self.update_header()
            self.work.redraw()
            self.settings.redraw()

    def on_return(self, e):
        if self.nb.index("current") == 0:
            self.work.submit()

    # ---------------------------------------------------------------- подсказка и закрытие
    def show_tip(self):
        """Всплывашка: программу не закрывать во время игры. Сама исчезает через 15 с."""
        tip = tk.Toplevel(self.root)
        tip.title("wheel2pad")
        tip.transient(self.root)
        tip.resizable(False, False)
        tip.configure(bg=PLUM)
        f = tk.Frame(tip, bg="#fff7e8", padx=18, pady=14)
        f.pack(fill="both", expand=True, padx=3, pady=3)  # сливовая рамка вокруг
        Tea(f, bg="#fff7e8").label.grid(row=0, column=0, rowspan=3, padx=(0, 14), sticky="n")
        tk.Label(f, text="Не закрывайте программу, пока играете", bg="#fff7e8",
                 font=("Segoe UI", 13, "bold"), fg=PLUM).grid(row=0, column=1, sticky="w")
        tk.Label(f, bg="#fff7e8", fg=TEXT, justify="left", font=("Segoe UI", 10),
                 text="Руль работает в игре только пока открыто окно wheel2pad.\n"
                      "Его можно свернуть (кнопка «—» вверху справа) — руль продолжит работать.\n"
                      "Закроете окно — игра перестанет видеть руль.").grid(
            row=1, column=1, sticky="w", pady=(6, 10))
        ttk.Button(f, text="Понятно", style="Accent.TButton", command=tip.destroy).grid(
            row=2, column=1, sticky="e")
        tip.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - tip.winfo_width()) // 2
        y = self.root.winfo_rooty() + 120
        tip.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        tip.lift()
        self.tip = tip
        self.root.after(15000, lambda: tip.winfo_exists() and tip.destroy())

    def on_close(self):
        if not self.settings.confirm_unsaved("Закрыть программу"):
            return
        if self.bridge.state[0] == "emulating" and not messagebox.askokcancel(
                "wheel2pad", "Сейчас руль работает как геймпад.\n\n"
                "Если закрыть программу, игра перестанет видеть руль.\n"
                "Свернуть окно можно кнопкой «—» — тогда всё продолжит работать.\n\n"
                "Закрыть программу?", icon="warning", parent=self.root):
            return
        self.bridge.stop.set()            # эмуляция выйдет из цикла и уберёт виртуальный геймпад
        self.worker.join(timeout=2.0)
        for job in self.root.tk.call("after", "info"):  # таймеры (опрос, анимация чая) — больше не нужны
            self.root.tk.call("after", "cancel", job)   # не after_cancel: тот ещё и удаляет команды Tcl
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ======================================================================== вкладка 1
class WorkTab:
    """«Работа программы» — бывшее чёрное окно консоли."""

    def __init__(self, nb, app):
        self.app, self.core = app, app.core
        self.tail = ""   # хвост журнала — из него берём текущий вопрос мастера
        self.console_open = False
        f = self.frame = ttk.Frame(nb, padding=12)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(4, weight=1)

        ttk.Label(f, text="Работа программы", style="Title.TLabel").grid(row=0, column=0, sticky="w")

        # Карточка: главное правило крупно на всю ширину; цвет полоски и значок —
        # состояние (✔ работает, ⏳ жду руль…), ниже — живая строка или что не так.
        card = tk.Frame(f, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        card.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 8))
        card.columnconfigure(2, weight=1)
        self.stripe = tk.Frame(card, bg=MUTED, width=6)
        self.stripe.grid(row=0, column=0, rowspan=2, sticky="ns")
        self.icon = tk.Label(card, bg=CARD, font=("Segoe UI", 20, "bold"), width=2)
        self.icon.grid(row=0, column=1, rowspan=2, padx=(8, 4))
        self.state_lbl = tk.Label(card, bg=CARD, font=("Segoe UI", 13, "bold"), anchor="w",
                                  justify="left", wraplength=800,
                                  text="Пока это окно открыто, руль работает в игре как геймпад Xbox 360. "
                                       "Окно можно свернуть, но не закрывать.")
        self.state_lbl.grid(row=0, column=2, sticky="ew", pady=(8, 2), padx=(0, 12))
        card.bind("<Configure>", lambda e: self.state_lbl.config(wraplength=max(e.width - 90, 300)))
        self.live = tk.Label(card, bg=CARD, fg=TEXT, font=("Segoe UI", 11), anchor="w")
        self.live.grid(row=1, column=2, sticky="w", pady=(0, 8))
        self.problem = ""   # «жду руль», «ошибка…» — коротко под главной фразой

        # Мастер калибровки — как установщик программы: «Шаг 4 из 10», крупное задание,
        # подсказка, полоса прогресса и «дорожка» этапов. Виден только во время калибровки.
        W = "#fff7e8"
        sc = self.step_card = tk.Frame(f, bg=W, highlightthickness=2, highlightbackground=PLUM)
        sc.columnconfigure(1, weight=1)
        Tea(sc, bg=W).label.grid(row=0, column=0, rowspan=4, padx=(12, 14), pady=10, sticky="n")
        self.step_no = tk.Label(sc, bg=W, fg=CORAL, font=("Segoe UI", 10, "bold"), anchor="w")
        self.step_no.grid(row=0, column=1, sticky="w", pady=(12, 0))
        self.step_lbl = tk.Label(sc, bg=W, fg=PLUM, font=("Segoe UI", 15, "bold"),
                                 anchor="w", justify="left", wraplength=700)
        self.step_lbl.grid(row=1, column=1, sticky="w", pady=(2, 0))
        self.step_hint = tk.Label(sc, bg=W, fg=TEXT, font=("Segoe UI", 10), anchor="w",
                                  justify="left", wraplength=700)
        self.step_hint.grid(row=2, column=1, sticky="w", pady=(4, 0))
        self.ans = tk.Frame(sc, bg=W)  # только когда мастер просит номер
        tk.Label(self.ans, text="Ваш ответ:", bg=W, fg=TEXT, font=("Segoe UI", 10, "bold")).grid(
            row=0, column=0, padx=(0, 6))
        self.entry = ttk.Entry(self.ans, width=6, font=("Segoe UI", 11))
        self.entry.grid(row=0, column=1)
        nxt = ttk.Button(sc, text="Далее  ⏎", style="Accent.TButton", command=self.submit)
        nxt.grid(row=0, column=2, rowspan=3, padx=16)
        Tip(nxt, "То же, что клавиша Enter: подтвердить шаг мастера калибровки.")
        prog = tk.Frame(sc, bg=W)
        prog.grid(row=4, column=1, columnspan=2, sticky="ew", pady=(10, 12), padx=(0, 16))
        prog.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(prog, style="Tea.Horizontal.TProgressbar", maximum=1.0)
        self.bar.grid(row=0, column=0, sticky="ew")
        self.stepper = tk.Canvas(prog, height=40, bg=W, highlightthickness=0)
        self.stepper.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.stepper.bind("<Configure>", lambda e: self.draw_stepper())
        self.stage = 0

        # Консоль (журнал) — спрятана: для любопытных и на случай проблем.
        tog = ttk.Frame(f)
        tog.grid(row=3, column=0, columnspan=2, sticky="ew")
        self.tog_btn = ttk.Button(tog, text="▸  Развернуть консоль", command=self.toggle_console)
        self.tog_btn.grid(row=0, column=0, sticky="w")
        Tip(self.tog_btn, "Все сообщения программы — подробности, подсказки, причины ошибок.\n"
                          "Если что-то пошло не так, консоль развернётся сама.")
        # Пока всё работает и консоль свёрнута — на свободном месте чашка и «можно играть».
        self.idle = ttk.Frame(f)
        Tea(self.idle).label.grid(row=0, column=0, pady=(30, 8))
        ttk.Label(self.idle, text="Всё работает — можно запускать игру", style="Title.TLabel").grid(
            row=1, column=0)
        ttk.Label(self.idle, style="Hint.TLabel",
                  text="Это окно сверните (кнопка «—» вверху справа) и играйте.").grid(row=2, column=0)
        self.state_code = "start"
        logf = self.logf = ttk.LabelFrame(f, text=" Консоль ", padding=4)
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(0, weight=1)
        self.log = tk.Text(logf, height=12, width=110, wrap="word", font=("Consolas", 10),
                           state="disabled", bg=CARD, fg=TEXT, relief="flat", padx=6, pady=4,
                           selectbackground=CORAL)
        self.log.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log.config(yscrollcommand=sb.set)
        self.log.tag_configure("answer", foreground=PLUM, font=("Consolas", 10, "bold"))

        btns = ttk.Frame(f)
        btns.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        b1 = ttk.Button(btns, text="⟳  Перекалибровать руль", command=self.recalibrate)
        b1.grid(row=0, column=0)
        Tip(b1, "Пройти калибровку заново: найти ось руля и педалей, назначить кнопки.\n"
                "Нужно, если подключили другой руль или переключили его режим.")
        b2 = ttk.Button(btns, text="Путь для HidHide", command=lambda: self.send("h"))
        b2.grid(row=0, column=1, padx=6)
        Tip(b2, "Скопирует в буфер путь к программе — вставьте его в HidHide,\n"
                "во вкладке «Applications» (кнопка «+», затем Ctrl+V).")
        b3 = ttk.Button(btns, text="Настройка кнопок  →", style="Accent.TButton",
                        command=lambda: app.nb.select(1))
        b3.grid(row=0, column=2)
        Tip(b3, "Переназначить кнопки и подобрать чувствительность — с живым просмотром.")

    def send(self, text):
        self.echo(text)
        self.core._lines.put(text)

    def echo(self, text):
        self.write(f" ⏎ {text}\n" if text else " ⏎\n", "answer")
        # Как Enter в консоли: вопрос мастера печатается без перевода строки, и без
        # этого следующий вопрос склеился бы с ним — мастер «застрял» бы на шаге.
        self.tail += "\n"

    def submit(self):
        text = self.entry.get().strip()
        self.entry.delete(0, "end")
        self.send(text)

    def recalibrate(self):
        if not self.app.settings.confirm_unsaved("Перекалибровать руль"):
            return
        self.send("r")

    def write(self, text, tag=None):
        self.log.config(state="normal")
        self.log.insert("end", text, tag)
        self.log.see("end")
        self.log.config(state="disabled")

    def pump_log(self):
        chunks = []
        while len(chunks) < 200:
            try:
                chunks.append(self.app.log_q.get_nowait())
            except queue.Empty:
                break
        if chunks:
            # «\r» — консольный приём «перепиши строку»; в журнале просто с новой строки
            text = "".join(chunks).replace("\r", "\n")
            self.write(text)
            self.tail = (self.tail + text)[-3000:]
            if self.app.bridge.state[0] == "calibrating":
                self.update_wizard()

    # ---------------------------------------------------------------- мастер как установщик
    def current_step(self):
        """Где сейчас мастер калибровки — по последним строкам журнала.

        Вернёт словарь: номер шага (0…len(WIZARD)-1), заголовок, крупное задание,
        подсказку, предупреждение, долю пройденного внутри шага (кнопки: 3 из 14)
        и нужно ли поле ответа. Сам мастер (core.calibrate) не меняется — он
        по-прежнему «печатает в консоль», а окно читает это и рисует красиво.
        """
        import re
        lines = [ln.strip() for ln in self.tail.split("\n") if ln.strip()]
        last = re.sub(r"\s{2,}", "  ", lines[-1]).rstrip("… ").strip() if lines else ""
        recent = " ".join(lines[-5:])
        step = {"i": 0, "title": "Начинаем", "text": "Подождите…", "hint": "", "warn": "",
                "sub": 0.0, "ask": False}
        for i, (stage, title, key, hint) in enumerate(WIZARD):
            if key and key in last:
                step.update(i=i, title=title, hint=hint)
                break
        else:
            # строка назначения кнопки: «B  на руле: лепесток R1  (в FH4: передача вверх)»
            m = re.match(r"^([A-Z_]+)\s+(?:на руле: (.+?)|не нужна — Enter)\s*(?:\(в FH4: (.+?)\))?"
                         r"(?:\s+→ (.+))?$", last)
            names = [n for n, *_ in self.core.XBOX_BUTTONS]
            if m and m[1] in names:
                name, wheel, fh4, got = m.groups()
                k = names.index(name)
                i = BUTTONS_STEP
                step.update(i=i, title=f"{WIZARD[i][1]} — {k + 1} из {len(names)}", hint=WIZARD[i][3],
                            sub=(k + (1 if got else 0)) / len(names))
                what = f"Кнопка Xbox «{xbox_name(name)}»" + (f" — {fh4}" if fh4 else "")
                if got:
                    step["text"] = f"{what}\nназначено: {got}"
                elif wheel:
                    step["text"] = f"{what}\nНажмите на руле: {wheel}"
                else:
                    step["text"] = f"{what}\nОбычно не нужна — нажмите «Далее»"
                if name in XBOX_EXPLAIN:
                    step["hint"] = f"{xbox_name(name)} — {XBOX_EXPLAIN[name]}. {step['hint']}"
                if "это сработал газ" in recent:
                    step["warn"] = ("Это сработал газ или тормоз, а не кнопка: лепестки L2/R2 работают "
                                    "как педали. Нажмите лепесток L1/R1, другую кнопку или «Далее».")
                return step
            if "Номер руля" in last:
                step.update(title="Выбор руля", text="Подключено несколько устройств —\nкакое из них руль?",
                            hint="Список с номерами — в консоли ниже. Впишите номер руля и нажмите «Далее».",
                            ask=True)
                return step
            if "Жду подключения руля" in recent:
                step.update(title="Подключите руль", text="Вставьте USB руля в компьютер",
                            hint="Как только руль появится, мастер продолжит сам.")
                return step
        if step["i"] == 0:
            tail = self.tail[-1500:]
            mode = "D-Input" if tail.rfind("в режиме D-Input") > tail.rfind("режиме X-Input") else "X-Input"
            step["text"] = (f"Руль сейчас в обычном режиме {mode}.\nПродолжаем в нём?" if mode == "X-Input"
                            else f"Руль сейчас в режиме {mode} (все 4 лепестка — кнопки).\nПродолжаем в нём?")
        if step["text"] == "Подождите…" and last:
            line = re.sub(r"^\d+\)\s*", "", last)  # «4) Выжмите ГАЗ…» → «Выжмите ГАЗ…»
            for a, b in (("и нажмите Enter", "и нажмите «Далее»"), ("нажмите Enter", "нажмите «Далее»"),
                         ("Enter —", "«Далее» —"), ("Enter (", "«Далее» ("), ("Enter,", "«Далее»,")):
                line = line.replace(a, b)
            step["text"] = line
        return step

    def update_wizard(self):
        st = self.current_step()
        self.stage = WIZARD[st["i"]][0]
        n = len(WIZARD)
        self.step_no.config(text=f"ШАГ {st['i'] + 1} ИЗ {n}  ·  {st['title'].upper()}")
        self.step_lbl.config(text=st["text"])
        self.step_hint.config(text=st["warn"] or st["hint"], fg=RED if st["warn"] else TEXT,
                              font=("Segoe UI", 10, "bold" if st["warn"] else "normal"))
        self.bar["value"] = min((st["i"] + st["sub"]) / (n - 1), 1.0)
        if st["ask"]:
            self.ans.grid(row=3, column=1, sticky="w", pady=(8, 0))
            self.entry.focus_set()
            self.toggle_console(True)  # список устройств — в консоли
        else:
            self.ans.grid_remove()
        self.draw_stepper()

    def draw_stepper(self):
        """«Дорожка» этапов под полосой: пройденные — коралловые, текущий — сливовый."""
        c = self.stepper
        c.delete("all")
        w = max(c.winfo_width(), 300)
        cur = STAGES.index(self.stage) if self.stage in STAGES else 0
        xs = [20 + k * (w - 40) / (len(STAGES) - 1) for k in range(len(STAGES))]
        for k in range(len(STAGES) - 1):
            c.create_line(xs[k] + 9, 10, xs[k + 1] - 9, 10, width=3, fill=CORAL if k < cur else LINE)
        for k, (x, name) in enumerate(zip(xs, STAGES)):
            fill, fg = (CORAL, "white") if k < cur else (PLUM, "white") if k == cur else (OFF, MUTED)
            c.create_oval(x - 9, 1, x + 9, 19, fill=fill, outline="")
            c.create_text(x, 10, text="✔" if k < cur else str(k + 1), fill=fg, font=("Segoe UI", 8, "bold"))
            c.create_text(x, 31, text=name, fill=PLUM if k == cur else (TEXT if k < cur else MUTED),
                          font=("Segoe UI", 9, "bold" if k == cur else "normal"))

    def toggle_console(self, show=None):
        self.console_open = (not self.console_open) if show is None else show
        if self.console_open:
            self.logf.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(4, 0))
            self.tog_btn.config(text="▾  Свернуть консоль")
            self.log.see("end")
        else:
            self.logf.grid_remove()
            self.tog_btn.config(text="▸  Развернуть консоль")
        self.update_idle()

    def update_idle(self):
        if self.state_code == "emulating" and not self.console_open:
            self.idle.grid(row=4, column=0, columnspan=2)
        else:
            self.idle.grid_remove()

    def set_state(self, code, text):
        icon, color = STATES.get(code, STATES["start"])
        self.stripe.config(bg=color)
        self.icon.config(text=icon, fg=color)
        self.state_lbl.config(fg=color)  # сама фраза — всегда главное правило, меняется только цвет
        self.problem = text if code in ("waiting", "stopped", "error") else ""
        self.state_code = code
        self.update_idle()
        if code == "calibrating":
            self.step_card.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 8))
            self.update_wizard()
        else:
            self.step_card.grid_remove()
        if code in ("stopped", "error"):
            self.toggle_console(True)  # что-то не так — причина в консоли, покажем её сразу

    def redraw(self):
        snap = self.app.bridge.snap
        if snap and self.app.bridge.state[0] == "emulating":
            x, rt, lt, want = snap["out"]
            self.live.config(text=f"Руль {x:+.2f}     Газ {rt:4.0%}     Тормоз {lt:4.0%}     "
                                  f"Кнопки: {' '.join(sorted(want)) or '—'}", fg=TEXT)
        elif self.live.cget("text") != self.problem:
            self.live.config(text=self.problem, fg=STATES.get(self.app.bridge.state[0], STATES["start"])[1])


# ======================================================================== вкладка 2
class SettingsTab:
    """«Настройка кнопок» — в духе x360ce, с живым просмотром."""

    def __init__(self, nb, app):
        self.app, self.core, self.bridge = app, app.core, app.bridge
        self.frame = ttk.Frame(nb, padding=8)
        self.frame.columnconfigure(0, weight=1)
        self.placeholder = ttk.Frame(self.frame)
        Tea(self.placeholder).label.grid(row=0, column=0, pady=(0, 10))
        self.placeholder_lbl = ttk.Label(self.placeholder, style="Big.TLabel", justify="center")
        self.placeholder_lbl.grid(row=1, column=0)
        self.content = None
        self.key = None          # что сейчас построено: (версия конфига, руль, число осей/кнопок/крестовин)
        self.version = None
        self.cfg = self.saved = None
        self.capture = None      # (кнопка Xbox, ControlDetector) — ждём нажатия на руле
        self.show_placeholder("Настройка появится, когда руль найден и откалиброван.")

    # ---------------------------------------------------------------- жизненный цикл
    def show_placeholder(self, text):
        if self.content is not None:
            self.content.destroy()
            self.content = None
            self.key = None
            self.capture = None
        self.placeholder_lbl.config(text=text)
        self.placeholder.grid(row=0, column=0, pady=70)

    def tick(self):
        b = self.bridge
        snap, cfg = b.snap, b.cfg
        if b.state[0] != "emulating" or snap is None or cfg is None:
            if self.content is not None or b.state[0] == "calibrating":
                self.show_placeholder("Идёт калибровка — она на вкладке «Работа программы»."
                                      if b.state[0] == "calibrating" else
                                      "Руль не подключён — настройка появится, когда он найдётся.")
            b.events.clear()
            return
        if b.cfg_version != self.version:  # новая калибровка — начинаем с того, что в файле
            self.version = b.cfg_version
            self.saved, self.cfg = copy.deepcopy(cfg), copy.deepcopy(cfg)
        key = (self.version, snap["name"], len(snap["axes"]), len(snap["buttons"]), len(snap["hats"]))
        if key != self.key:
            self.build(snap, key)
        # события руля нужны только кнопке «Нажать», но очередь чистим всегда
        while b.events:
            e = b.events.popleft()
            if self.capture:
                src = self.capture[1].feed(e)
                if src:
                    self.finish_capture(src)
        if self.capture:
            det = self.capture[1]
            if det.pedal_hit:
                det.pedal_hit = False
                if self.core.wheel_mode(snap["name"]) == "X-Input":
                    self.say("Это газ или тормоз, а не кнопка. Лепесток L2/R2 в режиме X-Input работает "
                             "как педаль — возьмите L1/R1 или другую кнопку. Esc — отмена.", RED)
                else:
                    self.say("Это педаль, а не кнопка — нажмите кнопку или лепесток. Esc — отмена.",
                             RED)
            src = det.poll()
            if src:
                self.finish_capture(src)

    def build(self, snap, key):
        self.placeholder.grid_forget()
        if self.content is not None:
            self.content.destroy()
        self.key, self.capture = key, None
        self.j = SnapJoy(self.bridge, snap)
        self.labels = self.core.WHEEL_LABELS.get(snap["name"], NO_LABELS)
        # Где у каждой оси «покой»: у руля/педалей — из калибровки, у остальных —
        # по текущему положению (настройку открывают, ничего не нажимая).
        self.rest = [-1.0 if v < -0.5 else 0.0 for v in snap["axes"]]
        for k, field in (("steer", "center"), ("gas", "rest"), ("brake", "rest"), ("clutch", "rest")):
            p = self.cfg.get(k)
            if p and p["axis"] < len(self.rest):
                self.rest[p["axis"]] = p[field]

        c = self.content = ttk.Frame(self.frame)
        c.grid(row=0, column=0, sticky="nsew")
        c.columnconfigure(0, weight=1)
        head = ttk.Frame(c)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        head.columnconfigure(0, weight=1)
        ttk.Label(head, text="Настройка кнопок и чувствительности", style="Title.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(head, justify="left", text=(
            "1. Нажмите кнопку на руле — слева загорится её лампочка, в центре видно, что получит игра.\n"
            "2. Чтобы поменять кнопку: «Нажать» в нужной строке справа и нажмите кнопку на руле "
            "(или выберите из списка).\n"
            "3. Всё работает в игре сразу. «Сохранить» — чтобы запомнить и на следующий запуск.")
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        Tea(head).label.grid(row=0, column=1, rowspan=2, sticky="e", padx=(10, 4))
        body = ttk.Frame(c)
        body.grid(row=1, column=0, sticky="nsew")
        self.build_options()
        self.build_wheel_panel(body)
        self.build_pad_panel(body)
        self.build_buttons_panel(body)
        self.build_sliders(c)
        self.build_bottom(c)
        self.refresh_all()

    # ---------------------------------------------------------------- сборка
    def build_options(self):
        """Варианты для выпадающих списков: все кнопки, крестовины и свободные оси руля."""
        j, lab = self.j, self.labels
        self.options = [("— не назначено", None)]
        for b in range(j.get_numbuttons()):
            name = lab["buttons"].get(b, "")
            self.options.append((f"Кнопка {b}" + (f" — {name}" if name else ""),
                                 {"type": "button", "index": b}))
        for h in range(j.get_numhats()):
            for word, val in (("вверх", (0, 1)), ("вниз", (0, -1)), ("влево", (-1, 0)), ("вправо", (1, 0))):
                self.options.append((f"Крестовина {h}: {word}", {"type": "hat", "index": h, "value": list(val)}))
        busy = self.core.busy_axes(self.cfg)
        for a in range(j.get_numaxes()):
            if a in busy or a in lab["hidden_axes"]:
                continue
            tag = lab["axes"].get(a, "")
            dirs = [("", 1.0)] if self.rest[a] < -0.5 else [(" +", 1.0), (" −", -1.0)]
            for suffix, full in dirs:
                self.options.append((f"Ось {a}{suffix}" + (f" — {tag}" if tag else "") + " (как кнопка)",
                                     {"type": "axis", "index": a, "rest": self.rest[a], "full": full}))

    def label_of(self, src):
        for text, o in self.options:
            if _same(o, src):
                return text
        text = self.core.describe_source(src)  # например, ось, которой нет в списке
        self.options.append((text, src))
        return text

    def build_wheel_panel(self, body):
        f = ttk.LabelFrame(body, text=" Что приходит с руля ", padding=6)
        f.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        roles = {}
        for key, word in (("steer", "руль"), ("gas", "газ"), ("brake", "тормоз"), ("clutch", "сцепление")):
            p = self.cfg.get(key)
            if p:
                roles.setdefault(p["axis"], []).append(word)
        self.axis_rows = []
        r = 0
        for a in range(self.j.get_numaxes()):
            if a in self.labels["hidden_axes"]:
                continue
            tag = self.labels["axes"].get(a) or ", ".join(roles.get(a, []))
            ttk.Label(f, text=f"Ось {a}" + (f" — {tag}" if tag else "")).grid(row=r, column=0, columnspan=2, sticky="w")
            cv = tk.Canvas(f, width=170, height=14, bg=CARD, highlightthickness=1, highlightbackground=ROSE)
            cv.grid(row=r + 1, column=0, sticky="w")
            bar = cv.create_rectangle(0, 0, 0, 14, fill=CORAL, width=0)
            if self.rest[a] > -0.5:
                cv.create_line(85, 0, 85, 14, fill=MUTED)
            val = ttk.Label(f, width=6, anchor="e")
            val.grid(row=r + 1, column=1, sticky="e", padx=(4, 0))
            self.axis_rows.append((a, cv, bar, val))
            r += 2
        ttk.Label(f, text="Кнопки", style="Bold.TLabel").grid(row=r, column=0, sticky="w", pady=(8, 2))
        grid = ttk.Frame(f)
        grid.grid(row=r + 1, column=0, columnspan=2, sticky="w")
        self.btn_lamps = []
        for b in range(self.j.get_numbuttons()):
            name = self.labels["buttons"].get(b, "")
            short = SHORT.get(name, name.replace("лепесток ", "").replace(" — не назначать", ""))
            lamp = tk.Label(grid, text=f"{b}" + (f" · {short}" if short else ""), bg=OFF, width=14,
                            anchor="w", padx=4)
            lamp.grid(row=b // 2, column=b % 2, padx=1, pady=1, sticky="w")
            self.btn_lamps.append(lamp)
        self.hat_labels = []
        for h in range(self.j.get_numhats()):
            lab = ttk.Label(f)
            lab.grid(row=r + 2 + h, column=0, columnspan=2, sticky="w", pady=(4, 0))
            self.hat_labels.append(lab)

    def build_pad_panel(self, body):
        f = ttk.LabelFrame(body, text=" Что получает игра (виртуальный Xbox 360) ", padding=6)
        f.grid(row=0, column=1, sticky="nsew", padx=(0, 6))
        c = self.pad_canvas = tk.Canvas(f, width=360, height=296, highlightthickness=0, bg=BG)
        c.grid(row=0, column=0)
        it = self.pad_items = {}
        # Курки — полоски с процентами, плечи — плашки; на картинке их почти не видно.
        for name, x0 in (("LT", 34), ("RT", 236)):
            c.create_rectangle(x0, 4, x0 + 90, 22, outline=ROSE, fill=CARD)
            it[name] = c.create_rectangle(x0 + 1, 5, x0 + 1, 22, fill=ORANGE, width=0)
            it[name + "_t"] = c.create_text(x0 + 45, 13, text=f"{name} 0%", font=("Segoe UI", 8, "bold"),
                                            fill=TEXT)
        for name, x0 in (("LB", 44), ("RB", 236)):
            it[name] = c.create_rectangle(x0, 28, x0 + 80, 46, fill=OFF, outline=ROSE)
            c.create_text(x0 + 40, 37, text=name, font=("Segoe UI", 8, "bold"), fill=TEXT)
        # Картинка геймпада (assets/gamepad.png), поверх неё — подсветка нажатого.
        ox, oy = 14, 54
        try:
            self.pad_img = tk.PhotoImage(file=str(asset("gamepad.png")))
            c.create_image(ox, oy, image=self.pad_img, anchor="nw")
        except tk.TclError:
            self.pad_img = None  # нет файла — останутся одни подсветки, это не страшно

        def oval(name, r, **kw):
            x, y, _ = GAMEPAD_SPOTS[name]
            return c.create_oval(ox + x - r, oy + y - r, ox + x + r, oy + y + r, state="hidden", **kw)

        for name in ABXY:  # нажата — круг её цвета с белой буквой
            x, y, r = GAMEPAD_SPOTS[name]
            it[name] = [oval(name, r + 1, fill=ABXY[name][0], outline=""),
                        c.create_text(ox + x, oy + y, text=name, fill="white", state="hidden",
                                      font=("Segoe UI", 10, "bold"))]
        for name in ("BACK", "START"):
            it[name] = [oval(name, 8, fill=CORAL, outline="")]
        for name in ("LS", "RS"):  # нажатие стика — коралловое кольцо
            it[name] = [oval(name, GAMEPAD_SPOTS[name][2] + 2, outline=CORAL, width=4)]
        dx, dy, dr = GAMEPAD_SPOTS["DPAD"]
        for name, (sx, sy) in (("DPAD_UP", (0, -1)), ("DPAD_DOWN", (0, 1)),
                               ("DPAD_LEFT", (-1, 0)), ("DPAD_RIGHT", (1, 0))):
            cx, cy = ox + dx + sx * dr * 0.6, oy + dy + sy * dr * 0.6
            it[name] = [c.create_rectangle(cx - 8, cy - 8, cx + 8, cy + 8, fill=CORAL, outline="",
                                           state="hidden")]
        # точка на левом стике едет вместе с рулём
        lx, ly, lr = GAMEPAD_SPOTS["LS"]
        self.stick_c = (ox + lx, oy + ly, lr - 8)
        it["stick"] = c.create_oval(0, 0, 0, 0, fill=PLUM, outline="white", width=2)
        self.pad_text = ttk.Label(f, style="Big.TLabel")
        self.pad_text.grid(row=1, column=0, pady=(4, 0))
        self.pad_btn_text = ttk.Label(f)
        self.pad_btn_text.grid(row=2, column=0)
        axes = []
        for key, word in (("steer", "руль"), ("gas", "газ"), ("brake", "тормоз"), ("clutch", "сцепление")):
            p = self.cfg.get(key)
            axes.append(f"{word} — ось {p['axis']}" if p else f"{word} — нет")
        ttk.Label(f, text="Оси из калибровки: " + ", ".join(axes) + ".\nПоменять их — кнопка "
                  "«Перекалибровать руль» на вкладке «Работа программы».", style="Hint.TLabel", justify="center"
                  ).grid(row=3, column=0, pady=(6, 0))

    def build_buttons_panel(self, body):
        f = ttk.LabelFrame(body, text=" Кнопка Xbox  ←  что нажать на руле ", padding=6)
        f.grid(row=0, column=2, sticky="nsew")
        texts = [t for t, _ in self.options]
        self.rows = {}
        for r, (name, wheel, fh4) in enumerate(self.core.XBOX_BUTTONS):
            lab = ttk.Label(f, text=xbox_name(name), style="Bold.TLabel", width=13)
            lab.grid(row=r, column=0, sticky="w")
            if name in XBOX_EXPLAIN:
                Tip(lab, f"{xbox_name(name)} — {XBOX_EXPLAIN[name]}")
            ttk.Label(f, text=fh4, foreground=MUTED, width=19).grid(row=r, column=1, sticky="w")
            cb = ttk.Combobox(f, state="readonly", values=texts, width=36)
            cb.grid(row=r, column=2, padx=2, pady=1)
            cb.bind("<<ComboboxSelected>>", lambda e, n=name: self.on_select(n))
            lamp = tk.Label(f, text="●", fg=OFF, bg=BG, font=("Segoe UI", 12))
            lamp.grid(row=r, column=3, padx=2)
            btn = ttk.Button(f, text="Нажать", width=8, command=lambda n=name: self.start_capture(n))
            btn.grid(row=r, column=4)
            if r == 0:
                Tip(btn, "Нажмите, потом нажмите на руле кнопку (или лепесток),\n"
                         "которая будет этой кнопкой Xbox. Esc — передумали.")
            self.rows[name] = (cb, lamp, btn)
        r = len(self.core.XBOX_BUTTONS)
        extra = ttk.Frame(f)
        extra.grid(row=r, column=0, columnspan=5, sticky="w", pady=(8, 0))
        self.rumble_var = tk.BooleanVar()
        ttk.Checkbutton(extra, text="Вибрация из игры на руль", variable=self.rumble_var,
                        command=self.on_rumble).grid(row=0, column=0, sticky="w")
        self.clutch_cb = None
        if self.cfg.get("clutch"):
            ttk.Label(extra, text="   Сцепление (педаль) → кнопка Xbox:").grid(row=0, column=1)
            self.clutch_cb = ttk.Combobox(extra, state="readonly", width=8,
                                          values=["—"] + list(self.core.XUSB_NAMES))
            self.clutch_cb.grid(row=0, column=2)
            self.clutch_cb.bind("<<ComboboxSelected>>", lambda e: self.on_clutch())

    def build_sliders(self, parent):
        f = ttk.LabelFrame(parent, text=" Чувствительность — меняется сразу, видно на геймпаде ",
                           padding=6)
        f.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self.sliders = {}
        for i, (key, text, lo, hi, help_) in enumerate(SLIDERS):
            cell = ttk.Frame(f)
            cell.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 18), pady=2)
            lab = ttk.Label(cell, text=text + "  ⓘ")
            lab.grid(row=0, column=0, columnspan=2, sticky="w")
            var = tk.DoubleVar()
            sc = ttk.Scale(cell, from_=lo, to=hi, variable=var, length=240,
                           command=lambda v, k=key: self.on_slide(k))
            sc.grid(row=1, column=0)
            Tip(lab, help_)
            Tip(sc, help_)
            val = ttk.Label(cell, width=5)
            val.grid(row=1, column=1, padx=(6, 0))
            self.sliders[key] = (var, val)

    def build_bottom(self, parent):
        f = ttk.Frame(parent, padding=(0, 8, 0, 0))
        f.grid(row=3, column=0, sticky="ew")
        f.columnconfigure(0, weight=1)
        self.status = ttk.Label(f, text="", foreground=PLUM)
        self.status.grid(row=0, column=0, sticky="w")
        buttons = (
            ("Авто (как геймпад)", lambda: self.auto("pad"), "TButton",
             "Каждая кнопка руля — той же кнопкой Xbox, один в один (как «Auto» у x360ce)."),
            ("Авто для FH4", lambda: self.auto("fh4"), "TButton",
             "Как на геймпаде, но передачи на лепестках:\nB (вверх) — лепесток R1, X (вниз) — лепесток L1."),
            ("Очистить", self.clear, "TButton", "Снять все кнопки, чтобы назначить с нуля."),
            ("Сбросить", self.reset, "TButton", "Вернуть то, что сохранено в последний раз."),
            ("Сохранить", self.save, "Accent.TButton",
             "Запомнить настройки — они будут и при следующем запуске."),
        )
        for i, (text, cmd, style, help_) in enumerate(buttons):
            b = ttk.Button(f, text=text, command=cmd, style=style)
            b.grid(row=0, column=i + 1, padx=2)
            Tip(b, help_)

    # ---------------------------------------------------------------- данные ↔ экран
    def refresh_all(self):
        """Переписывает списки, ползунки и галочки из self.cfg."""
        for name, (cb, _, _) in self.rows.items():
            srcs = self.cfg["buttons"].get(name) or []
            text = self.label_of(srcs[0]) if srcs else self.options[0][0]
            if len(srcs) > 1:
                text += f"  (+ ещё {len(srcs) - 1})"
            cb["values"] = [t for t, _ in self.options]
            cb.set(text)
        s = self.core.settings_of(self.cfg)
        for key, (var, val) in self.sliders.items():
            var.set(s[key])
            val.config(text=f"{s[key]:.2f}")
        self.rumble_var.set(bool(s["rumble"]))
        if self.clutch_cb is not None:
            self.clutch_cb.set(s.get("clutch_button") or "—")
        self.changed(refresh=False)

    def dirty(self):
        return self.cfg is not None and self.cfg != self.saved

    def changed(self, refresh=True):
        """Правка: отдать эмуляции (работает в игре сразу) и обновить подпись вкладки."""
        if refresh:
            self.refresh_all()
            return
        self.bridge.new_cfg = copy.deepcopy(self.cfg)
        self.app.nb.tab(self.frame, text="Настройка кнопок" + (" ●" if self.dirty() else ""))

    def set_sources(self, name, srcs):
        if srcs:
            self.cfg["buttons"][name] = srcs
        else:
            self.cfg["buttons"].pop(name, None)
        self.changed()

    def on_select(self, name):
        text = self.rows[name][0].get()
        if "(+ ещё" in text:
            return
        src = next((o for t, o in self.options if t == text), None)
        self.set_sources(name, [src] if src else [])
        self.say(f"{xbox_name(name)} ← {text}" + self.duplicate_note(name, src))

    def duplicate_note(self, name, src):
        if src is None:
            return ""
        others = [n for n, srcs in self.cfg["buttons"].items()
                  if n != name and any(_same(src, s) for s in srcs)]
        return f"   ! эта же кнопка руля назначена и на {', '.join(others)}" if others else ""

    def on_slide(self, key):
        var, val = self.sliders[key]
        v = round(var.get(), 2)
        val.config(text=f"{v:.2f}")
        self.cfg.setdefault("settings", {})[key] = v
        self.changed(refresh=False)

    def on_rumble(self):
        self.cfg.setdefault("settings", {})["rumble"] = bool(self.rumble_var.get())
        self.changed(refresh=False)

    def on_clutch(self):
        v = self.clutch_cb.get()
        self.cfg.setdefault("settings", {})["clutch_button"] = "" if v == "—" else v
        self.changed(refresh=False)

    def say(self, text, color=PLUM):
        if self.content is not None:
            self.status.config(text=text, foreground=color)

    # ---------------------------------------------------------------- «Нажать»
    def start_capture(self, name):
        self.cancel_capture()
        self.bridge.events.clear()
        busy = self.core.busy_axes(self.cfg)
        pedals = {p["axis"] for p in (self.cfg.get("gas"), self.cfg.get("brake"), self.cfg.get("clutch")) if p}
        base = self.core.read_axes(self.j)
        self.capture = (name, self.core.ControlDetector(self.j, base, busy, pedals))
        self.rows[name][2].config(text="Жду…")
        self.say(f"Нажмите на руле то, что будет кнопкой «{xbox_name(name)}». Esc — отмена.")

    def cancel_capture(self):
        if self.capture and self.content is not None:
            self.rows[self.capture[0]][2].config(text="Нажать")
            self.say("")
        self.capture = None

    def finish_capture(self, src):
        name = self.capture[0]
        self.rows[name][2].config(text="Нажать")
        self.capture = None
        self.set_sources(name, [src])
        self.say(f"{xbox_name(name)} ← {self.label_of(src)}" + self.duplicate_note(name, src))

    # ---------------------------------------------------------------- кнопки внизу
    def auto(self, preset):
        cfg = copy.deepcopy(self.cfg)
        # Раскладку SDL знает только рабочий поток (там pygame) — просим его.
        self.bridge.submit(lambda j: self.core.auto_buttons(j, cfg, preset),
                           lambda got: self.auto_done(preset, got))
        self.say("Авто: спрашиваю раскладку руля…")

    def auto_done(self, preset, got):
        if self.content is None:
            return
        if got is None or isinstance(got, Exception):
            messagebox.showinfo("wheel2pad", "Этот руль неизвестен SDL — автоматически не получится.\n"
                                "Назначьте кнопки вручную: «Нажать» и нажмите кнопку на руле.",
                                parent=self.app.root)
            self.say("")
            return
        self.cfg["buttons"] = got
        self.changed()
        self.say("Авто: кнопки как на геймпаде, один в один." if preset == "pad" else
                 "Авто для FH4: B (передача вверх) — лепесток R1, X (вниз) — лепесток L1.")

    def clear(self):
        self.cfg["buttons"] = {}
        self.changed()
        self.say("Все кнопки сняты. «Сбросить» — вернуть как было.")

    def reset(self):
        self.cfg = copy.deepcopy(self.saved)
        self.changed()
        self.say("Вернул сохранённые настройки.")

    def save(self):
        self.core.save_cfg(self.cfg)
        self.saved = copy.deepcopy(self.cfg)
        self.changed(refresh=False)
        self.say(f"Сохранено в {self.core.CONFIG_PATH.name}.", GREEN)

    def confirm_unsaved(self, action):
        """Перед закрытием/перекалибровкой: есть несохранённые правки — спросить."""
        if not self.dirty():
            return True
        ans = messagebox.askyesnocancel(
            "wheel2pad", f"{action}: на вкладке «Настройка кнопок» есть несохранённые изменения.\n\n"
                         "Сохранить их?", parent=self.app.root)
        if ans is None:
            return False
        if ans:
            self.save()
        return True

    # ---------------------------------------------------------------- живые значения
    def redraw(self):
        snap = self.bridge.snap
        if self.content is None or snap is None:
            return
        axes, buttons, hats = snap["axes"], snap["buttons"], snap["hats"]
        for a, cv, bar, val in self.axis_rows:
            v = axes[a] if a < len(axes) else 0.0
            if self.rest[a] < -0.5:      # курок/педаль: от левого края
                cv.coords(bar, 0, 0, (v + 1) / 2 * 170, 14)
            else:                         # руль и прочие: от середины
                x = 85 + v * 85
                cv.coords(bar, min(85, x), 0, max(85, x), 14)
            val.config(text=f"{v:+.2f}")
        for b, lamp in enumerate(self.btn_lamps):
            on = b < len(buttons) and buttons[b]
            lamp.config(bg=ON if on else OFF, fg="white" if on else TEXT)
        arrows = {(0, 1): "↑", (0, -1): "↓", (-1, 0): "←", (1, 0): "→",
                  (1, 1): "↗", (-1, 1): "↖", (1, -1): "↘", (-1, -1): "↙"}
        for h, lab in enumerate(self.hat_labels):
            v = tuple(hats[h]) if h < len(hats) else (0, 0)
            lab.config(text=f"Крестовина {h}: {arrows.get(v, '—')}")

        x, rt, lt, want = snap["out"]
        c, it = self.pad_canvas, self.pad_items
        sx, sy, travel = self.stick_c
        c.coords(it["stick"], sx + x * travel - 8, sy - 8, sx + x * travel + 8, sy + 8)
        for name, val, x0 in (("LT", lt, 34), ("RT", rt, 236)):
            c.coords(it[name], x0 + 1, 5, x0 + 1 + 89 * val, 22)
            c.itemconfig(it[name + "_t"], text=f"{name} {val:.0%}")
        for name in ("LB", "RB"):
            c.itemconfig(it[name], fill=ON if name in want else OFF)
        for name in ("A", "B", "X", "Y", "BACK", "START", "LS", "RS",
                     "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT"):
            for item in it[name]:
                c.itemconfig(item, state="normal" if name in want else "hidden")
        self.pad_text.config(text=f"Руль {x:+.2f}    Газ {rt:4.0%}    Тормоз {lt:4.0%}")
        self.pad_btn_text.config(text="Кнопки: " + (" ".join(sorted(want)) or "—"))
        for name, (_, lamp, _) in self.rows.items():
            lamp.config(fg=ON if name in want else OFF)
