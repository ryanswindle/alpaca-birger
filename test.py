import time

from alpaca.focuser import Focuser
from config import config


foc = Focuser(f"{config.server.host}:{config.server.port}", 0)

print(f"  Name:   {foc.Name}")
print(f"  Driver: {foc.DriverVersion}\n")

# Connect
print("Connecting...")
foc.Connected = True
print(f"  Connected: {foc.Connected}")

# Read parameters
print(f"  Absolute:           {foc.Absolute}")
print(f"  MaxStep:            {foc.MaxStep}")
print(f"  MaxIncrement:       {foc.MaxIncrement}")
print(f"  Position:           {foc.Position}")
print(f"  TempCompAvailable:  {foc.TempCompAvailable}")
print(f"  TempComp:           {foc.TempComp}")

# Cycle through a few positions
start = foc.Position
for target in [0x1000, 0x2000, 0x3000, 0x3FFF, 0, start]:
    print(f"\nMoving to position {target} (0x{target:04x})...")
    foc.Move(target)
    t0 = time.time()
    while foc.IsMoving:
        time.sleep(0.5)
        if (time.time() - t0) > 60:
            print("  Timed out waiting for move")
            break
    print(f"  Position: {foc.Position}")
    time.sleep(1)

# Disconnect
print("\nDisconnecting...")
foc.Connected = False
print(f"  Connected: {foc.Connected}")
print()
