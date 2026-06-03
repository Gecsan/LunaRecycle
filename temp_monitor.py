#!/usr/bin/env python3
"""Simple console temperature monitor for MCP9600 zones."""

import argparse
import time

import adafruit_mcp9600
import board
import busio


DEFAULT_ADDRESSES = [0x65, 0x66, 0x67]


def scan_i2c_addresses(i2c_bus):
    while not i2c_bus.try_lock():
        time.sleep(0.01)

    try:
        return set(i2c_bus.scan())
    finally:
        i2c_bus.unlock()


def build_sensor_map(i2c_bus, addresses):
    found = scan_i2c_addresses(i2c_bus)
    sensors = {}

    print("Found I2C devices:", [f"0x{addr:02X}" for addr in sorted(found)])

    for address in addresses:
        if address not in found:
            print(f"0x{address:02X}: not found")
            continue

        try:
            sensors[address] = adafruit_mcp9600.MCP9600(
                i2c_bus,
                address=address,
                tctype="K",
                tcfilter=0,
            )
            print(f"0x{address:02X}: sensor ready")
        except Exception as exc:
            print(f"0x{address:02X}: init error: {exc}")

    return sensors


def print_readings(sensors):
    if not sensors:
        print("No MCP9600 sensors are available.")
        return

    line_parts = []
    for address in sorted(sensors):
        sensor = sensors[address]
        try:
            hot_c = sensor.temperature
            ambient_c = sensor.ambient_temperature
            line_parts.append(
                f"0x{address:02X} hot={hot_c:6.2f}C ambient={ambient_c:6.2f}C"
            )
        except Exception as exc:
            line_parts.append(f"0x{address:02X} error={exc}")

    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] " + " | ".join(line_parts), flush=True)


def parse_args():
    parser = argparse.ArgumentParser(description="Console MCP9600 temperature monitor")
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Seconds between updates (default: 1.0)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Print one reading and exit",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    i2c = busio.I2C(board.SCL, board.SDA, frequency=85_000)
    sensors = build_sensor_map(i2c, DEFAULT_ADDRESSES)

    if args.once:
        print_readings(sensors)
        return

    print("Starting console temperature display. Press Ctrl+C to stop.")
    while True:
        print_readings(sensors)
        time.sleep(max(0.1, args.interval))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped.")
