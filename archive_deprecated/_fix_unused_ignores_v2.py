"""Fix broken files: re-apply unused-ignore removal WITHOUT eating newlines."""

import os
import re
import subprocess

# Get list of files with unused-ignore from saved mypy output
with open("mypy_errors.txt", encoding="utf-8") as f:
    text = f.read()

file_lines: dict[str, set[int]] = {}
for m in re.finditer(
    r'^(.+?):(\d+): error: Unused "type: ignore" comment\s+\[unused-ignore\]', text, re.MULTILINE
):
    fpath = m.group(1).replace("\\", "/")
    file_lines.setdefault(fpath, set()).add(int(m.group(2)))

total = sum(len(v) for v in file_lines.values())
print(f"Processing {total} unused-ignore across {len(file_lines)} files")

fixed = 0
for fpath, linenums in file_lines.items():
    if not os.path.exists(fpath):
        continue

    # Read git HEAD version to avoid accumulated corruption
    result = subprocess.run(["git", "show", f"HEAD:{fpath}"], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  SKIP (git error): {fpath}")
        continue

    lines = result.stdout.splitlines(keepends=True)

    new_lines = []
    for i, line in enumerate(lines, 1):
        if i in linenums:
            # Remove # type: ignore[...] but PRESERVE trailing newline
            # Use [^\S\n] instead of \s to avoid eating newlines
            new_line = re.sub(
                r"[^\S\n]*#[^\S\n]*type:[^\S\n]*ignore[^\S\n]*(\[[^\]]*\])?[^\S\n]*", "", line
            )
            # If line became only whitespace, keep it as empty line
            stripped = new_line.rstrip("\n\r")
            if stripped == "" or stripped.isspace():
                new_line = "\n"
            new_lines.append(new_line)
            if new_line != line:
                fixed += 1
        else:
            new_lines.append(line)

    with open(fpath, "w", encoding="utf-8", newline="") as f:
        f.writelines(new_lines)

print(f"Fixed {fixed} lines across {len(file_lines)} files")
