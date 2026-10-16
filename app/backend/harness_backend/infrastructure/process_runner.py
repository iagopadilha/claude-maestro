"""Running system processes (stdlib): synchronous with a timeout, detached (outlives the app), live pid and kill tree.

Never uses a shell: `argv` is always a list. It is the only app class that calls `subprocess` directly (the engine has its
own helpers because it runs as a loose script).
"""
import os
import subprocess
import threading
from typing import Protocol

IS_WINDOWS = os.name == "nt"
FLAG_DETACHED_PROCESS = 0x00000008
FLAG_CREATE_NEW_PROCESS_GROUP = 0x00000200
FLAG_CREATE_NO_WINDOW = 0x08000000
WINDOWS_DETACHED_FLAGS = FLAG_DETACHED_PROCESS | FLAG_CREATE_NEW_PROCESS_GROUP | FLAG_CREATE_NO_WINDOW
WINDOWS_OWN_GROUP_FLAGS = FLAG_CREATE_NEW_PROCESS_GROUP | FLAG_CREATE_NO_WINDOW
TIMEOUT_CODE = -9
START_FAILURE_CODE = 127
TIMEOUT_MESSAGE = "timed out"
TASKKILL_TIMEOUT_SECONDS = 15
DRAIN_OUTPUT_TIMEOUT_SECONDS = 10
WINDOWS_LIMITED_QUERY_PERMISSION = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
WINDOWS_ACCESS_DENIED_ERROR = 5
WINDOWS_STILL_ACTIVE_CODE = 259  # STILL_ACTIVE
KILL_SIGNAL = 9
REAP_DETACHED_TIMEOUT_SECONDS = 5
DETACHED_PROCESSES = {}  # pid -> Popen of the processes spawned by this process (module level: outlives the runner)
DETACHED_LOCK = threading.Lock()


class IProcessRunner(Protocol):
    def run_and_wait(self, argv, folder, timeout_seconds, input_text=None, extra_env=None, env=None) -> tuple:
        """Runs and waits. Returns (code, output, error) as UTF-8 text. A timeout kills the tree and returns TIMEOUT_CODE."""

    def spawn_detached(self, argv, folder, log_path, extra_env=None) -> int:
        """Starts the process independently of this one (it outlives the app), output and error in `log_path`. Returns the pid."""

    def is_alive(self, pid) -> bool:
        """True if the pid exists and is still running."""

    def kill_tree(self, pid) -> None:
        """Kills the process and its children. Never raises."""


def build_env(env=None, extra_env=None):
    """None when nothing changes (inherits the process environment); otherwise the base environment (or os.environ) + extras."""
    if env is None and not extra_env:
        return None
    result = dict(os.environ if env is None else env)
    result.update(extra_env or {})
    return result


class ProcessRunner:
    """Real implementation of IProcessRunner."""

    def run_and_wait(self, argv, folder, timeout_seconds, input_text=None, extra_env=None, env=None):
        """Runs `argv` in `folder` without a shell, in its own process group (so the timeout kills the whole tree).

        `input_text` goes through stdin (None = closed stdin). `env` replaces the whole environment (e.g. without secrets);
        `extra_env` adds variables on top. Returns (code, output, error); never raises:
        missing binary = (START_FAILURE_CODE, "", message); timeout = (TIMEOUT_CODE, partial output, "timed out").
        """
        options = {"stdin": subprocess.PIPE if input_text is not None else subprocess.DEVNULL, "stdout": subprocess.PIPE,
                   "stderr": subprocess.PIPE, "text": True, "encoding": "utf-8", "errors": "replace",
                   "cwd": folder if folder and os.path.isdir(folder) else None, "env": build_env(env, extra_env)}
        if IS_WINDOWS:
            options["creationflags"] = WINDOWS_OWN_GROUP_FLAGS
        else:
            options["start_new_session"] = True
        try:
            process = subprocess.Popen(list(argv), **options)
        except (OSError, ValueError) as failure:
            return START_FAILURE_CODE, "", "could not start %s: %s" % (os.path.basename(str(argv[0])) if argv else "?", failure)
        try:
            output, error = process.communicate(input_text, timeout=timeout_seconds)
            return process.returncode, output or "", error or ""
        except subprocess.TimeoutExpired:
            self.kill_tree(process.pid)
            output, error = "", ""
            try:
                output, error = process.communicate(timeout=DRAIN_OUTPUT_TIMEOUT_SECONDS)
            except (subprocess.SubprocessError, OSError, ValueError):
                pass
            return TIMEOUT_CODE, output or "", ((error or "") + "\n" + TIMEOUT_MESSAGE).strip()

    def spawn_detached(self, argv, folder, log_path, extra_env=None):
        """Detached Popen (Windows: DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP|CREATE_NO_WINDOW; others: new session), no shell,
        closed stdin, stdout+stderr appended to `log_path`. Returns the pid. Raises OSError if it cannot start."""
        os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
        options = {"stdin": subprocess.DEVNULL, "cwd": folder if folder and os.path.isdir(folder) else None,
                   "env": build_env(None, extra_env)}
        if IS_WINDOWS:
            options["creationflags"] = WINDOWS_DETACHED_FLAGS
        else:
            options["start_new_session"] = True
        with open(log_path, "ab") as log_file:
            process = subprocess.Popen(list(argv), stdout=log_file, stderr=log_file, **options)
        with DETACHED_LOCK:
            DETACHED_PROCESSES[process.pid] = process  # without the reference, the collected Popen warns "subprocess still running"
        return process.pid

    @staticmethod
    def _poll_detached(pid):
        """None if the pid was not spawned here; otherwise True/False from poll() (which also reaps a process that exited)."""
        with DETACHED_LOCK:
            process = DETACHED_PROCESSES.get(pid)
            if process is None:
                return None
            if process.poll() is None:
                return True
            del DETACHED_PROCESSES[pid]
            return False

    def is_alive(self, pid):
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return False
        detached_alive = self._poll_detached(pid)
        if detached_alive is not None:
            return detached_alive
        if IS_WINDOWS:
            return self._is_alive_windows(pid)
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False

    @staticmethod
    def _is_alive_windows(pid):
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(WINDOWS_LIMITED_QUERY_PERMISSION, False, pid)
        if not handle:
            return kernel.GetLastError() == WINDOWS_ACCESS_DENIED_ERROR  # access denied = the process exists
        code = ctypes.c_ulong()
        succeeded = kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        kernel.CloseHandle(handle)
        return bool(succeeded) and code.value == WINDOWS_STILL_ACTIVE_CODE

    def kill_tree(self, pid):
        """taskkill /T /F on Windows; SIGKILL to the process group elsewhere. Never raises."""
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return
        if IS_WINDOWS:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=TASKKILL_TIMEOUT_SECONDS)
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            try:
                os.killpg(os.getpgid(pid), KILL_SIGNAL)
            except OSError:
                try:
                    os.kill(pid, KILL_SIGNAL)
                except OSError:
                    pass
        self._reap_detached(pid)

    @staticmethod
    def _reap_detached(pid):
        """After a kill: waits (briefly) for the Popen spawned here to exit and removes it from the registry."""
        with DETACHED_LOCK:
            process = DETACHED_PROCESSES.pop(pid, None)
        if process is None:
            return
        try:
            process.wait(REAP_DETACHED_TIMEOUT_SECONDS)
        except (subprocess.SubprocessError, OSError):
            with DETACHED_LOCK:
                DETACHED_PROCESSES[pid] = process
