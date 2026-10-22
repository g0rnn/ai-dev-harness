"""Windows test children share a hidden console, including Git's shell descendants.

Imported only by tests. No commands run at import time. Patching Popen (rather
than each run call) also covers code invoked in-process by integration tests.
Explicit detached/no-console launches retain their production flags, so their
contract tests still exercise the original options. Output and exit codes are
unchanged; this policy lasts only for the test interpreter.
"""
import copy
import os
import subprocess


def window_options(options, platform=None):
    options = dict(options)
    if (platform or os.name) != "nt":
        return options
    flags = options.get("creationflags", 0)
    if not flags & (subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW):
        # CREATE_NO_WINDOW alone leaves a Git/sh descendant free to allocate a
        # console. Give the whole child tree a hidden console to inherit.
        options["creationflags"] = flags | subprocess.CREATE_NEW_CONSOLE
        startup = copy.copy(options.get("startupinfo")) if options.get("startupinfo") else subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        options["startupinfo"] = startup
    return options


def install():
    if os.name != "nt" or getattr(subprocess.Popen, "_harness_test_windowless", False):
        return
    original = subprocess.Popen

    class WindowlessPopen(original):
        _harness_test_windowless = True

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **window_options(kwargs))

    subprocess.Popen = WindowlessPopen


install()
