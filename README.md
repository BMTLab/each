# each

A small, single-file CLI tool that runs a shell command once per input token, 
replacing an explicit placeholder (such as `{}` or `{FILE}`) with each token.

It plays a similar role to `xargs`, but aims to be:

* more explicit (no guessing about where arguments go),
* safer by default (shell quoting enabled unless you opt out),
* more ergonomic for line-, delimiter-, or NUL-separated inputs.

---

## Features

1. Explicit placeholder substitution (default: `{}`)
2. Safe shell-quoting by default via `shlex.quote`
3. Flexible tokenization:
    * newline-based (default),
    * custom delimiters (`-d`),
    * NUL-delimited input (`-0`, like `xargs -0`).
4. Optional whitespace trimming and control over empty tokens
5. Sequential or parallel execution (`-P`), with stdin-safety guard
6. `--dry-run` preview and `-t/--trace` for shell-like tracing
7. Configurable stdin encoding and error handling
8. Extra environment variables and custom shell executable

> [!TIP]
> Think of `each` as "xargs with an explicit `{}` placeholder and safer defaults".

---

## Requirements

* Python 3.8+ (system Python is fine; no virtual environment required)
* POSIX-like shell environment (Linux, macOS, WSL, etc.)

The script has no third-party dependencies.

---

## Installation

### 1. Clone or download

```bash
# Clone the repository
git clone https://github.com/BMTLab/each.git
cd each
```

Or download `each.py` directly from the repository (e.g., via GitHub web UI).

### 2. Make the script executable

```bash
chmod +x each.py
```

You can now run it as:

```bash
./each.py 'echo {}'
```

### 3. (Optional) Install into your PATH

Rename the script to `each` and move it somewhere on your `PATH`, for example:

```bash
sudo ln -s <path-to-each>/each.py /usr/local/bin/each
```

Now you can use:

```bash
each 'echo {}'
```

---

## Quick start

### Echo each line

```bash
printf '%s\n' a b c | each 'echo {}'
# Output:
# a
# b
# c
```

### Count lines in files

```bash
printf '%s\n' file1.txt file2.txt | each 'wc -l {}'
```

### Find files and process them

With NUL-delimited input (safe for weird file names):

```bash
find . -type f -name '*.log' -print0 | each -0 'gzip -9 {}'
```

### Custom placeholder

```bash
printf '%s\n' a b c | each -p '{ITEM}' 'echo item={ITEM}'
# Output:
# item=a
# item=b
# item=c
```

### Custom placeholder without quoting

```bash
printf 'a b\nc d\n' | each -p '{FILE}' --no-quote 'echo {FILE}'
# Output:
# a b
# c d
```

> [!IMPORTANT]
> `--no-quote` disables shell quoting.
> Only use it when you fully control the input and are aware of the risks
> (spaces, globbing, shell metacharacters, etc.).

---

## Usage

Basic syntax:

```bash
each [OPTIONS] 'command with {} placeholder'
```

The `command` argument is a shell command template containing a placeholder (default `{}`).
For each input token, all occurrences of that placeholder in `command` are replaced
with the (optionally quoted) token, and the resulting command is executed.

### Command-line options

| Option                | Type                   | Default        | Description                                                                         |
| --------------------- | ---------------------- | -------------- | ----------------------------------------------------------------------------------- |
| `command`             | positional             | —              | Shell command template containing the placeholder (default `{}`).                   |
| `-p`, `--placeholder` | string                 | `{}`           | Placeholder substring to replace with each token.                                   |
| `-d`, `--delimiter`   | repeatable string      | —              | Literal delimiter(s) for splitting input (see below).                               |
| `-0`, `--null`        | flag                   | `False`        | Treat input as NUL-delimited (`\0`), like `xargs -0`.                               |
| `--strip`             | flag                   | `False`        | Strip leading/trailing whitespace from each token.                                  |
| `--keep-empty`        | flag                   | `False`        | Keep empty tokens (by default they are dropped).                                    |
| `-E`, `--encoding`    | string                 | `utf-8`        | Encoding for decoding stdin as text.                                                |
| `--errors`            | string                 | `strict`       | Error policy for decoding (`strict`, `replace`, `surrogatepass`, etc.).             |
| `-P`, `--max-procs`   | integer                | `1`            | Run up to N commands in parallel. Requires `--no-stdin` when N > 1.                 |
| `--no-stdin`          | flag                   | `False`        | Do not forward this process's stdin to child processes. Required for parallel mode. |
| `--dry-run`           | flag                   | `False`        | Do not execute anything; just print the final command per token.                    |
| `-t`, `--trace`       | flag                   | `False`        | Print commands before executing them (like `xargs -t`).                             |
| `--no-quote`          | flag                   | `False`        | Insert tokens as-is instead of quoting them for the shell.                          |
| `--env`               | repeatable `KEY=VALUE` | —              | Add or override environment variables for child processes.                          |
| `--shell`             | string                 | system default | Path to the shell executable (e.g., `/bin/bash`).                                   |

> [!TIP]
> Combine `--dry-run` and `--trace` while experimenting:
>
> ```bash
> find . -maxdepth 1 -type f -print0 | each -0 --dry-run -t 'rm {}'
> ```
>
> This will show you exactly what *would* be executed without actually deleting anything.

---

## Input tokenization

`each` consumes all of stdin, decodes it according to `--encoding`
and `--errors`, and then splits the resulting text into tokens.

### Default (newline-based)

If neither `-d/--delimiter` nor `-0/--null` are used, input is split using
`str.splitlines()`, which handles `\n`, `\r\n`, and `\r` transparently.

```bash
printf 'foo\nbar\n' | each 'echo {}'
```

### Custom delimiters (`-d` / `--delimiter`)

You can provide one or more literal delimiters. Internally, they are combined into a single regular expression 
that splits on any of them.

```bash
printf 'foo ;  bar ;baz' | each -d ';' --strip 'echo {}'
# Tokens: "foo", "bar", "baz"
```

You can repeat `-d`:

```bash
printf 'a,b;c' | each -d ',' -d ';' 'echo {}'
# Tokens: "a", "b", "c"
```

### NUL-delimited input (`-0` / `--null`)

To interoperate with tools like `find -print0` that produce NUL-delimited streams, use `-0`:

```bash
find . -type f -print0 | each -0 'wc -l {}'
```

### Whitespace stripping and empty tokens

* `--strip` trims leading and trailing whitespace from each token *before* deciding whether it is empty.
* `--keep-empty` keeps empty tokens; by default they are skipped.

This allows you to control how consecutive delimiters or trailing delimiters are handled.

---

## Execution model & parallelism

For each token:

1. The placeholder in the command template is replaced with the (optionally quoted) token.
2. The resulting string is executed as a shell command via `subprocess.run(..., shell=True, ...)`.

### Sequential execution (default)

With the default `-P 1`, commands are executed one by one.
The first failing child exit code is propagated as the exit code of `each`.

```bash
printf '%s\n' a b c | each 'echo {}'
```

### Parallel execution (`-P`)

You can run up to `N` commands in parallel:

```bash
find logs -type f -name '*.log' -print0 \
  | each -0 -P 4 --no-stdin 'gzip -9 {}'
```

> [!IMPORTANT]
> When `-P N` is used with `N > 1`, you **must** pass `--no-stdin`.
> Otherwise, `each` will abort with an error to avoid multiple child processes competing for the same stdin.

> [!WARNING]
> Parallel execution does not guarantee ordering of output.
> If you need ordered output, use sequential mode (`-P 1`, the default).

### Exit codes

* `0` – success, or nothing to do (no tokens).
* Non-zero – the first non-zero exit code returned by any child process.
* `65` – command template does not contain the configured placeholder.
* `66` – invalid `--env` entry (does not look like `KEY=VALUE`).
* `67` – `-P/--max-procs > 1` used without `--no-stdin`.

In error cases, a human-readable message is printed to stderr.

---

## Environment and shell

### Environment variables (`--env`)

You can extend or override environment variables for child processes:

```bash
printf '%s\n' a b | each --env FOO=bar 'echo $FOO:{}'
```

Each `--env KEY=VALUE` argument is parsed and merged into a copy of the current process environment.

### Custom shell (`--shell`)

By default, `each` uses the system default shell for `subprocess.run(..., shell=True)`.
You can explicitly choose a shell:

```bash
printf '%s\n' a b | each --shell /bin/bash 'echo ${0}:{}'
```

This can be useful if your system defaults to a different shell, and you rely on specific shell features.

---

## Safety considerations

`each` is a thin, explicit wrapper around your shell:

* It will run **exactly** the commands you ask it to run.
* By default, tokens are shell-quoted, which protects against many common issues with spaces and simple metacharacters.
* If you disable quoting (`--no-quote`), you are fully responsible for ensuring that inputs
are safe and properly escaped.

> [!WARNING]
> Do **not** use `each` with untrusted input when the command template can modify or delete data.
> Treat it like any other shell scripting tool.

For dry runs and debugging, prefer:

```bash
... | each --dry-run -t 'your command with {}'
```

---

## License & disclaimer

This project is licensed under the [MIT License](./LICENSE).

> [!IMPORTANT]
> This tool executes arbitrary commands in your shell. Running it with elevated privileges or against critical data 
> may cause data loss or other damage if used incorrectly. Use it at your own risk.
> The author and contributors accept no liability for any consequences of using this software.

---

## Contributing

Issues and pull requests are welcome.
If you would like to propose new flags or behavior, please include concrete examples and a brief rationale
so we can keep `each` small, understandable, and focused on its core job :innocent:
