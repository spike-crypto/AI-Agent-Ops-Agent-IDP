"""Versioned prompts.

Every system prompt is a `Prompt`: ordinary text plus a name and a version number.
The name and version travel with each model call into Langfuse and the logs, so a
trace always says which prompt produced an answer.

Rule: when you change a prompt's text, bump its version. `tests/test_prompts.py`
compares every prompt with `prompts.lock.json` and fails if the text changed but the
version did not. After a deliberate change run:

    python -m app.prompts --write-lock
"""

import hashlib
import json
import sys
from pathlib import Path

LOCK_FILE = Path(__file__).resolve().parent.parent / "prompts.lock.json"
REGISTRY: dict[str, "Prompt"] = {}


class Prompt(str):
    """A system prompt. Behaves exactly like its text, and also knows its name and version."""

    name: str
    version: int

    def __new__(cls, name: str, version: int, text: str) -> "Prompt":
        obj = super().__new__(cls, text)
        obj.name = name
        obj.version = int(version)
        existing = REGISTRY.get(name)
        if existing is not None and (existing.version != obj.version or str(existing) != text):
            raise ValueError(f"Two different prompts are both named '{name}'")
        REGISTRY[name] = obj
        return obj

    @property
    def label(self) -> str:
        return f"{self.name}@v{self.version}"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(str(self).encode()).hexdigest()


def all_prompts() -> list[Prompt]:
    """Every prompt in the app. Importing the agent registry is what defines them."""
    import app.agents.registry  # noqa: F401

    return [REGISTRY[name] for name in sorted(REGISTRY)]


def snapshot() -> dict[str, dict]:
    return {p.name: {"version": p.version, "sha256": p.sha256} for p in all_prompts()}


def read_lock() -> dict[str, dict]:
    return json.loads(LOCK_FILE.read_text(encoding="utf-8")) if LOCK_FILE.exists() else {}


def problems() -> list[str]:
    """What is wrong between the prompts in code and the lock file. Empty means all good."""
    locked, current, out = read_lock(), snapshot(), []
    for name, now in current.items():
        was = locked.get(name)
        if was is None:
            out.append(f"{name}: new prompt, not in the lock file yet")
        elif now["sha256"] != was["sha256"] and now["version"] <= was["version"]:
            out.append(f"{name}: text changed but the version is still {now['version']}; bump it")
        elif now["sha256"] == was["sha256"] and now["version"] != was["version"]:
            out.append(f"{name}: version changed to {now['version']} but the text is the same")
        elif now != was:
            out.append(f"{name}: changed to v{now['version']}; the lock file still has v{was['version']}")
    out += [f"{name}: in the lock file but no longer in the code" for name in locked if name not in current]
    return out


def main() -> None:
    if "--write-lock" in sys.argv:
        # Refuse to bless a text change that kept its old version number.
        stale = [p for p in problems() if "bump it" in p or "text is the same" in p]
        if stale:
            sys.exit("Fix these first:\n  " + "\n  ".join(stale))
        LOCK_FILE.write_text(json.dumps(snapshot(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Wrote {LOCK_FILE.name} with {len(snapshot())} prompts.")
    else:
        for p in all_prompts():
            print(f"{p.label:32} {p.sha256[:12]}  {len(p)} chars")
        issues = problems()
        print("\n".join(["", "Problems:"] + ["  " + i for i in issues]) if issues else "\nLock file matches.")
        sys.exit(1 if issues else 0)


if __name__ == "__main__":
    # Run through the imported module: `python -m` loads this file as __main__, which
    # would otherwise have its own empty REGISTRY, separate from the one the agents fill.
    from app.prompts import main as _main

    _main()
