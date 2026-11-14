"""Console output helpers. Messages are written for end users, not developers."""
import os
import sys

if os.name == "nt":
    os.system("")  # enables ANSI escape codes in the classic Windows console

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
ASSUME_YES = False  # set by --yes


def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def step(n, total, text):
    print(f"\n{_c('1;34', f'[{n}/{total}]')} {_c('1', text)}", flush=True)


def info(text):
    print(f"    {text}", flush=True)


def ok(text):
    print(f"    {_c('32', 'OK')} {text}", flush=True)


def warn(text):
    print(f"    {_c('33', '!!')} {text}", flush=True)


def err(text):
    print(f"    {_c('31', 'ERROR')} {text}", file=sys.stderr, flush=True)


def ask_yes_no(question, default=True):
    if ASSUME_YES or not sys.stdin.isatty():
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        answer = input(f"    {question} {suffix} ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def ask_choice(question, options, default_index=0):
    """options: list of display strings. Returns the chosen index."""
    if ASSUME_YES or not sys.stdin.isatty():
        return default_index
    for i, text in enumerate(options, 1):
        marker = "*" if i - 1 == default_index else " "
        print(f"     {marker}{i}) {text}")
    while True:
        answer = input(f"    {question} [{default_index + 1}] ").strip()
        if not answer:
            return default_index
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1


class StepFailed(Exception):
    """A step failed in a way the user needs to act on; message is user-facing."""
