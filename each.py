#!/usr/bin/python3
"""
Name: each.py
Author: Nikita Neverov (BMTLab)
Version: 1.0.0
Date: 2025-11-21
License: MIT

Description
-----------
Run a command once per input token, substituting a placeholder with that token.

The script is conceptually similar to ``xargs`` but uses an explicit placeholder
(e.g. ``{}`` or ``{FILE}``) inside a shell command template.
For each token read from stdin, the placeholder is replaced with the token
(optionally shell-quoted), and the resulting command is executed.

Key features
------------
- Safe shell quoting by default via :func:`shlex.quote`.
- Custom placeholder (default: ``"{}"``).
- Custom delimiters (``-d``), NUL mode (``-0``), or robust default
  :meth:`str.splitlines` behavior.
- Optional trimming of whitespace and/or keeping empty tokens.
- Parallel execution (``-P``) with stdin-safety guard (requires ``--no-stdin``).
- Dry-run (preview commands) and trace mode (print commands before running).
- Configurable stdin encoding and error handling.

Examples
--------
Line-delimited tokens::

    printf '%s\n' a b c | each 'echo {}'

NUL-delimited tokens (e.g., from ``find -print0``)::

    find . -type f -print0 | each -0 'wc -l {}'

Custom delimiter and trimming::

    printf 'foo ;  bar ;baz' | each -d ';' --strip 'echo {}'

Parallel gzip (stdin disabled to avoid contention)::

    find logs -type f -name '*.log' -print0 \
      | each -0 -P 4 --no-stdin 'gzip -9 {}'

Custom placeholder without quoting::

    printf 'a b\nc d\n' | each -p '{FILE}' --no-quote 'echo {FILE}'

Disclaimer
----------
This tool executes arbitrary commands in your shell.
Running it with elevated privileges or against critical data
may cause data loss or other damage if used incorrectly.
Use it at your own risk. The author and contributors accept no liability
for any consequences of using this software.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Sequence
from typing import Any

EXIT_OK: int = 0
EXIT_USAGE: int = 64
EXIT_NO_PLACEHOLDER: int = 65
EXIT_BAD_ENV: int = 66
EXIT_NEEDS_NO_STDIN_FOR_PAR: int = 67
EXIT_CHILD_FAILED: int = 70


def eprint(*args: Any) -> None:
    """Print the given arguments to stderr.

    Parameters
    ----------
    *args : Any
        Objects to print. They are passed directly to :func:`print`.
    """
    # noinspection PyTypeChecker
    print(*args, file=sys.stderr)


def compile_delimiters_regex(delimiters: Sequence[str]) -> re.Pattern[str]:
    """Compile a union regex from literal delimiters.

    Each delimiter is treated as a literal substring
    (escaped using :func:`re.escape`), and the final pattern matches any of them.

    Parameters
    ----------
    delimiters : Sequence[str]
        List of delimiter strings (treated literally).

    Returns
    -------
    re.Pattern
        Compiled regular expression that matches any of the delimiters.
    """
    escaped = (re.escape(d) for d in delimiters)
    pattern: str = "|".join(escaped)
    return re.compile(pattern)


def decode_stdin(encoding: str, errors: str) -> str:
    """Read stdin as bytes and decode it into text.

    Parameters
    ----------
    encoding : str
        Text encoding (for example ``"utf-8"``).
    errors : str
        Error strategy (for example ``"strict"``, ``"replace"``,
        or ``"surrogatepass"``).

    Returns
    -------
    str
        Decoded stdin contents as a single string.
    """
    data: bytes = sys.stdin.buffer.read()
    return data.decode(encoding, errors=errors)


def tokenize_input(
        text: str,
        delimiters: Sequence[str] | None,
        use_null: bool,
        keep_empty: bool,
        strip_ws: bool,
) -> list[str]:
    """Split input text into tokens according to the chosen strategy.

    Parameters
    ----------
    text : str
        Entire stdin content (already decoded).
    delimiters : Sequence[str] or None
        Optional literal delimiters. If provided and ``use_null`` is ``False``,
        a regex is built that splits on any of these delimiters.
        If ``None`` and ``use_null`` is ``False``, :meth:`str.splitlines` is used.
    use_null : bool
        If ``True``, split on NUL characters (``"\\x00"``) regardless of ``delimiters``.
    keep_empty : bool
        If ``True``, keep empty tokens (for example, consecutive delimiters).
        If ``False``, empty tokens are skipped.
    strip_ws : bool
        If ``True``, strip leading and trailing whitespace from each token
        before evaluating emptiness and before returning.

    Returns
    -------
    list[str]
        List of tokens in the order they appear.
    """
    if use_null:
        parts: list[str] = text.split("\x00")
    elif delimiters:
        delimiters_regex: re.Pattern[str] = compile_delimiters_regex(delimiters)
        parts = delimiters_regex.split(text)
    else:
        # Robust across ``\n``, ``\r\n``, ``\r``
        parts = text.splitlines()

    tokens: list[str] = []
    append = tokens.append

    for part in parts:
        token: str = part.strip() if strip_ws else part
        if not token and not keep_empty:
            continue
        append(token)

    return tokens


def apply_environment(env_kv: Sequence[str]) -> dict[str, str]:
    """Build a child-process environment mapping from ``KEY=VALUE`` items.

    The resulting mapping is ``{**os.environ, **extra}``, that is,
    the current process environment extended (or overridden) by the user-provided entries.

    Parameters
    ----------
    env_kv : Sequence[str]
        Items in the form ``"KEY=VALUE"``.

    Returns
    -------
    dict[str, str]
        Merged environment mapping.

    Raises
    ------
    ValueError
        If any item does not contain ``"="`` or starts with ``"="``
        (i.e., missing key).
    """
    extra: dict[str, str] = {}
    for item in env_kv:
        if "=" not in item or item.startswith("="):
            raise ValueError(f"Invalid env item (expected KEY=VALUE): {item!r}")
        key, val = item.split("=", 1)
        extra[key] = val

    merged: dict[str, str] = dict(os.environ)
    merged.update(extra)
    return merged


def build_command(
        template: str,
        placeholder: str,
        argument: str,
        quote: bool,
) -> str:
    """Substitute the placeholder with the (optionally quoted) argument.

    Parameters
    ----------
    template : str
        Command template containing the placeholder
        (for example ``"echo {}"`` or ``"wc -l {}"``).
    placeholder : str
        Placeholder string to replace (for example ``"{}"`` or ``"{FILE}"``).
    argument : str
        Replacement value (single token).
    quote : bool
        If ``True``, the argument is shell-quoted via :func:`shlex.quote`
        before substitution. If ``False``, the argument is inserted "as is".

    Returns
    -------
    str
        Final shell command string with placeholder occurrences replaced.
    """
    arg: str = shlex.quote(argument) if quote else argument
    return template.replace(placeholder, arg)


def run_command(
        command_str: str,
        shell_path: str | None,
        pass_stdin: bool,
        trace: bool,
        env: dict[str, str] | None,
) -> int:
    """Execute a single shell command string.

    Parameters
    ----------
    command_str : str
        Final command to execute (shell string).
    shell_path : str or None
        If provided, used as the shell executable (for example ``"/bin/bash"``).
        Otherwise, the system default shell is used.
    pass_stdin : bool
        If ``True``, forward the current process's stdin to the child.
        This must be ``False`` when running commands in parallel.
    trace : bool
        If ``True``, print the command to stderr before running it
        (similar to ``xargs -t``).
    env : dict[str, str] or None
        Custom environment mapping; if ``None``,
        the child inherits :data:`os.environ`.

    Returns
    -------
    int
        Child process exit code (``0`` means success).
    """
    if trace:
        eprint(f"+ {command_str}")

    # When pass_stdin=False we avoid passing our stdin to the child,
    # which is important in parallel mode
    stdin = sys.stdin if pass_stdin else None

    # Note: text=False keeps raw bytes for stdio, avoiding encoding surprises
    completed = subprocess.run(
        command_str,
        shell=True,
        executable=shell_path,
        stdin=stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
        text=False,
        check=False,
        env=env,
    )
    return completed.returncode


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the ``each`` CLI.

    Parameters
    ----------
    argv : Sequence[str] or None, optional
        Optional override for ``sys.argv[1:]``. If ``None``,
        the current process argument vector is used.

    Returns
    -------
    argparse.Namespace
        Parsed options and arguments.

    Raises
    ------
    SystemExit
        If validation fails
        (for example, missing placeholder in the command or invalid environment entries).
    """
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="each",
        description=(
            "Run each token through a command "
            "(friendlier xargs with explicit {} substitution)."
        ),
    )
    parser.add_argument(
        "command",
        type=str,
        help=(
            "Command template with a placeholder (default '{}'), "
            'e.g. "echo {}" or "wc -l {}".'
        ),
    )
    parser.add_argument(
        "-p",
        "--placeholder",
        type=str,
        default="{}",
        help="Placeholder to replace (default: '{}').",
    )
    # Input splitting
    parser.add_argument(
        "-d",
        "--delimiter",
        action="append",
        default=None,
        help=(
            "Literal delimiter; can be repeated. "
            "If omitted and not using -0, splitlines() is used."
        ),
    )
    parser.add_argument(
        "-0",
        "--null",
        action="store_true",
        help="NUL-delimited input (like xargs -0).",
    )
    parser.add_argument(
        "--strip",
        action="store_true",
        help="Strip leading/trailing whitespace from each token.",
    )
    parser.add_argument(
        "--keep-empty",
        action="store_true",
        help="Keep empty tokens produced by consecutive delimiters.",
    )
    # I/O and encoding
    parser.add_argument(
        "-E",
        "--encoding",
        default="utf-8",
        help="Encoding to decode stdin (default: utf-8).",
    )
    parser.add_argument(
        "--errors",
        default="strict",
        help=(
            "Decoding error policy: 'strict' (default), 'replace', "
            "'surrogatepass', etc."
        ),
    )
    # Execution control
    parser.add_argument(
        "-P",
        "--max-procs",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Run up to N commands in parallel (default: 1 = sequential). "
            "Requires --no-stdin when N > 1."
        ),
    )
    parser.add_argument(
        "--no-stdin",
        action="store_true",
        help=(
            "Do not forward this process's stdin to child processes "
            "(required for -P > 1)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not execute; just print the final command for each token.",
    )
    parser.add_argument(
        "-t",
        "--trace",
        action="store_true",
        help="Print each final command before executing it (like xargs -t).",
    )
    parser.add_argument(
        "--no-quote",
        action="store_true",
        help=(
            "Insert the token without shell quoting (unsafe if tokens contain "
            "spaces or metacharacters)."
        ),
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        help="Extra environment variable KEY=VALUE (repeatable).",
    )
    parser.add_argument(
        "--shell",
        default=None,
        help=(
            "Shell executable to use (e.g. /bin/bash). "
            "Default: system default for shell=True."
        ),
    )

    args: argparse.Namespace = parser.parse_args(argv)

    # Validate placeholder presence
    placeholder: str = args.placeholder
    if placeholder not in args.command:
        eprint(f"ERROR: command must contain placeholder {placeholder!r}")
        raise SystemExit(EXIT_NO_PLACEHOLDER)

    # Validate environment items (structure only)
    try:
        _ = apply_environment(args.env)
    except ValueError as exc:
        eprint(f"ERROR: {exc}")
        raise SystemExit(EXIT_BAD_ENV) from exc

    # Guard against stdin contention in parallel mode
    if args.max_procs > 1 and not args.no_stdin:
        eprint(
            "ERROR: -P/--max-procs > 1 requires --no-stdin to avoid "
            "stdin contention."
        )
        raise SystemExit(EXIT_NEEDS_NO_STDIN_FOR_PAR)

    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``each`` CLI.

    Parameters
    ----------
    argv : Sequence[str] or None, optional
        Command-line arguments excluding the program name.
        If ``None``, ``sys.argv[1:]`` is used.

    Returns
    -------
    int
        Exit status code. ``0`` on success, non-zero on error.
    """
    if argv is None:
        argv = sys.argv[1:]

    args: argparse.Namespace = parse_args(argv)

    # Build environment (structure validated already)
    env: dict[str, str] | None = apply_environment(args.env) if args.env else None

    # Ingest and tokenize stdin.
    text: str = decode_stdin(encoding=args.encoding, errors=args.errors)
    tokens: list[str] = tokenize_input(
        text=text,
        delimiters=args.delimiter,
        use_null=args.null,
        keep_empty=bool(args.keep_empty),
        strip_ws=bool(args.strip),
    )

    # Short-circuit: nothing to do
    if not tokens:
        return EXIT_OK

    # Pre-bind to local variables for minor speed/clarity improvements
    template: str = args.command
    placeholder: str = args.placeholder
    quote: bool = not args.no_quote

    if args.dry_run:
        for tok in tokens:
            cmd_str: str = build_command(
                template=template,
                placeholder=placeholder,
                argument=tok,
                quote=quote,
            )
            print(cmd_str)
        return EXIT_OK

    # Sequential execution path
    if args.max_procs <= 1:
        for tok in tokens:
            cmd_str: str = build_command(
                template=template,
                placeholder=placeholder,
                argument=tok,
                quote=quote,
            )
            rc: int = run_command(
                command_str=cmd_str,
                shell_path=args.shell,
                pass_stdin=not args.no_stdin,
                trace=args.trace,
                env=env,
            )
            if rc != 0:
                # Propagate the first failing child exit code
                return rc or EXIT_CHILD_FAILED
        return EXIT_OK

    # Parallel execution path (order of outputs is not guaranteed)
    return_code: int = EXIT_OK
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_procs) as pool:
        futures: list[concurrent.futures.Future[int]] = []
        for tok in tokens:
            cmd_str: str = build_command(
                template=template,
                placeholder=placeholder,
                argument=tok,
                quote=quote,
            )
            futures.append(
                pool.submit(
                    run_command,
                    cmd_str,
                    args.shell,
                    False,  # pass_stdin is False in parallel mode
                    args.trace,
                    env,
                )
            )

        for fut in concurrent.futures.as_completed(futures):
            rc = fut.result()
            if rc != 0 and return_code == EXIT_OK:
                return_code = rc or EXIT_CHILD_FAILED

    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
### End
