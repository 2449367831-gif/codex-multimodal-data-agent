"""Durable identity-only checkpoints for one canonical Codex context partition."""

from hashlib import sha256
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile


class ContextCheckpointError(RuntimeError):
    pass


_THREAD = re.compile(r"[A-Za-z0-9_.:-]{8,200}\Z")


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ContextCheckpointError("context_checkpoint_invalid")
        result[key] = value
    return result


class ContextCheckpoint:
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.partition = sha256(os.path.normcase(str(self.home)).encode()).hexdigest()
        self.ready_path = self.home / "agent-context-thread.json"
        self.claim_path = self.home / "agent-context-creation.json"

    @contextmanager
    def lock(self):
        handle = None
        acquired = False
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            handle = (self.home / "agent-context.lock").open("a+b")
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            if handle is not None:
                handle.close()
            raise ContextCheckpointError("context_worker_busy") from None
        try:
            yield
        finally:
            if acquired:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()

    def _read(self, path, state):
        try:
            with path.open("rb") as stream:
                raw = stream.read(2049)
        except FileNotFoundError:
            return None
        except OSError:
            raise ContextCheckpointError("context_checkpoint_unavailable") from None
        try:
            if len(raw) > 2048:
                raise ValueError()
            value = json.loads(raw, object_pairs_hook=_pairs)
            if not isinstance(value, dict) or set(value) != {"version", "partition", "state", "thread_id"}:
                raise ValueError()
            if type(value["version"]) is not int or value["version"] != 1 or value["partition"] != self.partition or value["state"] != state:
                raise ValueError()
            if state == "ready":
                if not isinstance(value["thread_id"], str) or not _THREAD.fullmatch(value["thread_id"]):
                    raise ValueError()
            elif value["thread_id"] is not None:
                raise ValueError()
            return value
        except (ValueError, TypeError, UnicodeError):
            raise ContextCheckpointError("context_checkpoint_invalid") from None

    def _publish_once(self, path, value):
        temporary = None
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.home,
                                    prefix=".agent-context-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(value, stream, ensure_ascii=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            # A hard link publishes the fully flushed file without overwriting a
            # concurrent winner. Readers never observe a partial checkpoint.
            os.link(temporary, path)
            return True
        except FileExistsError:
            return False
        except OSError:
            raise ContextCheckpointError("context_checkpoint_unavailable") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _value(self, state, thread_id):
        return {"version": 1, "partition": self.partition, "state": state, "thread_id": thread_id}

    def resolve(self, expected=None):
        if expected is not None and (not isinstance(expected, str) or not _THREAD.fullmatch(expected)):
            raise ContextCheckpointError("context_checkpoint_invalid")
        ready = self._read(self.ready_path, "ready")
        if ready is not None:
            if expected is not None and ready["thread_id"] != expected:
                raise ContextCheckpointError("context_checkpoint_mismatch")
            return ready["thread_id"]
        if expected is not None:
            # The existing server-owned binding can recover a missing checkpoint.
            return self.bind(expected)
        if self._read(self.claim_path, "creating") is not None:
            raise ContextCheckpointError("context_initialization_uncertain")
        return None

    def claim_creation(self):
        if self.resolve() is not None:
            raise ContextCheckpointError("context_already_created")
        if not self._publish_once(self.claim_path, self._value("creating", None)):
            raise ContextCheckpointError("context_initialization_uncertain")

    def bind(self, thread_id):
        if not isinstance(thread_id, str) or not _THREAD.fullmatch(thread_id):
            raise ContextCheckpointError("context_checkpoint_invalid")
        self._publish_once(self.ready_path, self._value("ready", thread_id))
        ready = self._read(self.ready_path, "ready")
        if ready is None or ready["thread_id"] != thread_id:
            raise ContextCheckpointError("context_checkpoint_mismatch")
        return thread_id
