"""
context.py  —  Project context generator
=========================================
Generates a single text snapshot of your entire project
that you can paste into AI prompts for context.

Usage:
    python context.py              # full project context
    python context.py --tree       # file tree only
    python context.py --files      # file contents only
    python context.py --minify     # minified output (compact, no comments)
    python context.py --out ctx.txt  # save to file instead of printing
"""

import os
import sys
import re
import argparse
from datetime import datetime
from pathlib import Path

# =============================================================================
# CONFIGURATION — edit this to match your project
# =============================================================================


# Directories to completely skip
SKIP_DIRS = {
    ".venv", "venv", "env", ".env",
    "node_modules", ".git", "__pycache__",
    "dist", "build", ".next", ".nuxt",
    "migrations",          # large auto-generated files — skip by default
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "coverage", "htmlcov",
}

# File extensions to include content for
INCLUDE_EXTENSIONS = {
    # Python
    ".py",
    # JavaScript / TypeScript
    ".js", ".jsx", ".ts", ".tsx",
    # Config / env
    ".env.example", ".toml", ".cfg", ".ini", ".yaml", ".yml",
    # Web
    ".html", ".css",
    # Data
    ".json",
    # Docs
    ".md", ".txt", ".rst",
    # SQL
    ".sql",
    # Shell
    ".sh", ".bat",
    # Nginx / systemd
    ".conf", ".service",
}

# Specific filenames to always include (even without matching extension)
INCLUDE_FILENAMES = {
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".gitignore", "Makefile", "Procfile",
    "wsgi.py", "manage.py", "requirements.txt",
    "package.json", "vite.config.js", "vite.config.ts",
}

# Files to always skip regardless of extension
SKIP_FILENAMES = {
    ".env",                  # never include secrets
    "package-lock.json",     # too noisy
    "yarn.lock",
    "poetry.lock",
    "*.pyc",
    "context.py",             # skip self to avoid recursion
    "ctx.txt",                # skip generated context files
}

# Max file size to include (bytes) — skip binary/huge files
MAX_FILE_SIZE = 100_000   # 100 KB

# Max total output size before warning (bytes)
WARN_TOTAL_SIZE = 500_000  # 500 KB

# =============================================================================
# LOGIC
# =============================================================================

def should_skip_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(".")

def should_include_file(path: Path) -> bool:
    name = path.name
    if name in SKIP_FILENAMES:
        return False
    if name in INCLUDE_FILENAMES:
        return True
    suffix = path.suffix.lower()
    # Handle compound extensions like .env.example
    if len(path.suffixes) >= 2:
        compound = "".join(path.suffixes[-2:]).lower()
        if compound in INCLUDE_EXTENSIONS:
            return True
    return suffix in INCLUDE_EXTENSIONS

def read_file(path: Path) -> str:
    if path.stat().st_size > MAX_FILE_SIZE:
        return f"[skipped — file too large: {path.stat().st_size:,} bytes]"
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[could not read: {e}]"

def collect_files(root: Path):
    """Walk the tree and return list of (relative_path, absolute_path)."""
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skip dirs in-place so os.walk doesn't descend into them
        dirnames[:] = sorted(
            d for d in dirnames if not should_skip_dir(d)
        )
        for filename in sorted(filenames):
            abs_path = Path(dirpath) / filename
            rel_path = abs_path.relative_to(root)
            if should_include_file(abs_path):
                files.append((rel_path, abs_path))
    return files

def build_tree(root: Path) -> str:
    """Build a visual directory tree (skipping excluded dirs/files)."""
    lines = [f"{root.name}/"]

    def _walk(path: Path, prefix: str):
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
        entries = [
            e for e in entries
            if not (e.is_dir() and should_skip_dir(e.name))
            and not (e.is_file() and not should_include_file(e))
        ]
        for i, entry in enumerate(entries):
            is_last = i == len(entries) - 1
            connector = "└── " if is_last else "├── "
            lines.append(f"{prefix}{connector}{entry.name}{'/' if entry.is_dir() else ''}")
            if entry.is_dir():
                extension = "    " if is_last else "│   "
                _walk(entry, prefix + extension)

    _walk(root, "")
    return "\n".join(lines)

# =============================================================================
# MINIFICATION
# =============================================================================

def _minify_python(src: str) -> str:
    """Strip Python comments and collapse blank lines."""
    out = []
    for line in src.splitlines():
        stripped = line.rstrip()
        # Remove full-line comments (keep shebangs)
        if re.match(r"^\s*#", stripped) and not stripped.startswith("#!"):
            continue
        # Strip inline comments (naive but good enough for context use)
        stripped = re.sub(r"\s+#(?!\s*noqa)[^\'\"\n]*$", "", stripped)
        out.append(stripped)
    return _collapse_blank_lines("\n".join(out))

def _minify_js_ts(src: str) -> str:
    """Strip // and /* */ comments, collapse blank lines."""
    # Remove block comments
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    # Remove line comments
    src = re.sub(r"//[^\n]*", "", src)
    return _collapse_blank_lines(src)

def _minify_css(src: str) -> str:
    """Strip CSS comments and collapse whitespace between rules."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return _collapse_blank_lines(src)

def _minify_html(src: str) -> str:
    """Strip HTML comments and collapse blank lines."""
    src = re.sub(r"<!--.*?-->", "", src, flags=re.DOTALL)
    return _collapse_blank_lines(src)

def _minify_generic(src: str) -> str:
    """Generic: just collapse blank lines."""
    return _collapse_blank_lines(src)

def _collapse_blank_lines(src: str) -> str:
    """Replace 2+ consecutive blank lines with a single blank line."""
    return re.sub(r"\n{3,}", "\n\n", src).strip()

_MINIFIERS = {
    ".py":   _minify_python,
    ".js":   _minify_js_ts,
    ".jsx":  _minify_js_ts,
    ".ts":   _minify_js_ts,
    ".tsx":  _minify_js_ts,
    ".css":  _minify_css,
    ".html": _minify_html,
}

def minify_content(path: Path, src: str) -> str:
    """Dispatch to the right minifier based on file extension."""
    suffix = path.suffix.lower()
    minifier = _MINIFIERS.get(suffix, _minify_generic)
    return minifier(src)

# =============================================================================
# BUILD
# =============================================================================

def build_context(
    tree_only=False,
    files_only=False,
    minify=False,
    ROOT=Path(__file__).parent.resolve(),
) -> str:
    files = collect_files(ROOT)
    sections = []

    if minify:
        # Compact single-line header
        header = (
            f"# PROJECT:{ROOT.name} | "
            f"generated:{datetime.now().strftime('%Y-%m-%dT%H:%M')} | "
            f"files:{len(files)}"
        )
    else:
        header = f"""
╔══════════════════════════════════════════════════════════════════╗
║              PROJECT CONTEXT — hypno-chatbot.online              ║
╚══════════════════════════════════════════════════════════════════╝
Generated : {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
Root      : {ROOT}
Files     : {len(files)} included
""".strip()

    sections.append(header)

    # ── File tree ──────────────────────────────────────────────────────────
    if not files_only:
        if minify:
            sections.append("\n## TREE")
        else:
            sections.append("\n" + "=" * 68)
            sections.append("FILE TREE")
            sections.append("=" * 68)
        sections.append(build_tree(ROOT))

    # ── File contents ──────────────────────────────────────────────────────
    if not tree_only:
        if minify:
            sections.append("\n## FILES")
        else:
            sections.append("\n" + "=" * 68)
            sections.append("FILE CONTENTS")
            sections.append("=" * 68)

        for rel_path, abs_path in files:
            lang = _lang_hint(abs_path)
            content = read_file(abs_path)

            if minify:
                content = minify_content(abs_path, content)
                sections.append(f"\n### {rel_path}")
                sections.append(f"```{lang}")
                sections.append(content)
                sections.append("```")
            else:
                sections.append(f"\n{'─' * 68}")
                sections.append(f"FILE: {rel_path}")
                sections.append(f"{'─' * 68}")
                sections.append(f"```{lang}")
                sections.append(content.rstrip())
                sections.append("```")

    return "\n".join(sections)

def _lang_hint(path: Path) -> str:
    """Return a markdown language hint for syntax highlighting."""
    mapping = {
        ".py": "python", ".js": "javascript", ".jsx": "jsx",
        ".ts": "typescript", ".tsx": "tsx", ".html": "html",
        ".css": "css", ".json": "json", ".md": "markdown",
        ".sh": "bash", ".bat": "bat", ".sql": "sql",
        ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
        ".conf": "nginx", ".service": "ini", ".env.example": "bash",
    }
    suffix = path.suffix.lower()
    if len(path.suffixes) >= 2:
        compound = "".join(path.suffixes[-2:]).lower()
        if compound in mapping:
            return mapping[compound]
    return mapping.get(suffix, "")

# =============================================================================
# ENTRY POINT
# =============================================================================

def main():
    global INCLUDE_EXTENSIONS
    parser = argparse.ArgumentParser(
        description="Generate project context for AI prompts."
    )
    parser.add_argument("--tree",   action="store_true", help="File tree only (no file contents)")
    parser.add_argument("--files",  action="store_true", help="File contents only (no tree)")
    parser.add_argument("--minify", action="store_true", help="Strip comments & blank lines to reduce token count")
    parser.add_argument("--out",    metavar="FILE",      help="Save output to a file instead of printing")
    parser.add_argument("--stats",  action="store_true", help="Print file list and sizes, then exit")
    parser.add_argument("--dir", metavar="DIRECTORY", help="Root directory to scan (default: current directory)")
    parser.add_argument("--ext", metavar="EXTENSIONS", help="Comma-separated list of file extensions to include (e.g. .py,.js,.md)")

    args = parser.parse_args()

    if args.ext:
        INCLUDE_EXTENSIONS = set(ext.strip().lower() for ext in args.ext.split(","))
    
    if args.dir:
        ROOT = Path(args.dir).resolve()
    else:
        ROOT = Path(__file__).parent.resolve()

    if args.stats:
        files = collect_files(ROOT)
        total = 0
        for rel, abs_path in files:
            size = abs_path.stat().st_size
            total += size
            print(f"  {size:>8,}  {rel}")
        print(f"\n  {'─'*40}")
        print(f"  {total:>8,}  TOTAL ({len(files)} files)")
        return
    

    context = build_context(
        tree_only=args.tree,
        files_only=args.files,
        minify=args.minify,
        ROOT=ROOT
    )
    size = len(context.encode("utf-8"))

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(context, encoding="utf-8")
        label = " (minified)" if args.minify else ""
        print(f"✓ Context written to: {out_path}  ({size:,} bytes{label})")
    else:
        print(context)
        if size > WARN_TOTAL_SIZE:
            hint = " Try --minify to cut token count further." if not args.minify else ""
            print(
                f"\n⚠  Output is {size:,} bytes ({size // 1024} KB). "
                "Consider using --tree for a lighter snapshot, "
                f"or --out ctx.txt to save to a file.{hint}",
                file=sys.stderr,
            )

if __name__ == "__main__":
    main()