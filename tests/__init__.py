from contextlib import contextmanager
from pathlib import Path
import shutil
import stat
import uuid


def remove_read_only(operation, path, error):
    if not isinstance(error[1], PermissionError):
        raise error[1]
    Path(path).chmod(stat.S_IREAD | stat.S_IWRITE)
    operation(path)


@contextmanager
def scratch_folder():
    # Python 3.13's Windows mkdtemp ACL excludes the sandbox token.
    root = Path(__file__).resolve().parents[1]
    folder = root / ("test-scratch-" + uuid.uuid4().hex)
    folder.mkdir()
    try:
        yield folder
    finally:
        if folder.resolve().parent == root:
            shutil.rmtree(folder, onerror=remove_read_only)
