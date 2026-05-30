import board
import busio
import adafruit_mcp9600

i2c = busio.I2C(board.SCL, board.SDA, frequency=10000)

mcp1 = adafruit_mcp9600.MCP9600(i2c, address=0x67)
mcp2 = adafruit_mcp9600.MCP9600(i2c, address=0x66)
mcp3 = adafruit_mcp9600.MCP9600(i2c, address=0x65)
