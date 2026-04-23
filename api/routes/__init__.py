"""HTTP routers for the consumer API.

server.py assembles these via `app.include_router`. Every file in this
package owns one resource cluster and nothing else — add new routes to
the router whose URL prefix matches, and do not call FastAPI() here.
"""
