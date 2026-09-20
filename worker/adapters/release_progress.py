import json


class ReleaseProgressReader:
    def __init__(self, path):
        self.path = path
        self.last = None

    def __call__(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        if data == self.last:
            return None
        self.last = data
        return data
