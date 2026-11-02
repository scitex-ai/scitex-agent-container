"""Keep timed native proxy IO local, then retain its complete coverage shards.

The subprocess still runs normal Python/site dependencies and the exact parent
coverage configuration. Only writable fixture artifacts move to node-local
storage; publication to the original collector is outside the shutdown clock.
"""

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import tempfile

from coverage import Coverage, CoverageData


def coverage_environment(environment, root):
    """Retarget data only; serialized config takes precedence over START/FILE."""
    child_env = dict(environment)
    serialized = environment.get("COVERAGE_PROCESS_CONFIG")
    start = environment.get("COVERAGE_PROCESS_START")
    if not serialized and not start:
        return child_env, None
    if serialized:
        config = Coverage(config_file=":data:" + serialized)
    else:
        config = Coverage(config_file=start, data_file=environment.get("COVERAGE_FILE"))
    if not config.get_option("run:parallel"):
        raise RuntimeError("Native proxy fixture requires existing parallel coverage")
    original = Path(config.get_option("run:data_file")).absolute()
    local = root / "coverage" / original.name
    local.parent.mkdir()
    child_env["COVERAGE_FILE"] = str(local)
    if serialized:
        config.set_option("run:data_file", str(local))
        # Use coverage's own serializer rather than changing its private wire
        # format or discarding the parent's branch/source/context settings.
        child_env["COVERAGE_PROCESS_CONFIG"] = config.config.serialize()
    return child_env, original


def publish_coverage(root, original):
    """Publish only closed, readable SQLite shards with no partial-file window."""
    if original is None:
        return
    shards = list((root / "coverage").iterdir())
    if not shards:
        raise RuntimeError("Native proxy did not retain subprocess coverage")
    original.parent.mkdir(parents=True, exist_ok=True)
    for source in shards:
        if not source.is_file() or source.is_symlink():
            raise RuntimeError("Unexpected native proxy coverage artifact")
        data = CoverageData(basename=str(source))
        data.read()  # A corrupt/unfinished shard must fail, never be hidden.
        fd, temporary = tempfile.mkstemp(
            prefix=".proxy-coverage-copy-", dir=original.parent
        )
        staging = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as destination, source.open("rb") as content:
                shutil.copyfileobj(content, destination)
                destination.flush()
                os.fsync(destination.fileno())
            # Same-filesystem hard link atomically publishes without replacing
            # another process's shard. Collector globs never see the staging file.
            os.link(staging, original.parent / source.name)
        finally:
            staging.unlink(missing_ok=True)


@contextmanager
def proxy_workspace():
    with tempfile.TemporaryDirectory(prefix="sac-proxy-fixture-", dir="/tmp") as folder:
        root = Path(folder)
        child_env, original = coverage_environment(os.environ, root)
        try:
            yield root, child_env
        except BaseException as failure:
            try:
                publish_coverage(root, original)
            except Exception as exc:
                failure.add_note(f"Owned proxy coverage publication failed: {exc}")
            raise
        else:
            publish_coverage(root, original)
