import os
import tempfile


# Qt widget tests run in headless/offscreen mode so normal unit-test runs do
# not depend on a desktop window server being available.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("JOYREAD_RUNTIME_DIR", tempfile.mkdtemp(prefix="joyread-tests-"))
