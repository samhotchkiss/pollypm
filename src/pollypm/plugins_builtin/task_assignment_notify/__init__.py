"""Task-assignment notification plugin.

Event-driven primary + @every 30s sweeper fallback. Resolves the target
session for each task-assignment event via naming convention
(``SessionRoleIndex``) and pings it through the configured
``SessionService``. See issue #244 and
``pollypm.work.task_assignment`` for the event type.
"""
