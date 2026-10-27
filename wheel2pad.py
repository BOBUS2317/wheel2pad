#!/usr/bin/env python3
"""
wheel2pad — превращает руль (Artplays V-1200 Vibro и подобные) в виртуальный
геймпад Xbox 360, чтобы игры вроде Forza Horizon 4 работали со стандартной
раскладкой геймпада.

  Руль            -> левый стик (ось X)
  Газ             -> правый курок (RT)
  Тормоз          -> левый курок (LT)
  Сцепление       -> кнопка (по умолчанию LB, можно отключить)
  Кнопки/лепестки -> кнопки Xbox, как назначите при калибровке
  Вибрация из игры -> вибромотор руля (если руль это поддерживает)

Запуск:
  python wheel2pad.py --calibrate   калибровка и назначение кнопок
  python wheel2pad.py               эмуляция (если конфига нет — сначала калибровка)
  python wheel2pad.py --show        эмуляция + строка с текущими значениями
  python wheel2pad.py --monitor     просто показать «сырые» оси/кнопки руля
  python wheel2pad.py --list        список устройств

Во время работы в окно можно ввести команду и нажать Enter:
  R — перенастроить руль, H — путь для HidHide (копируется в буфер обмена).
Поэтому пользователю хватает одного wheel2pad.exe — без дополнительных .bat.
"""
import argparse
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

# Руль надо читать, даже когда фокус в игре, а не в консоли.
os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame  # noqa: E402

FROZEN = getattr(sys, "frozen", False)  # True, когда запущено из wheel2pad.exe
# Настройки храним рядом с exe (или рядом со скриптом), а не во временной папке.
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "wheel2pad.json"


def real_exe():
    """Путь к реально работающему python.exe (его и нужно добавить в HidHide).

    В виртуальном окружении Windows .venv\\Scripts\\python.exe лишь запускает
    «настоящий» Python, поэтому sys.executable тут не подходит.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(32768)
            if ctypes.windll.kernel32.GetModuleFileNameW(None, buf, len(buf)):
                return buf.value
        except Exception:  # noqa: BLE001
            pass
    return sys.executable


def copy_to_clipboard(text):
    """Кладёт текст в буфер обмена Windows. Возвращает True, если получилось.

    clip.exe правильно понимает только UTF-16 с BOM: в другой кодировке
    кириллица в пути (например, в имени пользователя) превратится в «кракозябры».
    """
    if sys.platform != "win32":
        return False
    import subprocess
    try:
        subprocess.run(["clip"], input=("﻿" + text).encode("utf-16-le"), check=False)
        return True
    except Exception:  # noqa: BLE001
        return False


def hidhide_hint():
    """Путь, который нужно разрешить в HidHide; заодно копирует его в буфер,
    чтобы в окне выбора файла HidHide хватило Ctrl+V и Enter."""
    path = real_exe()
    copied = copy_to_clipboard(path)
    return ("  • если установлен HidHide — в его списке «Applications» должен быть файл:\n"
            f"      {path}\n"
            + ("    (путь уже скопирован: в HidHide нажмите «+», затем Ctrl+V и Enter)"
               if copied else ""))


def not_found_hint():
    return ("\nПроверьте:\n"
            "  • руль подключён по USB (попробуйте другой порт);\n"
            + hidhide_hint())


# --------------------------------------------------------------------------
# Драйвер ViGEmBus (виртуальный геймпад)
# --------------------------------------------------------------------------
def find_vigembus_installer():
    import importlib.util
    arch = "x64" if sys.maxsize > 2 ** 32 else "x86"
    rel = Path("vgamepad", "win", "vigem", "install", arch, f"ViGEmBusSetup_{arch}.msi")
    roots = []
    if FROZEN and hasattr(sys, "_MEIPASS"):
        roots.append(Path(sys._MEIPASS))
    try:
        spec = importlib.util.find_spec("vgamepad")
        if spec and spec.submodule_search_locations:
            roots.append(Path(list(spec.submodule_search_locations)[0]).parent)
    except Exception:  # noqa: BLE001
        pass
    for root in roots:
        if (root / rel).exists():
            return root / rel
    return None


def offer_vigembus_install(err):
    print("\nНе удалось создать виртуальный геймпад.")
    print(f"(техническая причина: {err})")
    print("\nСкорее всего, не установлен драйвер ViGEmBus — он нужен один раз.")
    msi = find_vigembus_installer() if sys.platform == "win32" else None
    if msi is None:
        sys.exit("Скачайте и установите его вручную:\n"
                 "  https://github.com/nefarius/ViGEmBus/releases\n"
                 "Потом перезагрузите компьютер и запустите программу снова.")
    wait_enter("Нажмите Enter — откроется установщик. В нём жмите Next / Install… ")
    import subprocess
    subprocess.call(["msiexec", "/i", str(msi)])
    sys.exit("\nЕсли установка прошла — перезагрузите компьютер и запустите программу снова.")

# (имя кнопки Xbox, подсказка для Forza Horizon 4 со стандартной раскладкой)
XBOX_BUTTONS = [
    ("A", "в FH4: ручник"),
    ("B", "в FH4: передача вверх — удобно на правый лепесток"),
    ("X", "в FH4: передача вниз — удобно на левый лепесток"),
    ("Y", "в FH4: перемотка (Rewind)"),
    ("LB", ""),
    ("RB", ""),
    ("BACK", "кнопка View / Back"),
    ("START", "кнопка Menu / Start (пауза)"),
    ("LS", "нажатие левого стика"),
    ("RS", "нажатие правого стика"),
    ("DPAD_UP", "крестовина вверх"),
    ("DPAD_DOWN", "крестовина вниз"),
    ("DPAD_LEFT", "крестовина влево"),
    ("DPAD_RIGHT", "крестовина вправо"),
]

XUSB_NAMES = {
    "A": "XUSB_GAMEPAD_A", "B": "XUSB_GAMEPAD_B",
    "X": "XUSB_GAMEPAD_X", "Y": "XUSB_GAMEPAD_Y",
    "LB": "XUSB_GAMEPAD_LEFT_SHOULDER", "RB": "XUSB_GAMEPAD_RIGHT_SHOULDER",
    "BACK": "XUSB_GAMEPAD_BACK", "START": "XUSB_GAMEPAD_START",
    "LS": "XUSB_GAMEPAD_LEFT_THUMB", "RS": "XUSB_GAMEPAD_RIGHT_THUMB",
    "DPAD_UP": "XUSB_GAMEPAD_DPAD_UP", "DPAD_DOWN": "XUSB_GAMEPAD_DPAD_DOWN",
    "DPAD_LEFT": "XUSB_GAMEPAD_DPAD_LEFT", "DPAD_RIGHT": "XUSB_GAMEPAD_DPAD_RIGHT",
    "GUIDE": "XUSB_GAMEPAD_GUIDE",
}

DEFAULT_SETTINGS = {
    # Какая доля хода руля (от центра до упора) даёт полный стик.
    # 1.0 — полный стик только на упоре. При режиме 900° попробуйте 0.3–0.5.
    "steer_range": 1.0,
    "steer_deadzone": 0.02,        # мёртвая зона в центре руля (0..1)
    "steer_anti_deadzone": 0.0,    # компенсация мёртвой зоны игры (0..0.3)
    "steer_gamma": 1.0,            # >1 — мягче в центре, <1 — острее
    "pedal_deadzone": 0.04,        # чтобы педаль в покое давала ровно 0
    "pedal_top_deadzone": 0.03,    # чтобы на упоре гарантированно было 100 %
    "clutch_button": "LB",         # куда отправлять сцепление; "" — никуда
    "clutch_threshold": 0.5,
    "rumble": True,                # передавать вибрацию из игры на руль
    "rate_hz": 500,
}


# --------------------------------------------------------------------------
# Ввод с клавиатуры без блокировки опроса руля
# --------------------------------------------------------------------------
_lines: "queue.Queue[str]" = queue.Queue()


def _stdin_reader():
    while True:
        try:
            _lines.put(input())
        except EOFError:
            _lines.put("")
            time.sleep(0.2)


threading.Thread(target=_stdin_reader, daemon=True).start()


def _drain_lines():
    while not _lines.empty():
        try:
            _lines.get_nowait()
        except queue.Empty:
            break


def wait_enter(prompt, on_tick=None):
    """Ждёт Enter, всё это время опрашивая руль и вызывая on_tick()."""
    _drain_lines()
    print(prompt, end="", flush=True)
    while True:
        if pygame.get_init():
            pygame.event.pump()
        if on_tick:
            on_tick()
        try:
            _lines.get_nowait()
            return
        except queue.Empty:
            time.sleep(0.004)


# --------------------------------------------------------------------------
# Команды во время работы (вместо отдельных .bat-файлов)
# --------------------------------------------------------------------------
RECALIBRATE = "recalibrate"

COMMANDS_HELP = ("Команды: щёлкните по этому окну, введите букву и нажмите Enter.\n"
                 "  R — перенастроить руль\n"
                 "  H — путь для HidHide (сразу копируется в буфер обмена)")

# Вторая буква в каждой паре — та же клавиша в русской раскладке
# (R → «к», H → «р»), чтобы не приходилось переключать язык.
_CMD_RECALIBRATE = {"r", "к"}
_CMD_HIDHIDE = {"h", "р"}


def poll_command():
    """Проверяет, не ввели ли команду, не останавливая опрос руля.

    Возвращает RECALIBRATE, если просят перенастроить руль. Путь для HidHide
    печатает сама, а на любой другой ввод (в том числе пустой Enter)
    напоминает список команд.
    """
    try:
        line = _lines.get_nowait()
    except queue.Empty:
        return None
    cmd = line.strip().lower()
    if cmd in _CMD_RECALIBRATE:
        return RECALIBRATE
    if cmd in _CMD_HIDHIDE:
        path = real_exe()
        copied = copy_to_clipboard(path)
        print(f"\nДобавьте в HidHide (вкладка «Applications»):\n  {path}")
        if copied:
            print("Путь уже скопирован: в HidHide нажмите «+», затем Ctrl+V и Enter.")
        return None
    print("\n" + COMMANDS_HELP)
    return None


# --------------------------------------------------------------------------
# Устройства
# --------------------------------------------------------------------------
def init_sdl():
    pygame.init()
    pygame.joystick.init()


def all_joysticks():
    return [pygame.joystick.Joystick(i) for i in range(pygame.joystick.get_count())]


def describe(j):
    return (f"«{j.get_name()}»  осей: {j.get_numaxes()}, кнопок: {j.get_numbuttons()}, "
            f"крестовин: {j.get_numhats()}")


def list_devices():
    joys = all_joysticks()
    if not joys:
        print("Устройства не найдены. Руль подключён? (и не скрыт ли он HidHide от python.exe?)")
    for i, j in enumerate(joys):
        print(f"[{i}] {describe(j)}  guid={j.get_guid()}")


def find_device(cfg):
    joys = all_joysticks()
    dev = (cfg or {}).get("device", {})
    for key in ("guid", "name"):
        want = dev.get(key)
        if not want:
            continue
        for j in joys:
            got = j.get_guid() if key == "guid" else j.get_name()
            if got == want:
                return j
    return None


def choose_device(prefer=None):
    """Выбирает руль для калибровки.

    prefer — прежний конфиг: если тот же руль подключён, берём его без вопросов.
    Это важно при перенастройке командой R: в этот момент в системе ещё может
    числиться наш собственный виртуальный геймпад, и без этого пришлось бы
    выбирать из списка.
    """
    j = find_device(prefer) if prefer else None
    if j is not None:
        print("Руль:", describe(j))
        return j
    joys = all_joysticks()
    if not joys:
        # Не выходим: в режиме одного exe проще дождаться руля, чем объяснять,
        # что программу надо перезапустить.
        print("Не найдено ни одного игрового устройства. Жду подключения руля…" + not_found_hint())
        while not joys:
            pygame.event.get()  # SDL обновляет список устройств при обработке событий
            time.sleep(0.3)
            joys = all_joysticks()
    if len(joys) == 1:
        print("Найдено устройство:", describe(joys[0]))
        return joys[0]
    print("Найдено несколько устройств:")
    for i, j in enumerate(joys):
        print(f"  [{i}] {describe(j)}")
    while True:
        _drain_lines()
        print("Номер руля: ", end="", flush=True)
        s = _lines.get().strip()
        if s.isdigit() and int(s) < len(joys):
            return joys[int(s)]


def read_axes(j):
    return [j.get_axis(i) for i in range(j.get_numaxes())]


# --------------------------------------------------------------------------
# Калибровка
# --------------------------------------------------------------------------
def sample_rest(j, secs=0.8):
    n, acc = 0, [0.0] * j.get_numaxes()
    end = time.time() + secs
    while time.time() < end:
        pygame.event.pump()
        for i, v in enumerate(read_axes(j)):
            acc[i] += v
        n += 1
        time.sleep(0.005)
    return [a / max(n, 1) for a in acc]


def capture_extremes(j, base, prompt, opposite=None):
    """Запоминает для каждой оси значение, дальше всего ушедшее от покоя.

    opposite — уже снятые крайние значения: тогда засчитывается только движение
    в другую сторону от покоя. Иначе при повороте «от левого упора к правому»
    левый упор (он тоже ±1.0) так и остался бы самым дальним.
    """
    ext = list(base)

    def tick():
        for i, v in enumerate(read_axes(j)):
            if opposite is not None and (v - base[i]) * (opposite[i] - base[i]) > 0:
                continue
            if abs(v - base[i]) > abs(ext[i] - base[i]):
                ext[i] = v

    wait_enter(prompt, tick)
    return ext


def best_axis(base, ext, exclude=(), min_move=0.25):
    cands = [(abs(ext[i] - base[i]), i) for i in range(len(base)) if i not in exclude]
    if not cands:
        return None
    move, idx = max(cands)
    return idx if move >= min_move else None


def capture_control(j, prompt):
    """Ждёт нажатия кнопки/крестовины руля. Enter — пропустить."""
    _drain_lines()
    pygame.event.clear()
    print(prompt, end="", flush=True)
    me = j.get_instance_id()
    while True:
        try:
            _lines.get_nowait()
            print("  — пропущено")
            return None
        except queue.Empty:
            pass
        for e in pygame.event.get():
            if getattr(e, "instance_id", None) != me:
                continue
            if e.type == pygame.JOYBUTTONDOWN:
                print(f"  → кнопка руля #{e.button}")
                return {"type": "button", "index": e.button}
            if e.type == pygame.JOYHATMOTION and e.value != (0, 0):
                print(f"  → крестовина #{e.hat} {e.value}")
                return {"type": "hat", "index": e.hat, "value": list(e.value)}
        time.sleep(0.004)


def wait_all_released(j):
    while True:
        pygame.event.pump()
        pressed = any(j.get_button(b) for b in range(j.get_numbuttons()))
        hats = any(j.get_hat(h) != (0, 0) for h in range(j.get_numhats()))
        if not pressed and not hats:
            pygame.event.clear()
            return
        time.sleep(0.01)


def calibrate(old_cfg=None):
    j = choose_device(prefer=old_cfg)
    j.init()
    if j.get_numaxes() < 2:
        sys.exit("У устройства меньше двух осей — похоже, выбран не руль.")

    print("\n=== Калибровка ===")
    print("Советы: переключите руль в режим 270° (если есть), педали подключены.")

    wait_enter("\n1) Отпустите педали, руль поставьте ровно по центру и нажмите Enter… ")
    base = sample_rest(j)

    left = capture_extremes(j, base, "2) Поверните руль ДО УПОРА ВЛЕВО и, не отпуская, нажмите Enter… ")
    right = capture_extremes(j, base, "3) Поверните руль ДО УПОРА ВПРАВО и, не отпуская, нажмите Enter… ",
                             opposite=left)
    score = [abs(left[i] - base[i]) + abs(right[i] - base[i]) for i in range(len(base))]
    s_axis = max(range(len(score)), key=score.__getitem__)
    if score[s_axis] < 0.5:
        sys.exit("Не увидел поворота руля. Запустите калибровку ещё раз.")
    if min(abs(left[s_axis] - base[s_axis]), abs(right[s_axis] - base[s_axis])) < 0.2:
        sys.exit("Руль повернули только в одну сторону. Запустите калибровку ещё раз "
                 "и поверните его до упора и влево, и вправо.")
    steer = {"axis": s_axis, "center": base[s_axis], "left": left[s_axis], "right": right[s_axis]}
    print(f"  руль: ось #{s_axis}")
    wait_enter("   Верните руль в центр и нажмите Enter… ")

    def pedal(name, prompt, exclude, required=True):
        while True:
            ext = capture_extremes(j, base, prompt)
            ax = best_axis(base, ext, exclude)
            if ax is not None:
                print(f"  {name}: ось #{ax}")
                return {"axis": ax, "rest": base[ax], "full": ext[ax]}
            if not required:
                print(f"  {name}: не используется")
                return None
            print("  Не увидел движения педали, попробуйте ещё раз.")

    gas = pedal("газ", "4) Выжмите ГАЗ до упора, отпустите и нажмите Enter… ", {s_axis})
    brake = pedal("тормоз", "5) Выжмите ТОРМОЗ до упора, отпустите и нажмите Enter… ", {s_axis})
    if brake["axis"] == gas["axis"]:
        if (brake["full"] - brake["rest"]) * (gas["full"] - gas["rest"]) > 0:
            sys.exit("Газ и тормоз дали одинаковый сигнал — похоже, оба раза нажималась одна педаль. "
                     "Запустите калибровку заново.")
        print("  (газ и тормоз на одной оси — одновременно нажимать их не получится, это ограничение руля)")
    clutch = pedal("сцепление",
                   "6) Если есть педаль СЦЕПЛЕНИЯ — выжмите её и отпустите. Enter (без нажатия — пропустить)… ",
                   {s_axis, gas["axis"], brake["axis"]}, required=False)

    print("\n=== Кнопки ===")
    print("Для каждой кнопки Xbox нажмите кнопку руля, которая будет её заменять.")
    print("Enter — пропустить. Кнопку MODE на руле не назначайте (она переключает режимы руля).\n")
    buttons = {}
    for name, hint in XBOX_BUTTONS:
        wait_all_released(j)
        src = capture_control(j, f"  {name:<10} {('(' + hint + ')') if hint else '':<55}")
        if src:
            for other, lst in buttons.items():
                if src in lst:
                    print(f"     ! эта же кнопка уже назначена на {other}")
            buttons[name] = [src]

    settings = dict(DEFAULT_SETTINGS)
    if old_cfg:
        settings.update(old_cfg.get("settings", {}))

    cfg = {
        "device": {"name": j.get_name(), "guid": j.get_guid()},
        "steer": steer,
        "gas": gas,
        "brake": brake,
        "clutch": clutch,
        "buttons": buttons,
        "settings": settings,
    }
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nГотово, настройки сохранены в {CONFIG_PATH.name}")
    return cfg


# --------------------------------------------------------------------------
# Преобразование значений
# --------------------------------------------------------------------------
def clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def steer_value(st, v, s):
    c = st["center"]
    d, r, l = v - c, st["right"] - c, st["left"] - c
    if d * r > 0:
        x = d / r
    elif d * l > 0:
        x = -d / l
    else:
        x = 0.0
    x = clamp(x / max(s["steer_range"], 0.05), -1.0, 1.0)
    m = abs(x)
    dz = s["steer_deadzone"]
    if m <= dz:
        return 0.0
    m = (m - dz) / (1 - dz)
    m = m ** s["steer_gamma"]
    ad = s["steer_anti_deadzone"]
    m = ad + (1 - ad) * m
    return clamp(m, 0.0, 1.0) * (1 if x > 0 else -1)


def pedal_value(p, v, s):
    span = p["full"] - p["rest"]
    if span == 0:
        return 0.0
    x = clamp((v - p["rest"]) / span, 0.0, 1.0)
    dz, top = s["pedal_deadzone"], s["pedal_top_deadzone"]
    if x <= dz:
        return 0.0
    return clamp((x - dz) / max(1 - dz - top, 0.05), 0.0, 1.0)


def source_pressed(j, src):
    try:
        if src["type"] == "button":
            return bool(j.get_button(src["index"]))
        if src["type"] == "hat":
            hx, hy = j.get_hat(src["index"])
            wx, wy = src["value"]
            # совпадение по направлению: диагонали тоже засчитываются
            return (wx == 0 or hx == wx) and (wy == 0 or hy == wy) and (hx, hy) != (0, 0)
    except pygame.error:
        pass
    return False


def safe_axis(j, idx):
    return j.get_axis(idx) if idx < j.get_numaxes() else 0.0


# --------------------------------------------------------------------------
# Эмуляция
# --------------------------------------------------------------------------
def wait_for_wheel(cfg):
    """Ждёт, пока появится руль из конфига. Вернёт руль или RECALIBRATE.

    Руль может не находиться по двум причинам: его ещё не подключили
    (тогда просто ждём) или у него сменился GUID — подключили другой руль
    или переключили режим D-Input/X-Input. Во втором случае поможет только
    перенастройка, поэтому команды R/H работают и во время ожидания.
    """
    print("Руль не найден, жду подключения…" + not_found_hint() +
          "\n  • если подключили другой руль или переключили его режим — введите R и нажмите Enter.")
    while True:
        pygame.event.get()  # без этого SDL не узнает о новом устройстве
        if poll_command() == RECALIBRATE:
            return RECALIBRATE
        j = find_device(cfg)
        if j is not None:
            print("Руль найден.")
            return j
        time.sleep(0.2)


def run(cfg, show=False):
    """Эмуляция. Возвращает RECALIBRATE, если пользователь попросил перенастройку."""
    s = dict(DEFAULT_SETTINGS)
    s.update(cfg.get("settings", {}))

    # Руль ищем ДО создания виртуального геймпада: если понадобится
    # перенастройка, в списке устройств не будет лишнего «Xbox 360 Controller».
    j = find_device(cfg)
    if j is None:
        j = wait_for_wheel(cfg)
        if j == RECALIBRATE:
            return RECALIBRATE
    j.init()

    try:
        import vgamepad as vg
        pad = vg.VX360Gamepad()
    except ImportError as e:
        if not FROZEN and "vgamepad" in str(e):
            sys.exit(f"Не установлен пакет vgamepad: {e}\nЗапустите start.bat — он всё поставит.")
        offer_vigembus_install(e)
    except Exception as e:  # noqa: BLE001  (vgamepad падает, если нет драйвера)
        offer_vigembus_install(e)
    joy_id = j.get_instance_id()

    xbtn = {name: getattr(vg.XUSB_BUTTON, XUSB_NAMES[name]) for name in XUSB_NAMES}

    rumble = {"large": 0, "small": 0, "dirty": False}

    def on_notify(client, target, large_motor, small_motor, led_number, user_data):
        rumble["large"], rumble["small"], rumble["dirty"] = large_motor, small_motor, True

    if s["rumble"]:
        try:
            pad.register_notification(callback_function=on_notify)
        except Exception as e:  # noqa: BLE001
            print("Вибрация недоступна:", e)

    print(f"Эмуляция запущена: {j.get_name()} → виртуальный Xbox 360.")
    print("Запускайте игру. Окно можно свернуть; остановить — закрыть окно или Ctrl+C.")
    print(COMMANDS_HELP)

    period = 1.0 / max(int(s["rate_hz"]), 30)
    last_show = last_rumble = 0.0
    rumble_warned = False
    held = set()
    result = None

    try:
        while True:
            t0 = time.perf_counter()

            if poll_command() == RECALIBRATE:
                print("\nПерехожу к перенастройке руля.")
                result = RECALIBRATE
                break

            for e in pygame.event.get():
                if e.type == pygame.JOYDEVICEREMOVED and e.instance_id == joy_id:
                    print("\nРуль отключён, жду повторного подключения…")
                    j = None
                elif e.type == pygame.JOYDEVICEADDED and j is None:
                    cand = find_device(cfg)
                    if cand is not None:
                        j = cand
                        j.init()
                        joy_id = j.get_instance_id()
                        print("Руль снова подключён.")

            if j is None:
                pad.reset()
                pad.update()
                held.clear()
                time.sleep(0.2)
                continue

            x = steer_value(cfg["steer"], safe_axis(j, cfg["steer"]["axis"]), s)
            rt = pedal_value(cfg["gas"], safe_axis(j, cfg["gas"]["axis"]), s)
            lt = pedal_value(cfg["brake"], safe_axis(j, cfg["brake"]["axis"]), s)

            want = {name for name, srcs in cfg["buttons"].items()
                    if name in xbtn and any(source_pressed(j, src) for src in srcs)}
            if cfg.get("clutch") and s.get("clutch_button") in xbtn:
                c = pedal_value(cfg["clutch"], safe_axis(j, cfg["clutch"]["axis"]), s)
                if c >= s["clutch_threshold"]:
                    want.add(s["clutch_button"])

            pad.left_joystick_float(x_value_float=x, y_value_float=0.0)
            pad.right_trigger_float(value_float=rt)
            pad.left_trigger_float(value_float=lt)
            for name in want - held:
                pad.press_button(button=xbtn[name])
            for name in held - want:
                pad.release_button(button=xbtn[name])
            held = want
            pad.update()

            now = time.perf_counter()
            if s["rumble"] and (rumble["dirty"] or now - last_rumble > 0.8):
                big, small = rumble["large"], rumble["small"]
                rumble["dirty"] = False
                last_rumble = now
                try:
                    if big or small:
                        ok = j.rumble(big / 255, small / 255, 1000)
                        if not ok and not rumble_warned:
                            rumble_warned = True
                            print("\n(Руль не принимает вибрацию в этом режиме — продолжаю без неё.)")
                    else:
                        j.stop_rumble()
                except Exception:  # noqa: BLE001
                    pass

            if show and now - last_show > 0.1:
                last_show = now
                print(f"\rРуль {x:+.2f}  Газ {rt:4.0%}  Тормоз {lt:4.0%}  "
                      f"Кнопки: {' '.join(sorted(want)) or '-':<30}", end="", flush=True)

            dt = time.perf_counter() - t0
            if dt < period:
                time.sleep(period - dt)
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        try:
            pad.reset()
            pad.update()
            if j is not None:
                j.stop_rumble()
        except Exception:  # noqa: BLE001
            pass
    # После выхода из функции объект pad удаляется, и vgamepad сам отключает
    # виртуальный геймпад — на время калибровки его в системе не будет.
    return result


def monitor():
    j = choose_device()
    j.init()
    print("Двигайте руль/педали, жмите кнопки. Ctrl+C — выход.")
    try:
        while True:
            pygame.event.pump()
            axes = " ".join(f"{i}:{v:+.2f}" for i, v in enumerate(read_axes(j)))
            btns = " ".join(str(b) for b in range(j.get_numbuttons()) if j.get_button(b)) or "-"
            hats = " ".join(str(j.get_hat(h)) for h in range(j.get_numhats()))
            print(f"\rОси {axes} | Кнопки {btns:<12} | Крестовина {hats}   ", end="", flush=True)
            time.sleep(0.03)
    except KeyboardInterrupt:
        print()


def load_cfg():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return None


def main():
    ap = argparse.ArgumentParser(description="Руль → виртуальный геймпад Xbox 360")
    ap.add_argument("--calibrate", action="store_true", help="калибровка и назначение кнопок")
    ap.add_argument("--show", action="store_true", help="показывать значения во время эмуляции")
    ap.add_argument("--monitor", action="store_true", help="показать сырые данные руля")
    ap.add_argument("--list", action="store_true", help="список устройств")
    ap.add_argument("--where", action="store_true", help="путь к python.exe для HidHide")
    args = ap.parse_args()

    if args.where:
        path = real_exe()
        print(path)
        copy_to_clipboard(path)
        return

    init_sdl()
    if args.list:
        list_devices()
        return
    if args.monitor:
        monitor()
        return

    cfg = load_cfg()
    need_calibration = args.calibrate or cfg is None
    # Цикл нужен для команды R: калибровка → эмуляция → (R) → калибровка → …
    # без перезапуска программы.
    while True:
        if need_calibration:
            cfg = calibrate(cfg)
            wait_enter("\nНажмите Enter, чтобы сразу запустить эмуляцию (или закройте окно)… ")
        if run(cfg, show=args.show) != RECALIBRATE:
            return
        need_calibration = True


if __name__ == "__main__":
    exit_code = 0
    try:
        main()
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code)
            exit_code = 1
        else:
            exit_code = e.code or 0
    except KeyboardInterrupt:
        # Ctrl+C посреди калибровки: KeyboardInterrupt не наследуется от Exception,
        # поэтому без этой ветки пользователь увидел бы страшный трейсбек,
        # а окно exe закрылось бы сразу, не дав ничего прочитать.
        print("\nОстановлено.")
        exit_code = 1
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("\nЧто-то пошло не так. Сфотографируйте это окно и отправьте разработчику.")
        exit_code = 1
    if FROZEN:
        # exe запускают двойным щелчком — не даём окну закрыться, пока не прочитали.
        wait_enter("\nНажмите Enter, чтобы закрыть окно… ")
    sys.exit(exit_code)
