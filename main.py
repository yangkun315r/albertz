
import os
import sys
import better_exchook
import threading
import logging
# Use this to debug Sqlite problems:
# import .sqlite_debugging
from ipykernel.ipkernel import IPythonKernel


def _embed_kernel_simple():
    """
    Mostly just for reference, and easy code browsing.
    """
    from ipykernel.embed import embed_kernel
    embed_kernel()


class IPythonBackgroundKernelWrapper:
    """
    You can remotely connect to this IPython kernel. See the output on stdout.
    https://github.com/ipython/ipython/issues/8097
    https://stackoverflow.com/questions/29148319/provide-remote-shell-for-python-script
    """

    def __init__(self, connection_filename="kernel.json", connection_fn_with_pid=True, logger=None,
                 user_ns=None, banner="Hello from background-zmq-ipython."):
        """
        :param str connection_filename:
        :param bool connection_fn_with_pid: will add "-<pid>" to the filename (before the extension)
        :param logging.Logger logger:
        """
        self._lock = threading.Lock()
        self._condition = threading.Condition(lock=self._lock)

        if connection_fn_with_pid:
            name, ext = os.path.splitext(connection_filename)
            connection_filename = "%s-%i%s" % (name, os.getpid(), ext)
        self._connection_filename = connection_filename

        self._thread = None  # type: threading.Thread
        self._shell_stream = None
        self._control_stream = None
        self._kernel = None  # type: IPythonKernel
        self._user_ns = user_ns
        self._banner = banner

        if not logger:
            logger = logging.Logger("IPython", level=logging.INFO)
            # or no logging? logger.addHandler(logging.NullHandler())
            logger.addHandler(logging.StreamHandler(sys.stdout))
        self._logger = logger

        self._create_session()
        self._create_sockets()
        self._write_connection_file()

    def _create_session(self):
        from jupyter_client.session import Session, new_id_bytes
        self._session = Session(username=u'kernel', key=new_id_bytes())

    def _create_sockets(self):
        import zmq
        import socket
        from ipykernel.heartbeat import Heartbeat

        context = zmq.Context()  # or existing? zmq.Context.instance()
        ip = socket.gethostbyname(socket.gethostname())
        transport = "tcp"
        addr = "%s://%s" % (transport, ip)
        shell_socket = context.socket(zmq.ROUTER)
        shell_port = shell_socket.bind_to_random_port(addr)
        iopub_socket = context.socket(zmq.PUB)
        iopub_port = iopub_socket.bind_to_random_port(addr)
        control_socket = context.socket(zmq.ROUTER)
        control_port = control_socket.bind_to_random_port(addr)

        # heartbeat doesn't share context, because it mustn't be blocked
        # by the GIL, which is accessed by libzmq when freeing zero-copy messages
        hb_ctx = zmq.Context()
        heartbeat = Heartbeat(hb_ctx, (transport, ip, 0))
        hb_port = heartbeat.port
        heartbeat.start()

        self._connection_info = dict(
            ip=ip,
            shell_port=shell_port, iopub_port=iopub_port, control_port=control_port, hb_port=hb_port)
        self._shell_socket = shell_socket
        self._control_socket = control_socket
        self._iopub_socket = iopub_socket

    def _cleanup_connection_file(self):
        try:
            os.remove(self._connection_filename)
        except (IOError, OSError):
            pass

    def _write_connection_file(self):
        import atexit
        from ipykernel import write_connection_file
        atexit.register(self._cleanup_connection_file)
        write_connection_file(self._connection_filename, key=self._session.key, **self._connection_info)
        # The key should be secret, to only allow the same user to connect.
        # Make sure the permissions are set accordingly.
        os.chmod(self._connection_filename, os.stat(self._connection_filename).st_mode & 0o0700)
        self._logger.info(
            "To connect another client to this IPython kernel, use: " +
            "jupyter console --existing %s" % self._connection_filename)

    def _setup_streams(self):
        """
        Setup ZMQ streams.
        These need to be constructed within the right active event loop,
        i.e. this must run in the background thread.
        """
        assert threading.current_thread() is self._thread
        from zmq.eventloop.zmqstream import ZMQStream
        # ZMQStream wants a Tornado IOLoop, not a asyncio loop.
        with self._condition:
            self._shell_stream = ZMQStream(self._shell_socket)
            self._control_stream = ZMQStream(self._control_socket)
            self._condition.notify_all()

    def _create_kernel(self):
        """
        Creates the kernel.
        This should be done in the background thread.
        """
        from traitlets.config.loader import Config
        assert threading.current_thread() is self._thread
        # Creating the kernel will also initialize the shell (ZMQInteractiveShell) on the first call.
        # The shell will have the history manager (HistoryManager).
        # HistoryManager/HistoryAccessor will init the Sqlite DB. It will be closed via atexit,
        # so we want to allow the access from a different thread at that point.
        # Also see here: https://github.com/ipython/ipython/issues/680
        config = Config()
        config.InteractiveShell.banner2 = self._banner
        config.HistoryAccessor.connection_options = dict(check_same_thread=False)
        kernel = IPythonKernel(
            session=self._session,
            shell_streams=[self._shell_stream, self._control_stream],
            iopub_socket=self._iopub_socket,
            log=self._logger,
            user_ns=self._user_ns,
            config=config)
        with self._condition:
            self._kernel = kernel
            self._condition.notify_all()

    def _start_kernel(self):
        """
        Starts the kernel itself.
        This must run in the background thread.
        """
        assert threading.current_thread() is self._thread
        self._setup_streams()
        self._create_kernel()
        self._logger.info("IPython: Start kernel now. pid: %i, thread: %r" % (os.getpid(), threading.current_thread()))
        self._kernel.start()

    def _thread_loop(self):
        assert threading.current_thread() is self._thread
        import asyncio

        # Need own event loop for this thread.
        loop = asyncio.new_event_loop()
        loop.call_soon(self._start_kernel)
        try:
            loop.run_forever()
        except KeyboardInterrupt:
            pass

    def start(self):
        thread = threading.Thread(target=self._thread_loop, name="IPython kernel")
        thread.daemon = True
        self._thread = thread
        thread.start()


def init_ipython_kernel(**kwargs):
    kernel_wrapper = IPythonBackgroundKernelWrapper(**kwargs)
    kernel_wrapper.start()


init_ipython_kernel.__doc__ = IPythonBackgroundKernelWrapper.__init__.__doc__


def _endless_dummy_loop():
    import time
    while True:
        try:
            time.sleep(1)
        except KeyboardInterrupt:
            print("KeyboardInterrupt in _endless_dummy_loop")
            return


def main():
    init_ipython_kernel(user_ns={"demo_var": 42})

    # Do nothing. Keep main thread alive, as IPython kernel lives in a daemon thread.
    # This is just a demo. Normally you would have your main loop in the main thread.
    _endless_dummy_loop()


if __name__ == '__main__':
    better_exchook.install()
    main()
