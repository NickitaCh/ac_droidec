"""A paced, oldest-first schedule. Successful timestamps live in the player cache."""
class PlayerSyncSchedule:
    def __init__(self, period=600):
        self.period = period
        self.due = {}
        self.retry = {}
        self.last_success = {}
        self.next_start = 0.0

    def reconcile(self, codes, last_updated):
        self.due = {code: max(max(last_updated.get(code, 0), self.last_success.get(code, 0)) + self.period,
                              self.retry.get(code, 0)) for code in codes}
        self.retry = {code: when for code, when in self.retry.items() if code in self.due}
        self.last_success = {code: when for code, when in self.last_success.items() if code in self.due}

    def take(self, now):
        if not self.due or now < self.next_start:
            return None
        code = min(self.due, key=self.due.get)
        if self.due[code] > now:
            return None
        # Headroom for polling granularity and occasional slow responses.
        self.next_start = now + self.period * 0.9 / len(self.due)
        return code

    def complete(self, code, now, success=True):
        if success:
            self.last_success[code] = now
            self.retry.pop(code, None)
            self.due[code] = now + self.period
        else:
            self.retry[code] = now + 60
            self.due[code] = self.retry[code]
