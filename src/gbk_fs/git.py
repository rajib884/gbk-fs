"""Read-only git access: fetch a file's stored bytes from a ref (#2, read_git).

The recovery scenario this exists for: a file's working-tree bytes are corrupted, so the
only clean source is git — the index (``:0:``) or a commit (``HEAD``, a SHA, a branch).
We fetch the raw blob with ``git cat-file -p`` (the stored bytes, **no** smudge/EOL
filters) and hand them to the same encoding-aware decode pipeline as a normal read, so the
whole inspect→recover loop stays inside the tool instead of shelling out to
``git show | iconv``.

Pure plumbing: no decoding here (callers own that) and no writes — git is invoked as an
external process via an argv list (never a shell), so refs/paths can't inject.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .errors import GitError

#: Convenience aliases for "the index / staging area" (git stage 0). Git's own spelling is
#: the ``:0:<path>`` / ``:<path>`` pathspec, which we also accept verbatim.
_INDEX_REFS = frozenset({"index", "staged", "stage", ":0:", ":"})

_GIT_TIMEOUT = 30  # seconds; cat-file of a single blob is near-instant


def _git_object(ref: str, rel: str) -> str:
    """Compose the git object spec for ``rel`` at ``ref``, cwd-relative (resolved under -C).

    Index refs use the stage-0 pathspec ``:0:./<rel>``; any other ref is a commit-ish and
    uses ``<ref>:./<rel>``. The ``./`` makes the path relative to the ``-C`` directory, so a
    sandbox root nested below the repo root still resolves correctly (verified).
    """
    if ref in _INDEX_REFS or ref.lower() in _INDEX_REFS:
        return f":0:./{rel}"
    return f"{ref}:./{rel}"


def git_blob_bytes(root: Path, rel: str, ref: str) -> bytes:
    """Return the raw stored bytes of ``rel`` at ``ref`` from the git repo containing ``root``.

    ``rel`` is a POSIX path relative to ``root`` (the sandbox root). ``ref`` is a commit-ish
    (``HEAD``, ``HEAD~3``, a branch, a SHA) or an index alias (``index``/``staged``/``:0:``).

    Raises :class:`GitError` if git is unavailable, ``root`` is not in a repository, the ref
    is unknown, or the path does not exist in that ref.
    """
    ref = (ref or "HEAD").strip()
    obj = _git_object(ref, rel)
    cmd = ["git", "-C", str(root), "cat-file", "-p", obj]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_GIT_TIMEOUT)
    except FileNotFoundError as exc:  # git not installed / not on PATH
        raise GitError("git executable not found on PATH; cannot read from a ref") from exc
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - defensive
        raise GitError(f"git timed out after {_GIT_TIMEOUT}s reading {obj!r}") from exc

    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        detail = stderr.splitlines()[0] if stderr else f"exit code {proc.returncode}"
        raise GitError(f"git could not read {rel!r} at ref {ref!r}: {detail}")

    return proc.stdout
