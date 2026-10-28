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
  python wheel2pad.py               окно программы (wheel2pad_gui.py): вкладка
                                    «Работа» — калибровка и эмуляция, вкладка
                                    «Настройка кнопок» — переназначение и живой просмотр
  python wheel2pad.py --calibrate   то же, но сначала заново откалибровать руль
  python wheel2pad.py --console     по-старому, в консоли (только через Python)
  python wheel2pad.py --monitor     просто показать «сырые» оси/кнопки руля
  python wheel2pad.py --list        список устройств

Работа с рулём (калибровка и эмуляция) идёт в фоновом потоке, окно — в главном;
связывает их Bridge. Пользователю хватает одного wheel2pad.exe — без .bat и консоли.
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

VERSION = "1.2"
FROZEN = getattr(sys, "frozen", False)  # True, когда запущено из wheel2pad.exe
# Настройки храним рядом с exe (или рядом со скриптом), а не во временной папке.
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent


def pick_config_path():
    """Где лежит wheel2pad.json. Обычно — рядом с exe (как x360ce.ini у x360ce).
    Но если exe положили туда, куда Windows не даёт писать (Program Files и т. п.),
    настройки не сохранились бы — тогда берём %LOCALAPPDATA%\\wheel2pad."""
    near = APP_DIR / "wheel2pad.json"
    alt = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "wheel2pad" / "wheel2pad.json"
    if near.exists():
        return near
    if alt.exists():
        return alt
    try:
        probe = APP_DIR / ".wheel2pad_write_test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return near
    except OSError:
        return alt


CONFIG_PATH = pick_config_path()


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
        # CREATE_NO_WINDOW: у оконного exe нет своей консоли, и без этого флага
        # на экране мелькало бы чёрное окно clip.exe.
        subprocess.run(["clip"], input=("﻿" + text).encode("utf-16-le"), check=False,
                       creationflags=0x08000000)
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


# Если на Artplays V-1200 удержать MODE 3 секунды, руль переключается в D-Input
# и для программы «пропадает» (другие имя и GUID). Проверено на живом руле:
# после переподключения USB он всегда снова в X-Input — это и есть лечение.
MODE_HINT = ("  • если на руле зажимали кнопку MODE — выдерните USB руля и вставьте обратно\n"
             "    (руль вернётся в обычный режим X-Input);\n")
# А если калибровали в D-Input, то после переподключения USB руль как раз
# в X-Input — тогда наоборот, нужно зажать MODE.
DINPUT_HINT = ("  • настройки сделаны для режима D-Input, а руль после подключения USB всегда\n"
               "    в X-Input — зажмите на руле MODE на 3 секунды (или {r}\n"
               "    и откалибруйте руль в обычном режиме);\n")


def r_hint():
    """Как попросить перекалибровку: в окне — кнопкой, в консоли — буквой R."""
    return "нажмите «Перекалибровать руль»" if BRIDGE else "введите R и нажмите Enter"


def mode_hint(cfg):
    dev = (cfg or {}).get("device", {})
    return DINPUT_HINT.format(r=r_hint()) if dev.get("name") == ARTPLAYS_DINPUT else MODE_HINT


def not_found_hint(cfg=None):
    return ("\nПроверьте:\n"
            "  • руль подключён по USB (попробуйте другой порт);\n"
            + mode_hint(cfg)
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

# (имя кнопки Xbox, что нажать на руле Artplays V-1200, что это в Forza Horizon 4).
# Названия кнопок руля — как на схеме в его инструкции: у руля нет «View» и
# «Menu», зато есть SHARE и OPTIONS, а лепестки подписаны L1/L2/R1/R2.
# Пустая строка «на руле» — кнопка не нужна, мастер предложит нажать Enter.
XBOX_BUTTONS = [
    ("A", "A", "ручник"),
    ("B", "лепесток R1", "передача вверх"),
    ("X", "лепесток L1", "передача вниз"),
    ("Y", "Y", "перемотка (Rewind)"),
    ("LB", "", "сцепление"),
    ("RB", "", ""),
    ("BACK", "SHARE", ""),
    ("START", "OPTIONS", "пауза, меню"),
    ("LS", "", ""),
    ("RS", "", ""),
    ("DPAD_UP", "крестовина вверх", ""),
    ("DPAD_DOWN", "крестовина вниз", ""),
    ("DPAD_LEFT", "крестовина влево", ""),
    ("DPAD_RIGHT", "крестовина вправо", ""),
]


def button_prompt(name, wheel, fh4):
    """Строка мастера: «B  → на руле: лепесток R1   (в FH4: передача вверх)»."""
    left = f"на руле: {wheel}" if wheel else "не нужна — Enter"
    right = f"(в FH4: {fh4})" if fh4 else ""
    return f"  {name:<10} {left:<26} {right:<28}"

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


def start_stdin_reader():
    """Только для консольного режима. В окне ответы приходят из поля ввода
    вкладки «Работа», а у оконного exe клавиатуры-консоли нет вовсе:
    input() падал бы, и каждые 0,2 с в очередь сыпались бы пустые «Enter»."""
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
EDIT = "edit"

COMMANDS_HELP = ("Команды: щёлкните по этому окну, введите букву и нажмите Enter.\n"
                 "  R — перенастроить руль\n"
                 "  H — путь для HidHide (сразу копируется в буфер обмена)")
GUI_HELP = ("Кнопки внизу: «Перекалибровать руль», «Путь для HidHide». Кнопки руля и\n"
            "чувствительность — на вкладке «Настройка кнопок».")


def commands_help():
    return GUI_HELP if BRIDGE else COMMANDS_HELP


# --------------------------------------------------------------------------
# Связь с окном программы
# --------------------------------------------------------------------------
class Bridge:
    """Связь рабочего потока (руль, калибровка, эмуляция) с окном (tkinter).

    tkinter можно трогать только из главного потока, а pygame удобнее держать
    целиком в одном рабочем — поэтому они не зовут друг друга напрямую:
      • рабочий поток публикует снимок руля (оси, кнопки, что ушло в игру),
        события руля и своё состояние; окно читает их по таймеру;
      • окно кладёт правки конфига в new_cfg (эмуляция подхватывает их сразу),
        а то, что требует самого руля (раскладка SDL для «Авто»), — в submit().
    """

    def __init__(self):
        import collections
        self.snap = None              # снимок руля или None, если руля нет
        self.events = collections.deque(maxlen=256)  # события руля для «Нажать»
        self.cfg = None               # конфиг эмуляции (копия для окна)
        self.cfg_version = 0          # растёт после каждой калибровки
        self.new_cfg = None           # правки из окна — применить в эмуляции
        self.state = ("start", "Запуск…")
        self.wanted = None            # какой руль ждём (имя из конфига)
        self.other_mode = None        # подключён тот же Artplays, но в другом режиме
        self.show_settings = False    # окну: переключиться на вкладку настройки
        self.stop = threading.Event()  # окно закрывают — эмуляции пора закругляться
        self._requests = queue.Queue()
        self.results = queue.Queue()  # (callback, результат) — вызвать в главном потоке
        self._last_pub = 0.0

    # ---- рабочий поток
    def set_state(self, code, text):
        self.state = (code, text)

    def set_cfg(self, cfg):
        import copy
        self.cfg = copy.deepcopy(cfg)
        self.cfg_version += 1

    def take_new_cfg(self):
        cfg, self.new_cfg = self.new_cfg, None
        return cfg

    def publish(self, j, out):
        now = time.perf_counter()
        if now - self._last_pub < 0.005:  # 200 раз в секунду окну за глаза
            return
        self._last_pub = now
        self.snap = {
            "name": j.get_name(), "instance_id": j.get_instance_id(),
            "axes": read_axes(j),
            "buttons": [j.get_button(b) for b in range(j.get_numbuttons())],
            "hats": [j.get_hat(h) for h in range(j.get_numhats())],
            "out": out,
        }

    def serve(self, j):
        while True:
            try:
                fn, cb = self._requests.get_nowait()
            except queue.Empty:
                return
            try:
                res = fn(j)
            except Exception as e:  # noqa: BLE001
                res = e
            self.results.put((cb, res))

    # ---- главный поток (окно)
    def submit(self, fn, cb):
        """Выполнить fn(руль) в рабочем потоке, потом cb(результат) — в окне."""
        self._requests.put((fn, cb))


BRIDGE = None  # Bridge, когда работает окно; None — консольный режим и тесты

# Вторая буква в каждой паре — та же клавиша в русской раскладке
# (R → «к», H → «р», S → «ы»), чтобы не приходилось переключать язык.
_CMD_RECALIBRATE = {"r", "к"}
_CMD_HIDHIDE = {"h", "р"}
_CMD_EDIT = {"s", "ы"}


def poll_command():
    """Проверяет, не ввели ли команду, не останавливая опрос руля.

    Возвращает RECALIBRATE, если просят перенастроить руль, и EDIT — если
    просят окно кнопок. Путь для HidHide печатает сама, а на любой другой ввод
    (в том числе пустой Enter) напоминает список команд.
    """
    try:
        line = _lines.get_nowait()
    except queue.Empty:
        return None
    cmd = line.strip().lower()
    if cmd in _CMD_RECALIBRATE:
        return RECALIBRATE
    if cmd in _CMD_EDIT:
        return EDIT
    if cmd in _CMD_HIDHIDE:
        path = real_exe()
        copied = copy_to_clipboard(path)
        print(f"\nДобавьте в HidHide (вкладка «Applications»):\n  {path}")
        if copied:
            print("Путь уже скопирован: в HidHide нажмите «+», затем Ctrl+V и Enter.")
        return None
    print("\n" + commands_help())
    return None


# --------------------------------------------------------------------------
# Устройства
# --------------------------------------------------------------------------
def init_sdl():
    pygame.init()
    pygame.joystick.init()


def all_joysticks():
    # В pygame-ce устройство открывается уже при создании Joystick(i).
    # Вызывать j.init() не нужно: с версии 2.4 он только печатает
    # DeprecationWarning, который пугает пользователя в окне программы.
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


# Сколько ждать настоящую кнопку после того, как дёрнулась «свободная» ось.
# У Artplays V-1200 в D-Input каждая кнопка ещё и двигает свою ось силы нажатия
# (оси #6–#15, как у геймпада PS3) — кнопка должна победить эту «тень».
AXIS_BUTTON_GRACE = 0.15


def capture_control(j, prompt, base=None, ignore=(), pedals=()):
    """Ждёт нажатия кнопки/крестовины руля. Enter — пропустить.

    base — значения осей в покое. Если оно передано, кнопкой может стать и ось:
    у Artplays V-1200 в режиме D-Input лепестки L2/R2 приходят осями #4/#5
    (рывком от −1 до +1), а не кнопками. Оси из ignore (руль, педали) кнопкой
    не становятся.

    pedals — оси педалей. Если вместо кнопки нажали педаль или лепесток,
    объясняем, почему ничего не назначилось: в режиме X-Input лепестки L2/R2 —
    это те же оси, что тормоз и газ, и отличить их от педалей нельзя.
    """
    _drain_lines()
    pygame.event.clear()
    print(prompt, end="", flush=True)
    det = ControlDetector(j, base, ignore, pedals)
    warned = False
    while True:
        try:
            _lines.get_nowait()
            print("  — пропущено")
            return None
        except queue.Empty:
            pass
        for e in pygame.event.get():
            src = det.feed(e)
            if src:
                print(f"  → {describe_source(src)}")
                return src
        if det.pedal_hit and not warned:
            warned = True
            print("\n     ! это сработал газ или тормоз, а не кнопка. Если вы нажали лепесток —\n"
                  "       этот лепесток работает как педаль, кнопкой его не назначить.\n"
                  "       Нажмите лепесток из другой пары, другую кнопку или Enter (пропустить): ",
                  end="", flush=True)
        src = det.poll()
        if src:
            print(f"  → {describe_source(src)}")
            return src
        time.sleep(0.004)


class ControlDetector:
    """Ловит первое нажатие на руле: кнопку, крестовину или ось-кнопку.

    Общая логика мастера калибровки (capture_control) и кнопки «Нажать»
    в окне кнопок. События pygame передаются в feed(), а poll() надо звать
    регулярно: ось засчитывается кнопкой не сразу, а если за
    AXIS_BUTTON_GRACE не пришла настоящая кнопка.
    """

    def __init__(self, j, base=None, ignore=(), pedals=()):
        self.j, self.me = j, j.get_instance_id()
        self.base, self.ignore, self.pedals = base, ignore, pedals
        self.cand = None        # (ось, когда сработала) — ждём, не придёт ли настоящая кнопка
        self.pedal_hit = False  # нажали педаль (или лепесток-педаль X-Input) вместо кнопки

    def feed(self, e):
        if getattr(e, "instance_id", None) != self.me:
            return None
        if e.type == pygame.JOYBUTTONDOWN:
            return {"type": "button", "index": e.button}
        if e.type == pygame.JOYHATMOTION and e.value != (0, 0):
            return {"type": "hat", "index": e.hat, "value": list(e.value)}
        base = self.base
        if e.type != pygame.JOYAXISMOTION or base is None or e.axis >= len(base):
            return None
        if abs(e.value - base[e.axis]) <= 0.5:
            return None
        if e.axis in self.pedals:
            self.pedal_hit = True
        elif e.axis not in self.ignore and self.cand is None:
            self.cand = (e.axis, time.time())
        return None

    def poll(self):
        if not self.cand or time.time() - self.cand[1] <= AXIS_BUTTON_GRACE:
            return None
        ax, self.cand = self.cand[0], None
        # «Полное» значение берём уже после паузы — лепесток к этому времени
        # дожат до конца; «нажато» — дальше середины между покоем и им.
        full = self.j.get_axis(ax)
        if abs(full - self.base[ax]) > 0.5:
            return {"type": "axis", "index": ax, "rest": self.base[ax], "full": full}
        return None  # отпустили раньше, чем успели засчитать


def describe_source(src):
    if src["type"] == "button":
        return f"кнопка руля #{src['index']}"
    if src["type"] == "hat":
        return f"крестовина #{src['index']} {tuple(src['value'])}"
    return f"ось руля #{src['index']} (как кнопка)"


def same_source(a, b):
    """Одна и та же кнопка руля? У осей rest/full могут чуть отличаться."""
    return (a["type"], a["index"], a.get("value")) == (b["type"], b["index"], b.get("value"))


def wait_all_released(j, base=None, ignore=()):
    """Ждёт, пока отпустят все кнопки, крестовины и оси-кнопки (лепестки)."""
    while True:
        pygame.event.pump()
        pressed = any(j.get_button(b) for b in range(j.get_numbuttons()))
        hats = any(j.get_hat(h) != (0, 0) for h in range(j.get_numhats()))
        axes = base is not None and any(
            abs(v - base[i]) > 0.5 for i, v in enumerate(read_axes(j))
            if i < len(base) and i not in ignore)
        if not pressed and not hats and not axes:
            pygame.event.clear()
            return
        time.sleep(0.01)


# Имена Artplays V-1200 в двух режимах (проверено на живом руле).
ARTPLAYS_XINPUT = "Controller (Wired Wheel)"
ARTPLAYS_DINPUT = "Wired Wheel"

MODE_NOTES = {
    ARTPLAYS_XINPUT: (
        "Руль Artplays V-1200 сейчас в обычном режиме X-Input — он включается сам\n"
        "при каждом подключении USB. Работает вибрация, газ и тормоз раздельные.\n"
        "Лепестки L2/R2 в этом режиме дублируют тормоз и газ, для передач — L1/R1.\n"
        "\n"
        "Есть второй режим — D-Input: в нём все 4 лепестка становятся кнопками, НО\n"
        "  • нет вибрации;\n"
        "  • газ и тормоз на одной оси — если нажать оба, они гасят друг друга;\n"
        "  • после каждого переподключения USB руль снова будет в X-Input, и MODE\n"
        "    придётся зажимать заново, а калибровка для D-Input нужна отдельная.\n"
        "Чтобы перейти в D-Input — прямо сейчас зажмите MODE на 3 секунды."),
    ARTPLAYS_DINPUT: (
        "Руль Artplays V-1200 сейчас в режиме D-Input: все 4 лепестка — кнопки,\n"
        "но нет вибрации, а газ и тормоз на одной оси (вместе они гасят друг друга).\n"
        "После переподключения USB руль сам вернётся в обычный режим X-Input,\n"
        "и эта калибровка работать не будет — понадобится калибровка для X-Input.\n"
        "Чтобы вернуться в X-Input — выдерните USB руля и вставьте обратно."),
}


def offer_mode_choice(j):
    """Перед калибровкой Artplays объясняет режимы X-Input / D-Input.

    Пока ждём Enter, следим за рулём: если его переключили (MODE 3 с или
    переподключение USB), Windows отключает старое устройство и подключает
    новое — тогда берём руль в новом режиме и рассказываем уже про него.
    Для других рулей ничего не делает.
    """
    while j.get_name() in MODE_NOTES:
        print("\n" + MODE_NOTES[j.get_name()])
        _drain_lines()
        print("\nEnter — калибровать в этом режиме… ", end="", flush=True)
        me, switched = j.get_instance_id(), False
        while not switched:
            try:
                _lines.get_nowait()
                return j
            except queue.Empty:
                pass
            for e in pygame.event.get():
                if e.type == pygame.JOYDEVICEREMOVED and e.instance_id == me:
                    switched = True
            time.sleep(0.02)
        print("\nРуль переключается, жду его в новом режиме…")
        end = time.time() + 15
        new = None
        while new is None and time.time() < end:
            pygame.event.get()  # без этого SDL не увидит новое устройство
            new = next((c for c in all_joysticks() if c.get_name() in MODE_NOTES), None)
            time.sleep(0.2)
        if new is None:
            return choose_device()  # не дождались — пусть пользователь выберет сам
        print("Руль:", describe(new))
        j = new
    return j


# --------------------------------------------------------------------------
# Названия органов руля и авто-раскладки (для окна кнопок)
# --------------------------------------------------------------------------
# Как называются кнопки и оси Artplays V-1200 на схеме из его инструкции —
# по номерам, которые записаны с живого руля в обоих режимах. hidden_axes —
# оси, которых физически нет (стоят на месте) или которые лишь повторяют
# кнопки (в D-Input оси #6–#15 — «сила нажатия» кнопок, как у PS3).
WHEEL_LABELS = {
    ARTPLAYS_XINPUT: {
        "buttons": {0: "A", 1: "B", 2: "X", 3: "Y", 4: "лепесток L1", 5: "лепесток R1",
                    6: "SHARE", 7: "OPTIONS", 10: "MODE — не назначать"},
        "axes": {0: "руль", 4: "тормоз + лепесток L2", 5: "газ + лепесток R2"},
        "hidden_axes": {1, 2, 3},
    },
    ARTPLAYS_DINPUT: {
        "buttons": {0: "A", 1: "B", 2: "X", 3: "Y", 4: "SHARE", 5: "MODE — не назначать",
                    6: "OPTIONS", 9: "лепесток L1", 10: "лепесток R1", 11: "крестовина вверх",
                    12: "крестовина вниз", 13: "крестовина влево", 14: "крестовина вправо"},
        "axes": {0: "руль", 1: "педали (тормоз − / газ +)", 4: "лепесток L2", 5: "лепесток R2"},
        "hidden_axes": {2, 3} | set(range(6, 16)),
    },
}

def wheel_mode(name):
    """Режим Artplays по имени устройства: "X-Input", "D-Input" или None (другой руль)."""
    return {ARTPLAYS_XINPUT: "X-Input", ARTPLAYS_DINPUT: "D-Input"}.get(name)


def device_title(j):
    """«Controller (Wired Wheel) (режим X-Input)» — чтобы всегда было видно режим."""
    mode = wheel_mode(j.get_name())
    return j.get_name() + (f" (режим {mode})" if mode else "")


# Кнопка Xbox → кнопка стандартного геймпада SDL. «Как геймпад» — один в один,
# как делает x360ce; «для FH4» — передачи (B/X) на лепестки L1/R1, как советует README.
AUTO_PAD = {"A": "a", "B": "b", "X": "x", "Y": "y", "LB": "leftshoulder",
            "RB": "rightshoulder", "BACK": "back", "START": "start",
            "LS": "leftstick", "RS": "rightstick", "DPAD_UP": "dpup",
            "DPAD_DOWN": "dpdown", "DPAD_LEFT": "dpleft", "DPAD_RIGHT": "dpright"}
AUTO_FH4 = dict(AUTO_PAD, B="rightshoulder", X="leftshoulder", LB=None, RB=None)

_HAT_MASKS = {1: (0, 1), 2: (1, 0), 4: (0, -1), 8: (-1, 0)}


def sdl_mapping(j):
    """Раскладка SDL для руля ({"a": "b0", "dpup": "h0.1", …}) или None.

    SDL знает, где у устройства «кнопка A», «левое плечо» и т. д., если оно
    есть в его базе. Оба режима Artplays там есть: X-Input — как Xbox 360,
    D-Input — через драйвер HIDAPI PS3.
    """
    try:
        from pygame._sdl2 import controller
        controller.init()
        for i in range(pygame.joystick.get_count()):
            if (pygame.joystick.Joystick(i).get_instance_id() == j.get_instance_id()
                    and controller.is_controller(i)):
                return controller.Controller(i).get_mapping()
    except Exception:  # noqa: BLE001
        pass
    return None


def mapping_source(token):
    """«b5» / «h0.4» / «a4» из раскладки SDL → источник кнопки в нашем формате."""
    import re
    m = re.fullmatch(r"b(\d+)", token or "")
    if m:
        return {"type": "button", "index": int(m[1])}
    m = re.fullmatch(r"h(\d+)\.(\d+)", token or "")
    if m and int(m[2]) in _HAT_MASKS:
        return {"type": "hat", "index": int(m[1]), "value": list(_HAT_MASKS[int(m[2])])}
    m = re.fullmatch(r"([+-]?)a(\d+)(~?)", token or "")
    if m:
        rest, full = {"+": (0.0, 1.0), "-": (0.0, -1.0)}.get(m[1], (-1.0, 1.0))
        if m[3]:
            rest, full = full, rest
        return {"type": "axis", "index": int(m[2]), "rest": rest, "full": full}
    return None


def busy_axes(cfg):
    """Оси руля и педалей — кнопками они не бывают."""
    return {p["axis"] for p in (cfg.get("steer"), cfg.get("gas"), cfg.get("brake"),
                                cfg.get("clutch")) if p}


def auto_buttons(j, cfg, preset="pad"):
    """Кнопки по раскладке SDL: preset "pad" — один в один, "fh4" — для FH4.

    Вернёт None, если SDL не знает этот руль.
    """
    m = sdl_mapping(j)
    if not m:
        return None
    busy = busy_axes(cfg)
    out = {}
    for name, key in (AUTO_FH4 if preset == "fh4" else AUTO_PAD).items():
        src = mapping_source(m.get(key)) if key else None
        if src and not (src["type"] == "axis" and src["index"] in busy):
            out[name] = [src]
    return out


def save_cfg(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)  # для запасного места в LOCALAPPDATA
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def calibrate(old_cfg=None):
    if BRIDGE:
        BRIDGE.snap = None
        BRIDGE.set_state("calibrating", "Калибровка руля — следуйте шагам ниже")
    j = offer_mode_choice(choose_device(prefer=old_cfg))
    if j.get_numaxes() < 2:
        sys.exit("У устройства меньше двух осей — похоже, выбран не руль.")

    print("\n=== Калибровка ===")
    print("Советы: переключите руль в режим 270° (если есть), педали подключены.")
    if j.get_name() in MODE_NOTES:
        print("  У Artplays V-1200 угол меняет тумблер на основании — переключать только\n"
              "  при выдернутом USB, иначе руль сработает неправильно.")

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
    print("Для каждой кнопки Xbox нажмите на руле кнопку, которая будет её заменять.")
    print("«На руле: …» — что нажать на Artplays V-1200 (можно и другую). Enter — пропустить.")
    print("Кнопку MODE не назначайте: если её удержать 3 секунды, руль переключится\n"
          "между X-Input и D-Input, и его придётся настраивать заново.\n")
    buttons = {}
    pedals = {p["axis"] for p in (gas, brake, clutch) if p}
    busy = pedals | {s_axis}  # эти оси кнопками не бывают
    for name, wheel, fh4 in XBOX_BUTTONS:
        wait_all_released(j, base, busy)
        src = capture_control(j, button_prompt(name, wheel, fh4), base, busy, pedals)
        if src:
            for other, lst in buttons.items():
                if any(same_source(src, s) for s in lst):
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
    save_cfg(cfg)
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
        if src["type"] == "axis":
            # ось-кнопка (лепесток): «нажато», если ушла дальше середины хода
            span = src["full"] - src["rest"]
            return span != 0 and (safe_axis(j, src["index"]) - src["rest"]) / span >= 0.5
    except pygame.error:
        pass
    return False


def safe_axis(j, idx):
    return j.get_axis(idx) if idx < j.get_numaxes() else 0.0


def settings_of(cfg):
    s = dict(DEFAULT_SETTINGS)
    s.update(cfg.get("settings", {}))
    return s


def compute_outputs(j, cfg, s):
    """Что получит игра: (стик X, RT, LT, множество нажатых кнопок Xbox).

    Одна функция и для эмуляции, и для окна кнопок — окно показывает ровно то,
    что уходит в виртуальный геймпад.
    """
    x = steer_value(cfg["steer"], safe_axis(j, cfg["steer"]["axis"]), s)
    rt = pedal_value(cfg["gas"], safe_axis(j, cfg["gas"]["axis"]), s)
    lt = pedal_value(cfg["brake"], safe_axis(j, cfg["brake"]["axis"]), s)
    want = {name for name, srcs in cfg["buttons"].items()
            if name in XUSB_NAMES and any(source_pressed(j, src) for src in srcs)}
    if cfg.get("clutch") and s.get("clutch_button") in XUSB_NAMES:
        c = pedal_value(cfg["clutch"], safe_axis(j, cfg["clutch"]["axis"]), s)
        if c >= s["clutch_threshold"]:
            want.add(s["clutch_button"])
    return x, rt, lt, want


def apply_to_pad(pad, xbtn, out, held):
    """Отправляет compute_outputs() в виртуальный геймпад. Вернёт новые held."""
    x, rt, lt, want = out
    pad.left_joystick_float(x_value_float=x, y_value_float=0.0)
    pad.right_trigger_float(value_float=rt)
    pad.left_trigger_float(value_float=lt)
    for name in want - held:
        pad.press_button(button=xbtn[name])
    for name in held - want:
        pad.release_button(button=xbtn[name])
    pad.update()
    return set(want)


# --------------------------------------------------------------------------
# Вибрация в обход SDL
# --------------------------------------------------------------------------
class XInputRumble:
    """Вибрация руля напрямую через XInput, когда SDL не может.

    Проверено на живом руле: в режиме X-Input Artplays для Windows — это
    «Xbox 360 Controller», и SDL шлёт ему вибрацию через XInput, сопоставляя
    «сырое» устройство со слотом XInput по одинаковым нажатиям. Пока рядом
    работает наш виртуальный Xbox 360, который повторяет руль один в один,
    сопоставить не выходит, и j.rumble() возвращает False (без виртуального
    геймпада тот же вызов вибрирует).

    Поэтому слот ищем сами — по VID/PID: их отдаёт недокументированная
    XInputGetCapabilitiesEx (ординал 108 в xinput1_4.dll; ей же пользуется SDL).
    Виртуальный геймпад (045E:028E) так отсеивается сам собой, а руль в D-Input —
    вообще не XInput-устройство, и чужой геймпад по ошибке не завибрирует.
    """

    def __init__(self, j):
        self.ok = False
        g = j.get_guid()
        try:
            # В GUID SDL VID и PID лежат байтами 4–5 и 8–9 (младший байт первым).
            self.vid = int(g[10:12] + g[8:10], 16)
            self.pid = int(g[18:20] + g[16:18], 16)
            import ctypes
            from ctypes import wintypes as wt

            class Gamepad(ctypes.Structure):
                _fields_ = [("wButtons", wt.WORD), ("bLeftTrigger", wt.BYTE),
                            ("bRightTrigger", wt.BYTE), ("sThumbLX", wt.SHORT),
                            ("sThumbLY", wt.SHORT), ("sThumbRX", wt.SHORT), ("sThumbRY", wt.SHORT)]

            class CapsEx(ctypes.Structure):
                _fields_ = [("Type", wt.BYTE), ("SubType", wt.BYTE), ("Flags", wt.WORD),
                            ("Gamepad", Gamepad), ("wLeftMotor", wt.WORD), ("wRightMotor", wt.WORD),
                            ("VendorId", wt.WORD), ("ProductId", wt.WORD),
                            ("ProductVersion", wt.WORD), ("unk1", wt.WORD), ("unk2", wt.DWORD)]

            class Vibration(ctypes.Structure):
                _fields_ = [("wLeftMotorSpeed", wt.WORD), ("wRightMotorSpeed", wt.WORD)]

            xi = ctypes.windll.xinput1_4
            self._caps_ex = xi[108]
            self._caps_ex.argtypes = [wt.DWORD, wt.DWORD, wt.DWORD, ctypes.POINTER(CapsEx)]
            self._caps_ex.restype = wt.DWORD
            self._set = xi.XInputSetState
            self._set.argtypes = [wt.DWORD, ctypes.POINTER(Vibration)]
            self._set.restype = wt.DWORD
            self._CapsEx, self._Vibration, self._byref = CapsEx, Vibration, ctypes.byref
            self.ok = True
        except Exception:  # noqa: BLE001  (не Windows, старая xinput, странный GUID)
            pass

    def _slot(self):
        """Слот XInput руля (ищем каждый раз: после переподключения он меняется)."""
        for u in range(4):
            caps = self._CapsEx()
            if (self._caps_ex(1, u, 0, self._byref(caps)) == 0
                    and (caps.VendorId, caps.ProductId) == (self.vid, self.pid)):
                return u
        return None

    def set(self, big, small):
        """big/small — 0..1. True, если руль принял команду."""
        if not self.ok:
            return False
        u = self._slot()
        if u is None:
            return False
        vib = self._Vibration(int(big * 65535), int(small * 65535))
        return self._set(u, self._byref(vib)) == 0


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
    print("Руль не найден, жду подключения…" + not_found_hint(cfg) +
          "\n  • если подключили другой руль или хотите работать в другом режиме руля —\n"
          f"    {r_hint()}.")
    want = (cfg or {}).get("device", {}).get("name")
    if BRIDGE:
        BRIDGE.snap = None
        BRIDGE.wanted, BRIDGE.other_mode = want, None
        BRIDGE.set_state("waiting", "Жду руль — подключите его (подробности — в консоли)")
    told = None
    while True:
        if BRIDGE and BRIDGE.stop.is_set():
            return None
        pygame.event.get()  # без этого SDL не узнает о новом устройстве
        cmd = poll_command()
        if cmd == RECALIBRATE:
            return RECALIBRATE
        if cmd == EDIT:
            print("\nСначала подключите руль — окно кнопок показывает его в реальном времени.")
        # Тот же Artplays, но в другом режиме? Так бывает чаще всего (MODE или
        # переподключение USB) — скажем прямо, а не общими словами.
        other = next((c.get_name() for c in all_joysticks()
                      if c.get_name() in MODE_NOTES and c.get_name() != want), None)
        if want in MODE_NOTES and other != told:
            told = other
            if BRIDGE:
                BRIDGE.other_mode = other
            if other:
                fix = ("выдерните USB руля и вставьте обратно — он вернётся в X-Input"
                       if wheel_mode(want) == "X-Input" else "зажмите на руле MODE на 3 секунды")
                print(f"\nРуль Artplays подключён, но сейчас он в режиме {wheel_mode(other)}, а настройки "
                      f"сделаны для {wheel_mode(want)}.\n  • {fix};\n  • или {r_hint()}, чтобы "
                      f"настроить руль в режиме {wheel_mode(other)}.")
        j = find_device(cfg)
        if j is not None:
            print("Руль найден.")
            return j
        time.sleep(0.2)


def run(cfg, show=False):
    """Эмуляция. Возвращает RECALIBRATE, если пользователь попросил перенастройку."""
    s = settings_of(cfg)

    # Руль ищем ДО создания виртуального геймпада: если понадобится
    # перенастройка, в списке устройств не будет лишнего «Xbox 360 Controller».
    j = find_device(cfg)
    if j is None:
        j = wait_for_wheel(cfg)
        if j is None or j == RECALIBRATE:  # None — окно закрыли, пока ждали руль
            return j

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
    xrumble = XInputRumble(j)

    xbtn = {name: getattr(vg.XUSB_BUTTON, XUSB_NAMES[name]) for name in XUSB_NAMES}

    rumble = {"large": 0, "small": 0, "dirty": False}

    def on_notify(client, target, large_motor, small_motor, led_number, user_data):
        rumble["large"], rumble["small"], rumble["dirty"] = large_motor, small_motor, True

    # Подписываемся всегда: вибрацию можно включить в окне кнопок прямо во время работы.
    try:
        pad.register_notification(callback_function=on_notify)
    except Exception as e:  # noqa: BLE001
        if s["rumble"]:
            print("Вибрация недоступна:", e)

    print(f"Эмуляция запущена: {device_title(j)} → виртуальный Xbox 360.")
    print("Запускайте игру. Окно можно свернуть; остановить — закрыть окно"
          + ("." if BRIDGE else " или Ctrl+C."))
    print(commands_help())
    if BRIDGE:
        BRIDGE.set_cfg(cfg)
        BRIDGE.other_mode = None
        BRIDGE.set_state("emulating", f"Работает: {device_title(j)} → виртуальный Xbox 360")

    period = 1.0 / max(int(s["rate_hz"]), 30)
    last_show = last_rumble = 0.0
    rumble_warned = False
    held = set()
    result = None

    try:
        while True:
            t0 = time.perf_counter()

            cmd = poll_command()
            if cmd == RECALIBRATE:
                print("\nПерехожу к перенастройке руля.")
                result = RECALIBRATE
                break
            if cmd == EDIT:
                if BRIDGE:
                    BRIDGE.show_settings = True
                else:
                    print("\nОкно кнопок есть в обычном (оконном) режиме программы — без --console.")

            if BRIDGE:
                if BRIDGE.stop.is_set():  # окно закрывают: выходим, прибрав за собой
                    break
                new = BRIDGE.take_new_cfg()  # правки из вкладки «Настройка» — сразу в работу
                if new is not None:
                    cfg.clear()
                    cfg.update(new)  # на месте: main_loop() передаст его в следующую калибровку
                    s = settings_of(cfg)
                    if not s["rumble"] and j is not None:
                        j.stop_rumble()
                        xrumble.set(0, 0)

            for e in pygame.event.get():
                if BRIDGE and getattr(e, "instance_id", None) == joy_id:
                    BRIDGE.events.append(e)  # для кнопки «Нажать» во вкладке «Настройка»
                if e.type == pygame.JOYDEVICEREMOVED and e.instance_id == joy_id:
                    print("\nРуль отключён, жду повторного подключения…\n" + mode_hint(cfg).rstrip())
                    j = None
                    if BRIDGE:
                        BRIDGE.snap = None
                        BRIDGE.set_state("waiting", "Руль отключён — жду, когда его снова подключат")
                elif e.type == pygame.JOYDEVICEADDED and j is None:
                    cand = find_device(cfg)
                    if cand is not None:
                        j = cand
                        joy_id = j.get_instance_id()
                        xrumble = XInputRumble(j)
                        print("Руль снова подключён.")
                        if BRIDGE:
                            BRIDGE.set_state("emulating",
                                             f"Работает: {device_title(j)} → виртуальный Xbox 360")

            if j is None:
                pad.reset()
                pad.update()
                held.clear()
                time.sleep(0.2)
                continue

            out = compute_outputs(j, cfg, s)
            x, rt, lt, want = out
            held = apply_to_pad(pad, xbtn, out, held)
            if BRIDGE:
                BRIDGE.publish(j, out)
                BRIDGE.serve(j)

            now = time.perf_counter()
            if s["rumble"] and (rumble["dirty"] or now - last_rumble > 0.8):
                big, small = rumble["large"], rumble["small"]
                rumble["dirty"] = False
                last_rumble = now
                try:
                    if big or small:
                        # Сначала через SDL, при отказе — напрямую в XInput (см. XInputRumble).
                        ok = (j.rumble(big / 255, small / 255, 1000)
                              or xrumble.set(big / 255, small / 255))
                        if not ok and not rumble_warned:
                            rumble_warned = True
                            print("\n(Руль не принимает вибрацию в этом режиме — продолжаю без неё.)")
                    else:
                        j.stop_rumble()
                        xrumble.set(0, 0)  # XInput, в отличие от SDL, сам не выключает мотор
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
            xrumble.set(0, 0)
        except Exception:  # noqa: BLE001
            pass
    # После выхода из функции объект pad удаляется, и vgamepad сам отключает
    # виртуальный геймпад — на время калибровки его в системе не будет.
    return result


def monitor():
    j = choose_device()
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


def calibrate_or_retry(cfg):
    """Калибровка. В окне её ошибки («не увидел поворота руля» и т. п.) не
    закрывают программу: пишем причину и ждём «Перекалибровать руль».
    Вернёт None, если окно закрыли, пока ждали."""
    while True:
        try:
            return calibrate(cfg)
        except SystemExit as e:
            if not BRIDGE or not isinstance(e.code, str):
                raise
            print("\n" + e.code + "\nНажмите «Перекалибровать руль», чтобы попробовать ещё раз.")
            BRIDGE.set_state("stopped", "Калибровка не удалась — причина в консоли")
            while poll_command() != RECALIBRATE:
                if BRIDGE.stop.is_set():
                    return None
                time.sleep(0.05)


def main_loop(args):
    """Калибровка (если нужна) → эмуляция → (R) → калибровка → … без перезапуска."""
    cfg = load_cfg()
    need_calibration = args.calibrate or cfg is None
    while True:
        if need_calibration:
            cfg = calibrate_or_retry(cfg)
            if cfg is None:
                return
            if BRIDGE:
                BRIDGE.show_settings = True  # сразу показать, что получилось, и дать поправить
        if run(cfg, show=args.show and not BRIDGE) != RECALIBRATE:
            return
        need_calibration = True


def gui_worker(args):
    """Рабочий поток окна: весь pygame, калибровка и эмуляция живут здесь."""
    try:
        init_sdl()
        print(f"wheel2pad {VERSION}")
        main_loop(args)
        if not BRIDGE.stop.is_set():
            BRIDGE.set_state("stopped", "Остановлено — причина в консоли")
    except SystemExit as e:
        if isinstance(e.code, str):
            print("\n" + e.code)
        BRIDGE.set_state("stopped", "Остановлено — причина в консоли")
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("\nЧто-то пошло не так. Сделайте снимок этого окна и отправьте разработчику.")
        BRIDGE.set_state("error", "Ошибка — подробности в консоли")


def attach_console():
    """У оконного exe нет консоли (sys.stdout — None). Для служебных флагов
    подключаемся к консоли, из которой его запустили, чтобы вывод был виден."""
    if sys.stdout is not None or sys.platform != "win32":
        return
    try:
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            sys.stdout = sys.stderr = open("CONOUT$", "w", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def show_fatal(text):
    """Сообщение, которое увидят и без консоли (оконный exe)."""
    print(text)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, "wheel2pad", 0x10)
        except Exception:  # noqa: BLE001
            pass


def main():
    ap = argparse.ArgumentParser(description="Руль → виртуальный геймпад Xbox 360")
    ap.add_argument("--calibrate", action="store_true", help="сначала заново откалибровать руль")
    ap.add_argument("--console", action="store_true",
                    help="по-старому, в консоли, без окна (только при запуске через Python)")
    ap.add_argument("--show", action="store_true", help="--console: строка значений во время эмуляции")
    ap.add_argument("--monitor", action="store_true", help="показать сырые данные руля")
    ap.add_argument("--list", action="store_true", help="список устройств")
    ap.add_argument("--where", action="store_true", help="путь к exe/python.exe для HidHide")
    ap.add_argument("--edit", action="store_true", help="открыться на вкладке «Настройка кнопок»")
    attach_console()  # чтобы --help и ошибки флагов были видны из терминала
    args = ap.parse_args()

    if args.where:
        path = real_exe()
        print(path)
        copy_to_clipboard(path)
        return
    if args.list or args.monitor:
        init_sdl()
        if args.list:
            list_devices()
        else:
            monitor()
        return
    if args.console:
        if FROZEN:
            show_fatal("--console работает только при запуске через Python (python wheel2pad.py "
                       "--console). wheel2pad.exe — оконная программа: запустите его без флагов.")
            return
        start_stdin_reader()
        init_sdl()
        main_loop(args)
        return

    import wheel2pad_gui
    wheel2pad_gui.App(sys.modules[__name__], args).run()


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
        # Ctrl+C посреди калибровки (--console): KeyboardInterrupt не наследуется
        # от Exception, поэтому без этой ветки был бы страшный трейсбек.
        print("\nОстановлено.")
        exit_code = 1
    except Exception:  # noqa: BLE001
        import traceback
        show_fatal("Что-то пошло не так. Сделайте снимок этого окна и отправьте разработчику.\n\n"
                   + traceback.format_exc())
        exit_code = 1
    sys.exit(exit_code)
