API
===

.. module:: greenws

Exceptions
----------

.. autoexception:: ConnectionError

.. autoexception:: HTTPError
   :show-inheritance:

.. autoexception:: StateError

.. autoexception:: Closed
   :show-inheritance:

.. autoexception:: ProgrammingError
   :show-inheritance:


Data Structures
---------------

.. autoclass:: Proposal

.. autoclass:: State
   :members:

WebSockets
----------

.. autoclass:: WebSocket
   :members:

.. autoclass:: ClientWebSocket
   :show-inheritance:
   :members:

.. autoclass:: ServerWebSocket
   :show-inheritance:
   :members:

Utilities
---------

.. autoclass:: WSHandler
   :show-inheritance:
   :members: websocket_class

.. autofunction:: uncgi_headers
