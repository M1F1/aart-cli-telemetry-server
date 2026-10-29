"""What the server does, written against ports: no framework, no database, no clock of its own.

Each use case takes the repository and the clock it needs and answers with a `returns` container,
so a failure is a value the web layer maps to a status rather than an exception it must catch.
"""
