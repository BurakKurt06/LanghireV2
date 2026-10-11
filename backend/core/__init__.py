# Core backend modules
import os as _os

# browser-use sends each run's task (with the resume), the values typed into
# forms and the pages visited to its analytics service unless this is off.
_os.environ["ANONYMIZED_TELEMETRY"] = "false"
_os.environ["BROWSER_USE_CLOUD_SYNC"] = "false"
