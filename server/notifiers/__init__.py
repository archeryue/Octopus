"""Async notification targets — webhook, browser-push, email, etc.

A `Notifier` is a destination Octopus can poke when something happens
in the background (a session goes idle, an AskUserQuestion is pending,
a schedule fails). The framework is small on purpose: NotifierBase
defines the one method each target implements (`send`), and the
NotifierManager wires up triggers (currently: session-idle).

Concrete notifiers:
  - WebhookNotifier (server/notifiers/webhook.py)

Add new types by writing a NotifierBase subclass + registering it in
NotifierManager._make.
"""

from .base import NotifierBase, NotifierEvent
from .manager import NotifierManager, notifier_manager
from .webhook import WebhookNotifier

__all__ = [
    "NotifierBase",
    "NotifierEvent",
    "NotifierManager",
    "notifier_manager",
    "WebhookNotifier",
]
