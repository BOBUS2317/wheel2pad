"""
wheel2pad — окно кнопок (в духе x360ce).

  слева   — что приходит с руля: оси полосками и числами, кнопки лампочками
            (с названиями со схемы Artplays V-1200: L1, R1, SHARE…);
  в центре — что получает игра: нарисованный геймпад Xbox 360 и числа;
  справа  — какая кнопка руля заменяет каждую кнопку Xbox: выпадающий список
            или «Нажать» → нажать нужное на руле;
  внизу   — чувствительность (ползунки действуют сразу) и кнопки
            «Авто», «Авто для FH4», «Очистить», «Сбросить», «Сохранить».

В шапке крупно написано, в каком режиме руль (X-Input / D-Input).

Модуль не импортирует wheel2pad сам: ядро передаётся параметром core. Иначе
при запуске wheel2pad.py как скрипта получилась бы вторая копия модуля со своим
фоновым потоком ввода. Всё, что видит игра, считает core.compute_outputs —
та же функция, что и в эмуляции, поэтому окно не может «врать».
"""
import copy
import tkinter as tk
from tkinter import messagebox, ttk

import pygame

TICK_MS = 10        # опрос руля (и отправка в виртуальный геймпад, если эмуляция идёт)
REDRAW_EVERY = 3    # перерисовка — на каждом 3-м опросе, ~30 кадров в секунду

ON, OFF = "#2ecc71", "#d6d6d6"
ABXY = {"A": ("#2fa84f", "#cdebd5"), "B": ("#e0393e", "#f6cfd0"),
        "X": ("#2f6fd8", "#cddcf6"), "Y": ("#e8b100", "#f8ebbf")}
MODE_BANNERS = {
    "X-Input": ("#dff3e3", "Режим руля: X-Input — обычный, включается сам при каждом подключении USB. "
                          "Вибрация есть; лепестки L2/R2 дублируют тормоз и газ."),
    "D-Input": ("#fff1c2", "Режим руля: D-Input — все 4 лепестка кнопками, но нет вибрации, а газ и "
                          "тормоз на одной оси. После переподключения USB руль вернётся в X-Input."),
}

# (ключ в settings, подпись, минимум, максимум)
SLIDERS = [
    ("steer_range", "Ход руля до полного стика", 0.1, 1.0),
    ("steer_deadzone", "Мёртвая зона руля в центре", 0.0, 0.2),
    ("steer_anti_deadzone", "Компенсация мёртвой зоны игры", 0.0, 0.3),
    ("steer_gamma", "Кривая руля (больше 1 — мягче в центре)", 0.5, 2.5),
    ("pedal_deadzone", "Мёртвая зона педалей (отпущено = 0 %)", 0.0, 0.2),
    ("pedal_top_deadzone", "Запас педалей до 100 %", 0.0, 0.2),
]

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


class Editor:
    def __init__(self, core, j, cfg, on_tick=None):
        self.core, self.j, self.on_tick = core, j, on_tick
        self.saved = copy.deepcopy(cfg)   # то, что лежит в wheel2pad.json
        self.cfg = copy.deepcopy(cfg)     # то, что сейчас на экране (и уже работает в эмуляции)
        self.result, self.removed = self.saved, False
        self.labels = core.WHEEL_LABELS.get(j.get_name(), NO_LABELS)
        self.capture = None               # (кнопка Xbox, ControlDetector) — ждём нажатия на руле
        self.ticks = 0

        pygame.event.pump()
        # Где у каждой оси «покой»: у руля/педалей — из калибровки, у остальных —
        # по текущему положению (окно открывают, ничего не нажимая).
        self.rest = [-1.0 if v < -0.5 else 0.0 for v in self.core.read_axes(j)]
        for key, field in (("steer", "center"), ("gas", "rest"), ("brake", "rest"), ("clutch", "rest")):
            p = cfg.get(key)
            if p and p["axis"] < len(self.rest):
                self.rest[p["axis"]] = p[field]

        try:  # чёткий текст на мониторах с масштабом больше 100 %
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
        self.root = tk.Tk()
        self.root.title("wheel2pad — кнопки и чувствительность")
        self.set_icon()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Escape>", lambda e: self.cancel_capture())
        self.style = ttk.Style(self.root)
        self.style.configure("Bold.TLabel", font=("Segoe UI", 9, "bold"))
        self.style.configure("Big.TLabel", font=("Segoe UI", 12, "bold"))

        self.build_options()
        self.build_header()
        body = ttk.Frame(self.root, padding=(8, 4))
        body.grid(row=1, column=0, sticky="nsew")
        self.build_wheel_panel(body)
        self.build_pad_panel(body)
        self.build_buttons_panel(body)
        self.build_sliders()
        self.build_bottom()
        self.refresh_all()

        # Окно должно оказаться поверх консоли, а не потеряться за ней.
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()
        self.root.after(TICK_MS, self.tick)

    # ------------------------------------------------------------------ сборка окна
    def set_icon(self):
        """Иконка программы вместо пера Tk. В exe её кладёт сборка (--add-data
        assets\\wheel2pad.ico) во временную папку sys._MEIPASS, без exe — рядом со скриптом."""
        import sys
        from pathlib import Path
        root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        try:
            self.root.iconbitmap(default=str(root / "assets" / "wheel2pad.ico"))
        except tk.TclError:
            pass  # нет файла — останется стандартная иконка, это не страшно

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

    def build_header(self):
        top = ttk.Frame(self.root, padding=(8, 8, 8, 0))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        ttk.Label(top, text=f"Руль: «{self.j.get_name()}»", style="Big.TLabel").grid(row=0, column=0, sticky="w")
        self.banner = tk.Label(top, anchor="w", justify="left", padx=8, pady=4, wraplength=1000)
        self.banner.grid(row=1, column=0, sticky="ew", pady=(4, 0))

    def set_banner(self, removed=False):
        if removed:
            self.banner.config(bg="#f8d7da", text="Руль отключён. Если зажимали MODE — это смена режима: "
                               "закройте окно, программа подскажет, что делать дальше.")
            return
        mode = self.core.wheel_mode(self.j.get_name())
        bg, text = MODE_BANNERS.get(mode, ("#e8eef7", "Это не Artplays V-1200 — названия кнопок руля "
                                                      "неизвестны, показаны номера."))
        self.banner.config(bg=bg, text=text)

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
            c = tk.Canvas(f, width=170, height=14, bg="white", highlightthickness=1, highlightbackground="#bbb")
            c.grid(row=r + 1, column=0, sticky="w")
            bar = c.create_rectangle(0, 0, 0, 14, fill="#5b9bd5", width=0)
            if self.rest[a] > -0.5:
                c.create_line(85, 0, 85, 14, fill="#999")
            val = ttk.Label(f, width=6, anchor="e")
            val.grid(row=r + 1, column=1, sticky="e", padx=(4, 0))
            self.axis_rows.append((a, c, bar, val))
            r += 2
        ttk.Label(f, text="Кнопки", style="Bold.TLabel").grid(row=r, column=0, sticky="w", pady=(8, 2))
        grid = ttk.Frame(f)
        grid.grid(row=r + 1, column=0, columnspan=2, sticky="w")
        self.btn_lamps = []
        for b in range(self.j.get_numbuttons()):
            name = self.labels["buttons"].get(b, "")
            short = SHORT.get(name, name.replace("лепесток ", "").replace(" — не назначать", ""))
            lamp = tk.Label(grid, text=f"{b}" + (f" · {short}" if short else ""), bg=OFF, width=14,
                            anchor="w", padx=4, relief="flat")
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
        c = self.pad_canvas = tk.Canvas(f, width=360, height=250, bg=self.root.cget("bg"), highlightthickness=0)
        c.grid(row=0, column=0)
        body_pts = [70, 62, 290, 62, 330, 92, 348, 182, 322, 236, 272, 222, 238, 186,
                    122, 186, 88, 222, 38, 236, 12, 182, 30, 92]
        c.create_polygon(body_pts, smooth=True, fill="#efefef", outline="#a8a8a8", width=2)
        it = self.pad_items = {}
        # курки (полоски) и плечи
        for name, x0 in (("LT", 40), ("RT", 230)):
            c.create_rectangle(x0, 6, x0 + 90, 22, outline="#888")
            it[name] = c.create_rectangle(x0, 6, x0, 22, fill="#e67e22", width=0)
            it[name + "_t"] = c.create_text(x0 + 45, 14, text=f"{name} 0%", font=("Segoe UI", 8, "bold"))
        for name, x0 in (("LB", 50), ("RB", 230)):
            it[name] = c.create_rectangle(x0, 32, x0 + 80, 50, fill=OFF, outline="#888")
            c.create_text(x0 + 40, 41, text=name, font=("Segoe UI", 8, "bold"))
        # стики
        it["LS"] = c.create_oval(65, 76, 125, 136, fill="#dcdcdc", outline="#777", width=2)
        it["stick"] = c.create_oval(85, 96, 105, 116, fill="#555", outline="")
        it["RS"] = c.create_oval(199, 134, 251, 186, fill="#dcdcdc", outline="#777", width=2)
        c.create_oval(217, 152, 233, 168, fill="#777", outline="")
        # крестовина
        for name, box in (("DPAD_UP", (122, 136, 138, 152)), ("DPAD_DOWN", (122, 168, 138, 184)),
                          ("DPAD_LEFT", (106, 152, 122, 168)), ("DPAD_RIGHT", (138, 152, 154, 168))):
            it[name] = c.create_rectangle(*box, fill=OFF, outline="#777")
        # A B X Y
        for name, (x, y) in (("A", (265, 128)), ("B", (290, 104)), ("X", (240, 104)), ("Y", (265, 80))):
            it[name] = c.create_oval(x - 12, y - 12, x + 12, y + 12, fill=ABXY[name][1], outline="#777")
            c.create_text(x, y, text=name, font=("Segoe UI", 9, "bold"), fill="#333")
        # BACK / START и «логотип»
        for name, x in (("BACK", 158), ("START", 202)):
            it[name] = c.create_oval(x - 9, 98, x + 9, 112, fill=OFF, outline="#777")
        c.create_text(158, 122, text="BACK", font=("Segoe UI", 7))
        c.create_text(202, 122, text="START", font=("Segoe UI", 7))
        c.create_oval(168, 66, 192, 90, fill="#cfd8cf", outline="#777")
        self.pad_text = ttk.Label(f, style="Big.TLabel")
        self.pad_text.grid(row=1, column=0, pady=(4, 0))
        self.pad_btn_text = ttk.Label(f)
        self.pad_btn_text.grid(row=2, column=0)
        axes = []
        for key, word in (("steer", "руль"), ("gas", "газ"), ("brake", "тормоз"), ("clutch", "сцепление")):
            p = self.cfg.get(key)
            axes.append(f"{word} — ось {p['axis']}" if p else f"{word} — нет")
        ttk.Label(f, text="Оси из калибровки: " + ", ".join(axes) + ".\nПоменять их — команда R "
                  "в окне программы (перекалибровка).", foreground="#666", justify="center"
                  ).grid(row=3, column=0, pady=(6, 0))

    def build_buttons_panel(self, body):
        f = ttk.LabelFrame(body, text=" Кнопка Xbox  ←  что нажать на руле ", padding=6)
        f.grid(row=0, column=2, sticky="nsew")
        texts = [t for t, _ in self.options]
        self.rows = {}
        for r, (name, wheel, fh4) in enumerate(self.core.XBOX_BUTTONS):
            ttk.Label(f, text=name, style="Bold.TLabel", width=11).grid(row=r, column=0, sticky="w")
            ttk.Label(f, text=fh4, foreground="#777", width=19).grid(row=r, column=1, sticky="w")
            cb = ttk.Combobox(f, state="readonly", values=texts, width=36)
            cb.grid(row=r, column=2, padx=2, pady=1)
            cb.bind("<<ComboboxSelected>>", lambda e, n=name: self.on_select(n))
            lamp = tk.Label(f, text="●", fg=OFF, font=("Segoe UI", 12))
            lamp.grid(row=r, column=3, padx=2)
            btn = ttk.Button(f, text="Нажать", width=8, command=lambda n=name: self.start_capture(n))
            btn.grid(row=r, column=4)
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

    def build_sliders(self):
        f = ttk.LabelFrame(self.root, text=" Чувствительность — меняется сразу, видно на геймпаде ",
                           padding=6)
        f.grid(row=2, column=0, sticky="ew", padx=8)
        self.sliders = {}
        for i, (key, text, lo, hi) in enumerate(SLIDERS):
            cell = ttk.Frame(f)
            cell.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 18), pady=2)
            ttk.Label(cell, text=text).grid(row=0, column=0, columnspan=2, sticky="w")
            var = tk.DoubleVar()
            sc = ttk.Scale(cell, from_=lo, to=hi, variable=var, length=240,
                           command=lambda v, k=key: self.on_slide(k))
            sc.grid(row=1, column=0)
            val = ttk.Label(cell, width=5)
            val.grid(row=1, column=1, padx=(6, 0))
            self.sliders[key] = (var, val)

    def build_bottom(self):
        f = ttk.Frame(self.root, padding=8)
        f.grid(row=3, column=0, sticky="ew")
        f.columnconfigure(0, weight=1)
        self.status = ttk.Label(f, text="", foreground="#1a5fb4")
        self.status.grid(row=0, column=0, sticky="w")
        for i, (text, cmd) in enumerate((("Авто (как геймпад)", lambda: self.auto("pad")),
                                         ("Авто для FH4", lambda: self.auto("fh4")),
                                         ("Очистить", self.clear), ("Сбросить", self.reset),
                                         ("Сохранить", self.save), ("Закрыть", self.close))):
            ttk.Button(f, text=text, command=cmd).grid(row=0, column=i + 1, padx=2)

    # ------------------------------------------------------------------ данные ↔ экран
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
        self.set_banner(self.removed)
        self.update_title()

    def update_title(self):
        dirty = self.cfg != self.saved
        self.root.title(("● " if dirty else "") + "wheel2pad — кнопки и чувствительность"
                        + (" (не сохранено)" if dirty else ""))

    def set_sources(self, name, srcs):
        if srcs:
            self.cfg["buttons"][name] = srcs
        else:
            self.cfg["buttons"].pop(name, None)
        self.refresh_all()

    def on_select(self, name):
        text = self.rows[name][0].get()
        if "(+ ещё" in text:
            return
        src = next((o for t, o in self.options if t == text), None)
        self.set_sources(name, [src] if src else [])
        self.say(f"{name} ← {text}" + self.duplicate_note(name, src))

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
        self.update_title()

    def on_rumble(self):
        self.cfg.setdefault("settings", {})["rumble"] = bool(self.rumble_var.get())
        self.update_title()

    def on_clutch(self):
        v = self.clutch_cb.get()
        self.cfg.setdefault("settings", {})["clutch_button"] = "" if v == "—" else v
        self.update_title()

    def say(self, text, color="#1a5fb4"):
        self.status.config(text=text, foreground=color)

    # ------------------------------------------------------------------ «Нажать»
    def start_capture(self, name):
        if self.removed:
            return
        self.cancel_capture()
        pygame.event.clear()
        busy = self.core.busy_axes(self.cfg)
        pedals = {p["axis"] for p in (self.cfg.get("gas"), self.cfg.get("brake"), self.cfg.get("clutch")) if p}
        base = self.core.read_axes(self.j)
        self.capture = (name, self.core.ControlDetector(self.j, base, busy, pedals))
        self.rows[name][2].config(text="Жду…")
        self.say(f"Нажмите на руле то, что будет кнопкой «{name}». Esc — отмена.")

    def cancel_capture(self):
        if self.capture:
            self.rows[self.capture[0]][2].config(text="Нажать")
            self.capture = None
            self.say("")

    def finish_capture(self, src):
        name = self.capture[0]
        self.rows[name][2].config(text="Нажать")
        self.capture = None
        self.set_sources(name, [src])
        self.say(f"{name} ← {self.label_of(src)}" + self.duplicate_note(name, src))

    # ------------------------------------------------------------------ кнопки внизу
    def auto(self, preset):
        got = self.core.auto_buttons(self.j, self.cfg, preset)
        if got is None:
            messagebox.showinfo("wheel2pad", "Этот руль неизвестен SDL — автоматически не получится.\n"
                                "Назначьте кнопки вручную: «Нажать» и нажмите кнопку на руле.",
                                parent=self.root)
            return
        self.cfg["buttons"] = got
        self.refresh_all()
        self.say("Авто: кнопки как на геймпаде, один в один." if preset == "pad" else
                 "Авто для FH4: B (передача вверх) — лепесток R1, X (вниз) — лепесток L1.")

    def clear(self):
        self.cfg["buttons"] = {}
        self.refresh_all()
        self.say("Все кнопки сняты. «Сбросить» — вернуть как было.")

    def reset(self):
        self.cfg = copy.deepcopy(self.saved)
        self.refresh_all()
        self.say("Вернул сохранённые настройки.")

    def save(self):
        self.core.save_cfg(self.cfg)
        self.saved = copy.deepcopy(self.cfg)
        self.update_title()
        self.say(f"Сохранено в {self.core.CONFIG_PATH.name}.", "#26a269")

    def close(self):
        if self.cfg != self.saved:
            ans = messagebox.askyesnocancel("wheel2pad", "Сохранить изменения?", parent=self.root)
            if ans is None:
                return
            if ans:
                self.save()
        self.result = copy.deepcopy(self.saved)
        self.root.destroy()

    # ------------------------------------------------------------------ опрос руля
    def tick(self):
        try:
            self._tick()
        finally:
            try:
                self.root.after(TICK_MS, self.tick)
            except tk.TclError:
                pass  # окно уже закрыли

    def _tick(self):
        if self.removed:
            pygame.event.get()
            return
        for e in pygame.event.get():
            if e.type == pygame.JOYDEVICEREMOVED and e.instance_id == self.j.get_instance_id():
                self.removed = True
                self.cancel_capture()
                self.set_banner(removed=True)
                return
            if self.capture:
                src = self.capture[1].feed(e)
                if src:
                    self.finish_capture(src)
        if self.capture:
            det = self.capture[1]
            if det.pedal_hit:
                det.pedal_hit = False
                if self.core.wheel_mode(self.j.get_name()) == "X-Input":
                    self.say("Это газ или тормоз, а не кнопка. Лепесток L2/R2 в режиме X-Input работает "
                             "как педаль — возьмите L1/R1 или другую кнопку. Esc — отмена.", "#c01c28")
                else:
                    self.say("Это педаль, а не кнопка — нажмите кнопку или лепесток. Esc — отмена.",
                             "#c01c28")
            src = det.poll()
            if src:
                self.finish_capture(src)

        s = self.core.settings_of(self.cfg)
        out = self.core.compute_outputs(self.j, self.cfg, s)
        if self.on_tick:
            self.on_tick(out)
        self.ticks += 1
        if self.ticks % REDRAW_EVERY == 0:
            self.redraw(out)

    def redraw(self, out):
        j, c, it = self.j, self.pad_canvas, self.pad_items
        for a, canvas, bar, val in self.axis_rows:
            v = self.core.safe_axis(j, a)
            if self.rest[a] < -0.5:      # курок/педаль: от левого края
                canvas.coords(bar, 0, 0, (v + 1) / 2 * 170, 14)
            else:                         # руль и прочие: от середины
                x = 85 + v * 85
                canvas.coords(bar, min(85, x), 0, max(85, x), 14)
            val.config(text=f"{v:+.2f}")
        for b, lamp in enumerate(self.btn_lamps):
            lamp.config(bg=ON if j.get_button(b) else OFF)
        arrows = {(0, 1): "↑", (0, -1): "↓", (-1, 0): "←", (1, 0): "→",
                  (1, 1): "↗", (-1, 1): "↖", (1, -1): "↘", (-1, -1): "↙"}
        for h, lab in enumerate(self.hat_labels):
            v = j.get_hat(h)
            lab.config(text=f"Крестовина {h}: {arrows.get(v, '—')}")

        x, rt, lt, want = out
        c.coords(it["stick"], 85 + x * 22, 96, 105 + x * 22, 116)
        for name, val, x0 in (("LT", lt, 40), ("RT", rt, 230)):
            c.coords(it[name], x0, 6, x0 + 90 * val, 22)
            c.itemconfig(it[name + "_t"], text=f"{name} {val:.0%}")
        for name in ("LB", "RB", "BACK", "START", "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT"):
            c.itemconfig(it[name], fill=ON if name in want else OFF)
        for name in ("LS", "RS"):
            c.itemconfig(it[name], fill=ON if name in want else "#dcdcdc")
        for name in ABXY:
            c.itemconfig(it[name], fill=ABXY[name][0 if name in want else 1])
        self.pad_text.config(text=f"Руль {x:+.2f}    Газ {rt:4.0%}    Тормоз {lt:4.0%}")
        self.pad_btn_text.config(text="Кнопки: " + (" ".join(sorted(want)) or "—"))
        for name, (_, lamp, _) in self.rows.items():
            lamp.config(fg=ON if name in want else OFF)


def edit(core, j, cfg, on_tick=None):
    """Открывает окно и ждёт, пока его закроют. Вернёт (сохранённый конфиг, руль_отключили)."""
    ed = Editor(core, j, cfg, on_tick)
    ed.root.mainloop()
    return ed.result, ed.removed
