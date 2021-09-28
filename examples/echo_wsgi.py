import logging

import gevent.pywsgi

import greenws

_log = logging.getLogger(__name__)


def application(environ, start_response):
    if environ.get("HTTP_AUTHORIZATION") != "hunter2":
        _log.error("client failed to provide us with valid Authorization")
        start_response("401 Unauthorized", [])
    elif "greenws.websocket" not in environ:
        _log.error("client didn't send Upgrade: websocket")
        start_response("400 Bad Request", [])
    else:
        _log.info("handshaking client...")
        ws = environ["greenws.websocket"]
        resource = environ["SCRIPT_NAME"]
        # empty resource is not allowed
        resource += environ["PATH_INFO"] or "/"
        resource += "?" + environ["QUERY_STRING"]
        try:
            proposal = ws.handshake(resource, greenws.uncgi_headers(environ))
            _log.info("client proposes %r", proposal)
            ws.accept()
        except greenws.ConnectionError:
            _log.exception("client failed handshake")
            start_response("400 Bad Request", [])
        else:
            _log.info("client handshook")
            while True:
                try:
                    ws.send(ws.receive())
                except greenws.Closed:
                    _log.warning("client closed", exc_info=True)
                    break
    return []


if __name__ == "__main__":
    # change level to logging.DEBUG if you want to see all the gritty details
    # :)
    logging.basicConfig(level=logging.INFO)
    gevent.pywsgi.WSGIServer(
        ("127.0.0.1", 5000),
        application,
        handler_class=greenws.WSHandler,
    ).serve_forever()
