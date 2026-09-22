#!/usr/bin/env python3
"""Preserve existing worker commands when upgrading the official HandBrake CLI."""

import os
import sys

BINARY = "/opt/handbrake/bin/HandBrakeCLI"
LEGACY_OPTIONS = {"--two-pass": "--multi-pass", "--no-two-pass": "--no-multi-pass"}


def main():
    # exec preserves process-group signals, exit status, and stdout/stderr streams.
    os.execv(BINARY, [BINARY, *(LEGACY_OPTIONS.get(arg, arg) for arg in sys.argv[1:])])


if __name__ == "__main__":
    main()
