#!/usr/bin/env python3

import board
import busio
import adafruit_mcp9600

# Adafruit MCP9600 breakout possible I2C addresses
MCP9600_ADDRESSES = [0x60, 0x64, 0x65, 0x66, 0x67]

EXPECTED_COUNT = 3

i2c = busio.I2C(board.SCL, board.SDA, frequency=100000)


def scan_i2c():
    while not i2c.try_lock():
        pass

    try:
        return i2c.scan()
    finally:
        i2c.unlock()


found_addresses = scan_i2c()

print("All I2C addresses found:")
print([f"0x{addr:02X}" for addr in found_addresses])
print()

possible_mcp9600 = [
    addr for addr in found_addresses
    if addr in MCP9600_ADDRESSES
]

detected = []

for addr in possible_mcp9600:
    try:
        sensor = adafruit_mcp9600.MCP9600(i2c, address=addr)

        hot_temp = sensor.temperature
        cold_temp = sensor.ambient_temperature

        detected.append(addr)

        print(
            f"Found MCP9600 at 0x{addr:02X} | "
            f"Hot junction: {hot_temp:.2f} C | "
            f"Cold junction: {cold_temp:.2f} C"
        )

    except Exception as error:
        print(f"Address 0x{addr:02X} responded, but MCP9600 read failed: {error}")

print()

if len(detected) == EXPECTED_COUNT:
    print(f"Success: detected all {EXPECTED_COUNT} MCP9600 boards.")
else:
    print(f"Warning: detected {len(detected)} MCP9600 board(s), expected {EXPECTED_COUNT}.")
    print("Detected MCP9600 addresses:", [f"0x{addr:02X}" for addr in detected])
