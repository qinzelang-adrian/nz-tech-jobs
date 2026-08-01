import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Must be set before any test module imports app.py — app.py starts its
# background scheduler (which fires an immediate real refresh) as a
# side effect of being imported, since it also needs to run that way under
# a production WSGI server.
os.environ.setdefault("DISABLE_SCHEDULER", "1")
