"""Cross-module file handoff. Minimal - routes file paths between tabs, nothing more.

A module announces a finished artifact with publish(data_type, path). Another module can
subscribe(data_type, callback) to be notified, or the user can trigger
request_handoff(data_type, target) to route the latest artifact to a specific module's tab.

The hub (main.py) installs the actual routing logic via set_handoff_handler - the bus itself
stays dumb and knows nothing about tabs, the registry, or the UI. This is deliberately NOT a
general event system.
"""


class MessageBus:
    def __init__(self):
        self._subscribers = {}   # data_type -> [callback(path), ...]
        self._latest = {}        # data_type -> most recently published path
        self._seq = 0            # instance-wide monotonic publish counter
        self._latest_seq = {}    # data_type -> seq of its most recent publish
        self._handoff_handler = None  # callable(data_type, target, path), set by the hub

    def subscribe(self, data_type, callback):
        """Register interest in a data type. callback(path) fires on each publish."""
        self._subscribers.setdefault(data_type, []).append(callback)

    def publish(self, data_type, path):
        """Announce a finished artifact. Records it as the latest (with a recency
        stamp) and notifies subscribers."""
        self._seq += 1
        self._latest[data_type] = path
        self._latest_seq[data_type] = self._seq
        for cb in list(self._subscribers.get(data_type, [])):
            cb(path)

    def latest(self, data_type):
        """Most recently published path for a data type, or None."""
        return self._latest.get(data_type)

    def latest_info(self, data_type):
        """(path, seq) for the most recent publish of data_type, or (None, 0).

        seq is a bus-wide monotonic counter, so consumers can compare recency ACROSS
        data types (the exporter picks whichever artifact was published last)."""
        return self._latest.get(data_type), self._latest_seq.get(data_type, 0)

    def set_handoff_handler(self, handler):
        """The hub installs handler(data_type, target, path) to perform tab routing."""
        self._handoff_handler = handler

    def request_handoff(self, data_type, target):
        """Ask the hub to route the latest `data_type` artifact to module `target`."""
        if self._handoff_handler is not None:
            self._handoff_handler(data_type, target, self._latest.get(data_type))
