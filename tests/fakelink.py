"""A stand-in for app.link.RobotLink with one route, for tests that drive Controller: commands go to `transport`
(anything with publish(cmd, payload) and a `state`, like the tests' fake clouds)."""


class FakeLink:
    def __init__(self, transport, route: str = "cloud"):
        self.t, self.route = transport, route

    @property
    def state(self):
        return self.t.state

    @property
    def sent(self):
        return self.t.sent

    def command(self, cmd, payload):
        self.t.publish(cmd, payload)
        return self.route
