"""Run a normal wrapped agent while termgrid owns the ConPTY input stream."""
import os
import subprocess
import threading

from pty_bridge import BridgeClient, receive_frame, send_frame


def stop_provider(process):
    """Bound provider shutdown so the wrapper can still deregister."""
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            pass  # TermGrid's job-object fallback will close remaining processes.


def run_agent(command, extra_args, cwd, env, queue_file, agent, no_restart, start_watcher,
              strip_env=None, pid_holder=None, session_name=None, inject_env=None,
              inject_delay=.3, enter_backend='console_input', set_activity_checker=None,
              activity_agent='unknown', trigger_flag=None):
    client = BridgeClient(int(os.environ['TERMGRID_BRIDGE_PORT']), os.environ['TERMGRID_BRIDGE_TOKEN'])
    screen_client = BridgeClient(int(os.environ['TERMGRID_BRIDGE_PORT']), os.environ['TERMGRID_BRIDGE_TOKEN'], connect=False)
    stopping = threading.Event()
    control = BridgeClient(int(os.environ['TERMGRID_BRIDGE_PORT']), os.environ['TERMGRID_BRIDGE_TOKEN'])

    def watch_shutdown():
        try:
            control.socket.settimeout(None)
            send_frame(control.socket, {'type': 'wait_shutdown'})
            if receive_frame(control.socket) == {'type': 'shutdown'}:
                stopping.set()
        except (OSError, ConnectionError, ValueError):
            pass

    threading.Thread(target=watch_shutdown, daemon=True).start()
    if set_activity_checker is not None:
        from pty_activity import get_activity_checker
        set_activity_checker(get_activity_checker(screen_client, activity_agent, trigger_flag))
    child_env = {**env, **(inject_env or {})}
    child_env.pop('TERMGRID_BRIDGE_TOKEN', None)
    child_env.pop('TERMGRID_BRIDGE_PORT', None)
    start_watcher(client.inject)
    try:
        while not stopping.is_set():
            process = subprocess.Popen([command] + extra_args, cwd=cwd, env=child_env)
            if pid_holder is not None:
                pid_holder[0] = process.pid
            while process.poll() is None and not stopping.wait(.1):
                pass
            if stopping.is_set() and process.poll() is None:
                stop_provider(process)
            if pid_holder is not None:
                pid_holder[0] = None
            if no_restart or stopping.is_set():
                break
            stopping.wait(3)
    finally:
        if set_activity_checker is not None:
            set_activity_checker(None)
        screen_client.close()
        client.close()
        control.close()
