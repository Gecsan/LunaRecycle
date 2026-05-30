#!/usr/bin/env python3
"""
Minimal TalonFX motor test for Raspberry Pi + CANivore + Phoenix 6

This version includes the missing non-FRC enable signal.
Without phoenix6.unmanaged.feed_enable(...), set_control() can return OK while
actual duty cycle and motor voltage stay at zero.

Run:
    CTR_TARGET=Hardware python3 motor_test.py

Device from Tuner X:
    CAN bus: CanBus
    TalonFX ID: 1
"""

import os
import sys
import time
import phoenix6
from phoenix6.hardware.talon_fx import TalonFX
from phoenix6.controls import DutyCycleOut

try:
    from phoenix6.canbus import CANBus
except Exception:
    CANBus = None


CAN_BUS_NAME = "CanBus"
MOTOR_ID = 1

TEST_OUTPUT = 0.15
TEST_SECONDS = 4.0
LOOP_PERIOD_SECONDS = 0.02
ENABLE_TIMEOUT_SECONDS = 0.100


def make_bus():
    if CANBus is not None:
        return CANBus(CAN_BUS_NAME)
    return CAN_BUS_NAME


def get_signal_value(signal):
    if hasattr(signal, "refresh"):
        signal.refresh()

    for method_name in ["get_value_as_double", "get_value"]:
        if hasattr(signal, method_name):
            try:
                return getattr(signal, method_name)()
            except Exception:
                pass

    for attr in ["value_as_double", "value"]:
        if hasattr(signal, attr):
            try:
                return getattr(signal, attr)
            except Exception:
                pass

    return None


def print_status(motor, elapsed):
    duty = get_signal_value(motor.get_duty_cycle())
    motor_v = get_signal_value(motor.get_motor_voltage())
    supply_v = get_signal_value(motor.get_supply_voltage())
    velocity = get_signal_value(motor.get_velocity())
    supply_i = get_signal_value(motor.get_supply_current())
    stator_i = get_signal_value(motor.get_stator_current())

    print(
        f"t={elapsed:5.2f}s | "
        f"duty={duty} | "
        f"motorV={motor_v} | "
        f"supplyV={supply_v} | "
        f"vel={velocity} | "
        f"supplyI={supply_i} | "
        f"statorI={stator_i}"
    )


def command_output(motor, request, output):
    # This is the critical non-FRC enable call.
    phoenix6.unmanaged.feed_enable(ENABLE_TIMEOUT_SECONDS)

    if hasattr(request, "with_output"):
        request = request.with_output(output)
    else:
        request.output = output

    status = motor.set_control(request)
    print(f"set_control({output}) -> {status}")
    return request


def main():
    print("Minimal TalonFX motor test with unmanaged.feed_enable")
    print(f"python={sys.executable}")
    print(f"CTR_TARGET={os.environ.get('CTR_TARGET')!r}")
    print(f"CAN_BUS_NAME={CAN_BUS_NAME!r}")
    print(f"MOTOR_ID={MOTOR_ID}")
    print(f"TEST_OUTPUT={TEST_OUTPUT}")
    print(f"TEST_SECONDS={TEST_SECONDS}")

    bus = make_bus()
    motor = TalonFX(MOTOR_ID, bus)
    request = DutyCycleOut(0.0)

    print("\nStarting in 2 seconds. Keep the mechanism safe.")
    time.sleep(2.0)

    try:
        request = command_output(motor, request, 0.0)
        time.sleep(0.5)

        print("\nRunning motor...")
        start = time.monotonic()
        last_print = 0.0

        while True:
            elapsed = time.monotonic() - start
            if elapsed >= TEST_SECONDS:
                break

            # Feed enable and resend the command continuously.
            phoenix6.unmanaged.feed_enable(ENABLE_TIMEOUT_SECONDS)
            if hasattr(request, "with_output"):
                request = request.with_output(TEST_OUTPUT)
            else:
                request.output = TEST_OUTPUT
            motor.set_control(request)

            if elapsed - last_print >= 0.25:
                print_status(motor, elapsed)
                last_print = elapsed

            time.sleep(LOOP_PERIOD_SECONDS)

        print("\nStopping motor...")
        stop_start = time.monotonic()
        while time.monotonic() - stop_start < 0.5:
            phoenix6.unmanaged.feed_enable(ENABLE_TIMEOUT_SECONDS)
            if hasattr(request, "with_output"):
                request = request.with_output(0.0)
            else:
                request.output = 0.0
            motor.set_control(request)
            time.sleep(LOOP_PERIOD_SECONDS)

        print_status(motor, TEST_SECONDS)
        print("Done.")

    except KeyboardInterrupt:
        print("Interrupted. Stopping motor...")
    finally:
        try:
            for _ in range(10):
                phoenix6.unmanaged.feed_enable(ENABLE_TIMEOUT_SECONDS)
                if hasattr(request, "with_output"):
                    request = request.with_output(0.0)
                else:
                    request.output = 0.0
                motor.set_control(request)
                time.sleep(LOOP_PERIOD_SECONDS)
        except Exception:
            pass


if __name__ == "__main__":
    main()
