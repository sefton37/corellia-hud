"""Prints the seconds to add to the real clock so that "now" reads HH:MM:SS local today (for HUD_TEST_CLOCK_OFFSET)."""
import sys
import time
from datetime import datetime

h, m, s = (int(x) for x in sys.argv[1].split(":"))
target = datetime.now().replace(hour=h, minute=m, second=s, microsecond=0).timestamp()
print(round(target - time.time(), 3))
