__license__ = 'GPL v3'
__copyright__ = '2008, Kovid Goyal <kovid at kovidgoyal.net>'
"""
Provides platform independent temporary files that persist even after
being closed.
"""
import os
import tempfile

from calibre.constants import __appname__, filesystem_encoding, get_windows_temp_path, ismacos, iswindows
from calibre.utils.safe_atexit import remove_dir, remove_file_atexit, remove_folder_atexit, unlink

_base_dir = _osx_cache_dir = None
_prevent_recursion = False
cleanup = unlink  # some plugins import this function


def osx_cache_dir():
    global _osx_cache_dir
    if _osx_cache_dir:
        return _osx_cache_dir
    if _osx_cache_dir is None:
        _osx_cache_dir = False
        import ctypes

        libc = ctypes.CDLL(None)
        buf = ctypes.create_string_buffer(512)
        l = libc.confstr(65538, ctypes.byref(buf), len(buf))  # _CS_DARWIN_USER_CACHE_DIR = 65538
        if 0 < l < len(buf):
            try:
                q = buf.value.decode('utf-8').rstrip('\0')
            except ValueError:
                pass
            if q and os.path.isdir(q) and os.access(q, os.R_OK | os.W_OK | os.X_OK):
                _osx_cache_dir = q
                return q


get_default_tempdir = tempfile.gettempdir


def base_dir():
    global _base_dir, _prevent_recursion
    if _base_dir is not None and not os.path.exists(_base_dir):
        # Some people seem to think that running temp file cleaners that
        # delete the temp dirs of running programs is a good idea!
        if _prevent_recursion:
            _base_dir = get_default_tempdir()
        else:
            _base_dir = None
    if _base_dir is None:
        td = os.environ.get('CALIBRE_WORKER_TEMP_DIR', None)
        if td is not None:
            from calibre.utils.serialize import msgpack_loads
            from polyglot.binary import from_hex_bytes

            try:
                td = msgpack_loads(from_hex_bytes(td))
            except Exception:
                td = None
        if td and os.path.exists(td):
            _base_dir = td
        else:
            base = os.environ.get('CALIBRE_TEMP_DIR', None)
            if base is not None and iswindows:
                base = os.getenv('CALIBRE_TEMP_DIR')
            prefix = f'{__appname__}-'
            if base is None:
                if iswindows:
                    # On windows, if the TMP env var points to a path that
                    # cannot be encoded using the mbcs encoding, then the
                    # python 2 tempfile algorithm for getting the temporary
                    # directory breaks. So we use the win32 api to get a
                    # unicode temp path instead. See
                    # https://bugs.launchpad.net/bugs/937389
                    base = get_windows_temp_path()
                elif ismacos:
                    # Use the cache dir rather than the temp dir for temp files as Apple
                    # thinks deleting unused temp files is a good idea. See note under
                    # _CS_DARWIN_USER_TEMP_DIR here
                    # https://developer.apple.com/library/mac/documentation/Darwin/Reference/ManPages/man3/confstr.3.html
                    base = osx_cache_dir()

            _base_dir = tempfile.mkdtemp(prefix=prefix, dir=base or get_default_tempdir())
            orig = _prevent_recursion
            _prevent_recursion = True
            try:
                remove_folder_atexit(_base_dir)
            finally:
                _prevent_recursion = orig

    return _base_dir


def fix_tempfile_module():
    # We want the tempfile module to use base_dir() as its tempdir, but we dont
    # want to call base_dir() now as it will possibly create a tempdir, do that
    # only on demand.
    global get_default_tempdir
    if tempfile._gettempdir is not base_dir:
        get_default_tempdir = tempfile._gettempdir
        tempfile._gettempdir = base_dir


def reset_base_dir():
    global _base_dir, get_default_tempdir
    _base_dir = None
    # If we previously replaced tempfile._gettempdir in fix_tempfile_module,
    # restore the original function so global tempfile behavior is not
    # permanently mutated across tests/process lifetime.
    try:
        if get_default_tempdir:
            tempfile._gettempdir = get_default_tempdir
    except Exception:
        # Don't allow failures during reset to raise; best-effort restore.
        pass
    finally:
        get_default_tempdir = None


def force_unicode(x):
    # Cannot use the implementation in calibre.__init__ as it causes a circular
    # dependency
    if isinstance(x, bytes):
        x = x.decode(filesystem_encoding)
    return x


def _make_file(suffix, prefix, base):
    suffix, prefix = map(force_unicode, (suffix, prefix))  # no2to3
    return tempfile.mkstemp(suffix, prefix, dir=base)


def _make_dir(suffix, prefix, base):
    suffix, prefix = map(force_unicode, (suffix, prefix))  # no2to3
    return tempfile.mkdtemp(suffix, prefix, base)


class PersistentTemporaryFile:
    """
    A file-like object that is a temporary file that is available even after being closed on
    all platforms. It is automatically deleted on normal program termination.
    """

    _file = None

    def __init__(self, suffix='', prefix='', dir=None, mode='w+b'):
        if prefix is None:
            prefix = ''
        if dir is None:
            dir = base_dir()
        fd, name = _make_file(suffix, prefix, dir)

        self._file = os.fdopen(fd, mode)
        self._name = name
        self._fd = fd
        remove_file_atexit(name)

    def __getattr__(self, name):
        if name == 'name':
            return self.__dict__['_name']
        return getattr(self.__dict__['_file'], name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def PersistentTemporaryDirectory(suffix='', prefix='', dir=None):
    """
    Return the path to a newly created temporary directory that will
    be automatically deleted on application exit.
    """
    if dir is None:
        dir = base_dir()
    tdir = _make_dir(suffix, prefix, dir)
    remove_folder_atexit(tdir)
    return tdir


class TemporaryDirectory:
    """
    A temporary directory to be used in a with statement.
    """

    def __init__(self, suffix='', prefix='', dir=None, keep=False):
        self.suffix = suffix
        self.prefix = prefix
        if dir is None:
            dir = base_dir()
        self.dir = dir
        self.keep = keep

    def __enter__(self):
        if not hasattr(self, 'tdir'):
            self.tdir = _make_dir(self.suffix, self.prefix, self.dir)
        return self.tdir

    def __exit__(self, *args):
        if not self.keep and os.path.exists(self.tdir):
            remove_dir(self.tdir)


class TemporaryFile:
    def __init__(self, suffix='', prefix='', dir=None, mode='w+b'):
        if prefix is None:
            prefix = ''
        if suffix is None:
            suffix = ''
        if dir is None:
            dir = base_dir()
        self.prefix, self.suffix, self.dir, self.mode = prefix, suffix, dir, mode
        self._file = None

    def __enter__(self):
        fd, name = _make_file(self.suffix, self.prefix, self.dir)
        self._file = os.fdopen(fd, self.mode)
        self._name = name
        self._file.close()
        return name

    def __exit__(self, *args):
        unlink(self._name)


class SpooledTemporaryFile(tempfile.SpooledTemporaryFile):
    def __init__(self, max_size=0, suffix='', prefix='', dir=None, mode='w+b', bufsize=-1):
        if prefix is None:
            prefix = ''
        if suffix is None:
            suffix = ''
        if dir is None:
            dir = base_dir()
        self._name = None
        tempfile.SpooledTemporaryFile.__init__(self, max_size=max_size, suffix=suffix, prefix=prefix, dir=dir, mode=mode)

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, val):
        self._name = val
        # If a disk-backed file name is provided explicitly, try to register it
        # for cleanup at exit (if the helper exists in this module).
        try:
            if val and 'remove_file_atexit' in globals():
                remove_file_atexit(val)
        except Exception:
            # Never let registration errors propagate
            pass

    def _maybe_register_name(self):
        """If the underlying spooled file has rolled to disk and provides a
        name, record it and register it for cleanup at exit (if helper exists).
        This is defensive: the in-memory buffer may not have _file with a name
        until it is rolled to disk, so call this from accessors.
        """
        f = getattr(self, '_file', None)
        if f is None:
            return
        name = getattr(f, 'name', None)
        if name and name != self._name:
            self._name = name
            try:
                if 'remove_file_atexit' in globals():
                    remove_file_atexit(name)
            except Exception:
                # Don't let registration failures affect normal operation
                pass

    # See https://bugs.python.org/issue26175
    def readable(self):
        # Ensure we capture and register a disk-backed filename if one exists
        self._maybe_register_name()
        f = getattr(self, '_file', None)
        if f is None:
            return False
        return getattr(f, 'readable', (lambda: True))()

    def seekable(self):
        self._maybe_register_name()
        f = getattr(self, '_file', None)
        if f is None:
            return False
        return getattr(f, 'seekable', (lambda: True))()

    def writable(self):
        self._maybe_register_name()
        f = getattr(self, '_file', None)
        if f is None:
            return False
        return getattr(f, 'writable', (lambda: True))()

    def close(self):
        # Ensure the underlying resource is closed promptly; this helps avoid
        # ResourceWarnings when tests treat such warnings as errors.
        try:
            return super().close()
        except Exception:
            # Swallow any exception on close to avoid noisy failures during
            # interpreter shutdown; Python may already be tearing down modules.
            try:
                f = getattr(self, '_file', None)
                if f is not None:
                    try:
                        f.close()
                    except Exception:
                        pass
            finally:
                return

    def __del__(self):
        # Best-effort cleanup on object finalization to avoid leaving open
        # file descriptors which raise ResourceWarning (treated as errors in CI).
        try:
            self.close()
        except Exception:
            pass


def better_mktemp(*args, **kwargs):
    fd, path = tempfile.mkstemp(*args, **kwargs)
    os.close(fd)
    return path
