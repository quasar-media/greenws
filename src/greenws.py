"""WebSockets for gevent."""

# parts borrowed from trio-websocket
# Copyright (c) 2018 Hyperion Gray

import collections
import enum
import logging
import secrets
import struct
import sys
import time

import gevent
import gevent.event
import gevent.lock
import gevent.pywsgi
import gevent.queue
import gevent.socket
import wsproto
import wsproto.events
import wsproto.utilities
from gevent.timeout import Timeout  # re-export

__version__ = "0.1.0a5"

#: A proposed connection.
#:
#: :ivar subprotocols: subprotocols proposed by the client
#: :vartype subprotocols: list(str)
Proposal = collections.namedtuple("Proposal", "subprotocols")


class State(enum.IntEnum):
    """Connection state."""

    #: The WebSocket is in the process of establishing a connection.
    CONNECTING = 1

    #: The WebSocket is open and operational.
    OPEN = 2

    #: The WebSocket is rejecting or closed.
    CLOSED = 3


# FIXME: this hierarchy is horrible
class ConnectionError(Exception):
    """Initial exchange failed."""


class HTTPError(ConnectionError):
    """Server rejected the upgrade request.

    :ivar status_code: the status code
    :vartype status_code: int
    :ivar headers: server-sent headers
    :vartype headers: list(tuple(str, str))
    :ivar body: response body
    :vartype body: bytes
    """

    def __init__(self, status_code, headers, body):
        super().__init__(status_code, headers, body)
        self.status_code = status_code
        self.headers = headers
        self.body = body

    def __str__(self):
        # TODO: perhaps body?
        return "[{:03d}]".format(self.status_code)


class StateError(Exception):
    """Indicates that the state the websocket is in is invalid."""


class Closed(StateError):
    """Indicates that the connection was closed during operation.

    :ivar status_code: the status code
    :vartype status_code: int
    :ivar reason: close reason
    :vartype reason: bytes or None
    """

    def __init__(self, status_code, reason=None):
        super().__init__(status_code, reason)
        self.code = status_code
        self.reason = reason

    def __str__(self):
        s = "[{:04d}]".format(self.status_code)
        if self.reason is not None:
            s += " " + self.reason
        return s


class ProgrammingError(StateError):
    """Indicates a programming error.

    This usually means methods were called in the wrong order: for example,
    accept() called after the connection was already open.
    """


class _MoveOn(BaseException):
    def __init__(self, shutdown=True):
        super().__init__(shutdown)
        self.shutdown = shutdown


class WebSocket:
    """The WebSocket base class.

    :param sock: socket to wrap (warning: **the caller is responsible for
        closing the socket!**)
    :param message_queue_size: how many messages to keep in memory (default: 0)
    :type message_queue_size: int
    :param max_message_size: maximum (total, not fragmented) message size, in
        bytes: if the remote sends a message bigger than this, the connection
        will be closed with code 1009 (default: 1MB)
    :type max_message_size: int
    :param receive_buffer_size: how much data to receive at once, in bytes
        (default: 4KB)
    :type receive_buffer_size: int
    :param periodic_ping: maximum time interval (in seconds, or fractions
        thereof) between pings, set to 0 to disable periodic pings (default: 5)
    :type periodic_ping: float
    :param periodic_ping_timeout: how long to wait (in seconds) for a pong in
        the ping loop (default: 5)
    :type periodic_ping_timeout: float
    :param periodic_ping_max_sequential_misses: how many misses are acceptable
        before closing the connection (default: 3)
    :type periodic_ping_max_sequential_misses: int

    .. warning::

       If the message queue fills up with *message_queue_size* messages, the
       reader loop will stop handling events. This means pings won't be replied
       to, among other things: take care in processing your messages, or set a
       larger queue size.

    Instances are greenlet-safe, though only after the initialization process
    is complete (just handshake() for clients and handshake() then accept() for
    servers) is complete. Calling those methods concurrently **can and will set
    fire to your house**.

    Instances are also context managers, though is only a convenience: if an
    exception is raised within the ``with:`` block, the connection will be
    closed with code 1011 (Internal Error) and no reason.  Otherwise, the
    connection will be closed with code 1000 (Normal Closure).  In either case,
    the client will be given 5 seconds to reply with a close frame: if the
    client fails to do so, the connection will be forcibly terminated. Those
    wishing to customize this behavior in any way are free to subclass the
    appropriate class for their use-case or to emulate this logic in a
    try/except/finally block instead.
    """

    def __init__(
        self, sock,
        message_queue_size=0,
        max_message_size=2 ** 20,  # 1MB
        receive_buffer_size=2 ** 12,  # 4KB
        periodic_ping=5.0, periodic_ping_timeout=5.0,
        periodic_ping_max_sequential_misses=3,
    ):
        self._sock = sock

        self.message_queue_size = message_queue_size
        self.max_message_size = max_message_size
        self.receive_buffer_size = receive_buffer_size
        self.periodic_ping = periodic_ping
        self.periodic_ping_timeout = periodic_ping_timeout
        self.periodic_ping_max_sequential_misses = periodic_ping_timeout

        self._ws_handshake = wsproto.H11Handshake(self._type)
        self._ws = None

        self._queue = None
        self._readlet = None
        self._reading_message_type = None
        self._reading_message_size = 0
        self._reading_message_parts = []

        self._pings = collections.OrderedDict()
        self._pinglet = None

        self._write_lock = gevent.lock.BoundedSemaphore()
        self._closed_event = gevent.event.Event()
        self._closed_args = None

        self._log = logging.getLogger(type(self).__qualname__)

    @property
    def state(self):
        """Connection state.

        :return: state
        :rtype: State
        """

        if (
            self._ws is None
            or self._ws.state is wsproto.ConnectionState.CONNECTING
        ):
            return State.CONNECTING
        elif self._ws.state is wsproto.ConnectionState.OPEN:
            return State.OPEN
        elif self._ws.state in {
            wsproto.ConnectionState.REMOTE_CLOSING,
            wsproto.ConnectionState.LOCAL_CLOSING,
            wsproto.ConnectionState.CLOSED,
            wsproto.ConnectionState.REJECTING,
        }:
            return State.CLOSED

        raise RuntimeError("wsproto unknown state, this is a bug")

    # TODO: fragmented messages
    def send(self, message):
        """Send a message.

        :param message: the message to send
        :type message: str or bytes

        :raises TypeError: if an appropriate object is passed
        :raises Closed: if the connection is closed
        """

        self._ensure_open()

        if isinstance(message, str):
            self._send(wsproto.events.TextMessage(message))
        elif isinstance(message, bytes):
            self._send(wsproto.events.BytesMessage(message))
        else:
            typ = type(message).__name__
            raise TypeError(f"message must be str or bytes, not {typ!r}")

    def receive(self, *, timeout=None):
        """Receive a message.

        :param timeout: how long to wait for a message (default: no timeout)
        :type timeout: float

        :raises Closed: if the connection is closed
        :raises Timeout: if timeout expires

        :return: the message received
        :rtype: str or bytes
        """

        self._ensure_open()

        try:
            msg = self._queue.get(timeout=timeout)
        except gevent.queue.Empty:
            raise Timeout(seconds=timeout) from None

        if msg is None:
            self._raise_closed()
        return msg

    # TODO: maybe apply timeout to closing?
    def receive_str(self, *, timeout=None):
        """Receive a text message.

        If the client sends a binary message, the connection will be closed
        with code 1003 (Unsupported Data) and :exc:`Closed` will be raised
        after waiting for the closing handshake to complete. Otherwise
        identical to calling :meth:`receive`.

        :return: the message received
        :rtype: str
        """

        msg = self.receive(timeout=timeout)
        if not isinstance(msg, str):
            # 1003 Unsupported Data
            self.close_nowait(code=1003, reason="expected text message")
            self._raise_closed()
        return msg

    def receive_bytes(self, *, timeout=None):
        """Receive a binary message.

        Identical to :meth:`receive_str` but operates on binary messages
        instead.
        """

        msg = self.receive(timeout=timeout)
        if not isinstance(msg, bytes):
            self.close_nowait(code=1003, reason="expected binary message")
            self._raise_closed()
        return msg

    def ping(self, payload=None, *, timeout=None):
        """Ping the remote and wait for a pong.

        :param payload: the "application data" field (default: 32 bits of
            randomness)
        :type payload: bytes
        :param timeout: how long to wait for a pong (default: no timeout)
        :type timeout: float

        :raises ValueError: if the payload is already in flight
        :raises Closed: if the connection is closed
        :raises Timeout: if timeout expires
        """

        self._ensure_open()

        if payload is None:
            payload = struct.pack("!I", secrets.randbits(32))
        payload = bytes(payload)
        if payload in self._pings:
            raise ValueError("duplicate ping payload")
        event = self._pings[payload] = gevent.event.Event()
        self._send(wsproto.events.Ping(payload=payload))
        if not event.wait():
            self._pings.pop(payload, None)
            raise Timeout(seconds=timeout) from None

        self._ensure_open()  # bleh

    def close(self, *, code, reason=None, timeout=None):
        """Gracefully close the connection.

        If *timeout* is not None and a timeout occurs, the connection will
        remain unusable but will not be forcefully closed. To do so, call
        :meth:`kill`. It is safe to call this method multiple times: if
        a close has already been requested, it'll simply wait for closing
        handshake to complete or for the connection to be killed.

        .. code-block:: python

           # a basic recipe for reliably closing a borked connection
           try:
               ws.close(code=1000, timeout=5)
           except Timeout:
               ws.kill()
               ws.close(code=1000)

        :param code: the status code to send to the remote
        :type code: int
        :param reason: an optional reason string to send to the remote
        :type reason: str
        :param timeout: how long to wait for close (default: no timeout)

        :raises Timeout: if timeout expires
        """

        self._log.debug("close code=%d reason=%r timeout=%r", code, reason, timeout)

        self.close_nowait(code=code, reason=reason)
        if not self._closed_event.wait(timeout=timeout):
            raise Timeout(seconds=timeout)

    def close_nowait(self, *, code, reason=None):
        """Gracefully close the connection.

        Identical to :meth:`close`, except in that it doesn't wait for the
        closing handshake to finish.
        """

        self._log.debug("close_nowait state=%r", self.state)

        if self.state is State.OPEN:
            self._send(wsproto.events.CloseConnection(
                code=code,
                reason=reason,
            ))

    def kill(self):
        """Forcibly close the connection.

        It is safe to call this method multiple times, and on a closed
        connection.
        """

        self._log.debug("kill _readlet=%r", self._readlet)

        if not self._readlet or self._readlet.dead:
            return

        # forgive me, Guido
        try:
            raise _MoveOn()
        except _MoveOn:
            exc_info = sys.exc_info()
        self._readlet.kill(exc_info, block=False)

    def _read_loop(self):
        # XXX: this method is far too complex and our system for "moving on"
        # by raising at a yield point here is extremely brittle

        self._log.debug("_read_loop enters")

        handlers = [
            (wsproto.events.Message, self._handle_message),
            (wsproto.events.Ping, self._handle_ping),
            (wsproto.events.Pong, self._handle_pong),
            (wsproto.events.CloseConnection, self._handle_close),
        ]

        shutdown = True
        while True:
            try:
                for event in self._ws.events():
                    self._log.debug("_read_loop event=%r", event)
                    for event_cls, handle in handlers:
                        if isinstance(event, event_cls):
                            try:
                                # I wish greenlet would let me shield this call
                                # from being killed; we yield in handler
                                # functions...
                                handle(event)
                            except _MoveOn:
                                raise
                            except BaseException:
                                self._log.exception("_read_loop handle()")
                                raise _MoveOn()
            except _MoveOn as e:
                shutdown = e.shutdown
                break

            self._log.debug("_read_loop recv()")
            try:
                data = self._sock.recv(self.receive_buffer_size)
            except gevent.socket.error:
                shutdown = False
                self._log.exception("_read_loop recv() failed")
                break
            except _MoveOn as e:
                shutdown = e.shutdown
                break
            self._log.debug("_read_loop len(data)=%d", len(data))

            if not data:
                if self._ws.state is not wsproto.ConnectionState.CLOSED:
                    shutdown = False
                    self._log.exception(
                        "_read_loop recv() unexpected EOF state=%r",
                        self._ws.state,
                    )
                break
            # TODO: handle RemoteProtocolError (?)
            self._ws.receive_data(data)

        self._log.debug(
            "_read_loop %r %r shutdown=%r",
            self.state,
            self._ws.state,
            shutdown,
        )

        if self.state is State.OPEN:
            # ensure state is not OPEN
            self._ws.send(wsproto.events.CloseConnection(code=1006))

        # wake up the receive() getters
        while True:
            try:
                self._queue.put(None, timeout=0)
            except gevent.queue.Full:
                break

        if shutdown:
            timeout = self._sock.gettimeout()
            self._sock.settimeout(3.0)

            try:
                self._sock.shutdown(gevent.socket.SHUT_WR)
            # honestly not sure if this can even raise...
            except gevent.socket.error:
                self._log.exception("_read_loop shutdown()")

            d = None
            while True:
                try:
                    d = self._sock.recv(self.receive_buffer_size)
                    self._log.exception("_read_loop recv() on shutdown")
                except _MoveOn:
                    self._log.debug("_read_loop moving on on shutdown")
                self._log.debug("_read_loop d=%r", d)
                if not d:
                    break

            self._sock.settimeout(timeout)

        # XXX: does this wake up writers?
        self._sock.close()

        if self._closed_args is None:
            # abnormal closure
            self._closed_args = (1006, None)

        self._closed_event.set()

        self._log.debug("_read_loop exits")

    def _handle_message(self, event):
        if self._reading_message_type is None:
            self._reading_message_type = type(event)
        elif self._reading_message_type is not type(event):
            # XXX: how are we supposed to handle this???
            self.close_nowait(code=1006, reason="overlapping message frames")

        self._reading_message_size += len(event.data)
        self._reading_message_parts.append(event.data)
        if (
            self.max_message_size > 0
            and self._reading_message_size > self.max_message_size
        ):
            self.close_nowait(
                code=1009,
                reason=f"maximum message size: {self.max_message_size} bytes",
            )

        if event.message_finished:
            delim = (
                "" if issubclass(type(event), wsproto.events.TextMessage)
                else b""
            )
            data = delim.join(self._reading_message_parts)
            self._reading_message_type = None
            self._reading_message_size = 0
            self._reading_message_parts = []
            self._queue.put(data)

    def _handle_ping(self, event):
        self._send(event.response())

    def _handle_pong(self, event):
        payload = bytes(event.payload)

        if payload not in self._pings:
            self._log.debug(
                "_handle_pong unsolicited pong payload=%r",
                payload,
            )

        while self._pings:
            # RFC 6455, section 5.5.3.
            # If an endpoint receives a Ping frame and has not yet sent Pong
            # frame(s) in response to previous Ping frame(s), the endpoint MAY
            # elect to send a Pong frame for only the most recently processed
            # Ping frame.
            key, e = self._pings.popitem(last=False)
            e.set()
            if payload == key:
                break

    def _handle_close(self, event):
        self._log.debug("_handle_close state=%s", self._ws.state)
        # if we initiated the close...
        if self._ws.state is wsproto.ConnectionState.CLOSED:
            # ...our job here is done...
            self._log.debug("_handle_close initiated by us, moving on")
            raise _MoveOn()
        elif self._ws.state in {
            # normal close initiated by the remote
            wsproto.ConnectionState.REMOTE_CLOSING,
            # frame protocol error
            wsproto.ConnectionState.OPEN,
        }:
            self._send(event.response())
            self._closed_args = (event.code, event.reason)
        else:
            # ???
            raise StateError("_handle_close catastrophic state")

    def _ping_loop(self):
        self._log.debug("_ping_loop enters")
        misses = 0
        while True:
            t = time.monotonic()
            try:
                self.ping(timeout=self.periodic_ping_timeout)
            except Timeout:
                misses += 1
                if misses > self.periodic_ping_max_sequential_misses:
                    self._log.error(
                        "_ping_loop missed %d pings, closing", misses,
                    )
                    self._close_with_timeout(
                        code=1008,  # Policy Violation
                        timeout=10.0,
                        reason="failure to reply to pings",
                    )
                    break
            except Closed:
                break
            else:
                misses = 0
                t -= time.monotonic()
            gevent.sleep(max(1, self.periodic_ping - t))
        self._log.debug("_ping_loop exits")

    def _send(self, event):
        data = self._ws.send(event)
        self._log.debug("_send event=%r len(data)=%d", event, len(data))
        self._sendall(data)

    def _sendall(self, data):
        self._log.debug("_sendall data=%r", data)
        with self._write_lock:
            try:
                self._sock.sendall(data)
            except gevent.socket.error:
                if gevent.getcurrent() is self._readlet:
                    # if we're _read_loop, we made it here via an event
                    # handler: raising _MoveOn is sure to close the socket
                    raise _MoveOn(shutdown=False)
                else:
                    self.kill()

    def _close_with_timeout(self, code, timeout, reason=None):
        try:
            self.close(code=code, timeout=timeout, reason=reason)
        except Timeout:
            self.kill()
            self.close(code=code)

    def _ensure_open(self):
        if self.state is not State.OPEN:
            self._raise_closed()

    def _raise_closed(self):
        # we must wait for the closing handshake to finish, if one is in
        # progress; otherwise we might not have _closed_args available to us
        self._closed_event.wait()
        assert self._closed_args is not None
        raise Closed(*self._closed_args)

    # called by subclasses
    def _start(self):
        if self.message_queue_size > 0:
            self._queue = gevent.queue.Queue(self.max_message_size)
        else:
            self._queue = gevent.queue.Channel()

        self._readlet = gevent.spawn(self._read_loop)
        if self.periodic_ping > 0:
            self._pinglet = gevent.spawn(self._ping_loop)

    # ditto
    def _make_headers(self, headers=None):
        return [
            (self._make_header_value(k), self._make_header_value(v))
            for k, v in headers or []
        ]

    # ditto x2
    def _ensure_connecting(self):
        if self.state is not State.CONNECTING:
            raise ProgrammingError(
                "state must be CONNECTING, is "
                + self._ws_handshake.state.name,
            )

    def _make_header_value(self, v):
        if isinstance(v, bytes):
            return v
        return v.encode("latin1")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_value is None:
            # 1000 Normal Closure
            self._close_with_timeout(code=1000, timeout=5.0)
        else:
            # 1011 Internal Error
            self._close_with_timeout(code=1011, timeout=5.0)


class ClientWebSocket(WebSocket):
    """Represents a WebSocket acting as the client.

    .. note::

       Make sure to read the documentation of the base WebSocket: it highlights
       a few caveats you might run into when using the greenws interface.
    """

    _type = wsproto.ConnectionType.CLIENT

    def handshake(self, host, resource, headers=None, subprotocols=None):
        """Perform an upgrade handshake.

        :param host: the value of the Host header
        :type host: str
        :param resource: the target resource (path + query string), must not be
            empty (set to "/" for URL root)
        :type resource: str
        :param headers: additional headers to send to the server, must not
            contain the Host header or any Sec-WebSocket-* headers
        :type headers: list(tuple(str or bytes, str or bytes))
        :param subprotocols: subprotocols to propose
        :type subprotocols: list(str)

        :raises ProgrammingError: if called at an inappropriate time
        :raises ValueError: if resource is empty
        :raises HTTPError: when the server responds with a non-101 response
        :raises ConnectionError: if connection attempt failed but HTTPError
            isn't appropriate (e.g. EOF, protocol error)
        """

        self._ensure_connecting()

        if not resource:
            raise ValueError("resource must not be empty")

        self._sendall(self._ws_handshake.send(wsproto.events.Request(
            host=host,
            target=resource,
            extra_headers=self._make_headers(headers),
            subprotocols=subprotocols or [],
        )))

        reject = None
        reject_body = None
        while True:
            cont = True
            for event in self._ws_handshake.events():
                self._log.debug("handhshake event=%r", event)
                if isinstance(event, wsproto.events.AcceptConnection):
                    cont = False
                    break
                elif isinstance(event, wsproto.events.RejectConnection):
                    reject = event
                    if not event.has_body:
                        cont = False
                        break
                    reject_body = []
                elif isinstance(event, wsproto.events.RejectData):
                    cont = False
                    reject_body.append(event.data)
                    if event.body_finished:
                        break

            if not cont:
                break

            data = self._sock.recv(self.receive_buffer_size)
            self._log.debug("handshake len(data)=%d", len(data))
            if not data:
                raise ConnectionError("EOF")
            try:
                self._ws_handshake.receive_data(data)
            except wsproto.utilities.RemoteProtocolError as e:
                raise ConnectionError(e.message)

        if reject:
            raise HTTPError(
                reject.status_code,
                self._make_headers(reject.headers),
                b"".join(reject_body),
            )

        self._ws = self._ws_handshake.connection
        assert self._ws

        self._start()


# XXX: for WSGI-based websockets, it *might* be better to use the
# start_response callable instead of writing to the socket manually but I'm not
# sure how feasible that is
class ServerWebSocket(WebSocket):
    """Represents a WebSocket acting as the server.

    .. note::

       Make sure to read the documentation of the base WebSocket: it highlights
       a few caveats you might run into when using the greenws interface.
    """

    _type = wsproto.ConnectionType.SERVER

    def handshake(self, resource, headers):
        """Process an upgrade request.

        This method is generally used when the request has already been parsed,
        e.g. when using the :class:`WSHandler`.

        .. note::

           At this point you may be asking: why do I need to handle the upgrade
           request explicitly? Why can't the server do it for me? The reason
           for this is simple: the initial handshake can (and will) result in
           errors of all kinds: the handling of those errors (perhaps wrapping
           them in pretty JSON API errors?) is left to you or your web
           framework.

        :param resource: the target resource (path + query string)
        :type resource: str
        :param headers: client-sent headers
        :type headers: list(tuple(str or bytes, str or bytes))

        :raises ProgrammingError: if called at an inappropriate time
        :raises ConnectionError: if the request is invalid

        :return: a connection proposal
        :rtype: Proposal
        """

        self._ensure_connecting()

        self._log.debug("handshake resource=%r headers=%r", resource, headers)

        try:
            self._ws_handshake.initiate_upgrade_connection(
                headers=self._make_headers(headers),
                path=resource,
            )
        except wsproto.utilities.RemoteProtocolError as e:
            raise ConnectionError(e.message)

        try:
            (event,) = list(self._ws_handshake.events())
        except ValueError:
            assert False, "expecting exactly one event from H11Handshake"

        assert isinstance(event, wsproto.events.Request), \
               "expecting Request event from H11Handshake"

        return Proposal(event.subprotocols)

    def accept(self, *, headers=None, subprotocol=None):
        """Accept the connection.

        :param headers: additional headers to send to the client
        :type headers: list(tuple(str, str))
        :param subprotocol: the subprotocol to use; should be one of the ones
            proposed by the client in :meth:`handshake`, otherwise the client
            is forced to fail the connection
        :type subprotocol: str

        :raises ProgrammingError: if called at an inappropriate time
        """

        self._ensure_connecting()

        self._log.debug("accept headers=%r subprotocol=%r", headers, subprotocol)

        self._sendall(self._ws_handshake.send(wsproto.events.AcceptConnection(
            subprotocol=subprotocol,
            extra_headers=self._make_headers(headers),
        )))
        self._ws = self._ws_handshake.connection
        assert self._ws is not None, \
               "expecting non-None connection after AcceptConnection"

        self._start()


class WSHandler(gevent.pywsgi.WSGIHandler):
    """A :mod:`gevent.pywsgi` WebSocket-capable handler.

    In case of a request with the "Upgrade" header set to "websocket"
    (case-insensitive), this handler sets the "greenws.websocket"
    environment key to an instance of :class:`ServerWebSocket`. It does **not**
    further validate the request or reply with "101 Switching Protocol": that
    is the responsibility of the WSGI application being called.
    """

    #: WebSocket factory to instantiate ``greenws.websocket`` with: passed the
    #: underlying socket as its only positional argument.
    websocket_class = ServerWebSocket

    __ws = None

    def get_environ(self):
        environ = super().get_environ()
        if environ.get("HTTP_UPGRADE", "").lower() == "websocket":
            self.__ws = environ["greenws.websocket"] = \
                self.websocket_class(self.socket)
        return environ

    def write(self, data):
        if (
            data
            and self.__ws is not None
            and self.__ws._ws_handshake.state is wsproto.ConnectionState.OPEN
        ):
            raise self.ApplicationError(
                "websocket initialized, can't write non-empty response"
            )
        return super().write(data)


def uncgi_headers(environ):
    """A helper to transform a WSGI environment into a list of headers.

    :param environ: the WSGI environment
    :type environ: dict

    :return: headers
    :rtype: list(tuple(str, str))
    """

    return [
        ("-".join(p.capitalize() for p in k[5:].split("_")), v)
        for k, v in environ.items()
        if k.startswith("HTTP_")
    ]
