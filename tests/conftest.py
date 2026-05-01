import os


# Qt widget tests run in headless/offscreen mode so normal unit-test runs do
# not depend on a desktop window server being available.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
