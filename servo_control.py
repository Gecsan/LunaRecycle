# Simple MG90S servo control using gpiozero
from gpiozero import AngularServo
from time import sleep
import sys

# GPIO pin for servo signal (change as needed)
SERVO_PIN = 12
SERVO_MIN_ANGLE = 0
SERVO_MAX_ANGLE = 180

# Pulse calibration for wider travel. Adjust if your specific servo needs tuning.
MIN_PULSE_WIDTH = 0.0005
MAX_PULSE_WIDTH = 0.0025
FRAME_WIDTH = 0.02

# Accept angle as argument (0-180 degrees)
def set_servo_angle(angle):
    servo = AngularServo(
        SERVO_PIN,
        min_angle=SERVO_MIN_ANGLE,
        max_angle=SERVO_MAX_ANGLE,
        min_pulse_width=MIN_PULSE_WIDTH,
        max_pulse_width=MAX_PULSE_WIDTH,
        frame_width=FRAME_WIDTH,
    )
    servo.angle = angle
    sleep(0.8)
    servo.detach()

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 servo_control.py <angle>")
        sys.exit(1)
    angle = float(sys.argv[1])
    if not SERVO_MIN_ANGLE <= angle <= SERVO_MAX_ANGLE:
        print(f"Angle must be between {SERVO_MIN_ANGLE} and {SERVO_MAX_ANGLE}")
        sys.exit(1)
    set_servo_angle(angle)
