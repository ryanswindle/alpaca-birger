import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

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
print(f"  SupportedActions:   {foc.SupportedActions}")

# Quarter points of whatever range this lens learned, parking at infinity
# (MaxStep) at the end. MaxStep is lens-specific, so nothing here is hardcoded.
quarter = foc.MaxStep // 4
for target in [quarter, 2 * quarter, 3 * quarter, 0, foc.MaxStep]:
    print(f"\nMoving to position {target} of {foc.MaxStep}...")
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
