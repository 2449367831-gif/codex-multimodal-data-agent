import os
from queue import Queue
import subprocess
import sys
from threading import Thread

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows production worker lifetime")
def test_killing_worker_terminates_its_descendant():
    import ctypes
    from ctypes import wintypes
    program = (
        "import subprocess,sys,time\n"
        "from app.worker_lifetime import retain_windows_worker_job\n"
        "assert retain_windows_worker_job()\n"
        "assert retain_windows_worker_job()\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)\n"
        "print(child.pid,flush=True)\ntime.sleep(60)\n"
    )
    worker = subprocess.Popen([sys.executable, "-c", program], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    messages = Queue()
    reader = Thread(target=lambda: messages.put(worker.stdout.readline()), daemon=True)
    reader.start()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    descendant = None
    try:
        child_pid = int(messages.get(timeout=10).strip())
        descendant = kernel.OpenProcess(0x100000 | 0x1000 | 0x0001, False, child_pid)
        assert descendant
        worker.kill()
        worker.wait(timeout=10)
        assert kernel.WaitForSingleObject(descendant, 5000) == 0
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.communicate(timeout=10)
        if descendant:
            if kernel.WaitForSingleObject(descendant, 0) != 0:
                kernel.TerminateProcess(descendant, 1)
                kernel.WaitForSingleObject(descendant, 5000)
            kernel.CloseHandle(descendant)
        reader.join(timeout=2)
