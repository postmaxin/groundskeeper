"""A stand-in for app.link.RobotLink with one route, for tests that drive Controller: commands go to `transport`
(anything with publish(cmd, payload) and a `state`, like the tests' fake clouds)."""


class FakeLink:
    def __init__(self, transport, route: str = "cloud"):
        self.t, self.route = transport, route
        self.outcomes = []

    @property
    def state(self):
        return self.t.state

    @property
    def sent(self):
        return self.t.sent

    def command(self, cmd, payload, route=None):
        self.t.publish(cmd, payload)
        return route or self.route

    def other_route(self, route):
        return None

    def note_command(self, route, seen):
        self.outcomes.append((route, seen))

    def keep_awake_beat(self, payload):
        self.t.publish("set_working_state", payload)
        return self.route
