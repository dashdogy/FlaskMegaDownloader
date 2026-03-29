from __future__ import annotations

import subprocess


def stop_process(process: subprocess.Popen | None, *, timeout: float = 5.0) -> bool:
    if process is None:
        return True

    if process.poll() is not None:
        return True

    try:
        process.terminate()
        process.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False
