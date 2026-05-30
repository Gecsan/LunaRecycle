#!/usr/bin/env python3
"""
Recycling System Hardware + GUI Script

Purpose:
- Read MCP9600 thermocouple boards on a Raspberry Pi 4B.
- Control one SSR output per temperature zone.
- Keep the GUI running even when motors are not found.
- Always show two configured TalonFX motors in the GUI.
- Control both motors through the same proven Phoenix path.
- GUI target is entered as RPM, while the script sends DutyCycleOut internally.
- Current RPM is read back from each TalonFX velocity signal.
- Uses phoenix6.unmanaged.feed_enable(...) so TalonFX outputs are actually enabled.
- Show detailed errors at the bottom of the GUI, not near the top.

Notes:
- Current temperature zones use addresses 0x65, 0x66, and 0x67.
- A future fourth zone at 0x64 is included by setting USE_ZONE_4 = True.
- Shredder is TalonFX ID 0 on CanBus.
- Extruder is TalonFX ID 1 on CanBus.
"""

import time
import traceback
import tkinter as tk
from tkinter import ttk

import board
import busio
import adafruit_mcp9600
from gpiozero import DigitalOutputDevice

# CTRE / Phoenix imports are optional so the GUI can still run without motors.
try:
    import phoenix6
    from phoenix6.hardware.talon_fx import TalonFX

    try:
        from phoenix6.canbus import CANBus
    except Exception:
        CANBus = None

    try:
        from phoenix6.controls import DutyCycleOut
    except Exception:
        DutyCycleOut = None

    PHOENIX_AVAILABLE = True
    PHOENIX_IMPORT_ERROR = None
except Exception as phoenix_import_error:
    phoenix6 = None
    TalonFX = None
    CANBus = None
    DutyCycleOut = None
    PHOENIX_AVAILABLE = False
    PHOENIX_IMPORT_ERROR = phoenix_import_error


# ============================================================
# General configuration
# ============================================================

GUI_UPDATE_MS = 500
MOTOR_COMMAND_UPDATE_MS = 20
I2C_FREQUENCY = 85_000
THERMOCOUPLE_TYPE = "K"
TC_FILTER = 0


# ============================================================
# Temperature zone / SSR configuration
# ============================================================

USE_ZONE_4 = False

TEMP_ZONES = [
    {"name": "zone_1", "address": 0x65, "ssr_gpio": 17, "setpoint_c": 100.0},
    {"name": "zone_2", "address": 0x66, "ssr_gpio": 27, "setpoint_c": 100.0},
    {"name": "zone_3", "address": 0x67, "ssr_gpio": 22, "setpoint_c": 100.0},
]

if USE_ZONE_4:
    TEMP_ZONES.append(
        {"name": "zone_4", "address": 0x64, "ssr_gpio": 23, "setpoint_c": 100.0}
    )

SSR_ACTIVE_HIGH = True
OFF_HYSTERESIS_C = 2.0
TEMP_RESCAN_SECONDS = 10.0


# ============================================================
# TalonFX / CANivore motor configuration
# ============================================================

ENABLE_MOTORS = True
CAN_BUS_NAME = "CanBus"

# Both motors use the exact same motor code path.
# rpm_for_full_output controls the conversion from GUI RPM to duty cycle.
# Example: target 3000 RPM with rpm_for_full_output 6000 sends 0.50 duty cycle.
MOTOR_CONFIGS = [
    {"name": "Shredder", "device_id": 0, "rpm_for_full_output": 6000.0},
    {"name": "Extruder", "device_id": 1, "rpm_for_full_output": 6000.0},
]

MOTOR_MIN_RPM = -6000.0
MOTOR_MAX_RPM = 6000.0
MOTOR_ENABLE_TIMEOUT_SECONDS = 0.100

# Deep-dive fix for the Shredder issue:
# 1) DutyCycleOut defaults enable_foc=True in Phoenix 6 Python, which can behave
#    differently from simple Tuner duty-cycle control. Force it OFF.
# 2) If a hardware/remote limit config is active, the TalonFX can be enabled but
#    still apply neutral output. Ignore hardware limits for these drive commands.
#    Set this to False if you later add real limit switches that must stop motion.
# 3) We send commands every 20 ms, so use one-shot control frames.
MOTOR_USE_FOC = False
MOTOR_IGNORE_HARDWARE_LIMITS = True
MOTOR_REQUEST_UPDATE_HZ = 0


# ============================================================
# Runtime state
# ============================================================

i2c = None
zones = []
last_temp_rescan_time = 0.0

canivore_bus = None
can_bus_error = None
motors = {}
motor_setup_error = None
motor_command_error = None


# ============================================================
# I2C / temperature board / SSR functions
# ============================================================

def scan_i2c_addresses(i2c_bus):
    while not i2c_bus.try_lock():
        time.sleep(0.01)

    try:
        return set(i2c_bus.scan())
    finally:
        i2c_bus.unlock()


def initialize_temperature_zones(i2c_bus):
    found = scan_i2c_addresses(i2c_bus)
    print("Found I2C devices:", [f"0x{addr:02X}" for addr in sorted(found)])

    initialized_zones = []

    for zone_cfg in TEMP_ZONES:
        ssr = DigitalOutputDevice(
            zone_cfg["ssr_gpio"],
            active_high=SSR_ACTIVE_HIGH,
            initial_value=False,
        )

        zone = {
            **zone_cfg,
            "sensor": None,
            "ssr": ssr,
            "online": False,
            "enabled_by_switch": True,
            "last_temp_c": None,
            "last_error": None,
        }

        if zone_cfg["address"] in found:
            try:
                print(f"Initializing {zone_cfg['name']} at 0x{zone_cfg['address']:02X}")
                zone["sensor"] = adafruit_mcp9600.MCP9600(
                    i2c_bus,
                    address=zone_cfg["address"],
                    tctype=THERMOCOUPLE_TYPE,
                    tcfilter=TC_FILTER,
                )
                zone["online"] = True
            except Exception as e:
                zone["sensor"] = None
                zone["online"] = False
                zone["last_error"] = str(e)
                zone["ssr"].off()
                print(
                    f"WARNING: Could not initialize {zone_cfg['name']} "
                    f"at 0x{zone_cfg['address']:02X}: {e}"
                )
        else:
            zone["last_error"] = f"Device not found at 0x{zone_cfg['address']:02X}"
            print(
                f"WARNING: {zone_cfg['name']} expected at 0x{zone_cfg['address']:02X}, "
                "but it was not found. SSR forced OFF."
            )

        initialized_zones.append(zone)

    return initialized_zones


def retry_missing_temperature_zones(i2c_bus, zone_list):
    global last_temp_rescan_time

    now = time.monotonic()

    if now - last_temp_rescan_time < TEMP_RESCAN_SECONDS:
        return

    last_temp_rescan_time = now

    missing_zones = [zone for zone in zone_list if not zone["online"]]

    if not missing_zones:
        return

    try:
        found = scan_i2c_addresses(i2c_bus)
    except Exception as e:
        for zone in missing_zones:
            zone["last_error"] = f"I2C rescan failed: {e}"
        print(f"WARNING: I2C rescan failed: {e}")
        return

    for zone in missing_zones:
        if zone["address"] not in found:
            zone["last_error"] = f"Device not found at 0x{zone['address']:02X}"
            continue

        try:
            print(f"Found missing {zone['name']} at 0x{zone['address']:02X}")
            zone["sensor"] = adafruit_mcp9600.MCP9600(
                i2c_bus,
                address=zone["address"],
                tctype=THERMOCOUPLE_TYPE,
                tcfilter=TC_FILTER,
            )
            zone["online"] = True
            zone["last_error"] = None
        except Exception as e:
            zone["sensor"] = None
            zone["online"] = False
            zone["last_error"] = str(e)
            zone["ssr"].off()
            print(f"WARNING: Could not reconnect {zone['name']}: {e}")


def update_temperature_zone(zone):
    if not zone["enabled_by_switch"]:
        zone["ssr"].off()
        return zone["last_temp_c"], False, "SWITCH OFF"

    if not zone["online"] or zone["sensor"] is None:
        zone["ssr"].off()
        return None, False, "MISSING"

    try:
        temp_c = zone["sensor"].temperature
        zone["last_temp_c"] = temp_c
        zone["last_error"] = None

        ssr_is_on = bool(zone["ssr"].value)

        if not ssr_is_on and temp_c <= zone["setpoint_c"]:
            zone["ssr"].on()
        elif ssr_is_on and temp_c >= zone["setpoint_c"] + OFF_HYSTERESIS_C:
            zone["ssr"].off()

        return temp_c, bool(zone["ssr"].value), "OK"

    except Exception as e:
        zone["ssr"].off()
        zone["online"] = False
        zone["sensor"] = None
        zone["last_error"] = str(e)
        return None, False, "ERROR"


def shutdown_temperature_outputs():
    for zone in zones:
        try:
            zone["ssr"].off()
            zone["ssr"].close()
        except Exception:
            pass


# ============================================================
# Motor functions rebuilt from scratch
# ============================================================

def clamp(value, low, high):
    return max(low, min(high, value))


def feed_motor_enable():
    if phoenix6 is None:
        raise RuntimeError("phoenix6 is not available.")

    phoenix6.unmanaged.feed_enable(MOTOR_ENABLE_TIMEOUT_SECONDS)


def make_can_bus():
    if not ENABLE_MOTORS:
        return None

    if not PHOENIX_AVAILABLE:
        raise RuntimeError(f"Phoenix library is not available: {PHOENIX_IMPORT_ERROR}")

    if CANBus is not None:
        return CANBus(CAN_BUS_NAME)

    return CAN_BUS_NAME


def initialize_motors():
    global motor_setup_error

    motors.clear()

    for cfg in MOTOR_CONFIGS:
        motors[cfg["device_id"]] = {
            "name": cfg["name"],
            "device_id": cfg["device_id"],
            "rpm_for_full_output": cfg["rpm_for_full_output"],
            "motor": None,
            "request": None,
            "set_rpm": 0.0,
            "current_rpm": None,
            "last_output": 0.0,
            "last_error": None,
            "has_been_commanded": False,
        }

    motor_setup_error = None


def apply_duty_request_options(request):
    """Force the control request to match the simple Tuner-style output path."""
    if hasattr(request, "with_enable_foc"):
        updated = request.with_enable_foc(MOTOR_USE_FOC)
        if updated is not None:
            request = updated

    if hasattr(request, "with_ignore_hardware_limits"):
        updated = request.with_ignore_hardware_limits(MOTOR_IGNORE_HARDWARE_LIMITS)
        if updated is not None:
            request = updated

    if hasattr(request, "update_freq_hz"):
        try:
            request.update_freq_hz = MOTOR_REQUEST_UPDATE_HZ
        except Exception:
            pass

    return request


def make_duty_request(output):
    """Create DutyCycleOut with explicit non-FOC, no-limit options when available."""
    try:
        request = DutyCycleOut(
            output,
            MOTOR_USE_FOC,
            False,
            False,
            False,
            MOTOR_IGNORE_HARDWARE_LIMITS,
            False,
        )
    except TypeError:
        try:
            request = DutyCycleOut(output, MOTOR_USE_FOC, False, False, False)
        except TypeError:
            request = DutyCycleOut(output)

    return apply_duty_request_options(request)


def ensure_motor_object(entry):
    global canivore_bus

    if entry.get("motor") is not None and entry.get("request") is not None:
        return

    if not ENABLE_MOTORS:
        raise RuntimeError("Motors are disabled.")

    if DutyCycleOut is None:
        raise RuntimeError("DutyCycleOut control is not available in this Phoenix install.")

    if canivore_bus is None:
        canivore_bus = make_can_bus()

    entry["motor"] = TalonFX(entry["device_id"], canivore_bus)
    entry["request"] = make_duty_request(0.0)


def rpm_to_duty_cycle(entry, rpm):
    full_rpm = float(entry.get("rpm_for_full_output", 6000.0))

    if full_rpm <= 0:
        raise RuntimeError(f"Invalid rpm_for_full_output for {entry['name']}: {full_rpm}")

    return clamp(rpm / full_rpm, -1.0, 1.0)


def get_signal_double(signal):
    if hasattr(signal, "refresh"):
        signal.refresh()

    for method_name in ["get_value_as_double", "get_value"]:
        if hasattr(signal, method_name):
            try:
                value = getattr(signal, method_name)()
                return float(value)
            except Exception:
                pass

    for attr in ["value_as_double", "value"]:
        if hasattr(signal, attr):
            try:
                value = getattr(signal, attr)
                return float(value)
            except Exception:
                pass

    return None


def refresh_motor_current_rpm(entry):
    motor = entry.get("motor")

    if motor is None:
        entry["current_rpm"] = None
        return None

    try:
        velocity_rps = get_signal_double(motor.get_velocity())

        if velocity_rps is None:
            entry["current_rpm"] = None
            return None

        current_rpm = velocity_rps * 60.0
        entry["current_rpm"] = current_rpm
        return current_rpm

    except Exception as e:
        entry["last_error"] = str(e)
        entry["current_rpm"] = None
        return None


def command_motor_rpm(entry, rpm):
    ensure_motor_object(entry)

    motor = entry["motor"]
    request = entry["request"]
    output = rpm_to_duty_cycle(entry, rpm)

    feed_motor_enable()

    if hasattr(request, "with_output"):
        request = request.with_output(output)
    else:
        request.output = output

    request = apply_duty_request_options(request)
    entry["request"] = request

    status = motor.set_control(request)

    if hasattr(status, "is_ok") and not status.is_ok():
        raise RuntimeError(f"DutyCycleOut command failed: {status}")

    entry["last_output"] = output
    entry["last_error"] = None
    entry["has_been_commanded"] = True


def stop_motor_if_commanded(entry):
    if not entry.get("has_been_commanded"):
        return

    try:
        command_motor_rpm(entry, 0.0)
    except Exception as e:
        entry["last_error"] = str(e)


def apply_motor_rpm_commands(desired_motor_states, desired_motor_rpms):
    global motor_command_error

    if not ENABLE_MOTORS:
        return

    errors = []

    for device_id, entry in motors.items():
        desired_on = desired_motor_states.get(device_id, False)
        set_rpm = desired_motor_rpms.get(device_id, 0.0)

        if desired_on:
            rpm_to_send = set_rpm
        elif entry.get("has_been_commanded"):
            rpm_to_send = 0.0
        else:
            continue

        try:
            command_motor_rpm(entry, rpm_to_send)
        except Exception as e:
            entry["last_error"] = str(e)
            errors.append(f"{entry['name']} ID {device_id}: {e}")

    motor_command_error = chr(10).join(errors) if errors else None


def shutdown_motors():
    for entry in motors.values():
        stop_motor_if_commanded(entry)


# ============================================================
# GUI
# ============================================================

class RecyclingSystemGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Recycling System")
        self.root.geometry("1180x620")

        self.zone_rows = {}
        self.zone_enabled_vars = {}
        self.zone_switch_text_vars = {}
        self.zone_setpoint_vars = {}

        self.motor_rows = {}
        self.motor_enabled_vars = {}
        self.motor_switch_text_vars = {}
        self.motor_rpm_vars = {}
        self.motor_set_rpm_vars = {}
        self.motor_current_rpm_vars = {}
        self.motor_command_vars = {}
        self.motor_rpm_errors = {}

        self.main_frame = ttk.Frame(root, padding=12)
        self.main_frame.pack(fill="both", expand=True)

        title = ttk.Label(
            self.main_frame,
            text="Recycling System",
            font=("Arial", 18, "bold"),
        )
        title.pack(anchor="w", pady=(0, 12))

        self.build_zone_table()
        self.build_motor_table()
        self.build_bottom_status_area()
        self.build_buttons()

        self.root.protocol("WM_DELETE_WINDOW", self.quit_cleanly)
        self.update_loop()
        self.motor_control_loop()

    def build_zone_table(self):
        zone_frame = ttk.LabelFrame(self.main_frame, text="Temperature Zones", padding=8)
        zone_frame.pack(fill="x", expand=False, pady=(0, 12))

        headers = [
            "Zone",
            "Switch",
            "I2C Address",
            "GPIO",
            "Temperature",
            "Setpoint C",
            "SSR",
            "Status",
        ]

        for col, header in enumerate(headers):
            label = ttk.Label(zone_frame, text=header, font=("Arial", 10, "bold"))
            label.grid(row=0, column=col, sticky="w", padx=6, pady=4)

        for row, zone in enumerate(zones, start=1):
            enabled_var = tk.BooleanVar(value=True)
            switch_text_var = tk.StringVar(value="ON")
            self.zone_enabled_vars[zone["name"]] = enabled_var
            self.zone_switch_text_vars[zone["name"]] = switch_text_var

            zone_var = tk.StringVar(value=zone["name"])
            address_var = tk.StringVar(value=f"0x{zone['address']:02X}")
            gpio_var = tk.StringVar(value=str(zone["ssr_gpio"]))
            temp_var = tk.StringVar(value="---")
            setpoint_var = tk.StringVar(value=f"{zone['setpoint_c']:.1f}")
            self.zone_setpoint_vars[zone["name"]] = setpoint_var
            ssr_var = tk.StringVar(value="OFF")
            status_var = tk.StringVar(value="Starting")

            self.zone_rows[zone["name"]] = {
                "zone": zone_var,
                "address": address_var,
                "gpio": gpio_var,
                "temp": temp_var,
                "ssr": ssr_var,
                "status": status_var,
            }

            ttk.Label(zone_frame, textvariable=zone_var).grid(row=row, column=0, sticky="w", padx=6, pady=4)
            ttk.Checkbutton(
                zone_frame,
                textvariable=switch_text_var,
                variable=enabled_var,
                command=lambda zone_name=zone["name"]: self.on_zone_switch_changed(zone_name),
            ).grid(row=row, column=1, sticky="w", padx=6, pady=4)
            ttk.Label(zone_frame, textvariable=address_var).grid(row=row, column=2, sticky="w", padx=6, pady=4)
            ttk.Label(zone_frame, textvariable=gpio_var).grid(row=row, column=3, sticky="w", padx=6, pady=4)
            ttk.Label(zone_frame, textvariable=temp_var).grid(row=row, column=4, sticky="w", padx=6, pady=4)
            ttk.Entry(zone_frame, textvariable=setpoint_var, width=10).grid(row=row, column=5, sticky="w", padx=6, pady=4)
            ttk.Label(zone_frame, textvariable=ssr_var).grid(row=row, column=6, sticky="w", padx=6, pady=4)
            ttk.Label(zone_frame, textvariable=status_var).grid(row=row, column=7, sticky="w", padx=6, pady=4)

    def build_motor_table(self):
        self.motor_frame = ttk.LabelFrame(self.main_frame, text="Motors", padding=8)
        self.motor_frame.pack(fill="x", expand=False, pady=(0, 12))

        headers = ["Motor", "Switch", "CAN ID", "Target RPM", "Set", "Set RPM", "Current RPM", "Command"]
        for col, header in enumerate(headers):
            label = ttk.Label(self.motor_frame, text=header, font=("Arial", 10, "bold"))
            label.grid(row=0, column=col, sticky="w", padx=6, pady=4)

        for row, cfg in enumerate(MOTOR_CONFIGS, start=1):
            device_id = cfg["device_id"]
            name = cfg["name"]

            enabled_var = tk.BooleanVar(value=False)
            switch_text_var = tk.StringVar(value="OFF")
            rpm_var = tk.StringVar(value="0")
            set_rpm_var = tk.StringVar(value="0 RPM")
            current_rpm_var = tk.StringVar(value="---")
            command_var = tk.StringVar(value="OFF -> 0 RPM")

            self.motor_enabled_vars[device_id] = enabled_var
            self.motor_switch_text_vars[device_id] = switch_text_var
            self.motor_rpm_vars[device_id] = rpm_var
            self.motor_set_rpm_vars[device_id] = set_rpm_var
            self.motor_current_rpm_vars[device_id] = current_rpm_var
            self.motor_command_vars[device_id] = command_var
            self.motor_rpm_errors[device_id] = None

            ttk.Label(self.motor_frame, text=name).grid(row=row, column=0, sticky="w", padx=6, pady=4)
            ttk.Checkbutton(
                self.motor_frame,
                textvariable=switch_text_var,
                variable=enabled_var,
                command=lambda motor_id=device_id: self.on_motor_switch_changed(motor_id),
            ).grid(row=row, column=1, sticky="w", padx=6, pady=4)
            ttk.Label(self.motor_frame, text=str(device_id)).grid(row=row, column=2, sticky="w", padx=6, pady=4)
            ttk.Entry(self.motor_frame, textvariable=rpm_var, width=12).grid(row=row, column=3, sticky="w", padx=6, pady=4)
            ttk.Button(
                self.motor_frame,
                text="Set",
                command=lambda motor_id=device_id: self.on_motor_set_clicked(motor_id),
            ).grid(row=row, column=4, sticky="w", padx=6, pady=4)
            ttk.Label(self.motor_frame, textvariable=set_rpm_var).grid(row=row, column=5, sticky="w", padx=6, pady=4)
            ttk.Label(self.motor_frame, textvariable=current_rpm_var).grid(row=row, column=6, sticky="w", padx=6, pady=4)
            ttk.Label(self.motor_frame, textvariable=command_var).grid(row=row, column=7, sticky="w", padx=6, pady=4)

            self.motor_rows[device_id] = row

    def build_bottom_status_area(self):
        status_frame = ttk.LabelFrame(self.main_frame, text="Errors / Messages", padding=8)
        status_frame.pack(fill="both", expand=True, pady=(0, 12))

        self.bottom_status_text = tk.Text(status_frame, height=8, wrap="word")
        self.bottom_status_text.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(status_frame, orient="vertical", command=self.bottom_status_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.bottom_status_text.configure(yscrollcommand=scrollbar.set)

        self.set_bottom_status("No errors.")

    def build_buttons(self):
        button_frame = ttk.Frame(self.main_frame)
        button_frame.pack(fill="x", pady=(0, 0))

        self.quit_button = ttk.Button(button_frame, text="Quit", command=self.quit_cleanly)
        self.quit_button.pack(side="right")

    def set_bottom_status(self, message):
        self.bottom_status_text.configure(state="normal")
        self.bottom_status_text.delete("1.0", "end")
        self.bottom_status_text.insert("1.0", message)
        self.bottom_status_text.configure(state="disabled")

    def read_zone_setpoint_c(self, zone_name):
        setpoint_var = self.zone_setpoint_vars.get(zone_name)

        if setpoint_var is None:
            raise ValueError(f"No setpoint field found for {zone_name}")

        raw_value = setpoint_var.get().strip().replace("C", "").replace("c", "")

        if not raw_value:
            raise ValueError(f"{zone_name}: setpoint is blank")

        try:
            setpoint_c = float(raw_value)
        except ValueError:
            raise ValueError(f"{zone_name}: invalid setpoint '{setpoint_var.get()}'")

        if setpoint_c < -200 or setpoint_c > 1372:
            raise ValueError(f"{zone_name}: setpoint {setpoint_c:.1f} C is outside the K-type range")

        return setpoint_c

    def on_zone_switch_changed(self, zone_name):
        enabled = self.zone_enabled_vars[zone_name].get()
        self.zone_switch_text_vars[zone_name].set("ON" if enabled else "OFF")

        for zone in zones:
            if zone["name"] == zone_name:
                zone["enabled_by_switch"] = enabled
                if not enabled:
                    zone["ssr"].off()
                break

    def on_motor_switch_changed(self, device_id):
        enabled = self.motor_enabled_vars[device_id].get()
        self.motor_switch_text_vars[device_id].set("ON" if enabled else "OFF")

        if not enabled and device_id in motors:
            stop_motor_if_commanded(motors[device_id])

    def read_motor_rpm(self, device_id):
        rpm_var = self.motor_rpm_vars.get(device_id)

        if rpm_var is None:
            raise ValueError(f"No RPM field found for motor ID {device_id}")

        raw_value = rpm_var.get().strip().replace("RPM", "").replace("rpm", "")

        if not raw_value:
            raise ValueError(f"Motor ID {device_id}: RPM is blank")

        try:
            rpm = float(raw_value)
        except ValueError:
            raise ValueError(f"Motor ID {device_id}: invalid RPM '{rpm_var.get()}'")

        if rpm < MOTOR_MIN_RPM or rpm > MOTOR_MAX_RPM:
            raise ValueError(
                f"Motor ID {device_id}: RPM {rpm:.1f} is outside the allowed range "
                f"{MOTOR_MIN_RPM:.0f} to {MOTOR_MAX_RPM:.0f}"
            )

        return rpm

    def on_motor_set_clicked(self, device_id):
        try:
            rpm = self.read_motor_rpm(device_id)
            motors[device_id]["set_rpm"] = rpm
            self.motor_set_rpm_vars[device_id].set(f"{rpm:.0f} RPM")
            self.motor_rpm_errors[device_id] = None
        except ValueError as e:
            self.motor_rpm_errors[device_id] = str(e)
            self.motor_enabled_vars[device_id].set(False)
            self.motor_switch_text_vars[device_id].set("OFF")

    def get_desired_motor_states(self):
        return {
            device_id: enabled_var.get()
            for device_id, enabled_var in self.motor_enabled_vars.items()
        }

    def get_desired_motor_rpms(self):
        desired_rpms = {}

        for device_id in self.motor_rpm_vars.keys():
            desired_rpms[device_id] = motors.get(device_id, {}).get("set_rpm", 0.0)

        return desired_rpms

    def refresh_current_motor_rpms(self):
        for cfg in MOTOR_CONFIGS:
            device_id = cfg["device_id"]
            entry = motors.get(device_id)
            current_var = self.motor_current_rpm_vars.get(device_id)

            if entry is None or current_var is None:
                continue

            current_rpm = refresh_motor_current_rpm(entry)
            current_var.set("---" if current_rpm is None else f"{current_rpm:.0f} RPM")

    def update_motor_command_rows(self):
        for cfg in MOTOR_CONFIGS:
            device_id = cfg["device_id"]
            command_var = self.motor_command_vars.get(device_id)
            entry = motors.get(device_id, {})

            if command_var is None:
                continue

            if not ENABLE_MOTORS:
                command_var.set("Disabled")
            elif self.motor_rpm_errors.get(device_id):
                command_var.set("Bad RPM")
            else:
                desired_on = self.motor_enabled_vars[device_id].get()
                rpm = entry.get("set_rpm", 0.0) if desired_on else 0.0
                output = rpm_to_duty_cycle(entry, rpm) if entry else 0.0
                command_var.set(
                    f"ON -> {rpm:.0f} RPM ({output:.2f})" if desired_on else "OFF -> 0 RPM"
                )

    def collect_bottom_messages(self):
        messages = []

        for zone in zones:
            if zone["last_error"]:
                messages.append(f"{zone['name']}: {zone['last_error']}")

        if not PHOENIX_AVAILABLE:
            messages.append(f"Phoenix library unavailable: {PHOENIX_IMPORT_ERROR}")

        if can_bus_error:
            messages.append(can_bus_error)

        if motor_setup_error:
            messages.append(f"Motor setup error: {motor_setup_error}")

        for device_id in sorted(self.motor_rpm_errors.keys()):
            if self.motor_rpm_errors[device_id]:
                name = motors.get(device_id, {}).get("name", f"Motor ID {device_id}")
                messages.append(f"{name}: {self.motor_rpm_errors[device_id]}")

        if motor_command_error:
            messages.append(f"Motor command error: {motor_command_error}")

        if not messages:
            return "No errors."

        return chr(10).join(messages)

    def motor_control_loop(self):
        try:
            apply_motor_rpm_commands(
                self.get_desired_motor_states(),
                self.get_desired_motor_rpms(),
            )
        except Exception as e:
            global motor_command_error
            motor_command_error = f"Motor control loop error: {e}"

        self.root.after(MOTOR_COMMAND_UPDATE_MS, self.motor_control_loop)

    def update_loop(self):
        try:
            retry_missing_temperature_zones(i2c, zones)

            for zone in zones:
                zone["enabled_by_switch"] = self.zone_enabled_vars[zone["name"]].get()
                row_vars = self.zone_rows.get(zone["name"])

                try:
                    zone["setpoint_c"] = self.read_zone_setpoint_c(zone["name"])
                except ValueError as setpoint_error:
                    zone["ssr"].off()
                    zone["last_error"] = str(setpoint_error)

                    if row_vars is not None:
                        row_vars["ssr"].set("OFF")
                        row_vars["status"].set("BAD SETPOINT")

                    continue

                temp_c, ssr_state, status = update_temperature_zone(zone)

                if row_vars is None:
                    continue

                row_vars["temp"].set("---" if temp_c is None else f"{temp_c:.1f} C")
                row_vars["ssr"].set("ON" if ssr_state else "OFF")
                row_vars["status"].set(status)

            self.refresh_current_motor_rpms()
            self.update_motor_command_rows()
            self.set_bottom_status(self.collect_bottom_messages())

        except Exception as e:
            self.set_bottom_status("GUI update error: " + str(e) + chr(10) + chr(10) + traceback.format_exc())
            print("GUI update error:")
            traceback.print_exc()

        self.root.after(GUI_UPDATE_MS, self.update_loop)

    def quit_cleanly(self):
        shutdown_motors()
        shutdown_temperature_outputs()
        self.root.destroy()


# ============================================================
# Main
# ============================================================

def main():
    global i2c
    global zones

    print("Starting Recycling System...")

    i2c = busio.I2C(board.SCL, board.SDA, frequency=I2C_FREQUENCY)
    zones = initialize_temperature_zones(i2c)

    initialize_motors()

    root = tk.Tk()
    RecyclingSystemGUI(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopping Recycling System...")
    finally:
        shutdown_motors()
        shutdown_temperature_outputs()
