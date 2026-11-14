"""Bounded subprocesses whose timeout owns the entire POSIX process group."""
import os
import signal
import subprocess


def stop_process_group(proc):
    """Stop descendants as well as the parent, then reap the parent."""
    def send(sig):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    try:
        proc.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        pass
    # A parent can exit while a helper with redirected stdio survives it.
    send(signal.SIGKILL)
    proc.wait()


def run(args, cwd, timeout, env=None):
    """Return (exit code, stdout, stderr); 124 means a terminated timeout."""
    try:
        with subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, errors="replace", start_new_session=True) as proc:
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                stop_process_group(proc)
                proc.communicate()
                return 124, "", "timed out after %ss" % timeout
            except BaseException:
                stop_process_group(proc)
                raise
            return proc.returncode, out.strip(), err.strip()
    except OSError as exc:
        return 127, "", str(exc)
