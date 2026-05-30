from phoenix6 import hardware
import time

# On non-FRC Linux, leaving the CAN bus blank defaults to can0.
# The ID does not need to be real just to start diagnostics.
motor = hardware.TalonFX(0)

print("Phoenix Diagnostic Server should be running on port 1250.")
print("Leave this running while using Tuner X. Press Ctrl+C to stop.")

while True:
    time.sleep(1)
