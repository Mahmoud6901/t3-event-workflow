"""What a bound project's T3 sessions may touch, in every turn, the orchestrator and its workers alike.

They write files only inside their own project (its folder and its git worktrees), the temporary folder, and Claude
Code's own plans and memory of the project. They read anything except another bound project's folders, the workflow's
data (every state/ and secrets/ folder, and other projects' homes) and T3's own data, which holds every project's
conversations. Of the workflow's own folders they read only the published code (PUBLIC), and change nothing. The
orchestrator's `t3-workflow` commands and a worker's harness command are allowed as such.
A shell command is judged by the paths it names, as far as its text tells: this stops mistakes and plain attempts, not
a determined program. Goose workers run no hooks. Standard library only: the hooks run it on every tool call.
"""
import base64
import json
import os
from pathlib import Path
import re
import tempfile

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}
PATH_KEYS = ("file_path", "notebook_path", "path")
TOKEN = re.compile(r'"[^"\r\n]*"|\'[^\'\r\n]*\'|[^\s"\'|;&<>(){}`,]+')
PATCHED = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+?)\s*$", re.MULTILINE)
# Redirection (not 2>&1), in-place edits, commands that create, change, move or delete files, and interpreters.
WRITES = re.compile(r"(?<![0-9&])>|\bsed\b[^|;&]*\s-[A-Za-z]*i|(?<![\w.\-/\\:])(?:Set-Content|Add-Content|Out-File|New-Item|Remove-Item|"
                    r"Move-Item|Copy-Item|Rename-Item|Clear-Content|rm|rmdir|rd|del|erase|mv|move|cp|copy|tee|touch|mkdir|"
                    r"md|ln|chmod|chown|truncate|install|rsync|git|patch|apply_patch|python[0-9.]*|py|node|perl|ruby|pwsh|"
                    r"powershell)\b", re.IGNORECASE)
# Commands that read a folder's whole tree: naming a folder that holds hidden ones is then reading them.
RECURSIVE = re.compile(r"(?<![\w.\-/\\:])(?:rg|find|fd|ag|ack|tree|du|robocopy|xcopy)\b|-recurse\b|--recursive\b|"
                       r"(?<!\S)-[A-Za-z]*[rR][A-Za-z]*\b|(?<!\S)/s\b", re.IGNORECASE)
WORD = r'(?:"[^"\r\n]+"|\'[^\'\r\n]+\'|[^\s"\';&|]+)'
HARNESS = re.compile(rf"(?:&\s*)?(?P<python>{WORD})\s+(?:-[A-Za-z]+\s+)*(?P<script>{WORD})\s+worker(?=\s|$)")
PYTHON = re.compile(r"pythonw?[0-9.]*(?:\.exe)?", re.IGNORECASE)
PUBLIC = ("references", "scripts", "agents", "assets", "SKILL.md", "README.md", "install.py", "LICENSE", "pyproject.toml")


def operation(command):
    """The orchestrator's `t3-workflow '<JSON>'` or `t3-workflow <base64 JSON>` command, or None."""
    if not isinstance(command, str):
        return None
    source = re.sub(r"(?:\s+2>&1)?\s+\|\s*head\s+-c\s+[1-9][0-9]*$", "", command.strip())
    quoted = re.fullmatch(r"t3-workflow '(\{[^\r\n]*\})'", source)
    encoded = re.fullmatch(r"t3-workflow ([A-Za-z0-9+/]+={0,2})", source)
    if not quoted and not encoded:
        return None
    try:
        value = json.loads(quoted.group(1) if quoted else base64.b64decode(encoded.group(1)).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def unquote(word):
    return word[1:-1] if len(word) > 1 and word[0] == word[-1] and word[0] in "\"'" else word


def expand(text):
    """A path as a shell reads it: the home folder, $HOME, PowerShell's $env:NAME (any case) and cmd's %NAME%."""
    text = re.sub(r"\$\{HOME\}|\$HOME(?!\w)", lambda m: str(Path.home()), text)
    text = re.sub(r"\$env:(\w+)", lambda m: os.environ.get(m[1], m[0]), text, flags=re.IGNORECASE)
    text = re.sub(r"%(\w+)%", lambda m: os.environ.get(m[1], m[0]), text)
    return os.path.expanduser(text)


class Fence:
    def __init__(self, cwd, own, others=(), code=(), data=(), home=None, temp=None, t3=None):
        self.cwd = str(cwd)
        self.own = [self.norm(p) for p in own]
        # A project bound in the workflow's own folder works in it: that folder is its own, not the workflow's code.
        self.code = [c for c in map(self.norm, code) if not any(self.inside(o, c) or self.inside(c, o) for o in self.own)]
        self.home = self.norm(home) if home else None
        private = [os.path.join(self.home, "state"), os.path.join(self.home, "secrets")] if self.home else []
        self.hidden = [self.norm(p) for p in (*others, *data, *([t3] if t3 else []))] + private
        self.locked = self.code + [self.norm(p) for p in data] + ([self.home] if self.home else [])
        claude = Path.home() / ".claude"  # Claude Code's own plans, and its memory of this project's folders.
        memory = [claude / "projects" / re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(str(o))) for o in own]
        self.writable_roots = self.own + [self.norm(p) for p in (temp or tempfile.gettempdir(), claude / "plans", *memory)]
        self.harnesses = {os.path.join(root, "scripts", "harness.py") for root in self.code + ([self.home] if self.home else [])}

    def norm(self, path):
        text = expand(str(path)).replace("\\", "/")
        if os.name == "nt" and re.match(r"^/tmp(?:/|$)", text):  # Git Bash's /tmp is the temporary folder.
            text = tempfile.gettempdir().replace("\\", "/") + text[4:]
        elif os.name == "nt" and re.match(r"^/[A-Za-z]/", text):  # Git Bash's /e/folder is E:/folder.
            text = f"{text[1]}:/{text[3:]}"
        return os.path.normcase(os.path.realpath(os.path.join(self.cwd, text)))

    @staticmethod
    def inside(path, root):
        return path == root or path.startswith(root.rstrip("\\/") + os.sep)

    def readable(self, path, deep=False):
        """Whether the path may be read; `deep` when everything under it is read too (a search or a recursive listing)."""
        for root in ([self.home] if self.home else []) + self.code:  # The workflow's folders: only their published code.
            if self.inside(path, root) and path != root:
                public = any(self.inside(path, self.norm(os.path.join(root, part))) for part in PUBLIC)
                if not public or root == self.home:
                    return public
        return not any(self.inside(path, h) or (deep and self.inside(h, path)) for h in self.hidden)

    def writable(self, path):
        return (self.readable(path) and not any(self.inside(path, root) for root in self.locked)
                and any(self.inside(path, root) for root in self.writable_roots))

    def unreadable(self, path):
        return (f"T3_WORKFLOW_FENCE: this project's sessions do not read {path}: it is another project's, or the "
                f"workflow's own data, or holds them. Work inside this project ({self.own[0] if self.own else self.cwd}).")

    def unwritable(self, path):
        return (f"T3_WORKFLOW_FENCE: this project's sessions change files only inside this project and its worktrees "
                f"({self.own[0] if self.own else self.cwd}), never {path}. The workflow's own code is changed by the "
                "user, from the workflow's repository.")

    def check(self, runtime, tool, tool_input):
        """The reason to deny this tool call, or None."""
        if not isinstance(tool, str) or tool.startswith("mcp__"):
            return None
        tool_input = tool_input if isinstance(tool_input, dict) else {}
        if tool in SHELL_TOOLS:
            command = next((tool_input[k] for k in ("command", "cmd") if isinstance(tool_input.get(k), str)), None)
            return self.shell(command)
        if tool == "apply_patch":
            for text in (v for v in tool_input.values() if isinstance(v, str)):
                for target in PATCHED.findall(text):
                    if not self.writable(self.norm(target)):
                        return self.unwritable(target)
            return None
        targets = [tool_input[k] for k in PATH_KEYS if isinstance(tool_input.get(k), str) and tool_input[k]]
        if tool in WRITE_TOOLS:
            return next((self.unwritable(t) for t in targets if not self.writable(self.norm(t))), None)
        pattern = tool_input.get("pattern") if tool == "Glob" else None
        deep = tool == "Grep" or (isinstance(pattern, str) and "**" in pattern)
        if isinstance(pattern, str) and (Path(pattern).is_absolute() or pattern.startswith(("~", "/", "\\", "$", "%"))):
            targets.append(re.split(r"[*?\[{]", pattern, maxsplit=1)[0] or pattern)
        return next((self.unreadable(t) for t in targets if not self.readable(self.norm(t), deep)), None)

    def shell(self, command):
        if not isinstance(command, str) or operation(command) is not None:
            return None
        for found in HARNESS.finditer(command):  # A worker's report: the harness invocation itself is allowed.
            if self.harness(found):
                command = f"{command[:found.start()]} {command[found.end():]}"
                break
        writes, deep = bool(WRITES.search(command)), bool(RECURSIVE.search(command))
        for word in self.paths(command):
            path = self.norm(word)
            if not self.readable(path, deep):
                return self.unreadable(word)
            if writes and any(self.inside(path, root) for root in self.locked):
                return self.unwritable(word)
        return None

    def harness(self, found):
        """Python running this project's harness (or the shared one, which passes the report on by its token)."""
        python, script = unquote(found["python"]), unquote(found["script"])
        return bool(PYTHON.fullmatch(Path(python.replace("\\", "/")).name)) and self.norm(script) in self.harnesses

    def paths(self, command):
        """The paths a command names: absolute ones, the home folder's or a variable's, and relative ones that climb
        out with `..`."""
        for raw in TOKEN.findall(command):
            for word in (unquote(raw), *unquote(raw).split("=", 1)[1:]):
                word = re.split(r"[*?\[]", word, maxsplit=1)[0]
                if word and "://" not in word and (re.match(r"^[A-Za-z]:[\\/]", word) or word.startswith(("/", "~", "$", "%"))
                                                   or ".." in re.split(r"[\\/]", word)):
                    yield word
